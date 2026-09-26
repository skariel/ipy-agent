"""Small prompt-toolkit frontend for the coordinator vertical slice.

This frontend renders coordinator events without owning execution policy.
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
from dataclasses import dataclass
from typing import Any

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.filters import has_focus, is_searching
from prompt_toolkit.formatted_text import FormattedText, to_formatted_text
from prompt_toolkit.formatted_text.utils import fragment_list_width, split_lines
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from .contracts import (
    InputCancelledError, InputReply, InputRequest, InputUnavailableError,
    OutputEvent, ProgressCallback,
)
from .coordinator import Coordinator, State, Submission
from .terminal_markdown import markdown_fragments

_STRING_ESCAPE = re.compile(r"(?:\x1b[\]PX^_]|[\x90\x98\x9d\x9e\x9f]).*?(?:\x07|\x1b\\|\x9c|$)", re.DOTALL)
_CSI = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
_ESCAPE = re.compile(r"\x1b[ -/]*[@-~]")
_TERMINAL_PROMPT_LOCKS = weakref.WeakKeyDictionary()
_PROMPT_INTERRUPTED = object()
_SHIFT_ENTER = (Keys.ShiftEscape, Keys.ControlM)
ANSI_SEQUENCES["\x1b[13;2u"] = _SHIFT_ENTER
ANSI_SEQUENCES["\x1b[27;2;13~"] = _SHIFT_ENTER
ANSI_SEQUENCES["\x1b[99;5u"] = (Keys.ControlC,)
ANSI_SEQUENCES["\x1b[27;5;99~"] = (Keys.ControlC,)
ANSI_SEQUENCES["\x1b[127;3u"] = (Keys.Escape, Keys.Backspace)
ANSI_SEQUENCES["\x1b[27;3;127~"] = (Keys.Escape, Keys.Backspace)
_EXTENDED_KEYS_ON = "\x1b[>4;2m\x1b[>1u"
_EXTENDED_KEYS_OFF = "\x1b[<u\x1b[>4;0m"
_COLOR_STYLE = {
    "user-prompt": "ansigreen bold", "continuation-prompt": "ansigreen bold",
    "say": "bg:#343541", "stdout": "bg:#283228", "stderr": "bg:#3c2828",
    "stream-prompt": "ansiblue bold", "output-prompt": "ansired bold",
    "output-note": "ansiyellow italic", "md-heading": "ansicyan bold",
    "md-bold": "bold", "md-italic": "italic", "md-code": "ansiyellow",
    "md-quote": "ansigreen italic",
}
_ANSI_BG = {"say": "48;2;52;53;65", "stdout": "48;2;40;50;40", "stderr": "48;2;60;40;40"}
_ANSI_TEXT = {
    "md-heading": "36;1", "md-bold": "1", "md-italic": "3",
    "md-code": "33", "md-quote": "32;3", "output-note": "33;3",
    "stream-prompt": "34;1", "output-prompt": "31;1",
}


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


class _TerminalHistory(InMemoryHistory):
    """Keep composer history but never retain a Python stdin reply."""

    def __init__(self, should_record):
        super().__init__()
        self._should_record = should_record

    def append_string(self, string: str) -> None:
        if self._should_record():
            super().append_string(string)

    def load_history_strings(self):
        return iter(self.get_strings())


@dataclass(frozen=True)
class _PromptResult:
    text: str
    stdin_request: InputRequest | None
    mode_generation: int


RESERVED_COMMANDS = frozenset({"help", "status", "interrupt", "quit", "exit", "history"})


HELP = """Enter an English request to ask the explicitly selected provider.
While work runs, the composer remains available: English queues as steering when
possible; direct cells and slash commands run later in FIFO order.
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
        no_color: bool = False,
    ):
        self.coordinator = coordinator
        self.frontend_id = frontend_id
        self.input = input
        self.output = output
        self.vi = vi
        self.multiline = multiline
        self.no_color = no_color or output is not None
        self._style = Style.from_dict({} if self.no_color else _COLOR_STYLE)
        self._input_number = 1
        self._cell_number = 0
        self._execution_numbers: dict[str, int] = {}
        self._stream_name: str | None = None
        self._stream_fragments: list[str] = []
        self._prompt_lock = _prompt_lock_for(coordinator)
        self._prompt_active = False
        self._stdin_request: InputRequest | None = None
        self._stdin_future: asyncio.Future[str] | None = None
        self._stdin_request_ended = asyncio.Event()
        self._stdin_request_ended.set()
        self._prompt_mode_changed = asyncio.Event()
        self._prompt_mode_generation = 0
        self._composer_draft = ""
        self._history_suppressed = False
        self._active_submission_task: asyncio.Task | None = None
        self._queued_watchers: set[asyncio.Task] = set()
        self._terminal_closed = False
        self.session = PromptSession(
            input=input,
            output=output,
            history=_TerminalHistory(lambda: not self._history_suppressed),
            enable_history_search=True,
            vi_mode=vi,
            multiline=multiline,
            key_bindings=self._bindings(),
            bottom_toolbar=self._toolbar,
            refresh_interval=0.15,
            style=self._style,
            color_depth=ColorDepth.DEPTH_1_BIT if self.no_color else ColorDepth.TRUE_COLOR,
        )

    def _bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("s-escape", "enter", eager=True)
        @bindings.add("escape", "enter", eager=True)
        def submit_multiline(event):
            event.current_buffer.validate_and_handle()

        @bindings.add("enter", filter=has_focus("DEFAULT_BUFFER") & ~is_searching, eager=True)
        def smart_enter(event):
            buffer = event.current_buffer
            if self._stdin_request is not None:
                buffer.validate_and_handle()
                return
            if self.multiline:
                if buffer.text.strip() in ("/quit", "/exit"):
                    buffer.validate_and_handle()
                else:
                    buffer.insert_text("\n")
                return
            before = buffer.document.current_line_before_cursor
            stripped = before.rstrip()
            if stripped.endswith(("\\", ":")):
                indent = len(before) - len(before.lstrip(" "))
                if stripped.endswith(":"):
                    indent += 4
                buffer.insert_text("\n" + " " * indent)
            else:
                buffer.validate_and_handle()

        return bindings

    def _toolbar(self) -> FormattedText:
        state = getattr(self.coordinator, "state", State.IDLE)
        phase = getattr(state, "value", str(state)).upper()
        if phase == "GENERATING":
            symbol = "|/-\\"[int(time.monotonic() / 0.15) % 4]
            activity = f"{symbol} Thinking (GENERATING)"
        elif phase in ("EXECUTING", "WAITING-FOR-INPUT"):
            activity = "Executing (EXECUTING)" if phase == "EXECUTING" else "Waiting for input"
        else:
            activity = phase
        service = getattr(self.coordinator, "context_service", None)
        context = getattr(service, "context", None)
        window = getattr(context, "window_tokens", None)
        tokens = getattr(service, "reported_input_tokens", None)
        if type(window) is int and window >= 1000:
            whole, remainder = divmod(window, 1000)
            capacity = f"{whole}.{remainder:03d}".rstrip("0").rstrip(".") + "k"
        else:
            capacity = str(window) if type(window) is int and window > 0 else "?"
        percentage = (
            str((tokens * 100 + window // 2) // window)
            if type(tokens) is int and tokens >= 0 and type(window) is int and window > 0 else "?"
        )
        model = sanitize(getattr(self.coordinator, "model", "?"))
        cache = getattr(self.coordinator, "cache_summary", ("?", "?", "?"))
        rate, read, write = cache if isinstance(cache, tuple) and len(cache) == 3 else ("?", "?", "?")
        text = f"{activity} | {model} | {percentage}%/{capacity} | CH {rate}% r{read} w{write}"
        return FormattedText([("", text.replace("\n", " ").replace("\t", " "))])

    def _panel_width(self) -> int:
        try:
            return max(1, self.session.output.get_size().columns)
        except (AttributeError, OSError):
            return 80

    def _panel_line(self, fragments: FormattedText, role: str) -> None:
        parts = list(to_formatted_text(fragments))
        if self.no_color:
            self._write("".join(text for _, text, *_ in parts))
            return
        width = self._panel_width()
        visible = fragment_list_width(parts)
        padding = width if visible == 0 else (-visible) % width
        background = _ANSI_BG[role]
        chunks = [f"\x1b[{background}m"]
        current = ""
        for style, text, *_ in parts:
            style_name = next((name for name in _ANSI_TEXT if f"class:{name}" in style.split()), "")
            code = _ANSI_TEXT.get(style_name, "")
            if code != current:
                chunks.append(f"\x1b[0m\x1b[{background}m")
                if code:
                    chunks.append(f"\x1b[{code}m")
                current = code
            chunks.append(text)
        if current:
            chunks.append(f"\x1b[0m\x1b[{background}m")
        chunks.extend((" " * padding, "\x1b[0m\n"))
        self._write_raw("".join(chunks))

    def _write_raw(self, value: str) -> None:
        if self.output is None:
            import sys

            sys.stdout.write(value)
            sys.stdout.flush()
        else:
            print_formatted_text(value, end="", output=self.output)

    def _panel_blank(self, role: str) -> None:
        if not self.no_color:
            self._panel_line(FormattedText([]), role)

    def _panel(self, content: FormattedText, role: str) -> None:
        for line in split_lines(content):
            self._panel_line(FormattedText(line), role)

    def _finish_stream(self) -> None:
        if self._stream_name is None:
            return
        role = self._stream_name
        text = "".join(self._stream_fragments)
        lines = text.splitlines()
        self._panel(FormattedText([("", "\n".join(lines[:5]))]), role)
        if len(lines) > 5:
            self._panel_line(FormattedText([("class:output-note", f"… showing 5 of {len(lines)} lines")]), role)
        self._panel_blank(role)
        self._write("")
        self._stream_name = None
        self._stream_fragments.clear()

    def _render_stream(self, name: str, text: str) -> None:
        if not text:
            return
        role = "stderr" if name == "stderr" else "stdout"
        if self._stream_name != role:
            self._finish_stream()
            self._panel_blank(role)
            self._panel_line(FormattedText([
                ("class:stream-prompt", f"{role} [{self._cell_number or '?'}]:")
            ]), role)
            self._stream_name = role
        self._stream_fragments.append(sanitize(text))

    def _render_say(self, text: str) -> None:
        self._finish_stream()
        self._panel_blank("say")
        self._panel(markdown_fragments(sanitize(text)), "say")
        self._panel_blank("say")
        self._write("")

    def _render_display(self, label: str, text: str) -> None:
        self._finish_stream()
        role = "stdout"
        lines = sanitize(text).splitlines()
        self._panel_blank(role)
        self._panel_line(FormattedText([
            ("class:output-prompt", f"{label} [{self._cell_number or '?'}]:")
        ]), role)
        self._panel(FormattedText([("", "\n".join(lines[:5]))]), role)
        if len(lines) > 5:
            self._panel_line(FormattedText([("class:output-note", f"… showing 5 of {len(lines)} lines")]), role)
        self._panel_blank(role)
        self._write("")

    def _write(self, text: Any, *, end: str = "\n") -> None:
        safe = sanitize(text)
        if self.output is None:
            import sys

            sys.stdout.write(safe + end)
            sys.stdout.flush()
        else:
            print_formatted_text(safe, end=end, output=self.output)

    @staticmethod
    def _event_key(event: object) -> tuple[str | None, int | None]:
        origin = getattr(event, "origin", None)
        return (
            getattr(origin, "request_id", None),
            getattr(event, "sequence", None),
        )

    def _show_live_event(self, event: object) -> None:
        kind = getattr(event, "kind", None)
        data = getattr(event, "data", {})
        metadata = getattr(event, "metadata", {})
        execution_id = getattr(getattr(event, "origin", None), "execution_id", None)
        if execution_id in self._execution_numbers:
            self._cell_number = self._execution_numbers[execution_id]
        if kind == "progress":
            phase = data.get("phase") if hasattr(data, "get") else None
            # The toolbar carries the ordinary generating/executing state.
            if phase in ("generation_start", "execution_start"):
                if phase == "execution_start" and execution_id is not None:
                    if execution_id not in self._execution_numbers:
                        self._cell_number += 1
                        self._execution_numbers[execution_id] = self._cell_number
                        if len(self._execution_numbers) > 128:
                            self._execution_numbers.pop(next(iter(self._execution_numbers)))
                self.session.app.invalidate()
                return
            if phase == "cell_complete":
                self._finish_stream()
                status = data.get("status")
                if status not in ("success", "error"):
                    self._write(f"Cell {sanitize(status)}.")
                self.session.app.invalidate()
                return
            text = data.get("text") if hasattr(data, "get") else None
            if isinstance(text, str) and text:
                self._write(text)
        elif kind == "stream":
            name = data.get("name", "stdout") if hasattr(data, "get") else "stdout"
            if name not in ("stdout", "stderr"):
                name = "stdout"
            text = data.get("text", "") if hasattr(data, "get") else ""
            # A preview cannot be replaced on an ordinary scrolling terminal.
            # Render only the completed, authoritative stream once.
            if (isinstance(text, str) and text
                    and not getattr(metadata, "get", lambda _key: None)("provisional")):
                self._render_stream(name, text)
        elif kind in ("display", "execute_result", "update"):
            bundle = data if hasattr(data, "get") else {}
            fallback = bundle.get("text/plain")
            if isinstance(fallback, str):
                text = fallback
            elif fallback is not None:
                text = json.dumps(
                    _thaw_display_value(fallback), ensure_ascii=False,
                    allow_nan=False, separators=(",", ":"),
                )
            else:
                mime_types = sorted(key for key in bundle if isinstance(key, str))
                text = "[Rich output; no text/plain fallback"
                if mime_types:
                    text += ": " + ", ".join(mime_types[:8])
                text += "]"
            if getattr(metadata, "get", lambda _key: None)("py_agent_source") == "say":
                self._render_say(text)
            else:
                label = "display update" if kind == "update" else "Out"
                self._render_display(label, text)
        elif kind == "error":
            self._finish_stream()
            message = data.get("evalue", "Execution failed") if hasattr(data, "get") else "Execution failed"
            self._write(f"Cell error: {message}")

    @staticmethod
    def _message_not_already_delivered(
        message: str,
        delivered_events: tuple[object, ...],
        status_events: tuple[object, ...] = (),
    ) -> str:
        say_texts = []
        step_limit_texts = []
        for event in delivered_events:
            data = getattr(event, "data", {})
            metadata = getattr(event, "metadata", {})
            if (getattr(event, "kind", None) in ("display", "execute_result", "update")
                    and getattr(metadata, "get", lambda _key: None)("py_agent_source") == "say"):
                value = data.get("text/plain") if hasattr(data, "get") else None
                if isinstance(value, str):
                    say_texts.append(value)
            if (getattr(event, "kind", None) == "progress"
                    and hasattr(data, "get") and data.get("phase") == "step_limit"):
                value = data.get("text")
                if isinstance(value, str):
                    step_limit_texts.append(value)
        for event in status_events:
            data = getattr(event, "data", {})
            if (getattr(event, "kind", None) == "progress"
                    and hasattr(data, "get") and data.get("phase") == "step_limit"):
                value = data.get("text")
                if isinstance(value, str):
                    step_limit_texts.append(value)
        if say_texts:
            prefix = "\n".join(say_texts)
            if message == prefix:
                message = ""
            elif message.startswith(prefix + "\n"):
                message = message[len(prefix) + 1:]
        for text in step_limit_texts:
            if message == text:
                message = ""
            elif message.endswith("\n" + text):
                message = message[:-(len(text) + 1)]
        return message

    async def _show_submission(
        self, submission: Submission, *, delivered_events: tuple[object, ...] = (),
    ) -> None:
        delivered_keys = {self._event_key(event) for event in delivered_events}
        pairs = submission.executions or (
            ((submission.execution, submission.result),) if submission.result is not None else ()
        )
        events = tuple(getattr(submission, "events", ()))
        returned_keys = {self._event_key(event) for event in events}
        all_events = events + tuple(
            event for event in delivered_events if self._event_key(event) not in returned_keys
        )
        message = self._message_not_already_delivered(
            submission.message, all_events, status_events=events,
        )
        events = tuple(event for event in events if self._event_key(event) not in delivered_keys)
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
                    self._render_stream(pending_stream, "".join(pending_text))
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
                        value = event.data.get("text/plain")
                        if isinstance(value, str):
                            self._render_say(value)
                        continue
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
                    label = "display update" if kind == "update" else "Out"
                    self._render_display(label, text)
                elif kind == "progress":
                    flush_stream()
                    self._show_live_event(event)
                else:
                    flush_stream()
            flush_stream()

        for execution, result in pairs:
            execution_id = getattr(getattr(execution, "origin", None), "execution_id", None)
            had_live_execution_start = any(
                self._event_key(event) in delivered_keys
                and getattr(getattr(event, "origin", None), "execution_id", None) == execution_id
                and event.kind == "progress" and event.data.get("phase") == "execution_start"
                for event in delivered_events
            )
            if execution_id in self._execution_numbers:
                self._cell_number = self._execution_numbers[execution_id]
            if execution is not None and not had_live_execution_start:
                if execution_id not in self._execution_numbers:
                    self._cell_number += 1
                    if execution_id is not None:
                        self._execution_numbers[execution_id] = self._cell_number
                        if len(self._execution_numbers) > 128:
                            self._execution_numbers.pop(next(iter(self._execution_numbers)))
                else:
                    self._cell_number = self._execution_numbers[execution_id]
                self.session.app.invalidate()
            ordered_events = events_by_execution.get(execution_id, ())
            live_output = any(
                self._event_key(event) in delivered_keys
                and getattr(event, "kind", None) == "stream"
                and not getattr(getattr(event, "metadata", {}), "get", lambda _key: None)("provisional")
                for event in all_events
                if getattr(getattr(event, "origin", None), "execution_id", None) == execution_id
            )
            if ordered_events and any(event.kind == "stream" for event in ordered_events):
                show_ordered_events(ordered_events)
            else:
                if not live_output:
                    for name, content in (("stdout", result.stdout), ("stderr", result.stderr)):
                        if content:
                            self._render_stream(name, content)
                if ordered_events:
                    show_ordered_events(ordered_events)
            self._finish_stream()
            if result.status == "error":
                live_error = any(
                    self._event_key(event) in delivered_keys and event.kind == "error"
                    for event in all_events
                    if getattr(getattr(event, "origin", None), "execution_id", None) == execution_id
                )
                if not live_error:
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
            for event in unattached_events:
                self._show_live_event(event)
        self._finish_stream()
        if message:
            self._write(message)

    def _prompt_message(self) -> FormattedText | str:
        request = self._stdin_request
        if request is not None:
            prompt = sanitize(request.prompt)
            return f"Python {'password ' if request.password else ''}input: {prompt} "
        return FormattedText([("class:user-prompt", f"In [{self._input_number}]: ")])

    def _partial_prompt_text(self) -> str:
        try:
            buffer = self.session.app.current_buffer
            return buffer.text if isinstance(buffer.text, str) else ""
        except (AttributeError, RuntimeError):
            return ""

    async def _read_prompt(self) -> _PromptResult:
        """Read from the sole PromptSession, tagging replies with their prompt mode."""
        await self._prompt_lock.acquire()
        self._prompt_active = True
        try:
            while True:
                request = self._stdin_request
                generation = self._prompt_mode_generation
                self._prompt_mode_changed.clear()
                # No await occurs between taking the mode snapshot and starting
                # the prompt, so a request cannot be lost while clearing the event.
                if (request is not self._stdin_request
                        or generation != self._prompt_mode_generation):
                    continue
                self._history_suppressed = request is not None

                async def read_prompt_safely():
                    try:
                        return await self.session.prompt_async(
                            lambda: self._prompt_message(),
                            prompt_continuation=lambda width, _line, _wrap: FormattedText([
                                ("class:continuation-prompt", " " * max(0, width - 5) + "...: ")
                            ]),
                            is_password=request.password if request is not None else False,
                            default=self._composer_draft if request is None else "",
                        )
                    except KeyboardInterrupt:
                        # A KeyboardInterrupt escaping a child Task aborts
                        # run_until_complete before its exception can be read.
                        # Hand it back to the parent prompt loop as a value.
                        return _PROMPT_INTERRUPTED

                prompt_task = asyncio.create_task(read_prompt_safely())
                mode_task = asyncio.create_task(self._prompt_mode_changed.wait())
                try:
                    done, _ = await asyncio.wait(
                        (prompt_task, mode_task), return_when=asyncio.FIRST_COMPLETED,
                    )
                    changed = (
                        request is not self._stdin_request
                        or generation != self._prompt_mode_generation
                    )
                    if changed:
                        if prompt_task.done():
                            # A completed composer remains a composer action even
                            # if stdin starts in the same event-loop turn. Never
                            # reinterpret it as the correlated stdin reply.
                            if request is None:
                                try:
                                    text = await prompt_task
                                except (EOFError, asyncio.CancelledError):
                                    raise
                                if text is _PROMPT_INTERRUPTED:
                                    raise KeyboardInterrupt
                                self._composer_draft = ""
                                return _PromptResult(text, None, generation)
                            # A completed reply for a request that has already
                            # ended is stale and must not be delivered elsewhere.
                            if await prompt_task is _PROMPT_INTERRUPTED:
                                raise KeyboardInterrupt
                        elif request is None:
                            # Keep an unfinished composer draft out of the stdin
                            # prompt's default buffer and restore it afterwards.
                            self._composer_draft = self._partial_prompt_text()
                            prompt_task.cancel()
                            await asyncio.gather(prompt_task, return_exceptions=True)
                        else:
                            prompt_task.cancel()
                            await asyncio.gather(prompt_task, return_exceptions=True)
                        continue
                    if prompt_task in done:
                        text = await prompt_task
                        if text is _PROMPT_INTERRUPTED:
                            raise KeyboardInterrupt
                        if request is None:
                            self._composer_draft = ""
                        return _PromptResult(text, request, generation)
                    # A generation change can outlive the event's set/clear; restart
                    # the prompt so every stdin start/end gets a clean mode boundary.
                    if request is None:
                        self._composer_draft = self._partial_prompt_text()
                    prompt_task.cancel()
                    await asyncio.gather(prompt_task, return_exceptions=True)
                finally:
                    mode_task.cancel()
                    if not prompt_task.done():
                        prompt_task.cancel()
                    # Drain even already-completed tasks: a concurrent mode
                    # change can otherwise leave an unobserved prompt error.
                    await asyncio.gather(mode_task, prompt_task, return_exceptions=True)
        finally:
            # Release before clearing the flag: a concurrent stdin request can
            # then distinguish this terminal's prompt from another frontend's.
            self._prompt_lock.release()
            self._prompt_active = False

    async def _request_input(self, request: InputRequest) -> InputReply:
        if getattr(self, "_terminal_closed", False):
            raise InputUnavailableError("The owning terminal frontend has detached")
        if not isinstance(request, InputRequest) or request.owner_frontend_id != self.frontend_id:
            raise InputUnavailableError("Interactive input belongs to a different terminal frontend")
        if self._prompt_lock.locked() and not getattr(self, "_prompt_active", False):
            raise InputUnavailableError(
                "Interactive input is unavailable while another terminal frontend is already showing another prompt",
            )
        if self._stdin_future is not None:
            raise InputUnavailableError("A Python stdin prompt is already pending in this terminal")
        future = asyncio.get_running_loop().create_future()
        self._stdin_request = request
        self._stdin_future = future
        self._stdin_request_ended.clear()
        self._history_suppressed = True
        self._prompt_mode_generation += 1
        self._prompt_mode_changed.set()
        try:
            app = getattr(self.session, "app", None)
            invalidate = getattr(app, "invalidate", None)
            if callable(invalidate):
                invalidate()
            try:
                value = await future
            except EOFError as exc:
                raise InputUnavailableError("Terminal stdin closed before interactive input arrived") from exc
            except KeyboardInterrupt as exc:
                raise InputCancelledError("Interactive input was cancelled") from exc
        finally:
            if self._stdin_future is future:
                self._stdin_future = None
                self._stdin_request = None
                self._prompt_mode_generation += 1
                self._prompt_mode_changed.set()
                self._stdin_request_ended.set()
                app = getattr(self.session, "app", None)
                invalidate = getattr(app, "invalidate", None)
                if callable(invalidate):
                    invalidate()
        return InputReply(
            request.origin, request.sequence, request.owner_frontend_id,
            value=value, password=request.password,
        )

    async def _submit(
        self, text: str, *, on_progress: ProgressCallback | None = None,
    ) -> Submission:
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
        if on_progress is not None and (accepts_extra or "on_progress" in names):
            kwargs["on_progress"] = on_progress
        return await method(self.frontend_id, text, **kwargs)

    async def _progress_callback(self, event: OutputEvent, delivered: list[OutputEvent]) -> None:
        # Output is emitted synchronously on the same event loop as the sole
        # PromptSession; do not start a second prompt or block waiting for a
        # prompt-toolkit application context from a background request task.
        delivered.append(event)
        self._show_live_event(event)

    async def _enqueue(self, text: str, on_progress: ProgressCallback):
        method = getattr(self.coordinator, "enqueue", None)
        if not callable(method):
            return None
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
        if accepts_extra or "on_progress" in names:
            kwargs["on_progress"] = on_progress
        return await method(self.frontend_id, text, **kwargs)

    async def _watch_queue_ticket(self, ticket, delivered: list[OutputEvent]) -> None:
        try:
            outcome = await ticket.completion
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._write(f"Queued request {ticket.origin.request_id} failed: {exc}")
            return
        if outcome.status == "steered":
            self._write("Steering added to the active turn.")
        elif outcome.status == "completed" and isinstance(outcome.submission, Submission):
            await self._show_submission(
                outcome.submission, delivered_events=tuple(delivered),
            )
        else:
            detail = outcome.error or outcome.status
            self._write(f"Queued request {outcome.origin.request_id} was not dispatched: {detail}")

    def _queue_work_is_active(self) -> bool:
        state = getattr(self.coordinator, "state", State.IDLE)
        active = self._active_submission_task is not None and not self._active_submission_task.done()
        watchers = any(not task.done() for task in self._queued_watchers)
        pending = getattr(self.coordinator, "pending_action_count", 0)
        queue_active = getattr(self.coordinator, "queue_active", False)
        return active or watchers or bool(pending) or queue_active or state is not State.IDLE

    def _start_submission(self, text: str) -> asyncio.Task:
        delivered: list[OutputEvent] = []

        async def on_progress(event: OutputEvent) -> None:
            await self._progress_callback(event, delivered)

        async def run_submission() -> None:
            try:
                submission = await self._submit(text, on_progress=on_progress)
            except asyncio.CancelledError:
                self._write("Request was cancelled; it will not be replayed.")
            except Exception as exc:
                self._write(f"Request failed: {exc}")
            else:
                await self._show_submission(submission, delivered_events=tuple(delivered))
            finally:
                if self._active_submission_task is asyncio.current_task():
                    self._active_submission_task = None

        task = asyncio.create_task(run_submission(), name="py-terminal-submission")
        self._active_submission_task = task
        return task

    async def _handle_command(self, text: str) -> bool | None:
        command, _, arguments = text[1:].partition(" ")
        command = command.strip().lower()
        arguments = arguments.strip()
        if command == "help" and not arguments:
            self._write(HELP)
            return True
        if command in {"quit", "exit"} and not arguments:
            if self._queue_work_is_active():
                self._write("Stopping: unfinished work and queued actions will not be dispatched.")
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
        """Render background output above the live composer, preserving its draft."""
        if self.output is None:
            # Prompt-toolkit redirects stdout/stderr through run_in_terminal,
            # redraws the active prompt, and preserves its editable buffer.
            # Install after the event loop exists and restore it on detach.
            with patch_stdout(raw=not self.no_color):
                self.session.output.write_raw(_EXTENDED_KEYS_ON)
                self.session.output.flush()
                try:
                    await self._run_loop()
                finally:
                    self._finish_stream()
                    self.session.output.write_raw(_EXTENDED_KEYS_OFF)
                    self.session.output.flush()
        else:
            try:
                await self._run_loop()
            finally:
                self._finish_stream()

    async def _run_loop(self) -> None:
        """Keep one composer available while coordinator work runs in the background."""
        last_interrupt = 0.0
        while True:
            try:
                prompt_result = await self._read_prompt()
                text = prompt_result.text
            except EOFError:
                future = self._stdin_future
                if future is not None and not future.done():
                    future.set_exception(InputUnavailableError(
                        "Terminal stdin closed before interactive input arrived",
                    ))
                if self._queue_work_is_active():
                    self._write("Input closed: unfinished work and queued actions will not be dispatched.")
                self._terminal_closed = True
                return
            except KeyboardInterrupt:
                future = self._stdin_future
                if future is not None and not future.done():
                    future.set_exception(InputCancelledError("Interactive input was cancelled"))
                    await self._stdin_request_ended.wait()
                    await self.coordinator.interrupt()
                    self._write("Python input cancelled; interrupt requested.")
                    continue
                now = time.monotonic()
                if last_interrupt and now - last_interrupt <= 2.0:
                    self._terminal_closed = True
                    return
                last_interrupt = now
                self._write("Input cancelled. Use /interrupt to stop work, or /quit to exit.")
                continue

            if prompt_result.stdin_request is not None:
                # Only text returned by the prompt for this exact request/mode
                # may answer stdin. A composer prompt can finish concurrently
                # with a stdin request and must never become its reply.
                future = self._stdin_future
                if (prompt_result.mode_generation == self._prompt_mode_generation
                        and self._stdin_request is prompt_result.stdin_request
                        and future is not None and not future.done()):
                    future.set_result(text)
                    await self._stdin_request_ended.wait()
                last_interrupt = 0.0
                continue

            if self._stdin_future is not None:
                # A completed composer line that raced a stdin request remains
                # an editable composer draft; do not dispatch it into stdin or
                # enqueue work while the owning execution is awaiting its reply.
                self._composer_draft = text
                continue

            last_interrupt = 0.0
            if not text.strip():
                continue
            self._write("")  # Separate submitted input from the next output panel.
            if text.startswith("/"):
                handled = await self._handle_command(text)
                if handled is False:
                    self._terminal_closed = True
                    future = self._stdin_future
                    if future is not None and not future.done():
                        future.set_exception(InputUnavailableError(
                            "Terminal stdin closed before interactive input arrived",
                        ))
                    return
                if handled is True:
                    continue

            enqueue = getattr(self.coordinator, "enqueue", None)
            if callable(enqueue) and self._queue_work_is_active():
                delivered: list[OutputEvent] = []

                async def on_queued_progress(event: OutputEvent) -> None:
                    await self._progress_callback(event, delivered)

                try:
                    ticket = await self._enqueue(text, on_queued_progress)
                    if ticket is None:
                        raise RuntimeError("Coordinator queue service is unavailable")
                except Exception as exc:
                    self._write(f"Request failed: {exc}")
                    continue
                if ticket.kind == "ask":
                    acknowledgement = f"Queued steering (position {ticket.position})."
                else:
                    acknowledgement = f"Queued action (position {ticket.position})."
                self._write(acknowledgement)
                self._input_number += 1
                self.session.app.invalidate()
                watcher = asyncio.create_task(
                    self._watch_queue_ticket(ticket, delivered),
                    name=f"py-terminal-queued-{ticket.origin.request_id}",
                )
                self._queued_watchers.add(watcher)
                watcher.add_done_callback(self._queued_watchers.discard)
                continue

            active = self._active_submission_task
            if (active is not None and not active.done()
                    and not callable(getattr(self.coordinator, "enqueue", None))):
                # Compatibility path for simple embedders predating the queue API.
                try:
                    await active
                except asyncio.CancelledError:
                    pass
            self._start_submission(text)
            self._input_number += 1
            self.session.app.invalidate()
