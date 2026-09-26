"""Unrestricted persistent local IPython executor.

The worker is a lifecycle and transport boundary, not a sandbox or a security
boundary. Code runs with the current user's permissions and can access the
filesystem, subprocesses, network, and credentials available to this process.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import signal
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from .contracts import (
    CompletionResult, ExecutionOutput, ExecutionRequest, ExecutionResult,
    ExecutorCapabilities, InputCancelledError, InputReply, InputRequest,
    InputUnavailableError, InspectionResult, MAX_INPUT_PROMPT_CHARS,
    MAX_INPUT_REQUESTS_PER_EXECUTION, MAX_INPUT_VALUE_CHARS, SayOutput,
)

MAX_FRAME = 1_048_576
MAX_OUTPUT_CHARS = 1_048_576
DEFAULT_OUTPUT_CHARS = 262_144
MAX_OUTPUT_FRAME_CHARS = 8_192
MAX_ERROR_CHARS = 8_192
MAX_SAY_CHARS = MAX_FRAME
MAX_SAY_MESSAGES = 1_024
MAX_RICH_OUTPUT_FRAMES = 256
MAX_RICH_OUTPUT_BYTES = 2_097_152
MAX_QUERY_CHARS = 65_536
MAX_INPUT_ERROR_CHARS = 8_192
MAX_PASSWORD_SECRETS = 128
MAX_PASSWORD_SECRET_CHARS = 1_048_576
MAX_COMPLETION_MATCHES = 512
MAX_COMPLETION_MATCH_CHARS = 2_048
MAX_INSPECTION_CHARS = 16_384


class _ProtocolError(ValueError):
    """Invalid or miscorrelated worker data."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _ProtocolError("Duplicate key in worker frame")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise _ProtocolError(f"Invalid JSON number: {value}")


def _valid_timeout(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


def _origin_payload(request: ExecutionRequest) -> dict[str, Any]:
    origin = request.origin
    payload = {
        "session_id": origin.session_id,
        "request_id": origin.request_id,
        "frontend_id": origin.frontend_id,
        "config_revision": origin.config_revision,
        "generation_id": origin.generation_id,
        "execution_id": origin.execution_id,
    }
    for key in ("session_id", "request_id", "frontend_id"):
        value = payload[key]
        if not isinstance(value, str) or not 0 < len(value) <= 128 or "\x00" in value:
            raise ValueError("Execution origin contains an invalid identity")
    for key in ("generation_id", "execution_id"):
        value = payload[key]
        if value is not None and (not isinstance(value, str) or not 0 < len(value) <= 128 or "\x00" in value):
            raise ValueError("Execution origin contains an invalid optional identity")
    revision = payload["config_revision"]
    if type(revision) is not int or revision < 0:
        raise ValueError("Execution origin has an invalid configuration revision")
    return payload


def _encode_frame(message: dict[str, Any]) -> bytes:
    try:
        payload = json.dumps(message, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("Execution request is not valid JSON data") from exc
    if not 0 < len(payload) <= MAX_FRAME:
        raise ValueError(f"Execution request exceeds the {MAX_FRAME}-byte transport limit")
    return len(payload).to_bytes(4, "big") + payload


def _redact_text(text: str, secrets: list[str]) -> str:
    marker = "[REDACTED]"
    if any(secret and secret in marker for secret in secrets):
        marker = ""
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            text = text.replace(secret, marker)
    return text


def _redact_json(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if isinstance(value, list):
        return [_redact_json(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_json(item, secrets) for item in value)
    if isinstance(value, Mapping):
        return {
            _redact_text(key, secrets) if isinstance(key, str) else key:
            _redact_json(item, secrets)
            for key, item in value.items()
        }
    return value


def _strip_partial_secret_suffix(text: str, secrets: list[str]) -> str:
    partial = 0
    for secret in secrets:
        for length in range(min(len(secret) - 1, len(text)), 0, -1):
            if length > partial and text.endswith(secret[:length]):
                partial = length
                break
    return text[:-partial] if partial else text


def _redact_stream_fragments(
    events: list[ExecutionOutput],
    secrets: list[str],
    *,
    truncated_streams: frozenset[str] = frozenset(),
) -> tuple[ExecutionOutput, ...]:
    """Redact complete stream text, including secrets split across frames."""
    if not secrets:
        return tuple(events)
    replacements: dict[int, str] = {}
    alternatives = [re.escape(secret) for secret in sorted(secrets, key=len, reverse=True) if secret]
    if not alternatives:
        return tuple(events)
    expression = re.compile("|".join(alternatives))
    for stream in ("stdout", "stderr"):
        indices = [
            index for index, event in enumerate(events)
            if event.kind == "stream" and event.data.get("name") == stream
            and isinstance(event.data.get("text"), str)
        ]
        if not indices:
            continue
        fragments = [events[index].data["text"] for index in indices]
        raw_combined = "".join(fragments)
        combined = (
            _strip_partial_secret_suffix(raw_combined, secrets)
            if stream in truncated_streams else raw_combined
        )
        spans = [(match.start(), match.end()) for match in expression.finditer(combined)]
        if not spans and combined == raw_combined:
            continue
        offset = 0
        for index, fragment in zip(indices, fragments):
            end = offset + len(fragment)
            safe_end = min(end, len(combined))
            pieces: list[str] = []
            cursor = offset
            for match_start, match_end in spans:
                if match_end <= offset:
                    continue
                if match_start >= safe_end:
                    break
                if match_start >= cursor:
                    pieces.append(combined[cursor:match_start])
                    marker = "[REDACTED]"
                    if any(secret and secret in marker for secret in secrets):
                        marker = ""
                    pieces.append(marker)
                cursor = max(cursor, min(match_end, safe_end))
            if cursor < safe_end:
                pieces.append(combined[cursor:safe_end])
            replacements[index] = "".join(pieces)
            offset = end
    if not replacements:
        return tuple(events)
    result = list(events)
    for index, text in replacements.items():
        event = result[index]
        data = dict(event.data)
        data["text"] = text
        result[index] = ExecutionOutput(
            "stream", data, display_id=event.display_id, metadata=event.metadata,
        )
    return tuple(result)


def _decode_frame(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise _ProtocolError("Malformed worker frame") from exc
    if not isinstance(value, dict):
        raise _ProtocolError("Worker frame must be an object")
    return value


class _OutputCollector:
    """Retain a bounded prefix and report every discarded character."""

    def __init__(self, maximum: int):
        self.maximum = maximum
        self.retained = 0
        self.parts: dict[str, list[str]] = {"stdout": [], "stderr": []}
        self.discarded = {"stdout": 0, "stderr": 0}

    def append(self, stream: str, text: str) -> str:
        available = self.maximum - self.retained
        retained = min(available, len(text))
        accepted = text[:retained]
        if accepted:
            self.parts[stream].append(accepted)
            self.retained += retained
        self.discarded[stream] += len(text) - retained
        return accepted

    def text(self, stream: str, secrets: list[str] | None = None) -> str:
        secrets = [] if secrets is None else secrets
        value = "".join(self.parts[stream])
        omitted = self.discarded[stream]
        if omitted:
            value = _strip_partial_secret_suffix(value, secrets)
        value = _redact_text(value, secrets)
        if omitted:
            value += f"\n[{stream} truncated: {omitted} characters omitted]\n"
        return value


class LocalExecutor:
    """A serialized, persistent, unrestricted IPython process.

    Execution, completion, and inspection share one framed control channel and
    lock, so queries observe the same namespace and cannot race a running cell.
    Queries are bounded and use static lookup rather than evaluating input.

    ``timeout`` is the worker startup timeout (kept as the original constructor
    option); execution has no implicit deadline. Interactive stdin uses bounded,
    correlated request/reply frames and never shares the worker's protocol input
    with user stdin. On POSIX,
    ``interrupt()`` first sends SIGINT and preserves the namespace if IPython
    handles it; if the worker does not stop promptly, the worker is terminated
    and its in-memory state is lost. Task cancellation terminates the worker
    immediately, because a cancelled caller cannot safely drain/reconcile a
    partially completed execution.
    """

    capabilities = ExecutorCapabilities(
        persistent=True, completion=True, inspection=True, rich_output=True,
        input=True, interrupt=True,
    )

    def __init__(
        self,
        *,
        executable: str | None = None,
        timeout: float = 15,
        interrupt_timeout: float = 2,
        input_timeout: float = 300,
        max_output_chars: int = DEFAULT_OUTPUT_CHARS,
    ):
        if not _valid_timeout(timeout):
            raise ValueError("timeout must be a finite positive number")
        if not _valid_timeout(interrupt_timeout):
            raise ValueError("interrupt_timeout must be a finite positive number")
        if not _valid_timeout(input_timeout):
            raise ValueError("input_timeout must be a finite positive number")
        if type(max_output_chars) is not int or not 1 <= max_output_chars <= MAX_OUTPUT_CHARS:
            raise ValueError(f"max_output_chars must be an integer from 1 to {MAX_OUTPUT_CHARS}")
        self.executable = executable or sys.executable
        self.timeout = timeout
        self.interrupt_timeout = interrupt_timeout
        self.input_timeout = input_timeout
        self.max_output_chars = max_output_chars
        self.process: asyncio.subprocess.Process | None = None
        self._execute_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._active_done = asyncio.Event()
        self._active_done.set()
        self._active_execution_id: str | None = None
        self._active_input_task: asyncio.Task | None = None
        self._active_input_cancel: asyncio.Event | None = None
        self._password_secrets: list[str] = []
        self._password_secret_chars = 0
        self._started = False
        self._closed = False

    async def _receive(self, process: asyncio.subprocess.Process) -> dict[str, Any]:
        if process.stdout is None:
            raise _ProtocolError("Worker output pipe is unavailable")
        header = await process.stdout.readexactly(4)
        size = int.from_bytes(header, "big")
        if not 0 < size <= MAX_FRAME:
            raise _ProtocolError("Invalid worker frame length")
        payload = await process.stdout.readexactly(size)
        return _decode_frame(payload)

    async def _send(self, process: asyncio.subprocess.Process, frame: bytes) -> None:
        if process.stdin is None:
            raise _ProtocolError("Worker input pipe is unavailable")
        process.stdin.write(frame)
        await process.stdin.drain()

    async def _query_worker(self, outgoing: bytes, request_id: str, expected_type: str) -> dict[str, Any]:
        """Send one bounded query while owning the same channel lock as execute."""
        async with self._execute_lock:
            process = self.process
            if not self._started or self._closed or process is None or process.returncode is not None:
                raise RuntimeError("Worker unavailable for completion/inspection")
            try:
                await self._send(process, outgoing)
                while True:
                    frame = await self._receive(process)
                    if frame.get("type") in ("output", "rich_output"):
                        # A descendant may retain an earlier cell's output FD.
                        # Drain its bounded frames without attributing them to a query.
                        continue
                    if (
                        type(frame.get("version")) is not int
                        or frame.get("version") != 1
                        or frame.get("type") != expected_type
                        or frame.get("request_id") != request_id
                    ):
                        raise _ProtocolError("Worker query result does not match the request")
                    return frame
            except asyncio.CancelledError:
                # A sent query may still have a reply in flight. Discard this
                # worker rather than letting that late frame corrupt a later cell.
                await asyncio.shield(self._stop_process(process))
                raise
            except (OSError, EOFError, asyncio.IncompleteReadError, TypeError, ValueError, RuntimeError) as exc:
                await self._stop_process(process)
                raise RuntimeError("Worker query failed; the persistent namespace is unavailable") from exc

    async def store_outputs(self, texts: tuple[str, ...]) -> tuple[int, ...]:
        """Save bounded old observations as strings in the live worker namespace.

        This is a control request, not a Python cell: no source is replayed and
        no provider-facing text is removed until all references are confirmed.
        """
        if (not isinstance(texts, tuple) or not 1 <= len(texts) <= 10
                or any(not isinstance(text, str) or len(text) > 8_000 for text in texts)):
            raise ValueError("Output storage requires 1–10 strings of at most 8000 characters")
        request_id = uuid4().hex
        outgoing = _encode_frame({
            "type": "store_outputs", "version": 1, "request_id": request_id,
            "texts": texts,
        })
        frame = await self._query_worker(outgoing, request_id, "outputs_stored")
        indexes = frame.get("indexes")
        if (set(frame) != {"type", "version", "request_id", "indexes"}
                or not isinstance(indexes, list) or len(indexes) != len(texts)
                or any(type(index) is not int or not 1 <= index <= 1_000_000_000
                       for index in indexes)
                or indexes != sorted(set(indexes))):
            raise _ProtocolError("Malformed worker output storage acknowledgement")
        return tuple(indexes)

    @staticmethod
    def _validate_query(code: str, cursor_pos: int) -> None:
        if not isinstance(code, str):
            raise TypeError("Query source must be text")
        if len(code) > MAX_QUERY_CHARS:
            raise ValueError(f"Query source exceeds the {MAX_QUERY_CHARS}-character limit")
        if type(cursor_pos) is not int or not 0 <= cursor_pos <= len(code):
            raise ValueError("Query cursor position is outside the source")

    async def complete(self, code: str, cursor_pos: int) -> CompletionResult:
        """Complete against the persistent worker namespace without evaluating code."""
        self._validate_query(code, cursor_pos)
        request_id = uuid4().hex
        outgoing = _encode_frame({
            "type": "complete", "version": 1, "request_id": request_id,
            "code": code, "cursor_pos": cursor_pos,
        })
        frame = await self._query_worker(outgoing, request_id, "completion")
        if set(frame) != {
            "type", "version", "request_id", "matches", "cursor_start", "cursor_end", "metadata",
        }:
            raise _ProtocolError("Malformed completion response")
        matches = frame["matches"]
        start, end, metadata = frame["cursor_start"], frame["cursor_end"], frame["metadata"]
        if (
            not isinstance(matches, list)
            or len(matches) > MAX_COMPLETION_MATCHES
            or any(type(match) is not str or len(match) > MAX_COMPLETION_MATCH_CHARS for match in matches)
            or type(start) is not int
            or type(end) is not int
            or not 0 <= start <= end <= len(code)
            or end != cursor_pos
            or not isinstance(metadata, dict)
        ):
            raise _ProtocolError("Invalid completion response")
        return CompletionResult(tuple(matches), start, end, metadata)

    async def inspect(self, code: str, cursor_pos: int, detail_level: int = 0) -> InspectionResult:
        """Inspect a name statically; no submitted source or object repr is run."""
        self._validate_query(code, cursor_pos)
        if type(detail_level) is not int or detail_level not in (0, 1):
            raise ValueError("Inspection detail level must be 0 or 1")
        request_id = uuid4().hex
        outgoing = _encode_frame({
            "type": "inspect", "version": 1, "request_id": request_id,
            "code": code, "cursor_pos": cursor_pos, "detail_level": detail_level,
        })
        frame = await self._query_worker(outgoing, request_id, "inspection")
        if set(frame) != {"type", "version", "request_id", "found", "data", "metadata"}:
            raise _ProtocolError("Malformed inspection response")
        found, data, metadata = frame["found"], frame["data"], frame["metadata"]
        if type(found) is not bool or not isinstance(data, dict) or not isinstance(metadata, dict):
            raise _ProtocolError("Invalid inspection response")
        if not found and data:
            raise _ProtocolError("Unsuccessful inspection carried data")
        if (any(type(mime) is not str for mime in data)
                or any(type(value) is not str or len(value) > MAX_INSPECTION_CHARS
                       for value in data.values())):
            raise _ProtocolError("Inspection MIME data exceeds its text bound")
        return InspectionResult(found, data, metadata)

    async def start(self) -> None:
        if self._closed or self._started or self.process is not None:
            raise RuntimeError("Executor already started or closed")
        kwargs: dict[str, Any] = {}
        if sys.platform != "win32":
            # Keep terminal SIGINT directed at the frontend; interrupt() targets
            # this worker explicitly instead of sharing the frontend's group.
            kwargs["start_new_session"] = True
        try:
            # -m resolves from the launch directory; load a pinned absolute
            # module file with -I so workspace packages cannot shadow runtime.
            worker_path = Path(__file__).resolve().with_name("local_worker.py")
            runtime_parent = str(worker_path.parent.parent)
            bootstrap = (
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "from py_agent.local_worker import main; main()"
            )
            self.process = await asyncio.create_subprocess_exec(
                self.executable,
                "-I", "-c", bootstrap, runtime_parent,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=MAX_FRAME + 4,
                **kwargs,
            )
            ready = await asyncio.wait_for(self._receive(self.process), timeout=self.timeout)
            if (
                set(ready) != {"type", "version"}
                or ready.get("type") != "ready"
                or type(ready.get("version")) is not int
                or ready["version"] != 1
            ):
                raise _ProtocolError("Invalid worker ready frame")
            self._started = True
        except BaseException:
            process = self.process
            if process is not None:
                await asyncio.shield(self._stop_process(process))
            else:
                self._closed = True
            raise

    def _remember_password(self, value: str) -> bool:
        if not value or value in self._password_secrets:
            return True
        if (len(self._password_secrets) >= MAX_PASSWORD_SECRETS
                or self._password_secret_chars + len(value) > MAX_PASSWORD_SECRET_CHARS):
            return False
        self._password_secrets.append(value)
        self._password_secret_chars += len(value)
        return True

    def _safe_output_events(
        self, events: list[ExecutionOutput], *, truncated_streams: frozenset[str] = frozenset(),
    ) -> tuple[ExecutionOutput, ...]:
        result = []
        for event in _redact_stream_fragments(
            events, self._password_secrets, truncated_streams=truncated_streams,
        ):
            data = _redact_json(event.data, self._password_secrets)
            metadata = _redact_json(event.metadata, self._password_secrets)
            display_id = _redact_text(event.display_id, self._password_secrets) if event.display_id else None
            result.append(ExecutionOutput(
                event.kind, data, display_id=display_id, metadata=metadata,
            ))
        return tuple(result)

    async def _service_input_request(
        self,
        process: asyncio.subprocess.Process,
        frame: dict[str, Any],
        request: ExecutionRequest,
        execution_id: str,
        origin: dict[str, Any],
        previous_sequence: int,
    ) -> int:
        expected = {
            "type", "version", "execution_id", "origin", "author", "sequence", "prompt", "password",
        }
        sequence = frame.get("sequence")
        if (
            set(frame) != expected
            or type(frame.get("version")) is not int
            or frame["version"] != 1
            or frame.get("execution_id") != execution_id
            or frame.get("origin") != origin
            or frame.get("author") != request.author
            or type(sequence) is not int
            or sequence != previous_sequence + 1
            or sequence > MAX_INPUT_REQUESTS_PER_EXECUTION
            or not isinstance(frame.get("prompt"), str)
            or len(frame["prompt"]) > MAX_INPUT_PROMPT_CHARS
            or type(frame.get("password")) is not bool
        ):
            raise _ProtocolError("Malformed or miscorrelated worker input request")

        if request.origin.execution_id is None:
            await self._send(process, _encode_frame({
                "type": "input_error", "version": 1,
                "execution_id": execution_id, "origin": origin,
                "author": request.author, "sequence": sequence,
                "password": frame["password"],
                "error": "Interactive input requires an execution-correlated origin",
                "cancelled": False,
            }))
            return sequence

        input_request = InputRequest(
            request.origin, sequence, request.origin.frontend_id,
            frame["prompt"], frame["password"],
        )

        async def reply_error(message: str, *, cancelled: bool = False) -> None:
            await self._send(process, _encode_frame({
                "type": "input_error", "version": 1,
                "execution_id": execution_id, "origin": origin,
                "author": request.author, "sequence": sequence,
                "password": input_request.password,
                "error": message[:MAX_INPUT_ERROR_CHARS], "cancelled": cancelled,
            }))

        if not request.allow_stdin:
            await reply_error(
                "Interactive input unavailable: this frontend set allow_stdin=false",
            )
            return sequence
        if request.input_handler is None:
            await reply_error(
                "Interactive input unavailable: the owning frontend has no stdin handler",
            )
            return sequence

        async def invoke_handler() -> InputReply:
            result = request.input_handler(input_request)
            if not hasattr(result, "__await__"):
                raise TypeError("Input handler must return an awaitable InputReply")
            return await result

        handler_task = asyncio.create_task(invoke_handler())
        cancel_event = asyncio.Event()
        cancel_waiter = asyncio.create_task(cancel_event.wait())
        self._active_input_task = handler_task
        self._active_input_cancel = cancel_event

        def consume_late_task(task: asyncio.Task) -> None:
            if not task.cancelled():
                try:
                    task.exception()
                except BaseException:
                    pass

        try:
            try:
                done, _pending = await asyncio.wait(
                    {handler_task, cancel_waiter},
                    timeout=self.input_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_event.is_set():
                    handler_task.cancel()
                    handler_task.add_done_callback(consume_late_task)
                    await reply_error("Interactive input was cancelled", cancelled=True)
                    return sequence
                if handler_task not in done:
                    handler_task.cancel()
                    handler_task.add_done_callback(consume_late_task)
                    await reply_error("Interactive input timed out before the frontend replied")
                    return sequence
                try:
                    reply = handler_task.result()
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise
                    await reply_error("Interactive input was cancelled", cancelled=True)
                    return sequence
                except (InputCancelledError, KeyboardInterrupt):
                    await reply_error("Interactive input was cancelled", cancelled=True)
                    return sequence
                except InputUnavailableError as exc:
                    message = str(exc) or "Interactive input is unavailable"
                    await reply_error(
                        "Password input failed; no value was accepted" if input_request.password else message,
                    )
                    return sequence
                except Exception as exc:
                    message = str(exc) or "Interactive input failed"
                    await reply_error(
                        "Password input failed; no value was accepted" if input_request.password
                        else f"Interactive input failed: {message}",
                    )
                    return sequence
            except asyncio.CancelledError:
                handler_task.cancel()
                handler_task.add_done_callback(consume_late_task)
                raise
        finally:
            cancel_waiter.cancel()
            if self._active_input_task is handler_task:
                self._active_input_task = None
            if self._active_input_cancel is cancel_event:
                self._active_input_cancel = None

        if (
            not isinstance(reply, InputReply)
            or reply.origin != input_request.origin
            or reply.sequence != input_request.sequence
            or reply.owner_frontend_id != input_request.owner_frontend_id
            or reply.password is not input_request.password
        ):
            await reply_error(
                "Interactive input reply did not match its originating frontend and execution",
            )
            return sequence
        if reply.password and not self._remember_password(reply.value):
            await reply_error("Password redaction capacity is exhausted; no value was accepted")
            return sequence
        await self._send(process, _encode_frame({
            "type": "input_reply", "version": 1,
            "execution_id": execution_id, "origin": origin,
            "author": request.author, "sequence": sequence,
            "password": reply.password, "value": reply.value,
        }))
        return sequence

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if request.language not in ("ipython", "python"):
            return ExecutionResult(request.origin, "error", "Unsupported language")
        if request.author not in ("user", "agent"):
            return ExecutionResult(request.origin, "error", "Execution author must be 'user' or 'agent'")
        if not isinstance(request.source, str):
            return ExecutionResult(request.origin, "error", "Execution source must be nonempty text")
        if len(request.source) > MAX_FRAME:
            return ExecutionResult(
                request.origin,
                "error",
                f"Execution source exceeds the {MAX_FRAME}-byte frame limit",
            )
        if not request.source.strip():
            return ExecutionResult(request.origin, "error", "Execution source must be nonempty text")

        execution_id = uuid4().hex
        try:
            origin = _origin_payload(request)
            outgoing = _encode_frame({
                "type": "execute",
                "version": 1,
                "execution_id": execution_id,
                "origin": origin,
                "author": request.author,
                # Preserve the exact routed source; do not strip, wrap, or merge
                # user and agent cells. IPython owns the persistent input history.
                "source": request.source,
            })
        except ValueError as exc:
            return ExecutionResult(request.origin, "error", str(exc))

        async with self._execute_lock:
            process = self.process
            if not self._started or self._closed or process is None or process.returncode is not None:
                return ExecutionResult(request.origin, "uncertain", "Worker unavailable; execution may not have run")

            collector = _OutputCollector(self.max_output_chars)
            say_outputs: list[SayOutput] = []
            output_events: list[ExecutionOutput] = []
            say_characters = 0
            rich_frame_count = 0
            rich_frame_bytes = 0
            input_sequence = 0
            preview_sent = False
            preview_buffers = {"stdout": "", "stderr": ""}

            def completed_output_events() -> tuple[ExecutionOutput, ...]:
                truncated_streams = frozenset(
                    stream for stream, omitted in collector.discarded.items() if omitted
                )
                completed = list(self._safe_output_events(
                    output_events, truncated_streams=truncated_streams,
                ))
                for stream in ("stdout", "stderr"):
                    omitted = collector.discarded[stream]
                    if omitted:
                        completed.append(ExecutionOutput("stream", {
                            "name": stream,
                            "text": f"\n[{stream} truncated: {omitted} characters omitted]\n",
                        }))
                return tuple(completed)

            try:
                await self._send(process, outgoing)
                # Mark active only after the full bounded request is delivered;
                # an interrupt during a queued write must not hit an idle worker.
                self._active_execution_id = execution_id
                self._active_done.clear()
                while True:
                    frame = await self._receive(process)
                    if frame.get("version") != 1 or type(frame.get("version")) is not int:
                        raise _ProtocolError("Unsupported worker protocol version")
                    if (
                        frame.get("execution_id") != execution_id
                        or frame.get("origin") != origin
                        or frame.get("author") != request.author
                    ):
                        # A descendant may retain a previous cell's output FD.
                        # Never attach delayed output to the next execution.
                        if frame.get("type") in ("output", "rich_output"):
                            continue
                        raise _ProtocolError("Worker result does not match the active execution")
                    kind = frame.get("type")
                    if kind == "input_request":
                        input_sequence = await self._service_input_request(
                            process, frame, request, execution_id, origin, input_sequence,
                        )
                        continue
                    if kind == "output":
                        if set(frame) != {"type", "version", "execution_id", "origin", "author", "stream", "text"}:
                            raise _ProtocolError("Malformed worker output frame")
                        if frame["stream"] not in ("stdout", "stderr"):
                            raise _ProtocolError("Invalid worker output stream")
                        if not isinstance(frame["text"], str) or len(frame["text"]) > MAX_OUTPUT_FRAME_CHARS:
                            raise _ProtocolError("Invalid worker output text")
                        accepted = collector.append(frame["stream"], frame["text"])
                        if accepted:
                            output_events.append(ExecutionOutput("stream", {
                                "name": frame["stream"], "text": accepted,
                            }))
                            if request.output_handler is not None and not preview_sent:
                                # Show one short, provisional line while the
                                # cell is still running. Hold any suffix that
                                # might be a split password; full output is
                                # redacted and decided only at cell completion.
                                stream_name = frame["stream"]
                                preview_buffers[stream_name] = (
                                    preview_buffers[stream_name] + accepted
                                )[:512]
                                pending_preview = preview_buffers[stream_name]
                                first_line = pending_preview.split("\n", 1)[0]
                                candidate = (first_line + "\n") if "\n" in pending_preview else pending_preview
                                longest = max((len(secret) for secret in self._password_secrets), default=0)
                                if longest:
                                    candidate = candidate[:max(0, len(candidate) - longest + 1)]
                                    candidate = _strip_partial_secret_suffix(candidate, self._password_secrets)
                                candidate = _redact_text(candidate, self._password_secrets)
                                if candidate:
                                    await request.output_handler(ExecutionOutput(
                                        "stream", {"name": frame["stream"], "text": candidate},
                                        metadata={"provisional": True},
                                    ))
                                    preview_sent = True
                        continue
                    if kind == "rich_output":
                        expected_fields = {
                            "type", "version", "execution_id", "origin", "author", "kind", "data",
                            "metadata", "display_id",
                        }
                        if set(frame) != expected_fields:
                            raise _ProtocolError("Malformed worker rich output frame")
                        output_kind, data = frame["kind"], frame["data"]
                        metadata, display_id = frame["metadata"], frame["display_id"]
                        if output_kind not in ("display", "execute_result", "update", "clear"):
                            raise _ProtocolError("Invalid worker rich output kind")
                        if not isinstance(data, dict) or not isinstance(metadata, dict):
                            raise _ProtocolError("Invalid worker MIME bundle")
                        if display_id is not None and (
                            not isinstance(display_id, str) or not 0 < len(display_id) <= 128
                            or "\x00" in display_id
                        ):
                            raise _ProtocolError("Invalid worker display ID")
                        if output_kind == "clear":
                            if set(data) != {"wait"} or type(data["wait"]) is not bool or display_id is not None:
                                raise _ProtocolError("Malformed worker clear-output frame")
                        try:
                            cost = len(json.dumps(
                                frame, ensure_ascii=True, allow_nan=False, separators=(",", ":"),
                            ).encode("ascii"))
                        except (TypeError, ValueError, RecursionError) as exc:
                            raise _ProtocolError("Invalid worker MIME payload") from exc
                        if (rich_frame_count >= MAX_RICH_OUTPUT_FRAMES
                                or rich_frame_bytes + cost > MAX_RICH_OUTPUT_BYTES):
                            raise _ProtocolError("Worker rich output exceeded its aggregate bound")
                        rich_frame_count += 1
                        rich_frame_bytes += cost
                        output_events.append(ExecutionOutput(
                            output_kind, data, display_id=display_id, metadata=metadata,
                        ))
                        continue
                    if kind == "say":
                        expected_fields = {
                            "type", "version", "execution_id", "origin", "author", "content", "final",
                        }
                        if set(frame) != expected_fields or type(frame["final"]) is not bool:
                            raise _ProtocolError("Malformed worker say frame")
                        try:
                            cost = len(json.dumps(
                                frame["content"], ensure_ascii=True, allow_nan=False, separators=(",", ":"),
                            ))
                            output = SayOutput(frame["content"], frame["final"])
                        except (TypeError, ValueError, RecursionError) as exc:
                            raise _ProtocolError("Invalid worker say content") from exc
                        if len(say_outputs) >= MAX_SAY_MESSAGES:
                            raise _ProtocolError("Worker emitted too many say messages")
                        if say_characters + cost > MAX_SAY_CHARS:
                            raise _ProtocolError("Worker say output exceeded its aggregate bound")
                        say_outputs.append(output)
                        say_characters += cost
                        continue
                    if kind == "result":
                        expected_fields = {
                            "type", "version", "execution_id", "origin", "author", "status", "error",
                            "final_requested", "output_reference",
                        }
                        if set(frame) != expected_fields or type(frame["final_requested"]) is not bool:
                            raise _ProtocolError("Malformed worker result frame")
                        status, error = frame["status"], frame["error"]
                        if status not in ("success", "error", "cancelled"):
                            raise _ProtocolError("Invalid worker execution status")
                        if status == "success":
                            if error is not None:
                                raise _ProtocolError("Successful execution carried an error")
                        elif not isinstance(error, str) or len(error) > MAX_ERROR_CHARS:
                            raise _ProtocolError("Invalid worker execution error")
                        final_requested = any(output.final for output in say_outputs)
                        if frame["final_requested"] != final_requested:
                            raise _ProtocolError("Worker final marker did not match its say messages")
                        reference = frame["output_reference"]
                        if reference is not None:
                            if (not isinstance(reference, dict)
                                    or set(reference) != {
                                        "index", "original_chars", "retained_chars", "omitted_chars",
                                    }
                                    or any(type(value) is not int or value < 0 for value in reference.values())
                                    or not 1 <= reference["index"] <= 1_000_000_000
                                    or reference["original_chars"] <= 8_000
                                    or reference["retained_chars"] > 1_048_576
                                    or reference["original_chars"] != (
                                        reference["retained_chars"] + reference["omitted_chars"]
                                    )):
                                raise _ProtocolError("Malformed worker output reference")
                            index = reference["index"]
                            omitted = reference["omitted_chars"]
                            notice = (
                                f"Output exceeded 8000 characters and was removed from the transcript. "
                                f"Saved as outputs[{index}] (str; {reference['retained_chars']} "
                                f"characters retained"
                                + (f", {omitted} omitted" if omitted else "")
                                + f"). Print a smaller slice: print(outputs[{index}][:4000]).\n"
                            )
                            safe_events = []
                            inserted = False
                            for output in output_events:
                                if output.kind == "stream":
                                    if not inserted:
                                        safe_events.append(ExecutionOutput(
                                            "stream", {"name": "stdout", "text": notice},
                                        ))
                                        inserted = True
                                else:
                                    safe_events.append(output)
                            if not inserted:
                                safe_events.append(ExecutionOutput(
                                    "stream", {"name": "stdout", "text": notice},
                                ))
                            stdout_result, stderr_result = _redact_text(notice, self._password_secrets), ""
                            completed = self._safe_output_events(safe_events, truncated_streams=frozenset())
                        else:
                            stdout_result = collector.text("stdout", self._password_secrets)
                            stderr_result = collector.text("stderr", self._password_secrets)
                            completed = completed_output_events()
                        return ExecutionResult(
                            request.origin,
                            status,
                            _redact_text(error, self._password_secrets) if error is not None else None,
                            stdout_result,
                            stderr_result,
                            tuple(SayOutput(
                                _redact_json(output.content, self._password_secrets), output.final,
                            ) for output in say_outputs),
                            status == "success" and frame["final_requested"],
                            completed,
                            output_reference=reference["index"] if reference is not None else None,
                        )
                    raise _ProtocolError("Unexpected worker frame")
            except asyncio.CancelledError:
                # Do not replay or pretend a cancelled side effect rolled back.
                # Kill to prevent an unread late result corrupting the next call.
                await asyncio.shield(self._stop_process(process))
                raise
            except Exception:
                # Includes failure of an optional live-output callback: never
                # leave an unread worker result to corrupt the next request.
                await self._stop_process(process)
                received = collector.retained + sum(collector.discarded.values())
                if received > 8_000:
                    # The worker died before it could assign outputs[index].
                    # Neither the transcript nor journal may expose the raw
                    # oversized prefix on this uncertain path.
                    notice = (
                        "Output exceeded 8000 characters and was removed after worker failure; "
                        "no outputs[index] is available for this cell.\n"
                    )
                    stdout_result, stderr_result = notice, ""
                    safe_events = []
                    inserted = False
                    for output in output_events:
                        if output.kind == "stream":
                            if not inserted:
                                safe_events.append(ExecutionOutput(
                                    "stream", {"name": "stdout", "text": notice},
                                ))
                                inserted = True
                        else:
                            safe_events.append(output)
                    if not inserted:
                        safe_events.append(ExecutionOutput("stream", {"name": "stdout", "text": notice}))
                    completed = self._safe_output_events(safe_events, truncated_streams=frozenset())
                else:
                    stdout_result = collector.text("stdout", self._password_secrets)
                    stderr_result = collector.text("stderr", self._password_secrets)
                    completed = self._safe_output_events(
                        output_events,
                        truncated_streams=frozenset(
                            stream for stream, omitted in collector.discarded.items() if omitted
                        ),
                    )
                return ExecutionResult(
                    request.origin,
                    "uncertain",
                    "Worker lost or protocol failed during execution; side effects may have occurred",
                    stdout_result,
                    stderr_result,
                    tuple(SayOutput(
                        _redact_json(output.content, self._password_secrets), output.final,
                    ) for output in say_outputs),
                    output_events=completed,
                )
            finally:
                self._active_execution_id = None
                self._active_done.set()

    async def interrupt(self) -> None:
        process = self.process
        if process is None or self._active_done.is_set() or self._active_execution_id is None:
            return
        input_task = self._active_input_task
        if input_task is not None and not input_task.done():
            cancel_event = self._active_input_cancel
            if cancel_event is not None:
                cancel_event.set()
            input_task.cancel()
            try:
                await asyncio.wait_for(self._active_done.wait(), timeout=self.interrupt_timeout)
            except asyncio.TimeoutError:
                await self._stop_process(process)
                try:
                    await asyncio.wait_for(self._active_done.wait(), timeout=self.interrupt_timeout)
                except asyncio.TimeoutError:
                    pass
            return
        if sys.platform == "win32":
            # No portable, isolated console interrupt is guaranteed here.
            # Termination is explicit and the namespace is discarded.
            await self._stop_process(process)
            try:
                await asyncio.wait_for(self._active_done.wait(), timeout=self.interrupt_timeout)
            except asyncio.TimeoutError:
                pass
        else:
            try:
                # The worker has its own process group. Interrupt synchronous
                # subprocesses in that group as well as the Python worker.
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._active_done.wait(), timeout=self.interrupt_timeout)
            except asyncio.TimeoutError:
                # User code may mask SIGINT or be blocked in an uninterruptible
                # operation. Do not claim it was interrupted if it did not stop.
                await self._stop_process(process)
                try:
                    await asyncio.wait_for(self._active_done.wait(), timeout=self.interrupt_timeout)
                except asyncio.TimeoutError:
                    # The process has been killed; the active reader will observe
                    # EOF shortly, but no longer owns a usable namespace.
                    pass

    async def _stop_process(self, process: asyncio.subprocess.Process) -> None:
        async with self._close_lock:
            if self.process is process:
                self.process = None
                self._started = False
                self._closed = True
            if process.returncode is None:
                try:
                    if sys.platform == "win32":
                        process.terminate()
                    else:
                        os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=self.interrupt_timeout)
                except asyncio.TimeoutError:
                    try:
                        if sys.platform == "win32":
                            process.kill()
                        else:
                            os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
            # The leader may have exited while a child ignored SIGTERM.
            # With a private session its process group can outlive the leader.
            if sys.platform != "win32":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    async def close(self) -> None:
        self._closed = True
        process = self.process
        if process is not None:
            await self._stop_process(process)
