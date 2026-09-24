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
from prompt_toolkit.filters import Condition, has_focus, is_searching
from prompt_toolkit.formatted_text import (
    FormattedText,
    PygmentsTokens,
    fragment_list_width,
    split_lines,
    to_formatted_text,
)
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from pygments import lex
from pygments.lexers import PythonLexer

# prompt_toolkit 3.0.52 otherwise discards xterm's Enter modifier and does
# not recognize CSI-u Shift+Enter. Preserve it as a distinct two-key gesture.
_SHIFT_ENTER = (Keys.ShiftEscape, Keys.ControlM)
ANSI_SEQUENCES["\x1b[13;2u"] = _SHIFT_ENTER
ANSI_SEQUENCES["\x1b[27;2;13~"] = _SHIFT_ENTER
# Kitty's keyboard protocol reports modified control keys as CSI-u.  Without
# these entries Ctrl-C and Alt-Backspace are inserted as visible escape junk.
ANSI_SEQUENCES["\x1b[99;5u"] = (Keys.ControlC,)
ANSI_SEQUENCES["\x1b[27;5;99~"] = (Keys.ControlC,)
ANSI_SEQUENCES["\x1b[127;3u"] = (Keys.Escape, Keys.Backspace)
ANSI_SEQUENCES["\x1b[27;3;127~"] = (Keys.Escape, Keys.Backspace)

# Ask capable terminals to report key modifiers instead of collapsing
# Shift-Enter to an indistinguishable carriage return. xterm's
# modifyOtherKeys protocol and Kitty's keyboard protocol are complementary;
# unsupported control sequences are safely ignored.
_EXTENDED_KEYS_ON = "\x1b[>4;2m\x1b[>1u"
_EXTENDED_KEYS_OFF = "\x1b[<u\x1b[>4;0m"

_COLOR_STYLE = {
    # User input stays on the terminal's normal background. Agent messages and
    # process streams use subtle, semantic panels: blue, green, and red.
    "user-prompt": "ansigreen bold",
    "say": "bg:#343541",
    # Match pi's dark tool-result panels. These are forced through true-color
    # rendering below so a 256-color approximation cannot turn green into gray.
    "stdout": "bg:#283228",
    "stderr": "bg:#3c2828",
    "continuation-prompt": "ansigreen bold",
    "output-prompt": "ansired bold",
    "stream-prompt": "ansiblue bold",
    "output-note": "ansiyellow italic",
    "md-heading": "ansicyan bold",
    "md-bold": "bold",
    "md-italic": "italic",
    "md-code": "ansiyellow",
    "md-quote": "ansigreen italic",
}

_PERMISSION_CHOICES = (
    ("deny", "Deny"),
    ("once", "Once"),
    ("session", "Session"),
    ("project", "This project"),
    ("global", "All projects"),
)

COMMANDS = (
    "/help",
    "/history",
    "/usage",
    "/trace",
    "/approve",
    "/deny",
    "/permissions",
    "/revoke",
    "/interrupt",
    "/reset",
    "/quit",
)
HELP = """In [n]: accepts agent requests, not direct Python execution. Its number counts
submitted messages and direct cells. Prefix a command with ! to execute it
directly in the sandbox (for example, !ls). Prefix Python with @ to execute it in
the same live IPython namespace as the agent; IPython % and %% magics work inside
that cell. Out[n]: and stdout/stderr labels use
worker cell numbers, which are independent of input numbers. Generated Python is hidden unless /trace
is on; results stay visible. Thinking animates in the status bar.
The status bar shows last reported input tokens / configured context window.
Missing values show ?. CH is the session's weighted cumulative cache-hit percentage.
Enter submits a complete line; after a trailing : or \\ it starts an indented
continuation line. Shift-Enter always submits; Alt-Backspace deletes a word.
Ctrl-R searches input history; Up/Down navigate it. Bracketed paste stays a
draft. With --multiline, Enter always inserts a newline and Shift-Enter submits
(Esc-Enter is a terminal-compatible fallback). /quit + Enter also exits in
multiline mode.
Ctrl-C interrupts active work or clears the draft; press it again within two
seconds to exit. On an empty idle prompt Ctrl-C exits. Ctrl-D on an
empty prompt exits, cancelling active work. Earlier effects are not rolled back.
/help                 show this help
/history [ID [OFFSET]] recent evidence, or read a page by event/cell ID
/usage                reported provider counters and estimated context
/trace                toggle generated Python and extra audit events
Permission requests open an arrow-key selection menu; Enter selects and Esc denies.
/approve ID SCOPE     fallback approval command: once, session, project, all
/deny ID              deny a pending permission request
/permissions          list pending requests and saved project/all-project grants
/revoke ID             revoke a saved project/all-project permission
/interrupt            interrupt active work; never replay automatically
/reset                shorten context at the next request; Python variables stay
                      alive, without a kernel restart
/quit                 cancel active work explicitly and terminate descendants
Input history is separate from the journal; --no-input-history disables only
composer history. Journal content can still contain secrets."""

# OSC (including clipboard/title), DCS/SOS/PM/APC and CSI. Match unterminated
# strings to end-of-input too, rather than exposing their payload as commands.
_STRING_ESCAPE = re.compile(r"(?:\x1b[\]PX^_]|[\x90\x98\x9d\x9e\x9f]).*?(?:\x07|\x1b\\|\x9c|$)", re.DOTALL)
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


def _inline_markdown(text: str, default_style: str = "") -> list[tuple[str, str]]:
    """Render a deliberately small, safe subset of inline Markdown."""
    fragments: list[tuple[str, str]] = []
    pattern = re.compile(r"(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|_[^_\n]+_|\[[^]\n]+\]\([^)\n]+\))")
    position = 0
    for match in pattern.finditer(text):
        if match.start() > position:
            fragments.append((default_style, text[position : match.start()]))
        token = match.group()
        if token.startswith("`"):
            fragments.append(("class:md-code", token[1:-1]))
        elif token.startswith(("**", "__")):
            fragments.append(("class:md-bold", token[2:-2]))
        elif token.startswith(("*", "_")):
            fragments.append(("class:md-italic", token[1:-1]))
        else:
            label, target = token[1:].split("](", 1)
            fragments.extend([(default_style, label), ("class:md-code", f" ({target[:-1]})")])
        position = match.end()
    fragments.append((default_style, text[position:]))
    return fragments


def _markdown_table_row(line: str) -> list[str] | None:
    """Split a simple GFM table row, honoring escaped pipes and inline code."""
    stripped = line.strip()
    if "|" not in stripped:
        return None
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith(r"\|"):
        stripped = stripped[:-1]
    cells, current, escaped, code = [], [], False, False
    for char in stripped:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
            current.append(char)
        elif char == "`":
            code = not code
            current.append(char)
        elif char == "|" and not code:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    cells.append("".join(current).strip())
    return cells if len(cells) > 1 else None


def _markdown_table_separator(line: str, columns: int) -> bool:
    cells = _markdown_table_row(line)
    return bool(
        cells and len(cells) == columns and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)
    )


def _table_fragments(rows: list[list[str]]) -> list[tuple[str, str]]:
    rendered = [[_inline_markdown(cell) for cell in row] for row in rows]
    widths = [max(sum(len(text) for _, text in row[column]) for row in rendered) for column in range(len(rows[0]))]
    fragments: list[tuple[str, str]] = []
    for row_number, row in enumerate(rows):
        if row_number:
            fragments.append(("", "\n"))
        for column, cell in enumerate(row):
            if column:
                fragments.append(("", "  "))
            style = "class:md-heading" if row_number == 0 else ""
            cell_fragments = _inline_markdown(cell, style)
            fragments.extend(cell_fragments)
            visible = sum(len(text) for _, text in cell_fragments)
            fragments.append((style, " " * (widths[column] - visible)))
    return fragments


def markdown_fragments(value: Any) -> FormattedText:
    """Convert common Markdown presentation to sanitized prompt_toolkit text."""
    safe = sanitize(value).strip("\n")
    if not isinstance(value, str):
        return FormattedText([("", safe)])
    fragments: list[tuple[str, str]] = []
    fenced = False
    lines = safe.split("\n")
    line_number = 0
    while line_number < len(lines):
        line = lines[line_number]
        if fragments:
            fragments.append(("", "\n"))
        if re.match(r"^\s*```", line):
            fenced = not fenced
            line_number += 1
            continue
        if fenced:
            fragments.append(("class:md-code", line))
            line_number += 1
            continue
        header = _markdown_table_row(line)
        if header and line_number + 1 < len(lines) and _markdown_table_separator(lines[line_number + 1], len(header)):
            rows = [header]
            line_number += 2
            while line_number < len(lines):
                row = _markdown_table_row(lines[line_number])
                if row is None or len(row) != len(header):
                    break
                rows.append(row)
                line_number += 1
            fragments.extend(_table_fragments(rows))
            continue
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.*)$", line)
        quote = re.match(r"^\s{0,3}>\s?(.*)$", line)
        bullet = re.match(r"^(\s*)[-+*]\s+(.*)$", line)
        if heading:
            fragments.extend(_inline_markdown(heading.group(1), "class:md-heading"))
        elif quote:
            fragments.append(("class:md-quote", "│ "))
            fragments.extend(_inline_markdown(quote.group(1), "class:md-quote"))
        elif bullet:
            fragments.append(("", f"{bullet.group(1)}• "))
            fragments.extend(_inline_markdown(bullet.group(2)))
        else:
            fragments.extend(_inline_markdown(line))
        line_number += 1
    return FormattedText(fragments)


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

    def __init__(
        self,
        supervisor,
        *,
        history_path: Path | None = None,
        vi: bool = False,
        multiline: bool = False,
        no_color: bool = False,
        input=None,
        output=None,
    ):
        self.supervisor = supervisor
        self.trace = False
        self._input_number = 1
        self.no_color = no_color
        self.multiline = multiline
        self._style = Style.from_dict({} if no_color else _COLOR_STYLE)
        self._pending: deque[dict] = deque()
        self._wake = asyncio.Event()
        self._acknowledged: set[str] = set()
        self._renderer: asyncio.Task | None = None
        self._interrupting: asyncio.Task | None = None
        self._closing = False
        self._last_ctrl_c: float | None = None
        self._previous_callback = None
        self._stream_key = None
        self._stream_partial = ""
        self._stream_open_line = False
        self._stream_column = 0
        self._stream_line_has_text = False
        self._stream_lines: dict[tuple, int] = {}
        self._stream_total_lines: dict[tuple, int] = {}
        self._stream_has_partial: dict[tuple, bool] = {}
        self._stream_truncation_reported: set[tuple] = set()
        self._permission_requests: deque[dict] = deque()
        self._permission_choice = 0
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
            style=self._style,
            input=input,
            output=output,
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
            "IDLE",
            "DONE",
            "FAILED",
            "CANCELLED",
            "INTERRUPTED",
            "CLOSED",
            "NEW",
        }

    def _bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add(Keys.BracketedPaste)
        def paste(event):
            # Insert atomically. In particular embedded newlines are not Enter
            # key events, even in the normal single-line submission mode.
            event.current_buffer.insert_text(event.data.replace("\r\n", "\n").replace("\r", "\n"))

        permission_pending = Condition(lambda: bool(self._permission_requests))

        @bindings.add("left", filter=permission_pending, eager=True)
        @bindings.add("up", filter=permission_pending, eager=True)
        def previous_permission_choice(event):
            self._permission_choice = (self._permission_choice - 1) % len(_PERMISSION_CHOICES)
            event.app.invalidate()

        @bindings.add("right", filter=permission_pending, eager=True)
        @bindings.add("down", filter=permission_pending, eager=True)
        def next_permission_choice(event):
            self._permission_choice = (self._permission_choice + 1) % len(_PERMISSION_CHOICES)
            event.app.invalidate()

        @bindings.add("enter", filter=permission_pending, eager=True)
        def confirm_permission_choice(event):
            self._resolve_permission_choice()
            event.app.invalidate()

        @bindings.add("escape", filter=permission_pending, eager=True)
        def deny_permission_choice(event):
            self._permission_choice = 0
            self._resolve_permission_choice()
            event.app.invalidate()

        @bindings.add("s-escape", "enter", eager=True)
        def submit_shift_enter(event):
            event.current_buffer.validate_and_handle()

        @bindings.add("escape", "enter", eager=True)
        def submit_escape_enter(event):
            # Legacy terminals encode Shift-Enter as Escape followed by Enter.
            event.current_buffer.validate_and_handle()

        @bindings.add("escape", "backspace", eager=True)
        @bindings.add("escape", "delete", eager=True)
        def delete_previous_word(event):
            buffer = event.current_buffer
            offset = buffer.document.find_start_of_previous_word()
            if offset is None:
                offset = -buffer.cursor_position
            if offset:
                buffer.delete_before_cursor(count=-offset)

        @bindings.add("enter", filter=~permission_pending & has_focus("DEFAULT_BUFFER") & ~is_searching, eager=True)
        def smart_enter(event):
            buffer = event.current_buffer
            if self.multiline:
                if buffer.text.strip() == "/quit":
                    event.app.exit(exception=_QuitRequested())
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

        @bindings.add("c-c", eager=True)
        def interrupt(event):
            now = time.monotonic()
            double_press = self._last_ctrl_c is not None and now - self._last_ctrl_c <= 2
            if double_press or (not self._active() and not event.current_buffer.text):
                event.app.exit(exception=_QuitRequested())
                return
            self._last_ctrl_c = now
            if event.current_buffer.text:
                event.current_buffer.reset()
            self.on_event({"kind": "notice", "content": "Interrupted. Press Ctrl-C again within 2 seconds to exit."})
            if self._active():
                self._schedule_interrupt()

        @bindings.add("c-d", eager=True)
        def eof(event):
            if event.current_buffer.text:
                event.current_buffer.delete()
            else:
                event.app.exit(exception=_QuitRequested())

        return bindings

    def _toolbar(self) -> FormattedText:
        if self._permission_requests:
            request = self._permission_requests[0]
            request_id = sanitize(request.get("request_id", "?"))
            resource = sanitize(request.get("resource", {})).replace("\n", " ")[:120]
            choices = " | ".join(
                f"[{label}]" if index == self._permission_choice else label
                for index, (_, label) in enumerate(_PERMISSION_CHOICES)
            )
            return FormattedText([("", f"Permission {request_id}: {resource}  ◀ {choices} ▶  Enter=select Esc=deny")])
        state = self.supervisor.status()
        usage = state.get("usage") or {}
        reports = usage if isinstance(usage, list) else [usage]
        last_usage = usage[-1] if isinstance(usage, list) and usage else usage
        normalized = last_usage.get("normalized", last_usage) if isinstance(last_usage, dict) else {}

        def reported(name):
            value = normalized.get(name) if isinstance(normalized, dict) else None
            return value if type(value) is int and value >= 0 else "?"

        cache_totals = {"input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
        cache_complete = {name: bool(reports) for name in cache_totals}
        for report in reports:
            counters = report.get("normalized", report) if isinstance(report, dict) else {}
            if not isinstance(counters, dict):
                cache_complete = {name: False for name in cache_complete}
                continue
            for name in ("input_tokens", "cache_read_tokens"):
                value = counters.get(name)
                if type(value) is int and value >= 0:
                    cache_totals[name] += value
                else:
                    cache_complete[name] = False
            # Providers use both names for the same cache-write counter.
            value = counters.get("cache_write_tokens")
            if type(value) is not int or value < 0:
                value = counters.get("cache_creation_tokens")
            if type(value) is int and value >= 0:
                cache_totals["cache_write_tokens"] += value
            else:
                cache_complete["cache_write_tokens"] = False
        cache_read = cache_totals["cache_read_tokens"] if cache_complete["cache_read_tokens"] else "?"
        cache_write = cache_totals["cache_write_tokens"] if cache_complete["cache_write_tokens"] else "?"
        session_rate = "?"
        if (
            cache_complete["input_tokens"]
            and cache_complete["cache_read_tokens"]
            and cache_totals["input_tokens"] > 0
        ):
            session_rate = str(
                (cache_totals["cache_read_tokens"] * 100 + cache_totals["input_tokens"] // 2)
                // cache_totals["input_tokens"]
            )
        cache_usage = f"CH {session_rate}% r{cache_read} w{cache_write}"

        window = state.get("context_window_tokens")
        context_usage = "?%/?"
        if type(window) is int and window > 0:
            # Exact decimal thousands: never silently round a configured window.
            whole, remainder = divmod(window, 1000)
            window_label = f"{whole}.{remainder:03d}".rstrip("0").rstrip(".") + "k" if window >= 1000 else str(window)
            input_tokens = state.get("context_input_tokens", reported("input_tokens"))
            percentage = (
                (input_tokens * 100 + window // 2) // window if type(input_tokens) is int and input_tokens >= 0 else "?"
            )
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
            f"{context_usage} | {cache_usage}" + (" | TRACE" if self.trace else "")
        )
        return FormattedText([("", sanitize(text).replace("\n", " ").replace("\t", " "))])

    def on_event(self, event: dict) -> None:
        """Nonblocking rendering callback; never mutates or journals an event."""
        kind = event.get("kind", "")
        if kind == "permission_request" and isinstance(event.get("content"), dict):
            request = event["content"]
            request_id = request.get("request_id")
            if isinstance(request_id, str) and not any(
                pending.get("request_id") == request_id for pending in self._permission_requests
            ):
                self._permission_requests.append(dict(request))
                if len(self._permission_requests) == 1:
                    self._permission_choice = 0
        elif kind == "permission_decision" and isinstance(event.get("content"), dict):
            request_id = event["content"].get("request_id")
            was_active = (
                bool(self._permission_requests) and self._permission_requests[0].get("request_id") == request_id
            )
            self._permission_requests = deque(
                request for request in self._permission_requests if request.get("request_id") != request_id
            )
            if was_active:
                self._permission_choice = 0
        self.session.app.invalidate()
        if kind == "state":
            return  # Coalesce frequent status changes in the toolbar.
        if kind == "output":
            # Parse every fragment before queuing; escape sequences can span
            # fragments, stream changes and delayed rendering.
            origin = (event.get("cell_id"), event.get("stream", "stdout"))
            cleaner = self._stream_sanitizers.setdefault(origin, _StreamSanitizer())
            content = event.get("content", "")
            event = {**event, "content": cleaner.feed(content if isinstance(content, str) else sanitize(content))}
        if kind in {"user_queued", "queued"}:
            if event.get("id"):
                # Journalled receipts have their own event ID; content references
                # the USER event. Keep deduplication without displaying receipts.
                reference = event.get("content")
                identifier = reference if isinstance(reference, str) and reference else event["id"]
                if identifier in self._acknowledged:
                    return
                self._acknowledged.add(identifier)
            return
        failed_cell = (
            kind == "cell_end"
            and isinstance(event.get("content"), dict)
            and event["content"].get("status") not in {"success", "wait"}
        )
        if (
            not self.trace
            and not failed_cell
            and kind
            not in {
                "output",
                "cell_end",
                "say",
                "error",
                "limit",
                "notice",
                "retry",
                "user_queued",
                "queued",
                "cancelled",
                "interrupted",
                "failed",
                "permission_request",
                "permission_decision",
                "permission_operation",
            }
        ):
            return
        self._pending.append(dict(event))
        self._wake.set()

    def _panel_width(self) -> int:
        try:
            return max(1, self.session.output.get_size().columns)
        except (AttributeError, OSError):
            return 80

    def _panel_line(self, fragments, role: str) -> None:
        parts = list(to_formatted_text(fragments))
        if self.no_color:
            formatted = FormattedText(parts)
        else:
            styled = []
            for style, text, *handler in parts:
                styled.append(((f"class:{role} " + style).rstrip(), text, *handler))
            panel_width = self._panel_width()
            content_width = fragment_list_width(styled)
            padding = panel_width if content_width == 0 else (-content_width) % panel_width
            styled.append((f"class:{role}", " " * padding))
            formatted = FormattedText(styled)
        print_formatted_text(
            formatted,
            output=self.session.output,
            style=self._style,
            color_depth=ColorDepth.DEPTH_1_BIT if self.no_color else ColorDepth.TRUE_COLOR,
        )

    def _panel_blank(self, role: str) -> None:
        if not self.no_color:
            self._panel_line(FormattedText([]), role)

    def _emit(self, content, *, python: bool = False, role: str = "") -> None:
        if self._finish_stream():
            self._blank_line()
        safe = sanitize(content)
        formatted = PygmentsTokens(lex(safe, PythonLexer())) if python and not self.no_color else safe
        if role:
            for line in split_lines(to_formatted_text(formatted)):
                self._panel_line(FormattedText(line), role)
        else:
            print_formatted_text(
                formatted,
                output=self.session.output,
                style=self._style,
                color_depth=ColorDepth.DEPTH_1_BIT if self.no_color else None,
            )

    def _emit_markdown(self, content: Any, *, role: str = "") -> None:
        if self._finish_stream():
            self._blank_line()
        fragments = markdown_fragments(content)
        if role:
            for line in split_lines(fragments):
                self._panel_line(FormattedText(line), role)
        else:
            print_formatted_text(
                FormattedText(fragments),
                output=self.session.output,
                style=self._style,
                color_depth=ColorDepth.DEPTH_1_BIT if self.no_color else None,
            )

    def _emit_heading(self, heading: str, *, display: bool, role: str) -> None:
        if self._finish_stream():
            self._blank_line()
        self._panel_blank(role)
        prompt = "output-prompt" if display else "stream-prompt"
        self._panel_line(FormattedText([(f"class:{prompt}", heading)]), role)

    def _emit_output_note(self, text: str, *, role: str = "stdout", padded: bool = False) -> None:
        if padded:
            self._panel_blank(role)
        self._panel_line(FormattedText([("class:output-note", text)]), role)
        if padded:
            self._panel_blank(role)

    def _write_stream(self, text: str) -> None:
        """Render sanitized stream fragments while completing each colored row."""
        if not text:
            return
        role = "stderr" if self._stream_key and self._stream_key[1] == "stderr" else "stdout"
        width = self._panel_width()
        parts = text.split("\n")
        for index, part in enumerate(parts):
            if part:
                print_formatted_text(
                    FormattedText([(f"class:{role}", part)]),
                    end="",
                    output=self.session.output,
                    style=self._style,
                    color_depth=ColorDepth.DEPTH_1_BIT if self.no_color else ColorDepth.TRUE_COLOR,
                )
                self._stream_line_has_text = True
                self._stream_column = (self._stream_column + fragment_list_width([("", part)])) % width
            if index < len(parts) - 1:
                padding = 0
                if not self.no_color and (self._stream_column or not self._stream_line_has_text):
                    padding = width - self._stream_column
                print_formatted_text(
                    FormattedText([(f"class:{role}", " " * padding)]),
                    output=self.session.output,
                    style=self._style,
                    color_depth=ColorDepth.DEPTH_1_BIT if self.no_color else ColorDepth.TRUE_COLOR,
                )
                self._stream_column = 0
                self._stream_line_has_text = False
        self._stream_open_line = not text.endswith("\n")

    def _blank_line(self) -> None:
        print_formatted_text(
            "", output=self.session.output, color_depth=ColorDepth.DEPTH_1_BIT if self.no_color else None
        )

    def _finish_stream(self, *, pad: bool = True) -> bool:
        if self._stream_key is None:
            return False
        role = "stderr" if self._stream_key[1] == "stderr" else "stdout"
        self._write_stream(self._stream_partial)
        # This newline separates UI blocks, not arbitrary transport fragments.
        if self._stream_open_line:
            self._write_stream("\n")
        if self._stream_key is not None and self._stream_lines.get(self._stream_key, 0) < 5:
            self._stream_lines[self._stream_key] = self._stream_lines.get(self._stream_key, 0) + 1
        if pad:
            self._panel_blank(role)
        self._stream_partial = ""
        self._stream_key = None
        self._stream_column = 0
        self._stream_line_has_text = False
        return True

    def _render_output(self, event: dict, content) -> None:
        stream = event.get("stream", "stdout")
        origin = (event.get("cell_id"), stream, bool(event.get("asynchronous")))
        safe = sanitize(content)  # streaming escape state was handled by on_event()
        if not safe:
            return
        late = " (late)" if event.get("asynchronous") else ""
        label = "Out" if stream in {"display", "markdown"} else sanitize(stream)
        gap = "" if stream in {"display", "markdown"} else " "
        heading = f"{label}{gap}[{self._cell_number(event)}]{late}:"
        role = "stderr" if stream == "stderr" else "stdout"
        if stream in {"display", "markdown"}:
            # Display values are atomic rather than pipe fragments, but apply the
            # same concise-output policy to their textual representation.
            all_lines = safe.splitlines()
            excerpt = "\n".join(all_lines[:5])
            if excerpt:
                self._emit_heading(heading, display=True, role=role)
                if stream == "markdown":
                    self._emit_markdown(excerpt, role=role)
                else:
                    self._emit(excerpt, role=role)
                if len(all_lines) > 5:
                    self._emit_output_note(f"… showing 5 of {len(all_lines)} lines", role=role)
                self._panel_blank(role)
                self._blank_line()
            return

        key = origin
        self._stream_total_lines[key] = self._stream_total_lines.get(key, 0) + safe.count("\n")
        if safe:
            self._stream_has_partial[key] = not safe.endswith("\n")
        if self._stream_lines.get(key, 0) >= 5:
            return
        if key != self._stream_key:
            if self._stream_key is not None:
                self._finish_stream()
                self._blank_line()
            self._emit_heading(heading, display=False, role=role)
            self._stream_key = key
            self._stream_column = 0
            self._stream_line_has_text = False

        # Preserve transport fragmentation while exposing at most five logical
        # lines from each cell/stream. The cap is applied after sanitization.
        while safe and self._stream_lines.get(key, 0) < 5:
            room = 2048 - len(self._stream_partial)
            piece, safe = safe[:room], safe[room:]
            self._stream_partial += piece
            while "\n" in self._stream_partial and self._stream_lines.get(key, 0) < 5:
                line, self._stream_partial = self._stream_partial.split("\n", 1)
                self._write_stream(line + "\n")
                self._stream_lines[key] = self._stream_lines.get(key, 0) + 1
            if self._stream_lines.get(key, 0) >= 5:
                self._stream_partial = ""
                return
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
        visible = False
        if kind in {"user_queued", "queued"}:
            return
        if kind == "say":
            if self._finish_stream():
                self._blank_line()
            self._panel_blank("say")
            self._emit_markdown(content, role="say")
            self._panel_blank("say")
            visible = True
        elif kind == "permission_request" and isinstance(content, dict):
            request_id = sanitize(content.get("request_id", "?"))
            permission_kind = sanitize(content.get("permission_kind", "permission"))
            resource = sanitize(content.get("resource", {}))
            reason = sanitize(content.get("reason", ""))
            message = (
                f"Permission requested [{request_id}] ({permission_kind}): {resource}"
                + (f"\nReason: {reason}" if reason else "")
                + "\nChoose in the menu with arrow keys and Enter; Esc denies. Slash commands remain available."
            )
            self._emit(message)
            visible = True
        elif kind in {"source", "cell", "cell_source"}:
            if self.trace:
                self._emit(f"Python [{self._cell_number(event)}]:")
                self._emit(content, python=True)
                visible = True
        elif kind == "output":
            self._render_output(event, content)
            return
        elif kind == "cell_end":
            active_key = self._stream_key
            active_role = (
                "stderr" if active_key is not None and active_key[1] == "stderr" else "stdout"
            )
            finished = self._finish_stream(pad=False)
            visible = finished
            cell = event.get("cell_id")
            for key in list(self._stream_total_lines):
                if key[0] != cell or key in self._stream_truncation_reported:
                    continue
                total = self._stream_total_lines[key] + int(self._stream_has_partial.get(key, False))
                if total > 5:
                    self._emit_output_note(
                        f"… showing 5 of {total} lines",
                        role="stderr" if key[1] == "stderr" else "stdout",
                        padded=key != active_key,
                    )
                    self._stream_truncation_reported.add(key)
                    visible = True
            if finished:
                self._panel_blank(active_role)
            if isinstance(content, dict) and content.get("status") not in {"success", "wait"}:
                self._emit(
                    f"Cell [{self._cell_number(event)}] {sanitize(content.get('status', 'error'))}: "
                    f"{sanitize(content.get('error', ''))}"
                )
                visible = True
            elif self.trace:
                self._emit(f"[cell_end {identifier}] {sanitize(content)}")
                visible = True
        else:
            self._emit(f"[{kind}{' ' + str(identifier) if identifier else ''}] {sanitize(content)}")
            visible = True
        if visible:
            self._blank_line()

    def _resolve_permission_choice(self) -> None:
        if not self._permission_requests:
            return
        request = self._permission_requests[0]
        request_id = request.get("request_id")
        if not isinstance(request_id, str):
            self._permission_requests.popleft()
            self._permission_choice = 0
            return
        scope, _ = _PERMISSION_CHOICES[self._permission_choice]
        try:
            self.supervisor.resolve_permission(
                request_id,
                allow=scope != "deny",
                scope="once" if scope == "deny" else scope,
            )
        except Exception as exc:
            self.on_event({"kind": "error", "content": str(exc)})
        finally:
            if self._permission_requests and self._permission_requests[0].get("request_id") == request_id:
                self._permission_requests.popleft()
            self._permission_choice = 0

    async def _render_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
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
        self.on_event({
            "kind": "notice",
            "content": "Interrupt requested; earlier or uncertain side effects remain. No replay.",
        })
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
        if text.startswith("@"):
            source = text[1:]
            if source.startswith(" "):
                source = source[1:]
            if not source.strip():
                raise ValueError("Direct Python input must follow @")
            await self.supervisor.execute_python(source)
            self._input_number += 1
            return True
        if text.startswith("!"):
            command = text[1:].strip()
            if not command:
                raise ValueError("Shell input must be a nonempty !command")
            if command == "clear":
                # This is trusted UI control, unlike worker output (whose ANSI is stripped).
                self.session.output.write_raw("\x1b[2J\x1b[H")
                self.session.output.flush()
                self._input_number += 1
                return True
            if command == "ls":
                text = "!ls -CF --color=never"
            elif command == "ll":
                text = "!ls -alF --color=never"
            elif command.startswith("ll "):
                text = "!ls -alF --color=never " + command[3:]
            await self.supervisor.execute_shell(text)
            self._input_number += 1
            return True
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
            self.on_event({
                "kind": "notice",
                "content": {
                    "estimated_input_tokens": status.get("estimated_input_tokens", "unknown"),
                    "provider_usage": status.get("usage") or "unknown (not reported)",
                },
            })
        elif command == "/trace" and not args:
            self.trace = not self.trace
            self.on_event({
                "kind": "notice",
                "content": f"Python/audit trace {'on' if self.trace else 'off'}; results stay visible. Use /history for originals.",
            })
        elif command == "/approve":
            if len(args) != 2 or args[1] not in {"once", "session", "project", "all"}:
                raise ValueError("Usage: /approve ID once|session|project|all")
            scope = "global" if args[1] == "all" else args[1]
            self.supervisor.resolve_permission(args[0], allow=True, scope=scope)
        elif command == "/deny":
            if len(args) != 1:
                raise ValueError("Usage: /deny ID")
            self.supervisor.resolve_permission(args[0], allow=False)
        elif command == "/permissions" and not args:
            self.on_event({"kind": "notice", "content": self.supervisor.permissions_status()})
        elif command == "/revoke":
            if len(args) != 1:
                raise ValueError("Usage: /revoke ID")
            if not self.supervisor.revoke_permission(args[0]):
                raise ValueError("Unknown saved permission ID")
            self.on_event({"kind": "notice", "content": f"Revoked permission {args[0]}"})
        elif command == "/interrupt" and not args:
            self._schedule_interrupt()
        elif command == "/reset" and not args:
            self.supervisor.request_reset()
            self.on_event({
                "kind": "notice",
                "content": "Context reset requested for the next request. Python variables stay alive; no kernel restart.",
            })
        else:
            raise ValueError(f"{command} takes no arguments; use /help")
        return True

    async def run(self) -> None:
        self._previous_callback = self.supervisor.on_event
        self.supervisor.on_event = self.on_event
        try:
            with patch_stdout(raw=False):
                # Without protocol negotiation many terminals send exactly the
                # same CR byte for Enter and Shift-Enter, making a binding
                # impossible. Request modifier-aware key sequences while this
                # prompt owns the terminal and restore the prior mode on exit.
                self.session.output.write_raw(_EXTENDED_KEYS_ON)
                self.session.output.flush()
                try:
                    while True:
                        try:
                            prompt = FormattedText([("class:user-prompt", f"In [{self._input_number}]: ")])
                            continuation = lambda width, line_number, wrap_count: FormattedText([
                                ("class:continuation-prompt", " " * max(0, width - 5) + "...: ")
                            ])
                            line = await self.session.prompt_async(
                                prompt, prompt_continuation=continuation, pre_run=self._pre_run
                            )
                            self._blank_line()
                            if not await self.handle_line(line):
                                break
                        except _QuitRequested:
                            if self._active():
                                self._emit(
                                    "Exiting: cancelling active work and terminating descendants. Partial effects remain."
                                )
                            break
                        except KeyboardInterrupt:
                            if self._active():
                                self._schedule_interrupt()
                        except EOFError:
                            # A closed input stream must not spin or orphan work.
                            if self._active():
                                self._emit(
                                    "Terminal input closed; cancelling active work and terminating descendants. Partial effects remain."
                                )
                            break
                        except Exception as exc:
                            self.on_event({"kind": "error", "content": str(exc)})
                finally:
                    self.session.output.write_raw(_EXTENDED_KEYS_OFF)
                    self.session.output.flush()
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


async def run(
    supervisor, *, history_path: Path | None = None, vi: bool = False, multiline: bool = False, no_color: bool = False
) -> None:
    await Terminal(supervisor, history_path=history_path, vi=vi, multiline=multiline, no_color=no_color).run()
