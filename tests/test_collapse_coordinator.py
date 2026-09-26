"""Host-owned collapse integration with deterministic providers and real-worker probes."""
from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from py_agent.builtin_services import BuiltinPlugin
from py_agent.context import COLLAPSE_REMINDER
from py_agent.contracts import (
    ExecutionResult,
    ExecutorCapabilities,
    ExecutorCapabilityError,
    ModelResponse,
    SayOutput,
)
from py_agent.coordinator import Coordinator, State
from py_agent.limits import Limits
from py_agent.local_executor import LocalExecutor
from py_agent.plugins import PluginRuntime
from py_agent.production_services import ProductionContextAdapter, ProductionObservationAdapter
from py_agent.session_journal import SQLiteSessionJournal


class ScriptProvider:
    model = "offline/collapse"

    def __init__(self, script):
        self.script = script
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        result = self.script(len(self.requests), request)
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, ModelResponse) else ModelResponse(result)


class ArchiveExecutor:
    capabilities = ExecutorCapabilities(persistent=True, interrupt=True)

    def __init__(self):
        self.requests = []
        self.archives = {}

    async def start(self):
        pass

    async def execute(self, request):
        self.requests.append(request)
        final = request.source == "say('done', final=True)"
        return ExecutionResult(
            request.origin, "success", stdout="evidence " * 100,
            final=final, say_outputs=(SayOutput("done", final=True),) if final else (),
        )

    async def store_collapsed(self, text):
        index = len(self.archives) + 1
        self.archives[index] = text
        return index

    async def interrupt(self):
        pass

    async def close(self):
        pass


def make_coordinator(script, *, context=None, executor=None, journal=None):
    context = context or ProductionContextAdapter()
    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
        context=context, observations=ProductionObservationAdapter(), journal=journal,
    )
    provider = ScriptProvider(script)
    executor = executor or ArchiveExecutor()
    coordinator.provider, coordinator.executor = provider, executor
    return coordinator, provider, executor, context


def joined(request):
    return "\n".join(text for _, text in request.context.messages)


def seed(context):
    context.prepare_request("previous investigation " * 100, "previous-request")
    context.commit_response("previous-request", "print('old evidence')", observation={"output": "old evidence " * 100})


@pytest.mark.asyncio
async def test_forced_mode_blocks_code_until_collapse_then_keeps_only_receipt():
    summary = "UNIQUE_WORKING_MEMORY: investigate parser handling."
    source = f"collapse('u1', 'm1', {summary!r})"

    def script(step, request):
        if step == 1:
            return ModelResponse("print('first')", usage={"normalized": {"input_tokens": 96}})
        if step == 2:
            assert "FORCED COLLAPSE MODE" in joined(request)
            assert "[context boundary m1]" in joined(request)
            return "say('done', final=True)"  # Must not dispatch despite being valid Python.
        if step == 3:
            assert "FORCED COLLAPSE MODE" in joined(request)
            assert "collapse cell rejected" in joined(request)
            return source
        assert step == 4
        text = joined(request)
        assert not any(role == "system" and content.startswith("FORCED COLLAPSE MODE:")
                       for role, content in request.context.messages)
        assert source not in text
        assert text.count(summary) == 1
        assert ("assistant", "Collapsed [u1, m1). Originals retained in collapsed[1].") in request.context.messages
        return "say('done', final=True)"

    context = ProductionContextAdapter(limits=Limits(input_tokens=100))
    coordinator, provider, executor, context = make_coordinator(script, context=context)
    await coordinator.start()
    try:
        result = await coordinator.submit("terminal", "Investigate the parser " * 100)
        assert result.result.final
        assert [item.source for item in executor.requests] == ["print('first')", "say('done', final=True)"]
        assert len(provider.requests) == 4
        archive = json.loads(executor.archives[1])
        assert archive["start_id"] == "u1" and archive["end_id"] == "m1"
        assert any(message["content"] == "print('first')"
                   for group in archive["groups"] for message in group["messages"])
        assert not context.force_collapse
        assert context.reported_input_tokens is None
        assert all(request.context.epoch == 1 for request in provider.requests)
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_normal_collapse_preserves_exact_user_end_and_journals_original_source(tmp_path):
    summary = "ONE_SUMMARY: previous investigation completed."
    source = f"collapse('u1', 'u2', {summary!r})"
    active = "Keep this request exactly.\nTrailing spaces:  "
    context = ProductionContextAdapter()
    seed(context)
    journal = SQLiteSessionJournal(tmp_path / "collapse.sqlite3")

    def script(step, request):
        if step == 1:
            assert "[context boundary u2]\n" + active in joined(request)
            return source
        assert step == 2
        text = joined(request)
        assert text.count(summary) == 1 and source not in text
        assert any(role == "user" and content.endswith("\n\n" + active)
                   and content.startswith("[context boundary u1]")
                   for role, content in request.context.messages)
        assert "[context boundary u2]" not in text
        return "say('done', final=True)"

    coordinator, provider, executor, _ = make_coordinator(script, context=context, journal=journal)
    await coordinator.start()
    try:
        result = await coordinator.submit("terminal", active)
        assert result.result.final
        assert len(executor.requests) == 1
        assert len(provider.requests) == 2
        records = [json.loads(row[0]) for row in journal.db.execute(
            "SELECT payload FROM events WHERE kind='context_collapse' ORDER BY seq",
        )]
        assert {record["content"]["outcome"] for record in records} == {"requested", "succeeded"}
        assert all(record["content"]["source"] == source for record in records)
        success = next(record for record in records if record["content"]["outcome"] == "succeeded")
        assert success["content"]["detail"] == "Collapsed [u1, u2). Originals retained in collapsed[1]."
        assert success["metadata"]["generation_id"] == provider.requests[0].origin.generation_id
        assert context.reported_input_tokens is None
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_markers_follow_every_ten_completed_cells_and_reminder_follows_fifty_responses():
    def script(step, request):
        markers = [text for _, text in request.context.messages if "[Automatic collapse boundary.]" in text]
        assert len(markers) == (step - 1) // 10
        assert (COLLAPSE_REMINDER in [text for role, text in request.context.messages if role == "system"]) == (step == 51)
        if step in (11, 21, 31, 41, 51):
            index = request.context.messages.index(("user", markers[-1]))
            assert request.context.messages[index - 1][0] == "observation"
            assert request.context.messages[index - 2][0] == "assistant"
        return "say('done', final=True)" if step == 52 else "pass"

    coordinator, provider, executor, _ = make_coordinator(script)
    await coordinator.start()
    try:
        result = await coordinator.submit("terminal", "Complete a long autonomous task")
        assert result.result.final
        assert len(provider.requests) == len(executor.requests) == 52
    finally:
        await coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [
    "collapse('u1', 'u2', 'memory'); print('side effect')",
    "collapse('u1', 'u2', dangerous())",
    "collapse(*('u1', 'u2', 'memory'))",
    "collapse('u1', 'u2', 'memory')\ncollapse('u1', 'u2', 'memory')",
])
async def test_mixed_and_dynamic_collapse_never_dispatch_and_bound_invalid_retries(source):
    coordinator, provider, executor, _ = make_coordinator(lambda _step, _request: source)
    await coordinator.start()
    try:
        result = await coordinator.submit("terminal", "Manage working memory")
        assert len(provider.requests) == 3
        assert not executor.requests and not executor.archives
        assert "3 consecutive invalid" in result.message
        assert coordinator.state is State.IDLE
        for request in provider.requests[1:]:
            assert source not in joined(request)
            assert "three literal strings" in joined(request)
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_forced_rejections_remain_latched_even_when_new_usage_is_lower():
    context = ProductionContextAdapter(limits=Limits(input_tokens=100))
    seed(context)
    context.context.reported_input_tokens = 91

    def script(_step, request):
        assert "FORCED COLLAPSE MODE:" in joined(request)
        return ModelResponse("!touch should-not-exist", usage={"normalized": {"input_tokens": 1}})

    coordinator, provider, executor, _ = make_coordinator(script, context=context)
    await coordinator.start()
    try:
        result = await coordinator.submit("terminal", "Continue investigating")
        assert len(provider.requests) == 3
        assert "3 consecutive invalid" in result.message
        assert not executor.requests and not executor.archives
        assert context.force_collapse
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_queued_steering_preview_id_matches_committed_boundary_and_can_be_collapsed():
    started, release = asyncio.Event(), asyncio.Event()
    context = ProductionContextAdapter()

    async def script(step, request):
        if step == 1:
            started.set()
            await release.wait()
            return "pass"
        if step == 2:
            assert ("user", "[context boundary u2]\nKeep queued steering exact") in request.context.messages
            # Steering is committed before provider generation, with the same ID.
            assert ("user", "[context boundary u2]\nKeep queued steering exact") in context.snapshot().messages
            return "collapse('u1', 'u2', 'Earlier work is complete; follow queued steering.')"
        assert step == 3
        assert any(text.endswith("\n\nKeep queued steering exact") for _, text in request.context.messages)
        assert "[context boundary u2]" not in joined(request)
        return "say('done', final=True)"

    coordinator, _provider, executor, _ = make_coordinator(script, context=context)
    await coordinator.start()
    task = asyncio.create_task(coordinator.submit("terminal", "Initial task " * 100))
    try:
        await asyncio.wait_for(started.wait(), 3)
        ticket = await coordinator.enqueue("terminal", "Keep queued steering exact")
        release.set()
        result = await asyncio.wait_for(task, 3)
        outcome = await asyncio.wait_for(ticket.completion, 3)
        assert result.result.final and outcome.status == "steered"
        assert len(executor.archives) == 1
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await coordinator.close()


@pytest.mark.asyncio
async def test_cancelled_archive_keeps_preexisting_history_unchanged():
    class BlockingArchiveExecutor(ArchiveExecutor):
        def __init__(self):
            super().__init__()
            self.storing = asyncio.Event()

        async def store_collapsed(self, _text):
            self.storing.set()
            await asyncio.Event().wait()

    context = ProductionContextAdapter()
    seed(context)
    original = context.snapshot().messages
    executor = BlockingArchiveExecutor()
    coordinator, _provider, _, _ = make_coordinator(
        lambda _step, _request: "collapse('u1', 'u2', 'Retain earlier findings.')",
        context=context, executor=executor,
    )
    await coordinator.start()
    task = asyncio.create_task(coordinator.submit("terminal", "New active task"))
    try:
        await asyncio.wait_for(executor.storing.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert context.snapshot().messages == original
        assert not context.context.collapsed and not executor.requests
        assert coordinator.state is State.FAILED
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await coordinator.close()


@pytest.mark.asyncio
async def test_start_rejects_missing_archive_capability_before_worker_or_model_dispatch():
    class UnsupportedExecutor(ArchiveExecutor):
        store_collapsed = None

        def __init__(self):
            super().__init__()
            self.started = False

        async def start(self):
            self.started = True

    executor = UnsupportedExecutor()
    coordinator, provider, _, _ = make_coordinator(
        lambda _step, _request: "pass", executor=executor,
    )
    try:
        with pytest.raises(ExecutorCapabilityError, match="store_collapsed"):
            await coordinator.start()
        assert coordinator.state is State.FAILED
        assert not executor.started and not executor.requests and not provider.requests
    finally:
        await coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RuntimeError, ValueError, asyncio.CancelledError])
async def test_archive_failure_is_fatal_and_does_not_dispatch_queued_work(error_type):
    class FailingArchiveExecutor(ArchiveExecutor):
        def __init__(self):
            super().__init__()
            self.storing = asyncio.Event()
            self.release = asyncio.Event()

        async def store_collapsed(self, _text):
            self.storing.set()
            await self.release.wait()
            raise error_type("archive transport failed")

    context = ProductionContextAdapter()
    seed(context)
    original = context.snapshot().messages
    executor = FailingArchiveExecutor()
    coordinator, provider, _, _ = make_coordinator(
        lambda _step, _request: "collapse('u1', 'u2', 'Preserve earlier findings.')",
        context=context, executor=executor,
    )
    await coordinator.start()
    task = asyncio.create_task(coordinator.submit("terminal", "Active task"))
    try:
        await asyncio.wait_for(executor.storing.wait(), 3)
        ticket = await coordinator.enqueue("terminal", "Queued task must not run")
        executor.release.set()
        with pytest.raises(error_type):
            await asyncio.wait_for(task, 3)
        outcome = await asyncio.wait_for(ticket.completion, 3)
        await asyncio.sleep(0)  # Allow the scheduled queue-worker callback to run.
        assert outcome.status == "interrupted" and "unavailable" in outcome.error
        assert coordinator.state is State.FAILED
        assert context.snapshot().messages == original
        assert not context.context.collapsed and not executor.archives and not executor.requests
        assert len(provider.requests) == 1
        with pytest.raises(RuntimeError, match="unavailable"):
            await coordinator.submit("terminal", "Do not regenerate after namespace loss")
    finally:
        executor.release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await coordinator.close()


@pytest.mark.asyncio
async def test_boundary_validation_failure_before_storage_remains_recoverable():
    class NeverStoreExecutor(ArchiveExecutor):
        async def store_collapsed(self, _text):
            pytest.fail("Invalid boundary must be rejected before archive transport")

    def script(step, request):
        if step == 1:
            return "collapse('missing', 'u1', 'A short summary.')"
        assert step == 2
        assert "Unknown or stale collapse boundary ID" in joined(request)
        return "say('done', final=True)"

    coordinator, provider, executor, _ = make_coordinator(script, executor=NeverStoreExecutor())
    await coordinator.start()
    try:
        result = await coordinator.submit("terminal", "A recoverable task")
        assert result.result.final and coordinator.state is State.IDLE
        assert len(provider.requests) == 2
        assert [request.source for request in executor.requests] == ["say('done', final=True)"]
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_real_worker_archive_is_readable_and_collapse_is_not_executed():
    setup = "persisted_value = 42\nprint('retained evidence ' * 100)"
    collapse_source = "collapse('u1', 'm1', 'persisted_value is 42; retain the result.')"
    inspect_source = (
        "import json\n"
        "archive = json.loads(collapsed[1])\n"
        "assert archive['start_id'] == 'u1' and archive['end_id'] == 'm1'\n"
        f"assert any(m['content'] == {setup!r} for g in archive['groups'] for m in g['messages'])\n"
        "assert persisted_value == 42\n"
        "assert 'collapse' not in globals()\n"
        "say('archive verified', final=True)"
    )

    def script(step, request):
        if step == 1:
            return ModelResponse(setup, usage={"normalized": {"input_tokens": 96}})
        if step == 2:
            assert "FORCED COLLAPSE MODE" in joined(request)
            return collapse_source
        assert step == 3
        assert collapse_source not in joined(request)
        return inspect_source

    context = ProductionContextAdapter(limits=Limits(input_tokens=100))
    executor = LocalExecutor()
    coordinator, provider, _, _ = make_coordinator(script, context=context, executor=executor)
    await coordinator.start()
    try:
        result = await asyncio.wait_for(coordinator.submit("terminal", "Preserve real worker data " * 100), 20)
        assert result.result.status == "success" and result.result.final
        assert "archive verified" in result.message
        assert len(provider.requests) == 3
        assert [request.source for request, _result in result.executions] == [setup, inspect_source]
        assert len(context.context.collapsed) == 1
        assert coordinator.state is State.IDLE
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_real_archive_cancellation_stops_worker_and_coordinator_without_history_loss():
    context = ProductionContextAdapter()
    seed(context)
    original = context.snapshot().messages
    executor = LocalExecutor()
    coordinator, provider, _, _ = make_coordinator(
        lambda _step, _request: "collapse('u1', 'u2', 'Preserve earlier evidence.')",
        context=context, executor=executor,
    )
    await coordinator.start()
    sent = asyncio.Event()
    original_send = executor._send

    async def pause_archive_after_send(process, frame):
        await original_send(process, frame)
        if json.loads(frame[4:])["type"] == "store_collapsed":
            sent.set()
            await asyncio.Event().wait()

    executor._send = pause_archive_after_send
    task = asyncio.create_task(coordinator.submit("terminal", "New task"))
    try:
        await asyncio.wait_for(sent.wait(), 5)
        ticket = await coordinator.enqueue("terminal", "Never dispatch after worker loss")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        outcome = await asyncio.wait_for(ticket.completion, 3)
        assert coordinator.state is State.FAILED and outcome.status == "interrupted"
        assert executor.process is None or executor.process.returncode is not None
        assert context.snapshot().messages == original
        assert not context.context.collapsed and len(provider.requests) == 1
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await coordinator.close()
