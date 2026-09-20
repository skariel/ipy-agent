"""UI-independent serialized actor. Only the sandbox worker executes source.

No await separates final stale checks, acceptance and the committed dispatch.
Input arriving after that point is steering during execution, never Python stdin.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import asdict
import time
import sqlite3

from .context import Context
from .history import JournalQuotaExceeded
from .limits import Limits, LimitExceeded
from .observations import pack_observations
from .protocol import WORKER_TYPES, ProtocolError, encode_frame, read_frame, write_frame


class Supervisor:
    def __init__(self, provider, sandbox, journal, limits=None, on_event=None, *, context_window_tokens=None):
        self.provider, self.sandbox, self.journal = provider, sandbox, journal
        self.limits = limits or Limits()
        self.context_window_tokens = self.limits.input_tokens if context_window_tokens is None else context_window_tokens
        if type(self.context_window_tokens) is not int or self.context_window_tokens <= 0:
            raise ValueError("context_window_tokens must be a positive integer")
        self._memories_count = 0
        self._namespace_summary = {"variables": [], "truncated": False}
        self.on_event = on_event or (lambda event: None)
        self.state = "IDLE"
        self.context = None
        self.pending: list[dict] = []
        self.included: set[str] = set()
        self.revision = 0
        self.cell_id = None
        self.usage: list[dict] = []
        self._wake = asyncio.Event()
        self._driver = self._reader = self._stderr = self._generation = None
        self._cell_done = None
        self._closed = False
        self._kernel_dead = False
        self._reset = False
        self._requests = self._cells = self._generations = 0
        self._known_cells: set[str] = set()
        self._output_bytes: dict[str, int] = {}
        self._broker_ids: set[tuple[str, str]] = set()
        self._broker_counts: dict[str, int] = {}
        self._current_group = None
        self._observation = []
        self._finals = []
        self._storage_failed = False
        self._invalidated = None
        self._orphans = set()
        self._storage_cleanup = None
        self._stderr_tail = ""
        self.process = None

    def _emit(self, kind, content, *, notify=True, **metadata):
        if self._storage_failed:
            # Cleanup/status notifications must not recursively write a full disk.
            event = {"kind": kind, "content": content, "persisted": False, **metadata}
        else:
            try:
                event = self.journal.append(kind, content, **metadata)
            except (JournalQuotaExceeded, OSError, sqlite3.Error) as exc:
                self._fatal_storage(kind, exc)
                raise
        if notify:
            self.on_event(event)
        return event

    def _fatal_storage(self, kind, exc):
        self._storage_failed = self._kernel_dead = True
        self.revision += 1
        self.state = "FAILED"
        if self._generation:
            self._generation.cancel()
        if self._invalidated:
            self._invalidated.set()
        # Cleanup cannot depend on another successful append or the caller path.
        if self._storage_cleanup is None:
            self._storage_cleanup = asyncio.create_task(self.sandbox.close())
        fatal = {"kind": "error", "content": "Journal unavailable; stopping kernel. Evidence beyond this boundary was not accepted.", "persisted": False}
        with suppress(Exception):
            fatal = self.journal.append_emergency(f"Journal stopped while recording {kind}: {exc}")
        self.on_event(fatal)

    def _commit_epoch(self, *args, **kwargs):
        try:
            return self.journal.commit_epoch(*args, **kwargs)
        except (JournalQuotaExceeded, OSError, sqlite3.Error) as exc:
            self._fatal_storage("epoch_commit", exc)
            raise

    def _state(self, state):
        self.state = state
        self._emit("state", state)

    async def start(self):
        if self._driver or self._closed:
            raise RuntimeError("Supervisor cannot be started twice")
        # A fresh supervisor never restores arbitrary objects or replays cells.
        if self.journal.recent(1):
            raise RuntimeError("Reopening runs is not implemented; create a fresh run. Existing history remains readable.")
        self.process = await self.sandbox.start()
        try:
            self._stderr = asyncio.create_task(self._read_stderr())
            ready = await asyncio.wait_for(read_frame(self.process.stdout, allowed_types={"ready"}), 20)
            self._emit("kernel", {"epoch": "k1", "status": "fresh_empty_namespace", "ready": ready})
            self.context = Context(self.limits, context_window_tokens=self.context_window_tokens,
                                   memories_count=self._memories_count, namespace_summary=self._namespace_summary)
            self.context.check(self.context.messages())
            self._commit_epoch([], [], [], epoch_id="x1", kernel_epoch="k1",
                               starting_memories_count=self._memories_count,
                               starting_namespace_summary=self._namespace_summary,
                               system_prompt=self.context.contract)
            self._reader = asyncio.create_task(self._read_worker())
            self._driver = asyncio.create_task(self._run())
            self._state("IDLE")
        except BaseException:
            await self.close()
            raise

    def submit(self, text):
        if self._closed or self._kernel_dead or self.context is None:
            raise RuntimeError("No live kernel; start a fresh session. No source will be replayed.")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Input must be nonempty text")
        if len(text.encode("utf-8")) > self.limits.max_user_bytes:
            raise LimitExceeded("User input exceeds max_user_bytes; split it explicitly")
        event = self._emit("user", text)
        self.pending.append(event)
        self.revision += 1
        self._emit("user_queued", event["id"])
        if self._generation and not self._generation.done():
            self._generation.cancel()
        if self._invalidated:
            self._invalidated.set()
        self._wake.set()
        return event["id"]

    def request_reset(self):
        self._reset = True
        # This only changes the next request's context, never runs a cell.
        self._emit("reset_requested", "Clear old context at the next request; Python variables stay alive")

    def status(self):
        return {"model": self.provider.model, "state": self.state, "cell_id": self.cell_id,
                "context_epoch": self.context.epoch if self.context else 0, "kernel_epoch": "k1",
                "queued": len(self.pending), "requests": self._requests,
                "estimated_input_tokens": self.context.estimate(self.context.messages()) if self.context else 0,
                "estimate_method": "conservative UTF-8 bytes + serialization; not provider tokens",
                "usage": self.usage, "context_window_tokens": self.context_window_tokens,
                "context_input_tokens": self.context.reported_input_tokens if self.context else None}

    def _include(self):
        for event in self.pending:
            if event["id"] not in self.included:
                self.context.add("user", event["content"], [event["id"]])
                self.included.add(event["id"])
                self._emit("user_included", event["id"])

    async def _run(self):
        while not self._closed:
            await self._wake.wait()
            self._wake.clear()
            if self._kernel_dead:
                return
            try:
                await self._drive()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._emit("error", str(exc))
                self._state("FAILED")
                # Pause visibly; another user message can adjust the task/input.

    async def _drive(self):
        while not self._closed and not self._kernel_dead:
            completion, revision, generation_id = await self._generate()
            if completion is None:
                continue
            # Atomic relative to submit(): no await before writer.write().
            if self._closed or self._kernel_dead or asyncio.current_task().cancelling():
                return
            if revision != self.revision:
                self._emit("generation_stale", {"generation_id": generation_id, "usage": completion.usage})
                continue
            result, finals = await self._execute(completion.text, generation_id, phase=completion.phase)
            if self.pending:
                continue
            if result["status"] == "wait":
                self._wake.clear()
                self._state("IDLE")
                return
            if finals and result["status"] == "success":
                self._wake.clear()
                self._state("DONE")
                return

    async def _generate(self):
        for attempt in range(self.limits.generation_retries + 1):
            self._include()
            self._wake.clear()  # this revision's wakeup is now being handled
            if self._reset or self.context.needs_reset():
                self._compact_context()
            messages = self.context.messages()
            estimate = self.context.check(messages)
            if self._requests >= self.limits.max_requests:
                raise LimitExceeded("Request budget exhausted; no further provider calls")
            self._requests += 1
            self._generations += 1
            generation_id = f"a1:g{self._generations:06d}"
            revision = self.revision
            max_tokens = self.limits.output_tokens
            request_record = {"messages": messages, "model": self.provider.model,
                              "max_tokens": max_tokens, "estimated_input_tokens": estimate}
            details = getattr(self.provider, "request_details", None)
            if callable(details):
                # Exact nonsecret serialized request, never authorization headers.
                request_record["provider_request"] = details(messages, max_tokens=max_tokens)
            self._emit("generation_request", request_record,
                       generation_id=generation_id, context_epoch=f"x{self.context.epoch}")
            self._state("GENERATING")
            self._generation = asyncio.create_task(self.provider.generate(messages, max_tokens=max_tokens))
            self._invalidated = asyncio.Event()
            invalidation = asyncio.create_task(self._invalidated.wait())
            started = time.monotonic()
            try:
                await asyncio.wait({self._generation, invalidation}, return_when=asyncio.FIRST_COMPLETED)
                if not self._generation.done():
                    raise asyncio.CancelledError
                completion = self._generation.result()
            except asyncio.CancelledError:
                self._emit("generation_cancelled", {"generation_id": generation_id, "usage": "unknown",
                           "stale": revision != self.revision})
                if self._closed or self._kernel_dead or asyncio.current_task().cancelling():
                    raise
                return None, revision, generation_id
            except Exception as exc:
                self._emit("generation_error", {"generation_id": generation_id, "error": str(exc), "usage": "unknown"})
                self.context.observation({"generation_error": str(exc), "executed": False})
                if getattr(exc, "kind", None) in {"authentication", "configuration", "response_format", "response_error"}:
                    raise RuntimeError(str(exc)) from None  # retrying cannot repair credentials
                if attempt == self.limits.generation_retries:
                    raise RuntimeError(f"Provider failed within retry bound; paused. Last error: {exc}") from exc
                continue
            finally:
                invalidation.cancel()
                if self._generation and not self._generation.done():
                    task = self._generation
                    task.cancel()
                    self._orphans.add(task)
                    task.add_done_callback(lambda done, gid=generation_id: self._late_generation(done, gid))
                self._generation = None
                self._invalidated = None
            self.usage.append(completion.usage)
            self._emit("generation_response", asdict(completion), generation_id=generation_id,
                       duration=time.monotonic() - started, stale=revision != self.revision)
            if self._closed or self._kernel_dead or asyncio.current_task().cancelling():
                raise asyncio.CancelledError
            if revision != self.revision:
                self._emit("generation_stale", {"generation_id": generation_id})
                return None, revision, generation_id
            # Usage from cancelled, stale or orphan requests never controls the
            # live epoch's eviction threshold (their audit counters stay above).
            self.context.record_usage(completion.usage)
            if completion.successful:
                return completion, revision, generation_id
            rejection = completion.rejection_reason or completion.finish_reason
            feedback = self._emit("response_rejected", {"reason": rejection, "executed": False}, generation_id=generation_id)
            self.context.observation({"rejected_generation": rejection,
                                      "executed": False, "instruction": "Your previous response was not executed. Correct the reported error and return one complete raw IPython cell in the final answer, with any brief rationale as # comments. Do not repeat commentary as actions or continue partial source."}, [feedback["id"]])
            if attempt < self.limits.generation_retries:
                self._emit("retry", f"Response not executed: {rejection}. Asking the model to correct it ({attempt + 1}/{self.limits.generation_retries} retries).")
            if attempt == self.limits.generation_retries:
                raise RuntimeError(f"No successful executable completion within retry bound. Last rejection: {rejection}")
        raise AssertionError("unreachable")

    def _late_generation(self, task, generation_id):
        self._orphans.discard(task)
        try:
            completion = task.result()
        except BaseException:
            return
        if not self._closed:
            self.usage.append(completion.usage)
            with suppress(Exception):
                self._emit("generation_response", asdict(completion), generation_id=generation_id, stale=True)
        # Never dispatch results from revoked provider tasks.

    async def _execute(self, source, generation_id, *, phase=None):
        if self._closed or self._kernel_dead or asyncio.current_task().cancelling():
            raise asyncio.CancelledError
        self._cells += 1
        cell_id = f"a1:c{self._cells:06d}"
        frame = {"v": 1, "type": "execute", "cell_id": cell_id, "source": source}
        data = encode_frame(frame)  # reject oversize before committed dispatch
        event = self._emit("source", source, cell_id=cell_id, generation_id=generation_id)
        self.cell_id = cell_id
        self._known_cells.add(cell_id)
        self._output_bytes[cell_id] = 0
        self._broker_counts[cell_id] = 0
        self._current_group = self.context.add("assistant", source, [event["id"]])
        if phase is not None:
            self._current_group.messages[0]["phase"] = phase
        self._observation = []
        self._finals = []
        self._cell_done = asyncio.get_running_loop().create_future()
        for user in self.pending:
            self._emit("user_accepted", user["id"], cell_id=cell_id)
        self.pending.clear()
        self._state("EXECUTING")
        self._emit("dispatch", {"status": "committed", "kernel_epoch": "k1"}, cell_id=cell_id)
        try:
            self.process.stdin.write(data)
            async with asyncio.timeout(self.limits.cell_seconds):
                await self.process.stdin.drain()
                result = await self._cell_done
        except BaseException as exc:
            self._kernel_dead = True
            await self.sandbox.close()
            self._emit("cell_uncertain", "Cell interrupted/crashed/timed out. Partial effects may remain; no replay.", cell_id=cell_id)
            if isinstance(exc, TimeoutError):
                raise LimitExceeded(
                    f"Cell deadline exceeded ({self.limits.cell_seconds:g}s); kernel terminated. "
                    "Start a fresh session; use --cell-seconds SECONDS for longer cells."
                ) from exc
            if isinstance(exc, EOFError):
                # EOF and diagnostic stderr travel on separate descriptors. Drain
                # briefly after termination so the actual failure reaches the UI.
                if self._stderr:
                    with suppress(Exception):
                        await asyncio.wait_for(asyncio.shield(self._stderr), 0.5)
                detail = self._stderr_tail or "No worker diagnostic was received."
                raise RuntimeError(f"Worker transport closed (launcher exit {self.process.returncode}). {detail}") from exc
            raise
        finally:
            self.cell_id = None
            self._cell_done = None
        self._memories_count = result.get("memories_count")
        self._namespace_summary = result.get("namespace_summary")
        packed = pack_observations(self._observation, self.limits.observation_chars)
        self.context.observation({"cell_id": cell_id, **packed, "status": result["status"],
                                  "error": result.get("error"), "full_evidence": cell_id}, group=self._current_group)
        finals = self._finals
        if result["status"] == "success":
            for content in finals:
                self._emit("say", content, cell_id=cell_id, final=True)
        elif finals:
            self._emit("final_discarded", "Final output not published because cell did not succeed", cell_id=cell_id)
        self._current_group = None
        return result, finals

    def _compact_context(self):
        """Start a fresh context epoch, retaining only undispatched user input."""
        pending = {e["id"] for e in self.pending}
        retained, evicted = self.context.retention(pending, memories_count=self._memories_count,
                                                  namespace_summary=self._namespace_summary)
        # Synchronous durable commit: no steering can interleave with this swap.
        self._commit_epoch([r for g in retained for r in g.refs],
                           [r for g in evicted for r in g.refs], sorted(pending),
                           epoch_id=f"x{self.context.epoch + 1}", kernel_epoch="k1",
                           starting_memories_count=self._memories_count,
                           starting_namespace_summary=self._namespace_summary,
                           system_prompt=self.context.system_prompt(self._memories_count, self._namespace_summary),
                           retained_messages=[g.messages for g in retained])
        self.context.commit(retained, memories_count=self._memories_count, namespace_summary=self._namespace_summary)
        self._reset = False
        self._emit("epoch", {"context_epoch": self.context.epoch,
                             "retained_groups": len(retained), "kernel_epoch": "k1"})

    async def _read_worker(self):
        try:
            while not self._closed:
                frame = await read_frame(self.process.stdout, allowed_types=WORKER_TYPES - {"ready"})
                cell_id = frame["cell_id"]
                if cell_id not in self._known_cells:
                    raise ProtocolError("Worker supplied unknown cell ID")
                kind = frame["type"]
                active = cell_id == self.cell_id and self._cell_done is not None and not self._cell_done.done()
                if not active and kind != "output":
                    raise ProtocolError("Worker event outside its active cell")
                size = len(encode_frame(frame))
                if self._output_bytes[cell_id] + size > self.limits.max_output_bytes:
                    raise LimitExceeded("Cell output/frame quota exceeded; kernel stopped before further evidence was accepted")
                self._output_bytes[cell_id] += size
                if kind == "broker_request":
                    await self._broker(frame)
                elif kind == "output":
                    event = self._emit("output", frame["text"], cell_id=cell_id, stream=frame["stream"], asynchronous=not active)
                    if active:
                        self._current_group.refs.append(event["id"])
                        self._observation.append({"id": event["id"], "stream": frame["stream"], "text": frame["text"]})
                    else:
                        self.context.observation({"asynchronous_output_from": cell_id, "excerpt": frame["text"][:self.limits.observation_chars]}, [event["id"]])
                elif kind == "say":
                    event = self._emit("say_staged" if frame["final"] else "say", frame["content"],
                                       cell_id=cell_id, final=frame["final"], notify=not frame["final"])
                    self._current_group.refs.append(event["id"])
                    self._observation.append({"id": event["id"], "say": frame["content"], "final": frame["final"]})
                    if frame["final"]:
                        self._finals.append(frame["content"])
                elif kind == "cell_end":
                    event = self._emit("cell_end", frame, cell_id=cell_id)
                    self._current_group.refs.append(event["id"])
                    self._cell_done.set_result(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._closed or (self._kernel_dead and (self._cell_done is None or self._cell_done.done())):
                # Intentional termination already has a cause (deadline/interrupt).
                # Its ensuing EOF is not a second, unexplained worker crash.
                return
            self._kernel_dead = True
            if self._cell_done and not self._cell_done.done():
                self._cell_done.set_exception(exc)
            else:
                self._emit("error", f"Worker lost: {exc}. Live state is unavailable; no replay.")
                self._state("FAILED")
                if self._generation:
                    self._generation.cancel()
            await self.sandbox.close()

    async def _broker(self, frame):
        key = (frame["cell_id"], frame["request_id"])
        if key in self._broker_ids:
            raise ProtocolError("Duplicate broker request")
        self._broker_ids.add(key)
        self._broker_counts[frame["cell_id"]] += 1
        if self._broker_counts[frame["cell_id"]] > 100:
            raise LimitExceeded("Broker request limit exceeded")
        response = {"v": 1, "type": "broker_response", "cell_id": frame["cell_id"], "request_id": frame["request_id"]}
        try:
            # Validated method allowlist only; no paths, eval, capabilities or user input.
            method = {"recent": self.journal.recent, "search": self.journal.search, "read": self.journal.read}[frame["method"]]
            response["result"] = method(**frame["args"])
            # History index pages may expand under JSON escaping: bound at transport.
            encode_frame(response)
        except (ValueError, KeyError, ProtocolError) as exc:
            response.pop("result", None)
            response["error"] = f"History request rejected: {exc}"
        self._emit("retrieval", {"request": frame, "response": response}, cell_id=frame["cell_id"])
        await write_frame(self.process.stdin, response)

    async def _read_stderr(self):
        total = 0
        while not self._closed:
            data = await self.process.stderr.read(4096)
            if not data:
                return
            total += len(data)
            if total > self.limits.max_output_bytes:
                self._kernel_dead = True
                await self.sandbox.close()
                return
            text = data.decode("utf-8", "replace")
            self._stderr_tail = (self._stderr_tail + text)[-4096:]
            self._emit("launcher_stderr", text)

    async def interrupt(self):
        if self._closed:
            return
        self.revision += 1  # revoke dispatch authority even if cancellation is swallowed
        if self._invalidated:
            self._invalidated.set()
        if self.state == "GENERATING" and self._generation:
            # Stop the task, not just the provider, so cancellation cannot restart it.
            if self._driver:
                self._driver.cancel()
                with suppress(asyncio.CancelledError):
                    await self._driver
            self._generation = None
            self._driver = asyncio.create_task(self._run())
            self._wake.clear()
            self._emit("interrupted", "Generation cancelled; source not executed. Submit steering to continue.")
            self._state("INTERRUPTED")
        elif self.state == "EXECUTING":
            self._kernel_dead = True
            if self._driver:
                self._driver.cancel()
                with suppress(asyncio.CancelledError):
                    await self._driver
            await self.sandbox.interrupt()
            self._emit("interrupted", "Kernel terminated. Partial effects may remain; start a fresh session. No replay.")
            self._state("INTERRUPTED")

    async def close(self):
        if self._closed:
            return
        self._closed = True
        self.revision += 1
        if self._invalidated:
            self._invalidated.set()
        for task in tuple(self._orphans):
            task.cancel()
        tasks = {task for task in (self._driver, self._generation, self._reader, self._stderr, *self._orphans)
                 if task and task is not asyncio.current_task()}
        for task in tasks:
            task.cancel()
        # Kill the sandbox BEFORE waiting on third-party provider cooperation.
        await self.sandbox.close()
        if self._storage_cleanup:
            with suppress(Exception):
                await self._storage_cleanup
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=1)
            for task in done:
                with suppress(BaseException):
                    task.result()
            if pending:
                self.on_event({"kind": "error", "content": "Provider cleanup incomplete; no execution authority, incurred usage unknown.", "persisted": False})
        self.state = "CANCELLED"
