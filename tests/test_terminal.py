"""Terminal tests inject input/output; no provider or arbitrary code is run."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types

from prompt_toolkit.data_structures import Size
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
import pytest

from py_agent import cli
from py_agent.terminal import HELP, Terminal, private_history, sanitize


class Output(DummyOutput):
    def __init__(self, columns=80):
        self.parts = []
        self.columns = columns

    def write(self, data):
        self.parts.append(data)

    def write_raw(self, data):
        self.parts.append(data)

    def get_size(self):
        return Size(rows=24, columns=self.columns)

    @property
    def text(self):
        return "".join(self.parts)


class Journal:
    def __init__(self):
        self.reads = []

    def recent(self, n):
        return [{"id": "a1:e1", "kind": "source", "excerpt": "print(2)"}]

    def read(self, identifier, **kwargs):
        self.reads.append((identifier, kwargs))
        return {"content": "evidence", "next_offset": 8, "truncated": False}


class Supervisor:
    def __init__(self):
        self.on_event = lambda event: None
        self.journal = Journal()
        self.submitted = []
        self.interrupts = 0
        self.resets = 0
        self.closed = False
        self.state = "IDLE"
        self.interrupt_gate = None
        self.permission_decisions = []
        self.revoked = []

    def status(self):
        return {
            "model": "fake/test",
            "state": self.state,
            "cell_id": "a1:c0001",
            "context_epoch": "e1",
            "queued": len(self.submitted),
            "estimated_input_tokens": 123,
            "usage": {},
        }

    def submit(self, text):
        self.submitted.append(text)
        return f"a1:e{len(self.submitted)}"

    async def interrupt(self):
        self.interrupts += 1
        if self.interrupt_gate is not None:
            await self.interrupt_gate.wait()
        self.state = "INTERRUPTED"

    def request_reset(self):
        self.resets += 1

    def resolve_permission(self, request_id, *, allow, scope="once"):
        self.permission_decisions.append((request_id, allow, scope))

    def permissions_status(self):
        return {"pending": [{"request_id": "perm-1"}], "grants": [{"id": "p-saved"}]}

    def revoke_permission(self, grant_id):
        self.revoked.append(grant_id)
        return grant_id == "p-saved"

    async def close(self):
        self.closed = True


async def until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), 3)


@pytest.mark.parametrize(
    "payload,expected",
    [
        ("hello\x1b]52;c;ZXZpbA==\x07world", "helloworld"),
        ("a\x1b]0;title\x1b\\b", "ab"),
        ("a\x1bPgarbage\x1b\\b", "ab"),
        ("a\x1b[2J\x1b[31mred\x1b[0m", "ared"),
        ("a\x9b2Jb\x9d52;evil\x9c", "ab"),
        ("a\x1b]52;unterminated", "a"),
        ("a\x08\x00\x7fb\u202ereversed\u2066", "abreversed"),
        ("x\r\ny\rz\t😀中文", "x\ny\nz\t😀中文"),
    ],
)
def test_sanitize_escapes_controls_and_unicode(payload, expected):
    assert sanitize(payload) == expected


def test_sanitize_does_not_interpret_html():
    assert sanitize("<b>not markup</b>") == "<b>not markup</b>"
    assert sanitize({"value": "你好"}) == '{"value": "你好"}'


def test_private_history_permissions_and_no_save_option(tmp_path):
    directory = tmp_path / "private"
    history = private_history(directory / "input-history")
    history.store_string("secret\nsecond line")
    assert list(history.load_history_strings()) == ["secret\nsecond line"]
    assert (directory.stat().st_mode & 0o777) == 0o700
    assert ((directory / "input-history").stat().st_mode & 0o777) == 0o600
    terminal = Terminal(Supervisor(), input=None, output=Output())
    assert terminal.session.history.__class__.__name__ == "InMemoryHistory"


def test_history_rejects_links_and_public_parent(tmp_path):
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="private"):
        private_history(public / "history")
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    target = directory / "target"
    target.write_text("unchanged")
    link = directory / "history"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        private_history(link)
    link.unlink()
    os.link(target, link)
    with pytest.raises(ValueError, match="one link"):
        private_history(link)
    assert target.read_text() == "unchanged"


def test_history_rejects_fifo_without_hanging(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    fifo = directory / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular"):
        private_history(fifo)


async def test_paste_remains_draft_output_preserves_cursor_and_quit():
    supervisor, output = Supervisor(), Output()
    with create_pipe_input() as pipe:
        terminal = Terminal(supervisor, input=pipe, output=output, no_color=True)
        callback = supervisor.on_event
        task = asyncio.create_task(terminal.run())
        await until(lambda: terminal.session.app.is_running)
        pipe.send_text("\x1b[200~hello\nworld\x1b[201~")
        await until(lambda: terminal.session.default_buffer.text == "hello\nworld")
        assert supervisor.submitted == []
        pipe.send_text("\x1b[D")
        await until(lambda: terminal.session.default_buffer.cursor_position == len("hello\nworld") - 1)
        cursor = terminal.session.default_buffer.cursor_position
        supervisor.on_event({"kind": "say", "content": "progress\x1b]52;c;evil\x07"})
        supervisor.on_event({"kind": "source", "cell_id": "a1:c0001", "content": "6 * 7"})
        for fragment in ("f", " ", "file.py", "\n"):
            supervisor.on_event({"kind": "output", "cell_id": "a1:c0001", "stream": "stdout", "content": fragment})
            await asyncio.sleep(0)
        supervisor.on_event({"kind": "output", "cell_id": "a1:c0001", "stream": "display", "content": "42"})
        await until(lambda: "progress" in output.text and "Out[1]:" in output.text)
        assert "In [1]:" in output.text
        assert "6 * 7" not in output.text
        assert "f file.py" in output.text
        assert output.text.count("stdout [1]:") == 1
        assert terminal.session.default_buffer.text == "hello\nworld"
        assert terminal.session.default_buffer.cursor_position == cursor
        assert "evil" not in output.text
        pipe.send_text("\r")
        await until(lambda: supervisor.submitted == ["hello\nworld"])
        pipe.send_text("/quit\r")
        await asyncio.wait_for(task, 3)
        assert supervisor.closed
        assert supervisor.on_event is callback
        assert "Queued a1:e1" in output.text


async def test_numbered_prompt_advances_only_after_successful_user_submissions():
    supervisor, output = Supervisor(), Output()
    with create_pipe_input() as pipe:
        terminal = Terminal(supervisor, input=pipe, output=output)
        task = asyncio.create_task(terminal.run())
        await until(lambda: terminal.session.app.is_running and "In [1]:" in output.text)
        pipe.send_text("\r/help\r")
        await until(lambda: "accepts agent requests" in output.text)
        assert terminal._input_number == 1
        pipe.send_text("discard\x03")
        await until(lambda: terminal.session.default_buffer.text == "")
        assert terminal._input_number == 1
        pipe.send_text("first request\r")
        await until(lambda: "In [2]:" in output.text)
        assert supervisor.submitted == ["first request"]
        pipe.send_text("/trace\rsecond request\r")
        await until(lambda: "In [3]:" in output.text)
        assert supervisor.submitted == ["first request", "second request"]
        pipe.send_text("/quit\r")
        await asyncio.wait_for(task, 3)
    assert terminal._input_number == 3
    assert supervisor.closed
    assert "You:" not in output.text


async def test_failed_submission_does_not_advance_prompt_number(monkeypatch):
    supervisor = Supervisor()
    terminal = Terminal(supervisor, output=Output())

    def rejected(text):
        raise RuntimeError("No live kernel")

    monkeypatch.setattr(supervisor, "submit", rejected)
    with pytest.raises(RuntimeError, match="No live kernel"):
        await terminal.handle_line("request")
    assert terminal._input_number == 1
    assert not terminal._pending


async def test_multiline_enter_and_escape_enter():
    supervisor, output = Supervisor(), Output()
    with create_pipe_input() as pipe:
        terminal = Terminal(supervisor, input=pipe, output=output, multiline=True)
        task = asyncio.create_task(terminal.run())
        await until(lambda: terminal.session.app.is_running)
        pipe.send_text("line one\rline two")
        await until(lambda: terminal.session.default_buffer.text == "line one\nline two")
        assert supervisor.submitted == []
        pipe.send_text("\x1b\r")
        await until(lambda: supervisor.submitted == ["line one\nline two"])
        pipe.send_text("/quit\x1b\r")
        await asyncio.wait_for(task, 3)


async def test_interrupt_is_nonblocking_and_ctrl_c_idle_clears():
    supervisor = Supervisor()
    supervisor.state = "EXECUTING"
    supervisor.interrupt_gate = asyncio.Event()
    with create_pipe_input() as pipe:
        terminal = Terminal(supervisor, input=pipe, output=Output())
        task = asyncio.create_task(terminal.run())
        await until(lambda: terminal.session.app.is_running)
        pipe.send_text("draft\x03")
        await until(lambda: supervisor.interrupts == 1)
        # Interrupt is in progress, yet the composer can accept steering.
        pipe.send_text(" steering\r")
        await until(lambda: supervisor.submitted == ["draft steering"])
        supervisor.interrupt_gate.set()
        await until(lambda: supervisor.state == "INTERRUPTED")
        pipe.send_text("discard\x03")
        await until(lambda: terminal.session.default_buffer.text == "")
        pipe.send_text("\x04")
        await asyncio.wait_for(task, 3)
        assert supervisor.closed


async def test_active_ctrl_d_exits_with_explicit_cancellation():
    supervisor, output = Supervisor(), Output()
    supervisor.state = "GENERATING"
    with create_pipe_input() as pipe:
        terminal = Terminal(supervisor, input=pipe, output=output)
        task = asyncio.create_task(terminal.run())
        await until(lambda: terminal.session.app.is_running)
        pipe.send_text("\x04")
        await asyncio.wait_for(task, 3)
        assert "cancelling active work" in output.text
        assert supervisor.closed


@pytest.mark.parametrize("state", ["IDLE", "DONE", "FAILED", "INTERRUPTED"])
async def test_ctrl_c_on_empty_idle_prompt_exits(state):
    supervisor = Supervisor()
    supervisor.state = state
    with create_pipe_input() as pipe:
        terminal = Terminal(supervisor, input=pipe, output=Output())
        task = asyncio.create_task(terminal.run())
        await until(lambda: terminal.session.app.is_running)
        pipe.send_text("\x03")
        await asyncio.wait_for(task, 3)
        assert supervisor.closed


@pytest.mark.parametrize("state", ["GENERATING", "EXECUTING"])
@pytest.mark.parametrize("vi,multiline", [(False, False), (True, False), (False, True)])
async def test_double_ctrl_c_exits_even_while_interrupt_is_stuck(state, vi, multiline):
    supervisor = Supervisor()
    supervisor.state = state
    supervisor.interrupt_gate = asyncio.Event()  # deliberately never released
    with create_pipe_input() as pipe:
        terminal = Terminal(supervisor, input=pipe, output=Output(), vi=vi, multiline=multiline)
        task = asyncio.create_task(terminal.run())
        await until(lambda: terminal.session.app.is_running)
        pipe.send_text("draft\x03")
        await until(lambda: supervisor.interrupts == 1)
        assert not task.done()
        pipe.send_text("\x03")
        await asyncio.wait_for(task, 3)
        assert supervisor.closed


async def test_double_ctrl_c_in_one_key_batch_exits():
    supervisor = Supervisor()
    supervisor.state = "GENERATING"
    with create_pipe_input() as pipe:
        terminal = Terminal(supervisor, input=pipe, output=Output())
        task = asyncio.create_task(terminal.run())
        await until(lambda: terminal.session.app.is_running)
        pipe.send_text("\x03\x03")
        await asyncio.wait_for(task, 3)
        assert supervisor.closed


async def test_quit_enter_exits_multiline_without_escape_enter():
    supervisor = Supervisor()
    supervisor.state = "EXECUTING"
    with create_pipe_input() as pipe:
        terminal = Terminal(supervisor, input=pipe, output=Output(), multiline=True)
        task = asyncio.create_task(terminal.run())
        await until(lambda: terminal.session.app.is_running)
        pipe.send_text("  /quit  \r")
        await asyncio.wait_for(task, 3)
        assert supervisor.closed
        assert not supervisor.submitted


async def test_closed_input_cancels_active_work_instead_of_spinning():
    supervisor, output = Supervisor(), Output()
    supervisor.state = "EXECUTING"
    with create_pipe_input() as pipe:
        terminal = Terminal(supervisor, input=pipe, output=output)
        task = asyncio.create_task(terminal.run())
        await until(lambda: terminal.session.app.is_running)
        pipe.close()
        await asyncio.wait_for(task, 3)
    assert supervisor.closed
    assert "Terminal input closed; cancelling active work" in output.text


async def test_commands_do_not_submit_code_and_history_is_paged():
    supervisor, output = Supervisor(), Output()
    terminal = Terminal(supervisor, output=output)
    for command in (
        "/help",
        "/history",
        "/history a1:c1 12",
        "/usage",
        "/trace",
        "/permissions",
        "/approve perm-1 once",
        "/approve perm-2 session",
        "/approve perm-3 project",
        "/approve perm-4 all",
        "/deny perm-5",
        "/revoke p-saved",
        "/reset",
        "/unknown",
    ):
        assert await terminal.handle_line(command)
    assert supervisor.submitted == []
    assert supervisor.resets == 1
    assert supervisor.permission_decisions == [
        ("perm-1", True, "once"),
        ("perm-2", True, "session"),
        ("perm-3", True, "project"),
        ("perm-4", True, "global"),
        ("perm-5", False, "once"),
    ]
    assert supervisor.revoked == ["p-saved"]
    assert supervisor.journal.reads == [("a1:c1", {"offset": 12, "limit": 8000})]
    assert terminal.trace
    assert "Esc-Enter" in HELP
    assert not await terminal.handle_line("/quit")
    with pytest.raises(ValueError):
        await terminal.handle_line("/history a b c")
    with pytest.raises(ValueError):
        await terminal.handle_line("/quit unexpected")
    with pytest.raises(ValueError):
        await terminal.handle_line("/history a NaN")


async def test_python_hidden_by_default_and_results_keep_ipython_labels():
    output, supervisor = Output(), Supervisor()
    terminal = Terminal(supervisor, output=output, no_color=True)
    assert not terminal.trace
    source = {"kind": "source", "cell_id": "a1:c0007", "content": "print('hi')\x1b]0;evil\x07"}
    terminal.on_event(source)
    terminal.on_event({"kind": "output", "cell_id": "a1:c0007", "stream": "stdout", "content": "hi\n"})
    terminal.on_event({"kind": "output", "cell_id": "a1:c0007", "stream": "stderr", "content": "warning\n"})
    terminal.on_event({"kind": "output", "cell_id": "a1:c0007", "stream": "display", "content": "42"})
    assert source["content"].endswith("\x07")  # Original was not rewritten.
    assert len(terminal._pending) == 3
    while terminal._pending:
        terminal._render_event(terminal._pending.popleft())
    for text in ("stdout [7]:", "hi", "stderr [7]:", "warning", "Out[7]:", "42"):
        assert text in output.text
    assert "print('hi')" not in output.text
    assert "Python [7]:" not in output.text
    assert "In [7]:" not in output.text
    assert "evil" not in output.text
    assert "\x1b" not in output.text


async def test_trace_adds_python_and_audit_records_but_is_not_needed_for_results():
    terminal = Terminal(Supervisor(), output=Output())
    events = [
        {"kind": "source", "cell_id": "a1:c0007", "content": "print('traced code')"},
        {"kind": "generation_request", "content": {"messages": "private prompt"}},
        {"kind": "generation_response", "content": {"raw": "raw SSE"}},
        {"kind": "say_staged", "content": "not released yet"},
        {"kind": "cell_end", "content": {"status": "success"}},
    ]
    for event in events:
        terminal.on_event(event)
    # Successful cell_end is an invisible stream-flush boundary.
    assert [event["kind"] for event in terminal._pending] == ["cell_end"]
    terminal._render_event(terminal._pending.popleft())
    terminal.on_event({"kind": "say", "content": "released once"})
    assert len(terminal._pending) == 1
    terminal._pending.clear()
    await terminal.handle_line("/trace")
    terminal._pending.clear()
    for event in events:
        terminal.on_event(event)
    assert [e["kind"] for e in terminal._pending] == [e["kind"] for e in events]
    await terminal.handle_line("/trace")
    terminal._pending.clear()
    terminal.on_event({"kind": "output", "content": "still visible"})
    assert len(terminal._pending) == 1


def render(terminal, event):
    terminal.on_event(event)
    while terminal._pending:
        terminal._render_event(terminal._pending.popleft())


def stream(terminal, content, *, cell="a1:c0001", kind="stdout", late=False):
    render(terminal, {"kind": "output", "cell_id": cell, "stream": kind, "asynchronous": late, "content": content})


def test_stream_fragments_preserve_lines_without_repeated_headers():
    output = Output()
    terminal = Terminal(Supervisor(), output=output, no_color=True)
    for fragment in ["f", " ", "file.py", "\n", "another", " line\n"]:
        stream(terminal, fragment)
    text = output.text.replace("\r\n", "\n")
    assert text == "stdout [1]:\nf file.py\nanother line\n"
    assert terminal._stream_partial == ""


@pytest.mark.parametrize(
    "boundary",
    [
        {"kind": "cell_end", "cell_id": "a1:c0001", "content": {"status": "success"}},
        {"kind": "say", "content": "answer"},
        {"kind": "source", "cell_id": "a1:c0002", "content": "print(2)"},
        {"kind": "notice", "content": "notice"},
    ],
)
def test_partial_line_flushes_at_visible_control_or_cell_boundary(boundary):
    output = Output()
    terminal = Terminal(Supervisor(), output=output, no_color=True)
    terminal.trace = boundary["kind"] == "source"  # source is a visible boundary only in trace
    stream(terminal, "no newline")
    assert "no newline" not in output.text
    render(terminal, boundary)
    assert "stdout [1]:\nno newline\n" in output.text.replace("\r\n", "\n")
    assert terminal._stream_partial == ""


def test_stream_switches_keep_order_and_late_cell_origin():
    output = Output()
    terminal = Terminal(Supervisor(), output=output, no_color=True)
    stream(terminal, "left")
    stream(terminal, "warning\n", kind="stderr")
    stream(terminal, "right\n")
    stream(terminal, "old\n", cell="a1:c0002", late=True)
    assert output.text.replace("\r\n", "\n") == (
        "stdout [1]:\nleft\nstderr [1]:\nwarning\nstdout [1]:\nright\nstdout [2] (late):\nold\n"
    )


def test_long_partial_line_is_bounded_without_newlines_or_excerpt_insertion():
    output = Output()
    terminal = Terminal(Supervisor(), output=output, no_color=True)
    payload = "雪" * 4000 + "x" * 15000
    for start in range(0, len(payload), 311):
        stream(terminal, payload[start : start + 311])
        assert len(terminal._stream_partial.encode("utf-8")) <= 8192
    render(terminal, {"kind": "cell_end", "content": {"status": "success"}})
    assert output.text.replace("\r\n", "\n") == "stdout [1]:\n" + payload + "\n"


@pytest.mark.parametrize(
    "fragments",
    [
        ["\x1b", "]52;c;", "SECRET", "\x1b", "\\", "safe\n"],
        ["\x9d52;c;", "SECRET", "\x9c", "safe\n"],
        ["\x1bP", "SECRET\n" * 3000, "\x1b", "\\", "safe\n"],
        ["\x1bP", "SECRET\x07", "STILL_SECRET", "\x1b\\safe\n"],
        ["\x1b[", "2", "J", "safe\n"],
        ["\x1b[3", "1m", "safe", "\x1b[", "0m", "\n"],
    ],
)
def test_stream_sanitization_survives_fragment_boundaries(fragments):
    output = Output()
    terminal = Terminal(Supervisor(), output=output, no_color=True)
    terminal.trace = True
    for fragment in fragments:
        stream(terminal, fragment)
        assert len(terminal._stream_partial.encode("utf-8")) <= 8192
    assert output.text.replace("\r\n", "\n") == "stdout [1]:\nsafe\n"


def test_escape_state_survives_stream_switch_cell_end_and_late_output():
    output = Output()
    terminal = Terminal(Supervisor(), output=output, no_color=True)
    stream(terminal, "\x1b]52;c;")
    stream(terminal, "warning\n", kind="stderr")
    render(terminal, {"kind": "cell_end", "cell_id": "a1:c0001", "content": {"status": "success"}})
    stream(terminal, "SECRET\x07visible\n", late=True)
    assert "SECRET" not in output.text
    assert "stdout [1] (late):" in output.text
    assert "visible" in output.text


def test_queued_backlog_cannot_expose_escape_payload_or_modify_original_events():
    output = Output()
    terminal = Terminal(Supervisor(), output=output, no_color=True)
    event = {"kind": "output", "cell_id": "a1:c0001", "stream": "stdout", "content": "\x1b]52;c;"}
    terminal.on_event(event)
    for _ in range(260):
        terminal.on_event({"kind": "notice", "content": "busy"})
    assert len(terminal._pending) == 261
    stream(terminal, "SECRET\x07safe\n")
    assert event["content"] == "\x1b]52;c;"
    assert "SECRET" not in output.text
    assert "safe" in output.text


def test_stream_crlf_normalization_survives_pipe_boundaries():
    output = Output()
    terminal = Terminal(Supervisor(), output=output, no_color=True)
    for fragment in ["a\r", "\nb\r", "c\n"]:
        stream(terminal, fragment)
    assert output.text.replace("\r\n", "\n") == "stdout [1]:\na\nb\nc\n"


async def test_shutdown_flushes_a_partial_stream_without_cell_end():
    supervisor, output = Supervisor(), Output()
    with create_pipe_input() as pipe:
        terminal = Terminal(supervisor, input=pipe, output=output, no_color=True)
        task = asyncio.create_task(terminal.run())
        await until(lambda: terminal.session.app.is_running)
        supervisor.on_event({"kind": "output", "cell_id": "a1:c0001", "stream": "stdout", "content": "shutdown-tail"})
        await until(lambda: terminal._stream_partial == "shutdown-tail")
        pipe.send_text("/quit\n")
        await asyncio.wait_for(task, 3)
    assert "shutdown-tail" in output.text
    assert supervisor.closed
    assert terminal._stream_partial == ""


def test_output_origin_fallback_late_output_and_failure_are_visible():
    output = Output()
    terminal = Terminal(Supervisor(), output=output, no_color=True)
    terminal.on_event({
        "kind": "output",
        "cell_id": "a1:c0002",
        "stream": "stdout",
        "asynchronous": True,
        "content": "late\x1b[2J\u202e output",
    })
    terminal.on_event({"kind": "output", "stream": "display", "content": "你好😀"})
    terminal.on_event({
        "kind": "cell_end",
        "cell_id": "a1:c0003",
        "content": {"status": "error", "error": "ValueError: failed"},
    })
    while terminal._pending:
        terminal._render_event(terminal._pending.popleft())
    assert "stdout [2] (late):" in output.text
    assert "Out[?]:" in output.text
    assert "你好😀" in output.text
    assert "Cell [3] error: ValueError: failed" in output.text
    assert "\x1b" not in output.text
    assert "\u202e" not in output.text


@pytest.mark.parametrize("kind", ["source", "cell", "cell_source"])
async def test_source_requires_trace_and_hidden_events_are_not_replayed(kind):
    output = Output()
    terminal = Terminal(Supervisor(), output=output, no_color=True)
    event = {"kind": kind, "cell_id": "a1:c0007", "content": "private_generated_code()"}
    terminal.on_event(event)
    assert not terminal._pending
    await terminal.handle_line("/trace")
    terminal._pending.clear()
    render(terminal, event)
    assert "Python [7]:" in output.text
    assert "private_generated_code()" in output.text
    assert "In [7]:" not in output.text
    terminal.on_event(event)  # Queued while tracing; toggling off also prevents rendering it.
    terminal.trace = False
    before = output.text
    terminal._render_event(terminal._pending.popleft())
    assert output.text == before
    assert "input numbers" in HELP
    assert "worker cell numbers" in HELP


def test_source_highlighting_is_enabled_when_trace_is_on(monkeypatch):
    from prompt_toolkit.formatted_text import PygmentsTokens

    calls = []
    monkeypatch.setattr("py_agent.terminal.print_formatted_text", lambda text, **kwargs: calls.append(text))
    terminal = Terminal(Supervisor(), output=Output())
    terminal.trace = True
    terminal.on_event({"kind": "source", "cell_id": "a1:c1", "content": "value = 42\x1b]52;c;evil\x07"})
    terminal._render_event(terminal._pending.popleft())
    assert calls[0] == "Python [1]:"
    assert isinstance(calls[1], PygmentsTokens)
    assert "evil" not in repr(calls[1].token_list)


def test_thinking_spinner_animates_only_during_generation(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("py_agent.terminal.time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    supervisor = Supervisor()
    terminal = Terminal(supervisor, output=Output(), no_color=True)
    assert terminal.session.refresh_interval == 0.15

    def toolbar():
        return "".join(text for _, text in terminal._toolbar())

    idle = toolbar()
    clock[0] = 0.16
    assert toolbar() == idle
    supervisor.state = "GENERATING"
    first = toolbar()
    clock[0] = 0.31
    second = toolbar()
    assert first != second
    assert "Thinking (GENERATING)" in first
    assert first.startswith("/ Thinking")
    assert second.startswith("- Thinking")
    supervisor.state = "EXECUTING"
    executing = toolbar()
    clock[0] = 0.46
    assert toolbar() == executing
    assert executing.startswith("Executing (EXECUTING)")
    supervisor.state = "DONE"
    assert "Thinking" not in toolbar()
    assert terminal._renderer is None  # animation has not spawned a background task


async def test_reset_notice_keeps_live_python_variables():
    terminal = Terminal(Supervisor(), output=Output())
    await terminal.handle_line("/reset")
    notice = terminal._pending[-1]["content"]
    assert "Python variables stay alive" in notice
    assert "no kernel restart" in notice
    assert "checkpoint" not in HELP
    assert "checkpoint" not in notice


async def test_vi_history_search_and_resize(tmp_path):
    supervisor, output = Supervisor(), Output(columns=12)
    with create_pipe_input() as pipe:
        terminal = Terminal(supervisor, input=pipe, output=output, vi=True)
        assert terminal.session.editing_mode == EditingMode.VI
        assert terminal.session.enable_history_search
        task = asyncio.create_task(terminal.run())
        await until(lambda: terminal.session.app.is_running)
        pipe.send_text("你好😀\r")
        await until(lambda: supervisor.submitted == ["你好😀"])
        output.columns = 5
        terminal.session.app._on_resize()
        pipe.send_text("/quit\r")
        await asyncio.wait_for(task, 3)
    assert "你好" in output.text


async def test_emacs_ctrl_r_and_history_navigation():
    supervisor, output = Supervisor(), Output()
    with create_pipe_input() as pipe:
        terminal = Terminal(supervisor, input=pipe, output=output)
        task = asyncio.create_task(terminal.run())
        await until(lambda: terminal.session.app.is_running)
        pipe.send_text("first original\r")
        await until(lambda: len(supervisor.submitted) == 1)
        pipe.send_text("second original\r")
        await until(lambda: len(supervisor.submitted) == 2)
        pipe.send_text("\x1b[A")
        await until(lambda: terminal.session.default_buffer.text == "second original")
        pipe.send_text("\x03\x12first")
        await until(lambda: terminal.session.search_buffer.text == "first")
        pipe.send_text("\r")
        try:
            await until(lambda: terminal.session.default_buffer.text == "first original")
        except TimeoutError:
            pytest.fail(
                repr({
                    "text": terminal.session.default_buffer.text,
                    "history": terminal.session.history.get_strings(),
                    "working": list(terminal.session.default_buffer._working_lines),
                    "submitted": supervisor.submitted,
                    "search": terminal.session.search_buffer.text,
                })
            )
        pipe.send_text("\x03/quit\r")
        await asyncio.wait_for(task, 3)


async def test_terminal_queue_preserves_all_events_and_status_unknown():
    terminal = Terminal(Supervisor(), output=Output())
    for i in range(300):
        terminal.on_event({"kind": "say", "content": str(i)})
    assert len(terminal._pending) == 300
    terminal._pending.clear()
    terminal.on_event({"kind": "user_queued", "id": "a1:e1"})
    terminal.on_event({"kind": "user_queued", "id": "a1:e1"})
    assert len(terminal._pending) == 1
    toolbar = "".join(text for _, text in terminal._toolbar())
    assert "ctx(last) ?%/? out(last) ?" in toolbar
    assert "ctx~" not in toolbar
    assert "123" not in toolbar


def test_trace_source_has_no_terminal_character_excerpt():
    output = Output()
    terminal = Terminal(Supervisor(), output=output, no_color=True)
    terminal.trace = True
    source = "# " + "x" * 16000 + "\nprint('the end')"
    render(terminal, {"kind": "source", "cell_id": "a1:c1", "content": source})
    assert source in output.text.replace("\r\n", "\n")
    assert "terminal excerpt" not in output.text


async def test_journal_queue_receipt_and_submit_fallback_are_one_ack():
    terminal = Terminal(Supervisor(), output=Output())
    terminal.on_event({"kind": "user_queued", "id": "a1:e6", "content": "a1:e5"})
    terminal.on_event({"kind": "user_queued", "id": "a1:e5", "content": ""})
    assert len(terminal._pending) == 1
    assert terminal._pending[0]["user_id"] == "a1:e5"


@pytest.mark.parametrize(
    "usage,expected",
    [
        ([{"normalized": {"input_tokens": 500, "output_tokens": 23}}], "50%/1k out(last) 23"),
        (
            [{"normalized": {"input_tokens": 999}}, {"normalized": {"input_tokens": 600, "output_tokens": 30}}],
            "60%/1k out(last) 30",
        ),
        ([{"normalized": {"input_tokens": 999}}, {}], "?%/1k out(last) ?"),
        ({"normalized": {"input_tokens": 0, "output_tokens": 0}}, "0%/1k out(last) 0"),
        ({"normalized": {"output_tokens": 23}}, "?%/1k out(last) 23"),
        ({"normalized": {"input_tokens": 500}}, "50%/1k out(last) ?"),
        ({"input_tokens": 500, "output_tokens": 23}, "50%/1k out(last) 23"),
        ({"normalized": {"input_tokens": None, "output_tokens": None}}, "?%/1k out(last) ?"),
        ({"normalized": {"input_tokens": True, "output_tokens": -1}}, "?%/1k out(last) ?"),
        (
            {
                "normalized": {
                    "input_tokens": 500,
                    "output_tokens": 23,
                    "cache_read_tokens": 400,
                    "reasoning_tokens": 10,
                }
            },
            "50%/1k out(last) 23",
        ),
    ],
)
async def test_toolbar_reads_last_reported_tokens_without_estimation_or_cache_arithmetic(usage, expected):
    supervisor = Supervisor()
    original = supervisor.status()
    supervisor.status = lambda: {**original, "usage": usage, "context_window_tokens": 1000}
    terminal = Terminal(supervisor, output=Output())
    toolbar = "".join(text for _, text in terminal._toolbar())
    assert f"ctx(last) {expected}" in toolbar
    assert "ctx~" not in toolbar
    assert "123" not in toolbar


@pytest.mark.parametrize(
    "window,input_tokens,expected",
    [
        (272000, 81600, "30%/272k"),
        (272500, 81750, "30%/272.5k"),
        (272001, 0, "0%/272.001k"),
        (500, 250, "50%/500"),
        (1000, 1501, "150%/1k"),
        (1000, 1555, "156%/1k"),
        (272000, None, "?%/272k"),
        (None, 81600, "?%/?"),
        (0, 81600, "?%/?"),
        (-1, 81600, "?%/?"),
        (True, 81600, "?%/?"),
        (272000.0, 81600, "?%/?"),
    ],
)
async def test_toolbar_context_percentage_uses_only_valid_configured_window(window, input_tokens, expected):
    supervisor = Supervisor()
    original = supervisor.status()
    supervisor.status = lambda: {
        **original,
        "context_window_tokens": window,
        "usage": [{"normalized": {"input_tokens": input_tokens, "output_tokens": 17}}],
    }
    toolbar = "".join(text for _, text in Terminal(supervisor, output=Output())._toolbar())
    assert f"ctx(last) {expected} out(last) 17" in toolbar


@pytest.mark.parametrize("current,expected", [(81600, "30%/272k"), (None, "?%/272k")])
async def test_toolbar_ignores_orphan_or_previous_epoch_usage(current, expected):
    supervisor = Supervisor()
    original = supervisor.status()
    supervisor.status = lambda: {
        **original,
        "context_window_tokens": 272000,
        "context_input_tokens": current,
        "usage": [{"normalized": {"input_tokens": 271000, "output_tokens": 23}}],
    }
    terminal = Terminal(supervisor, output=Output())
    assert f"ctx(last) {expected}" in "".join(text for _, text in terminal._toolbar())


def test_config_exact_schema_and_types(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('model="openai/explicit"\nstream=true\n[budgets]\ninput_tokens=10000\noutput_tokens=4000\n')
    config = cli.load_config(path)
    assert config["model"] == "openai/explicit"
    assert config["budgets"]["output_tokens"] == 4000
    path.write_text("context_window_tokens=272000")
    assert cli.load_config(path)["context_window_tokens"] == 272000
    for text in (
        'network="proxy"',
        'api_key="secret"',
        "model=123",
        'stream="yes"',
        "[budgets]\nunknown=12",
        "[budgets]\nmax_requests=true",
        "budgets=12",
        "[budgets]\ncell_seconds=nan",
        "[budgets]\nmax_requests=0",
        "[budgets]\ntail_groups=-1",
        "context_window_tokens=0",
        "context_window_tokens=true",
        'context_window_tokens="272000"',
    ):
        path.write_text(text)
        with pytest.raises(ValueError):
            cli.load_config(path)
    path.write_text('model="x"')
    path.chmod(0o666)
    with pytest.raises(ValueError, match="writable"):
        cli.load_config(path)


def test_config_missing_symlink_and_limits(tmp_path):
    missing = tmp_path / "missing"
    assert cli.load_config(missing) == {}
    with pytest.raises(ValueError, match="does not exist"):
        cli.load_config(missing, required=True)
    target = tmp_path / "target"
    target.write_text('model="x"')
    missing.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        cli.load_config(missing)
    target.write_bytes(b"#" * 65537)
    with pytest.raises(ValueError, match="65536"):
        cli.load_config(target)


def test_non_tty_fails_before_config_or_storage(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    host = tmp_path / "does-not-exist"
    assert cli.main(["--host-root", str(host)]) == 2
    assert not host.exists()
    assert "Batch/JSON mode is not implemented" in capsys.readouterr().err


def test_cli_help_and_no_unsandboxed_switch(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--network" in help_text
    assert "--multiline" in help_text
    assert "checkpoint" not in help_text
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["--checkpoint-tokens", "2048"])
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["--unsandboxed"])


def test_cli_permission_switches_are_explicit(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert cli.main(["--allow-domain", "example.com"]) == 1
    assert "--network proxy" in capsys.readouterr().err
    assert cli.main(["--fake-responses", "fake.json", "--model", "openai/model"]) == 1
    assert "cannot be combined" in capsys.readouterr().err


def test_fake_responses_have_no_count_or_size_ceiling(tmp_path):
    path = tmp_path / "fake.json"
    responses = ["# " + "x" * 2100] * 1001
    path.write_text(json.dumps(responses))
    assert cli._fake_responses(path) == responses


def test_fake_responses_strict_format(tmp_path):
    path = tmp_path / "fake.json"
    path.write_text('["say(42, final=True)"]')
    assert cli._fake_responses(path) == ["say(42, final=True)"]
    for text in ("[]", "{}", "[12]", "[null]"):
        path.write_text(text)
        with pytest.raises(ValueError):
            cli._fake_responses(path)


def test_normal_startup_is_brief_but_keeps_privacy_warning(tmp_path, capsys):
    sandbox = types.SimpleNamespace(workspace=tmp_path, policy={"network": "proxy", "allowed_domains": []})
    cli._print_policy(sandbox)
    text = capsys.readouterr().out
    assert "worker network: proxy (0 pre-approved; prompts for others)" in text
    assert "workspace and shared /tmp" in text
    assert "readable private data can reach the model/provider" in text
    assert "Sandbox policy:" not in text
    assert len(text.splitlines()) == 2


def test_cli_check_sandbox_no_model_or_tty(monkeypatch, tmp_path, capsys):
    import py_agent.sandbox as sandbox_module

    created = []

    class Sandbox:
        def __init__(self, workspace, host_dir, scratch, **kwargs):
            self.workspace, self.host_dir, self.scratch = workspace, host_dir, scratch
            self.kwargs = kwargs
            self.closed = False
            self.checked = False
            self.policy = {"network": kwargs["network"], "broad_root_warning": True}
            created.append(self)

        async def preflight(self):
            self.checked = True

        async def close(self):
            self.closed = True

    monkeypatch.setattr(sandbox_module, "Sandbox", Sandbox)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    host = tmp_path / "host"
    assert (
        cli.main([
            "--check-sandbox",
            "--host-root",
            str(host),
            "--workspace",
            str(tmp_path),
            "--network",
            "proxy",
            "--allow-domain",
            "example.com",
        ])
        == 0
    )
    instance = created[0]
    assert instance.checked
    assert instance.closed
    assert instance.host_dir == host  # Entire control root protected, not only this run.
    assert instance.kwargs == {"network": "proxy", "allowed_domains": ("example.com",)}
    assert instance.scratch.is_relative_to(tmp_path / ".py" / "sessions")
    output = capsys.readouterr().out
    assert "broad write root" in output
    assert "Nested environments" in output
    assert "External networking is not thereby verified" in output


@pytest.mark.parametrize(
    "config_path", ["/tmp/py-test-untrusted-config.toml", "/var/../tmp/py-test-untrusted-config.toml"]
)
async def test_explicit_config_in_shared_tmp_outside_workspace_is_rejected(tmp_path, config_path):
    args = cli.parser().parse_args([
        "--workspace",
        str(tmp_path),
        "--host-root",
        str(tmp_path / "host"),
        "--config",
        config_path,
        "--network",
        "proxy",
    ])
    with pytest.raises(ValueError, match="workspace or /tmp is unsafe"):
        await cli._run(args, {})


@pytest.mark.parametrize(
    "window_args,window_config,expected_window",
    [
        ([], {}, 9000),
        ([], {"context_window_tokens": 272000}, 272000),
        (["--context-window-tokens", "128000"], {"context_window_tokens": 272000}, 128000),
    ],
)
async def test_cli_integration_fake_provider_wiring_and_cleanup(
    monkeypatch, tmp_path, window_args, window_config, expected_window
):
    import py_agent.sandbox as sandbox_module

    captured = {}

    from py_agent.limits import Limits

    class Sandbox:
        policy = {"network": "proxy"}

        def __init__(self, *args, **kwargs):
            captured["sandbox"] = self
            self.workspace = args[0]
            self.closed = False

        def permanent_protected_paths(self):
            return ()

        async def close(self):
            self.closed = True

    class Supervisor:
        def __init__(self, provider, sandbox, journal, *, limits, on_event, context_window_tokens, permissions):
            captured.update(
                provider=provider,
                sandbox=sandbox,
                journal=journal,
                context_window_tokens=context_window_tokens,
                limits=limits,
                permissions=permissions,
                supervisor=self,
            )
            self.closed = False
            self.started = False

        async def start(self):
            self.started = True

        async def close(self):
            self.closed = True

    async def terminal(supervisor, **kwargs):
        captured["terminal_kwargs"] = kwargs
        assert supervisor.started

    monkeypatch.setitem(sys.modules, "py_agent.supervisor", types.SimpleNamespace(Supervisor=Supervisor))
    monkeypatch.setattr(sandbox_module, "Sandbox", Sandbox)
    monkeypatch.setattr(cli, "run_terminal", terminal)
    fake_path = tmp_path / "fake.json"
    fake_path.write_text('["say(1, final=True)"]')
    args = cli.parser().parse_args([
        "--workspace",
        str(tmp_path),
        "--host-root",
        str(tmp_path / "host"),
        "--fake-responses",
        str(fake_path),
        "--network",
        "proxy",
        "--no-input-history",
        "--multiline",
        "--input-tokens",
        "9000",
        *window_args,
    ])
    assert await cli._run(args, {"budgets": {"input_tokens": 12000}, **window_config}) == 0
    assert captured["limits"] == Limits(input_tokens=9000)
    assert captured["provider"].model == "fake/deterministic"
    assert captured["context_window_tokens"] == expected_window
    assert not list(tmp_path.rglob("memory.md"))
    assert captured["supervisor"].closed
    assert captured["sandbox"].closed
    assert captured["terminal_kwargs"]["history_path"] is None
    assert captured["terminal_kwargs"]["multiline"] is True
    with pytest.raises(Exception):
        captured["journal"].recent()  # Connection was closed.
