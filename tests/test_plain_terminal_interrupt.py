"""Ctrl-C prompt behavior; no provider or worker is started."""
import asyncio

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
import pytest

from py_agent.plain_terminal import PlainTerminal


class Stub:
    pass


def test_second_quick_prompt_interrupt_exits_without_repeating(monkeypatch):
    terminal = PlainTerminal(Stub(), output=DummyOutput())
    messages = []
    attempts = 0

    async def interrupt(_prompt, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise KeyboardInterrupt

    monkeypatch.setattr(terminal.session, "prompt_async", interrupt)
    monkeypatch.setattr(terminal, "_write", messages.append)
    asyncio.run(terminal.run())
    assert attempts == 2
    assert len(messages) == 1
    assert "Use /interrupt to stop work" in messages[0]


def test_prompt_interrupt_does_not_escape_child_task_or_cancel_active_work(monkeypatch):
    class ActiveStub:
        def __init__(self):
            self.interruptions = 0

        async def interrupt(self):
            self.interruptions += 1

    coordinator = ActiveStub()
    terminal = PlainTerminal(coordinator, output=DummyOutput())
    messages = []
    attempts = 0

    async def interrupt(_prompt, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise KeyboardInterrupt

    monkeypatch.setattr(terminal.session, "prompt_async", interrupt)
    monkeypatch.setattr(terminal, "_write", messages.append)

    async def run():
        errors = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        await terminal.run()
        await asyncio.sleep(0)
        assert not errors

    asyncio.run(run())
    assert coordinator.interruptions == 0
    assert attempts == 2
    assert len(messages) == 1


@pytest.mark.parametrize("sequence", [
    "\x1b[99;5u", "\x1b[99;6u", "\x1b[67;6u",
    "\x1b[27;5;99~", "\x1b[27;6;99~", "\x1b[27;6;67~",
])
async def test_extended_ctrl_c_cancels_composer_without_inserting_escape_text(sequence):
    class ActiveStub:
        def __init__(self):
            self.interruptions = 0

        async def interrupt(self):
            self.interruptions += 1

    coordinator = ActiveStub()
    messages = []
    with create_pipe_input() as pipe:
        terminal = PlainTerminal(coordinator, input=pipe, output=DummyOutput())
        terminal._write = messages.append
        running = asyncio.create_task(terminal.run())
        async def wait_for_prompt():
            while not terminal.session.app.is_running:
                await asyncio.sleep(0)
        await asyncio.wait_for(wait_for_prompt(), 3)
        pipe.send_text("unsent draft" + sequence)
        async def wait_for_cancellation():
            while not messages:
                await asyncio.sleep(0)
        await asyncio.wait_for(wait_for_cancellation(), 3)
        # EOF exits the next prompt without relying on a second timed Ctrl-C.
        pipe.close()
        await asyncio.wait_for(running, 3)

    assert len(messages) == 1
    assert "Input cancelled" in messages[0]
    assert coordinator.interruptions == 0


def test_prompt_eof_exits_without_message(monkeypatch):
    terminal = PlainTerminal(Stub(), output=DummyOutput())
    messages = []

    async def eof(_prompt, **_kwargs):
        raise EOFError

    monkeypatch.setattr(terminal.session, "prompt_async", eof)
    monkeypatch.setattr(terminal, "_write", messages.append)
    asyncio.run(terminal.run())
    assert messages == []
