"""Foreground prompt_toolkit client; execution policy stays in the supervisor.

Rendering is deliberately plain text, with optional Python highlighting. Never
interpret worker text as ANSI/HTML. Originals remain in the host journal.
"""
from __future__ import annotations

import asyncio
from collections import deque
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Any
import unicodedata

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.formatted_text import FormattedText, PygmentsTokens
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.patch_stdout import patch_stdout
from pygments import lex
from pygments.lexers import PythonLexer

COMMANDS = ("/help", "/history", "/usage", "/trace", "/interrupt", "/reset", "/quit")
HELP = """In [n]: accepts agent requests, not direct Python execution. Its number counts
submitted messages. Out[n]: and stdout/stderr labels use worker cell numbers,
which are independent of input numbers. Generated Python is hidden unless /trace
is on; results stay visible. Thinking animates in the status bar.
ctx(last) is last reported input tokens / configured context window, not a live
estimate. Missing counts or window show ?. out(last) is reported output tokens.
Enter submits; Ctrl-R searches input history; Up/Down navigate it.
Bracketed paste stays a draft. With --multiline, Enter inserts a newline and
Esc-Enter submits; /quit + Enter also exits in multiline mode.
Ctrl-C interrupts active work or clears a draft; press it again within two
seconds to exit. Ctrl-C on an empty idle prompt exits immediately. Ctrl-D on an
empty prompt exits, cancelling active work. Earlier effects are not rolled back.
/help                 show this help
/history [ID [OFFSET]] recent evidence, or read a page by event/cell ID
/usage                reported provider counters and estimated context
/trace                toggle generated Python and extra audit events
/interrupt            interrupt active work; never replay automatically
/reset                shorten context at the next request; Python variables stay
                      alive, without a kernel restart
/quit                 cancel active work explicitly and terminate descendants
Input history is separate from the journal; --no-input-history disables only
composer history. Journal content can still contain secrets."""

# OSC (including clipboard/title), DCS/SOS/PM/APC and CSI. Match unterminated
# strings to end-of-input too, rather than exposing their payload as commands.
_STRING_ESCAPE = re.compile(r"(?:\x1b[\]PX^_]|[\x90\x98\x9d\x9e\x9f]).*?(?:\x07|\x1b\\|\x9c|$)", re.S)
_CSI = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
_ESCAPE = re.compile(r"\x1b[ -/]*[@-~]")


def sanitize(value: Any) -> str:
    """Return display-only text without terminal escapes or spoofing controls."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = _STRING_ESCAPE.sub("", text)
    text = _CSI.sub("", text)
    text = _ESCAPE.sub("", text)
    # Normalize CR; it must not let untrusted output overwrite preceding text.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cf", "Cs"})


def private_history(path: Path) -> FileHistory:
    """Create a private composer history; do not follow preexisting symlinks."""
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f"Input history path contains a symlink: {part}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise ValueError(f"Input history directory must be private (chmod 700): {path.parent}")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError("Input history must be an owned regular file with one link")
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    return FileHistory(path)


class _StreamSanitizer:
    """Discard escapes across pipe reads without buffering their payloads."""

    def __init__(self):
        self.state = "text"
        self.cr = False
        self.osc = False

    def feed(self, text: str) -> str:
        result = []
        for char in text:
            if self.state == "string":
                if char == "\x9c" or (char == "\x07" and self.osc):
                    self.state = "text"
                elif char == "\x1b":
                    self.state = "string_escape"
                continue
            if self.state == "string_escape":
                if char in "\\\x9c" or (char == "\x07" and self.osc):
                    self.state = "text"
                elif char != "\x1b":
                    self.state = "string"
                continue
            if self.state == "escape":
                if char in "]PX^_":
                    self.osc = char == "]"
                    self.state = "string"
                elif char == "[":
                    self.state = "csi"
                elif " " <= char <= "/":
                    self.state = "escape_intermediate"
                elif char != "\x1b":
                    self.state = "text"
                continue
            if self.state in {"csi", "escape_intermediate"}:
                if char == "\x1b":
                    self.state = "escape"
                elif "@" <= char <= "~":
                    self.state = "text"
                continue
            previous_cr, self.cr = self.cr, False
            if char == "\x1b":
                self.state = "escape"
            elif char == "\x9b":
                self.state = "csi"
            elif char in "\x90\x98\x9d\x9e\x9f":
                self.osc = char == "\x9d"
                self.state = "string"
            elif char == "\r":
                result.append("\n")
                self.cr = True
            elif char == "\n" and previous_cr:
                pass
            elif char in "\n\t" or unicodedata.category(char) not in {"Cc", "Cf", "Cs"}:
                result.append(char)
        return "".join(result)


class _QuitRequested(Exception):
    """Leave the prompt through normal supervisor cleanup, not process exit."""


class Terminal:
    """UI adapter with injectable prompt input/output for deterministic tests."""

    def __init__(self, supervisor, *, history_path: Path | None = None,
                 vi: bool = False, multiline: bool = False, no_color: bool = False,
                 input=None, output=None):
        self.supervisor = supervisor
        self.trace = False
        self._input_number = 1
        self.no_color = no_color
        self.multiline = multiline
        self._pending: deque[dict] = deque()
        self._wake = asyncio.Event()
        self._dropped = 0
        self._acknowledged: deque[str] = deque(maxlen=256)
        self._renderer: asyncio.Task | None = None
        self._interrupting: asyncio.Task | None = None
        self._closing = False
        self._last_ctrl_c: float | None = None
        self._previous_callback = None
        self._stream_key = None
        self._stream_partial = ""
        self._stream_open_line = False
        # Only parser flags, never raw escape payloads. Keep them across stream
        # switches/cell_end because a late subprocess can resume the same pipe.
        self._stream_sanitizers: dict[tuple, _StreamSanitizer] = {}
        self.session = PromptSession(
            history=private_history(history_path) if history_path is not None else InMemoryHistory(),
            editing_mode=EditingMode.VI if vi else EditingMode.EMACS,
            multiline=multiline,
            key_bindings=self._bindings(),
            completer=WordCompleter(COMMANDS, sentence=True),
            complete_while_typing=False,
            enable_history_search=True,
            enable_system_prompt=False,
            enable_open_in_editor=False,
            enable_suspend=False,
            bottom_toolbar=self._toolbar,
            refresh_interval=0.15,  # redraw thinking animation; no separate task
            color_depth=ColorDepth.DEPTH_1_BIT if no_color else None,
            input=input, output=output,
        )
        # Editing between Ctrl-C presses cancels the exit gesture. Ignore empty
        # resets caused by aborting a prompt, so double Ctrl-C survives resets.
        def edited(buffer):
            if buffer.text:
                self._last_ctrl_c = None
        self.session.default_buffer.on_text_changed += edited
        self.session.search_buffer.on_text_changed += edited

    def _active(self) -> bool:
        return str(self.supervisor.status().get("state", "IDLE")).upper() not in {
            "IDLE", "DONE", "FAILED", "CANCELLED", "INTERRUPTED", "CLOSED", "NEW",
        }

    def _bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add(Keys.BracketedPaste)
        def paste(event):
            # Insert atomically. In particular embedded newlines are not Enter
            # key events, even in the normal single-line submission mode.
            event.current_buffer.insert_text(event.data.replace("\r\n", "\n").replace("\r", "\n"))

        @bindings.add("escape", "enter")
        def submit_multiline(event):
            event.current_buffer.validate_and_handle()

        @bindings.add("enter", filter=Condition(lambda: self.multiline and self.session.default_buffer.text.strip() == "/quit"))
        def quit_multiline(event):
            event.app.exit(exception=_QuitRequested())

        @bindings.add("c-c", eager=True)
        def interrupt(event):
            now = time.monotonic()
            double_press = self._last_ctrl_c is not None and now - self._last_ctrl_c <= 2
            if double_press or (not self._active() and not event.current_buffer.text):
                event.app.exit(exception=_QuitRequested())
                return
            self._last_ctrl_c = now
            self.on_event({"kind": "notice", "content": "Press Ctrl-C again to exit."})
            if self._active():
                self._schedule_interrupt()
            else:
                # Reset the prompt and history loader together, preserving the
                # second-press timestamp across prompt resets.
                event.app.exit(exception=KeyboardInterrupt())

        @bindings.add("c-d", eager=True)
        def eof(event):
            if event.current_buffer.text:
                event.current_buffer.delete()
            else:
                event.app.exit(exception=_QuitRequested())

        return bindings

    def _toolbar(self) -> FormattedText:
        state = self.supervisor.status()
        usage = state.get("usage") or {}
        if isinstance(usage, list):
            usage = usage[-1] if usage else {}
        normalized = usage.get("normalized", usage) if isinstance(usage, dict) else {}
        def reported(name):
            value = normalized.get(name) if isinstance(normalized, dict) else None
            return value if type(value) is int and value >= 0 else "?"

        window = state.get("context_window_tokens")
        context_usage = "?%/?"
        if type(window) is int and window > 0:
            # Exact decimal thousands: never silently round a configured window.
            whole, remainder = divmod(window, 1000)
            window_label = (f"{whole}.{remainder:03d}".rstrip("0").rstrip(".") + "k"
                            if window >= 1000 else str(window))
            input_tokens = state.get("context_input_tokens", reported("input_tokens"))
            percentage = ((input_tokens * 100 + window // 2) // window
                          if type(input_tokens) is int and input_tokens >= 0 else "?")
            context_usage = f"{percentage}%/{window_label}"
        phase = str(state.get("state", "?")).upper()
        if phase == "GENERATING":
            spinner = "|/-\\"[int(time.monotonic() / 0.15) % 4]
            activity = f"{spinner} Thinking (GENERATING)"
        elif phase == "EXECUTING":
            activity = "Executing (EXECUTING)"
        else:
            activity = phase
        text = (
            f"{activity} | {state.get('model', '?')} | "
            f"{state.get('cell_id') or '-'} {state.get('context_epoch', '-')} | "
            f"queued {state.get('queued', 0)} | ctx(last) {context_usage} "
            f"out(last) {reported('output_tokens')}"
            + (" | TRACE" if self.trace else "")
        )
        return FormattedText([("", sanitize(text).replace("\n", " ").replace("\t", " "))])

    def on_event(self, event: dict) -> None:
        """Nonblocking rendering callback; never mutates or journals an event."""
        kind = event.get("kind", "")
        self.session.app.invalidate()
        if kind == "state":
            return  # Coalesce frequent status changes in the toolbar.
        if kind == "output":
            # Parse every fragment BEFORE the bounded display queue can drop it.
            # Otherwise a dropped OSC introducer could expose its later payload.
            origin = (event.get("cell_id"), event.get("stream", "stdout"))
            cleaner = self._stream_sanitizers.setdefault(origin, _StreamSanitizer())
            content = event.get("content", "")
            event = {**event, "content": cleaner.feed(content if isinstance(content, str) else sanitize(content))}
        if kind in {"user_queued", "queued"} and event.get("id"):
            # Journalled receipts have their own event ID; content references
            # the USER event. Deduplicate against submit()'s fallback receipt.
            reference = event.get("content")
            identifier = reference if isinstance(reference, str) and reference else event["id"]
            event = {**event, "user_id": identifier}
            if identifier in self._acknowledged:
                return
            self._acknowledged.append(identifier)
        failed_cell = (kind == "cell_end" and isinstance(event.get("content"), dict)
                       and event["content"].get("status") not in {"success", "wait"})
        if not self.trace and not failed_cell and kind not in {
            "output", "cell_end", "say", "error", "limit", "notice", "retry",
            "user_queued", "queued", "cancelled", "interrupted", "failed",
        }:
            return
        if len(self._pending) >= 256:
            self._pending.popleft()
            self._dropped += 1
        self._pending.append(dict(event))
        self._wake.set()

    def _emit(self, content, *, python: bool = False) -> None:
        self._finish_stream()
        safe = sanitize(content)
        if len(safe) > 12000:
            safe = safe[:12000] + "\n[terminal excerpt; original available in /history]"
        formatted = PygmentsTokens(lex(safe, PythonLexer())) if python and not self.no_color else safe
        print_formatted_text(formatted, output=self.session.output,
                             color_depth=ColorDepth.DEPTH_1_BIT if self.no_color else None)

    def _write_stream(self, text: str) -> None:
        """Already sanitized text: preserve its newlines, including their absence."""
        if text:
            print_formatted_text(text, end="", output=self.session.output,
                                 color_depth=ColorDepth.DEPTH_1_BIT if self.no_color else None)
            self._stream_open_line = not text.endswith("\n")

    def _finish_stream(self) -> None:
        if self._stream_key is None:
            return
        self._write_stream(self._stream_partial)
        # This newline separates UI blocks, not arbitrary transport fragments.
        if self._stream_open_line:
            self._write_stream("\n")
        self._stream_partial = ""
        self._stream_key = None

    def _render_output(self, event: dict, content) -> None:
        stream = event.get("stream", "stdout")
        origin = (event.get("cell_id"), stream)
        safe = sanitize(content)  # streaming escape state was handled by on_event()
        if not safe:
            return
        late = " (late)" if event.get("asynchronous") else ""
        label = "Out" if stream == "display" else sanitize(stream)
        gap = "" if stream == "display" else " "
        heading = f"{label}{gap}[{self._cell_number(event)}]{late}:"
        if stream == "display":
            # Display events have no line/continuation marker. Keep their existing
            # value boundaries, unlike byte-stream stdout/stderr fragments.
            self._emit(heading)
            self._emit(safe)
            return
        key = (*origin, bool(event.get("asynchronous")))
        if key != self._stream_key:
            self._emit(heading)
            self._stream_key = key
        # 2048 Unicode characters occupy at most 8 KiB of UTF-8. No timer task.
        while safe:
            room = 2048 - len(self._stream_partial)
            piece, safe = safe[:room], safe[room:]
            self._stream_partial += piece
            newline = self._stream_partial.rfind("\n")
            if newline >= 0:
                self._write_stream(self._stream_partial[:newline + 1])
                self._stream_partial = self._stream_partial[newline + 1:]
            if len(self._stream_partial) == 2048:
                self._write_stream(self._stream_partial)
                self._stream_partial = ""

    @staticmethod
    def _cell_number(event: dict) -> str:
        identifier = event.get("cell_id")
        match = re.fullmatch(r"(?:[^:]+:)?c([0-9]+)", identifier) if isinstance(identifier, str) else None
        return (match[1].lstrip("0") or "0") if match else "?"

    def _render_event(self, event: dict) -> None:
        kind, content = event.get("kind", "event"), event.get("content", "")
        identifier = event.get("cell_id") or event.get("id") or ""
        if kind in {"user_queued", "queued"}:
            self._emit(f"Queued {event.get('user_id', identifier)}")
        elif kind == "say":
            self._emit(content)
        elif kind in {"source", "cell", "cell_source"}:
            if self.trace:
                self._emit(f"Python [{self._cell_number(event)}]:")
                self._emit(content, python=True)
        elif kind == "output":
            self._render_output(event, content)
        elif kind == "cell_end":
            self._finish_stream()
            if isinstance(content, dict) and content.get("status") not in {"success", "wait"}:
                self._emit(f"Cell [{self._cell_number(event)}] {sanitize(content.get('status', 'error'))}: {sanitize(content.get('error', ''))}")
            elif self.trace:
                self._emit(f"[cell_end {identifier}] {sanitize(content)}")
        else:
            self._emit(f"[{kind}{' ' + str(identifier) if identifier else ''}] {sanitize(content)}")

    async def _render_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            if self._dropped:
                self._emit(f"[terminal skipped {self._dropped} display events; originals remain in /history]")
                self._dropped = 0
            while self._pending:
                self._render_event(self._pending.popleft())
            # print_formatted_text schedules coordinated redraws in the current
            # application's context; this task is started from prompt pre_run.
            await asyncio.sleep(0)

    def _pre_run(self) -> None:
        if self._renderer is None:
            self._renderer = asyncio.create_task(self._render_loop())

    def _schedule_interrupt(self) -> None:
        if self._interrupting is None or self._interrupting.done():
            self._interrupting = asyncio.create_task(self._interrupt())

    async def _interrupt(self) -> None:
        self.on_event({"kind": "notice", "content": "Interrupt requested; earlier or uncertain side effects remain. No replay."})
        try:
            await self.supervisor.interrupt()
        except Exception as exc:
            self.on_event({"kind": "error", "content": str(exc)})

    async def handle_line(self, text: str) -> bool:
        """Return False to exit. Commands never become worker source."""
        self._last_ctrl_c = None
        if not text.strip():
            return True
        if text.strip() == "/quit":
            if self._active():
                self._emit("Cancelling active work and terminating its descendants; partial effects remain.")
            return False
        if not text.startswith("/"):
            event_id = self.supervisor.submit(text)
            self._input_number += 1
            # Supervisors own queued receipts. Avoid adding a second journal
            # entry, but ensure every accepted submission is visibly acknowledged.
            self.on_event({"kind": "user_queued", "id": event_id, "content": ""})
            return True
        parts = text.split()
        command, args = parts[0], parts[1:]
        if command not in COMMANDS:
            self.on_event({"kind": "error", "content": "Unknown local command. Use /help."})
        elif command == "/help" and not args:
            self.on_event({"kind": "notice", "content": HELP})
        elif command == "/history":
            if len(args) > 2:
                raise ValueError("Usage: /history [ID [OFFSET]]")
            if args:
                offset = int(args[1]) if len(args) == 2 else 0
                result = self.supervisor.journal.read(args[0], offset=offset, limit=8000)
            else:
                result = self.supervisor.journal.recent(10)
            self.on_event({"kind": "notice", "content": result})
        elif command == "/usage" and not args:
            status = self.supervisor.status()
            self.on_event({"kind": "notice", "content": {
                "estimated_input_tokens": status.get("estimated_input_tokens", "unknown"),
                "provider_usage": status.get("usage") or "unknown (not reported)",
            }})
        elif command == "/trace" and not args:
            self.trace = not self.trace
            self.on_event({"kind": "notice", "content": f"Python/audit trace {'on' if self.trace else 'off'}; results stay visible. Use /history for originals."})
        elif command == "/interrupt" and not args:
            self._schedule_interrupt()
        elif command == "/reset" and not args:
            self.supervisor.request_reset()
            self.on_event({"kind": "notice", "content": "Context reset requested for the next request. Python variables stay alive; no kernel restart."})
        else:
            raise ValueError(f"{command} takes no arguments; use /help")
        return True

    async def run(self) -> None:
        self._previous_callback = self.supervisor.on_event
        self.supervisor.on_event = self.on_event
        try:
            with patch_stdout(raw=False):
                while True:
                    try:
                        line = await self.session.prompt_async(f"In [{self._input_number}]: ", pre_run=self._pre_run)
                        if not await self.handle_line(line):
                            break
                    except _QuitRequested:
                        if self._active():
                            self._emit("Exiting: cancelling active work and terminating descendants. Partial effects remain.")
                        break
                    except KeyboardInterrupt:
                        if self._active():
                            self._schedule_interrupt()
                    except EOFError:
                        # A closed input stream must not spin or orphan work.
                        if self._active():
                            self._emit("Terminal input closed; cancelling active work and terminating descendants. Partial effects remain.")
                        break
                    except Exception as exc:
                        self.on_event({"kind": "error", "content": str(exc)})
        finally:
            self._closing = True
            try:
                # Do not wait for an interrupt before closing: an SDK may ignore
                # cancellation. The supervisor closes the sandbox independently.
                if self._interrupting is not None:
                    self._interrupting.cancel()
                await self.supervisor.close()
                if self._interrupting is not None:
                    done, pending = await asyncio.wait({self._interrupting}, timeout=1)
                    for task in done:
                        if not task.cancelled():
                            task.exception()
                    if pending:
                        self._emit("Interrupt cleanup did not finish; supervisor shutdown has been requested.")
            finally:
                if self._renderer is not None:
                    self._renderer.cancel()
                    await asyncio.gather(self._renderer, return_exceptions=True)
                # A final synchronous flush runs after the prompt has stopped.
                while self._pending:
                    self._render_event(self._pending.popleft())
                self._finish_stream()
                self.supervisor.on_event = self._previous_callback


async def run(supervisor, *, history_path: Path | None = None, vi: bool = False,
              multiline: bool = False, no_color: bool = False) -> None:
    await Terminal(supervisor, history_path=history_path, vi=vi,
                   multiline=multiline, no_color=no_color).run()
