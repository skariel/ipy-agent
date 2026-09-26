"""Ctrl-C prompt behavior; no provider or worker is started."""
import asyncio

from prompt_toolkit.output import DummyOutput

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


def test_prompt_eof_exits_without_message(monkeypatch):
    terminal = PlainTerminal(Stub(), output=DummyOutput())
    messages = []

    async def eof(_prompt, **_kwargs):
        raise EOFError

    monkeypatch.setattr(terminal.session, "prompt_async", eof)
    monkeypatch.setattr(terminal, "_write", messages.append)
    asyncio.run(terminal.run())
    assert messages == []
