"""Small prompt-toolkit frontend for the coordinator vertical slice.

This frontend intentionally does not depend on the legacy supervisor terminal.
It renders bounded coordinator output and only the sanitized ``text/plain``
fallback from rich MIME events.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
import unicodedata
import weakref
from collections.abc import Mapping
from typing import Any

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.history import InMemoryHistory

from .contracts import InputCancelledError, InputReply, InputRequest, InputUnavailableError
from .coordinator import Coordinator, Submission

_STRING_ESCAPE = re.compile(r"(?:\x1b[\]PX^_]|[\x90\x98\x9d\x9e\x9f]).*?(?:\x07|\x1b\\|\x9c|$)", re.DOTALL)
_CSI = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
_ESCAPE = re.compile(r"\x1b[ -/]*[@-~]")
_TERMINAL_PROMPT_LOCKS = weakref.WeakKeyDictionary()


def _prompt_lock_for(coordinator: object) -> asyncio.Lock:
    try:
        lock = _TERMINAL_PROMPT_LOCKS.get(coordinator)
    except TypeError:
        return asyncio.Lock()
    if lock is None:
        lock = asyncio.Lock()
        _TERMINAL_PROMPT_LOCKS[coordinator] = lock
    return lock


def _thaw_display_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_display_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw_display_value(item) for item in value]
    return value


def sanitize(value: Any) -> str:
    """Make untrusted strings safe to print as terminal text."""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    text = _STRING_ESCAPE.sub("", text)
    text = _CSI.sub("", text)
    text = _ESCAPE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        char for char in text
        if char in "\n\t" or unicodedata.category(char) not in {"Cc", "Cf", "Cs"}
    )


class _NoHistory(InMemoryHistory):
    """Prompt history that never retains values entered for Python stdin."""

    def append_string(self, string: str) -> None:
        del string

    def load_history_strings(self):
        return iter(())


RESERVED_COMMANDS = frozenset({"help", "status", "interrupt", "quit", "exit", "history"})


HELP = """Enter an English request to ask the explicitly selected provider.
@python                execute Python/IPython in the shared local namespace
!shell                 execute an IPython shell escape in that namespace
%magic                 execute an IPython magic
/config                inspect or update typed session configuration
/plugins               list discovered metadata and loaded plugin manifests
/history                list recent journal events (read-only; disabled without --journal)
/history search QUERY   search recent journal events
/history page ID [OFFSET [CHARS]]  read one bounded event page; never replays code
Enabled plugins may add slash commands.
/help                  show this help
/status                show coordinator state and selected provider
/interrupt             interrupt active work
/quit                  exit
Prefix English beginning with @, !, %, or / with a backslash."""


class PlainTerminal:
    """A minimal terminal adapter for :class:`Coordinator`.

    ``input`` and ``output`` are prompt-toolkit streams and can be injected for
    tests. Session lifecycle stays with the CLI; leaving this frontend does not
    itself close the coordinator.
    """

    def __init__(
        self,
        coordinator: Coordinator,
        *,
        input=None,
        output=None,
        frontend_id: str = "terminal",
        vi: bool = False,
        multiline: bool = False,
    ):
        self.coordinator = coordinator
        self.frontend_id = frontend_id
        self.input = input
        self.output = output
        self.vi = vi
        self._prompt_lock = _prompt_lock_for(coordinator)
        self.session = PromptSession(
            input=input,
            output=output,
            history=InMemoryHistory(),
            enable_history_search=True,
            vi_mode=vi,
            multiline=multiline,
        )

    def _write(self, text: Any, *, end: str = "\n") -> None:
        safe = sanitize(text)
        if self.output is None:
            import sys

            sys.stdout.write(safe + end)
            sys.stdout.flush()
        else:
            print_formatted_text(safe, end=end, output=self.output)

    async def _show_submission(self, submission: Submission) -> None:
        if submission.message:
            self._write(submission.message)
        pairs = submission.executions or (
            ((submission.execution, submission.result),) if submission.result is not None else ()
        )
        events = tuple(getattr(submission, "events", ()))
        events_by_execution = {}
        for event in events:
            execution_id = getattr(getattr(event, "origin", None), "execution_id", None)
            if execution_id is not None:
                events_by_execution.setdefault(execution_id, []).append(event)

        def show_ordered_events(ordered_events) -> None:
            pending_stream = None
            pending_text: list[str] = []

            def flush_stream() -> None:
                nonlocal pending_stream, pending_text
                if pending_stream is not None and pending_text:
                    text = "".join(pending_text)
                    self._write(
                        f"{pending_stream}:\n{text}",
                        end="" if text.endswith("\n") else "\n",
                    )
                pending_stream, pending_text = None, []

            for event in ordered_events:
                kind = getattr(event, "kind", None)
                if kind == "stream":
                    data = getattr(event, "data", {})
                    name = data.get("name", "stdout") if hasattr(data, "get") else "stdout"
                    if name not in ("stdout", "stderr"):
                        name = "stdout"
                    text = data.get("text", "") if hasattr(data, "get") else ""
                    if not isinstance(text, str) or not text:
                        continue
                    if pending_stream != name:
                        flush_stream()
                        pending_stream = name
                    pending_text.append(text)
                elif kind in ("display", "execute_result", "update"):
                    flush_stream()
                    metadata = getattr(event, "metadata", {})
                    if getattr(metadata, "get", lambda _key: None)("py_agent_source") == "say":
                        continue  # say() remains rendered through the existing message path.
                    bundle = getattr(event, "data", {})
                    fallback = bundle.get("text/plain") if hasattr(bundle, "get") else None
                    if isinstance(fallback, str):
                        text = fallback
                    elif fallback is not None:
                        text = json.dumps(
                            _thaw_display_value(fallback), ensure_ascii=False,
                            allow_nan=False, separators=(",", ":"),
                        )
                    else:
                        mime_types = sorted(
                            key for key in bundle if isinstance(key, str)
                        ) if hasattr(bundle, "keys") else []
                        text = "[Rich output; no text/plain fallback"
                        if mime_types:
                            text += ": " + ", ".join(mime_types[:8])
                        text += "]"
                    label = "display update" if kind == "update" else "display"
                    self._write(
                        f"{label}:\n{text}", end="" if text.endswith("\n") else "\n",
                    )
                else:
                    flush_stream()
            flush_stream()

        for execution, result in pairs:
            if execution is not None:
                self._write(
                    f"[{execution.author} execution; request {execution.origin.request_id}; "
                    f"execution {execution.origin.execution_id}]"
                )
            ordered_events = events_by_execution.get(
                getattr(getattr(execution, "origin", None), "execution_id", None), (),
            )
            if ordered_events and any(event.kind == "stream" for event in ordered_events):
                show_ordered_events(ordered_events)
            else:
                for name, content in (("stdout", result.stdout), ("stderr", result.stderr)):
                    if content:
                        safe = sanitize(content)
                        self._write(f"{name}:\n{safe}", end="" if safe.endswith("\n") else "\n")
                if ordered_events:
                    show_ordered_events(ordered_events)
            if result.status == "error":
                self._write(f"Execution error: {result.error or 'unknown error'}")
            elif result.status == "cancelled":
                self._write("Execution was interrupted; the session may no longer be usable.")
            elif result.status == "uncertain":
                self._write(
                    "Execution result is uncertain; side effects may have occurred. "
                    + (result.error or "The executor is unavailable.")
                )
        known_execution_ids = {
            getattr(getattr(execution, "origin", None), "execution_id", None)
            for execution, _result in pairs
        }
        unattached_events = [
            event for event in events
            if getattr(getattr(event, "origin", None), "execution_id", None) not in known_execution_ids
        ]
        if unattached_events:
            show_ordered_events(unattached_events)

    async def _request_input(self, request: InputRequest) -> InputReply:
        if not isinstance(request, InputRequest) or request.owner_frontend_id != self.frontend_id:
            raise InputUnavailableError("Interactive input belongs to a different terminal frontend")
        if self._prompt_lock.locked():
            raise InputUnavailableError(
                "Interactive input is unavailable while this terminal is already showing another prompt",
            )
        try:
            async with self._prompt_lock:
                prompt = PromptSession(
                    input=self.input,
                    output=self.output,
                    history=_NoHistory(),
                    enable_history_search=False,
                    vi_mode=self.vi,
                    multiline=False,
                )
                value = await prompt.prompt_async(
                    sanitize(request.prompt), is_password=request.password,
                )
        except EOFError as exc:
            raise InputUnavailableError("Terminal stdin closed before interactive input arrived") from exc
        except KeyboardInterrupt as exc:
            raise InputCancelledError("Interactive input was cancelled") from exc
        return InputReply(
            request.origin, request.sequence, request.owner_frontend_id,
            value=value, password=request.password,
        )

    async def _submit(self, text: str) -> Submission:
        method = self.coordinator.submit
        kwargs: dict[str, object] = {}
        try:
            parameters = inspect.signature(method).parameters.values()
            accepts_extra = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
            )
            names = {parameter.name for parameter in parameters}
        except (TypeError, ValueError):
            accepts_extra, names = False, set()
        if accepts_extra or "allow_stdin" in names:
            kwargs["allow_stdin"] = True
        if accepts_extra or "input_handler" in names:
            kwargs["input_handler"] = self._request_input
        return await method(self.frontend_id, text, **kwargs)

    async def _handle_command(self, text: str) -> bool | None:
        command, _, arguments = text[1:].partition(" ")
        command = command.strip().lower()
        arguments = arguments.strip()
        if command == "help" and not arguments:
            self._write(HELP)
            return True
        if command in {"quit", "exit"} and not arguments:
            return False
        if command == "interrupt" and not arguments:
            await self.coordinator.interrupt()
            self._write("Interrupt requested.")
            return True
        if command == "status" and not arguments:
            self._write(
                f"Coordinator state: {self.coordinator.state.value}; "
                f"provider: {getattr(self.coordinator, 'provider_id', 'unknown')}; "
                f"model: {getattr(self.coordinator, 'model', 'unknown')}; "
                f"configuration revision: {getattr(self.coordinator, 'config_revision', 'unknown')}"
            )
            return True
        if command in {"help", "quit", "exit", "interrupt", "status"}:
            self._write(f"Usage: /{command}")
            return True
        return None

    async def run(self) -> None:
        """Accept actions until /quit, EOF, or a second quick Ctrl-C."""
        last_interrupt = 0.0
        while True:
            try:
                async with self._prompt_lock:
                    text = await self.session.prompt_async("py> ")
            except EOFError:
                return
            except KeyboardInterrupt:
                now = time.monotonic()
                if last_interrupt and now - last_interrupt <= 2.0:
                    return
                last_interrupt = now
                self._write("Input cancelled. Press Ctrl-C again to exit, or use /quit.")
                continue

            last_interrupt = 0.0
            if not text.strip():
                continue
            if text.startswith("/"):
                handled = await self._handle_command(text)
                if handled is False:
                    return
                if handled is True:
                    continue
            try:
                submission = await self._submit(text)
            except KeyboardInterrupt:
                await self.coordinator.interrupt()
                self._write("Interrupt requested; execution will not be replayed.")
                continue
            except Exception as exc:
                self._write(f"Request failed: {exc}")
                continue
            await self._show_submission(submission)
