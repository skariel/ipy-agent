"""Request phases: admission ownership, FIFO reservation and cancellation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from py_agent.coordinator_lifecycle import ExecutionLifecycle
from py_agent.coordinator_support import State


@pytest.mark.asyncio
async def test_submission_slot_rejects_concurrent_and_fifo_overtaking():
    lifecycle = ExecutionLifecycle(SimpleNamespace())
    lifecycle.state = State.IDLE
    async with lifecycle.submission_slot(queued=False):
        with pytest.raises(RuntimeError, match="busy"):
            async with lifecycle.submission_slot(queued=False):
                pytest.fail("concurrent submission admitted")
    lifecycle._pending_actions.append(SimpleNamespace())
    with pytest.raises(RuntimeError, match="queued"):
        async with lifecycle.submission_slot(queued=False):
            pytest.fail("queued work overtaken")
    async with lifecycle.submission_slot(queued=True):
        assert await lifecycle.boundary_queue_size() == 1


@pytest.mark.asyncio
async def test_submission_slot_releases_lock_on_cancellation():
    lifecycle = ExecutionLifecycle(SimpleNamespace())
    lifecycle.state = State.IDLE
    admitted = asyncio.Event()

    async def request():
        async with lifecycle.submission_slot(queued=False):
            admitted.set()
            await asyncio.Future()

    task = asyncio.create_task(request())
    await admitted.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with lifecycle.submission_slot(queued=False):
        assert lifecycle.state is State.IDLE


@pytest.mark.asyncio
async def test_boundary_budget_preserves_fifo_without_absorbing_new_arrivals():
    lifecycle = ExecutionLifecycle(SimpleNamespace())
    first, second, late = SimpleNamespace(), SimpleNamespace(), SimpleNamespace()
    lifecycle._pending_actions.extend((first, second))
    budget = await lifecycle.boundary_queue_size()
    lifecycle._pending_actions.append(late)
    drained = [await lifecycle.take_boundary_action() for _ in range(budget)]
    assert drained == [first, second]
    assert await lifecycle.take_boundary_action() is late
    assert await lifecycle.take_boundary_action() is None


@pytest.mark.asyncio
async def test_agent_turn_interprets_once_and_retains_last_execution():
    from py_agent.builtin_services import BuiltinPlugin
    from py_agent.contracts import (
        AgentDecision,
        ExecutionResult,
        ExecutorCapabilities,
        ModelResponse,
    )
    from py_agent.coordinator import Coordinator
    from py_agent.plugins import PluginRuntime

    class Provider:
        model = "fake/phases"

        async def generate(self, request):
            return ModelResponse("value = 1")

    class Interpreter:
        def __init__(self):
            self.calls = 0

        def interpret(self, response):
            self.calls += 1
            return AgentDecision("execute", source=response.text)

    class Executor:
        capabilities = ExecutorCapabilities(persistent=True, interrupt=True)

        async def start(self):
            pass

        async def execute(self, request):
            # Deliberately non-final: the step-limit submission must retain
            # the last executed request, not just the accumulated results.
            return ExecutionResult(request.origin, "success")

        async def close(self):
            pass

    coordinator = Coordinator(
        PluginRuntime.load(builtins={"builtin": BuiltinPlugin()}),
        router="default",
        interpreter="basic",
        provider="fake",
        executor="local",
        max_agent_steps=1,
    )
    interpreter = Interpreter()
    coordinator.provider, coordinator.interpreter, coordinator.executor = (
        Provider(),
        interpreter,
        Executor(),
    )
    await coordinator.start()
    try:
        submission = await coordinator.submit("terminal", "calculate")
        assert interpreter.calls == 1
        assert len(submission.executions) == 1
        assert submission.execution is submission.executions[0][0]
        assert submission.result is submission.executions[0][1]
        assert coordinator.state is State.IDLE
    finally:
        await coordinator.close()
