"""Capability discovery, bounded inspection, visible activity, safe recovery."""
from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from py_agent import cli
from py_agent.context import CONTRACT
from py_agent.contracts import ModelResponse
from py_agent.inspection import source
from py_agent.inspection import test_summary as summarize_tests
from py_agent.local_executor import LocalExecutor
from py_agent.provider import ProviderError
from py_agent.runtime_status import Activity
from py_agent.stdlib import HELPERS, helper_prompt, install_helpers, runtime_helpers


def test_prompt_registry_matches_actual_runtime_injection():
    bindings = runtime_helpers(say=lambda *a: None, preview=lambda *a: None,
                               read_output=lambda *a: None, llm=lambda *a: "text")
    namespace = {}
    install_helpers(namespace, bindings)
    assert set(namespace) == set(HELPERS)
    assert helper_prompt() in CONTRACT
    for name, (signature, _) in HELPERS.items():
        assert signature in CONTRACT
        assert callable(namespace[name])
    with pytest.raises(RuntimeError):
        install_helpers({}, {"llm": lambda: None})


def test_source_excerpt_is_bounded_numbered_and_nonexecuting(tmp_path):
    path = tmp_path / "source.py"
    path.write_text("raise RuntimeError('must not execute')\nsecond\n" + "x" * 100000 + "\nfourth\n")
    assert source(path, 2, 2) == "2: second"
    excerpt = source(path, 3, 4, limit=200)
    assert len(excerpt) <= 200
    assert "truncated" in excerpt
    assert source(path, 4, 4) == "4: fourth"
    with pytest.raises(ValueError):
        source(path, 0)


def test_test_summary_uses_retained_results_and_exposes_failures():
    result = subprocess.CompletedProcess(["never rerun"], 1,
        "x" * 10000 + "\nFAILED tests/test_example.py::test_bad\n1 failed, 2 passed\n", "diagnostic")
    summary = summarize_tests(result, limit=400)
    assert summary["returncode"] == 1
    assert summary["failures"] == ["FAILED tests/test_example.py::test_bad"]
    assert "1 failed" in summary["summary"]
    assert summary["truncated"]
    assert len(str(summary)) < 800
    with pytest.raises(TypeError):
        summarize_tests(SimpleNamespace(returncode=None, stdout="", stderr=""))


@pytest.mark.asyncio
async def test_inspection_helpers_are_available_in_real_worker(tmp_path):
    from py_agent.contracts import ExecutionRequest, Origin
    path = tmp_path / "example.py"
    path.write_text("value = 42\n")
    executor = LocalExecutor()
    await executor.start()
    try:
        result = await executor.execute(ExecutionRequest(
            Origin("s", "r", "terminal", 1, "g", "e"),
            f"print(source({str(path)!r}))\n"
            "import subprocess\nr = subprocess.CompletedProcess(['retained'], 1, '1 failed', '')\n"
            "print(test_summary(r)['returncode'])", "user"))
        assert result.status == "success", result.error
        assert "1: value = 42" in result.stdout
    finally:
        await executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_cell", [False, True])
async def test_resume_only_pending_model_request_never_replays_completed_cell(monkeypatch, prior_cell):
    class Provider:
        model = "openai/gpt-4o"
        def __init__(self):
            self.requests = []
            self.recover = False
        async def generate(self, request):
            self.requests.append(request)
            if prior_cell and len(self.requests) == 1:
                return ModelResponse("side_effect_count = globals().get('side_effect_count', 0) + 1\nprint(side_effect_count)")
            if not self.recover:
                raise ProviderError("ReadError", kind="transport")
            return ModelResponse("say(str(globals().get('side_effect_count', 0)), final=True)")
    provider = Provider()
    coordinator = cli._build_coordinator("litelm", model=provider.model)
    coordinator.provider = provider
    async def no_wait(delay, operation):
        pass
    monkeypatch.setattr(coordinator, "_wait_provider_retry", no_wait)
    await coordinator.start()
    try:
        with pytest.raises(ProviderError, match="exhausted"):
            await coordinator.submit("terminal", "Continue the pending task")
        assert coordinator.recovery is not None
        assert coordinator.recovery.executed_cells == int(prior_cell)
        saved = provider.requests[-1].context
        saved_options = dict(provider.requests[-1].options)
        coordinator._effort_override = "high"
        assert coordinator.activity is None
        status = await coordinator._dispatch_command("recovery", coordinator.config_store.snapshot)
        assert "never replayed" in status
        provider.recover = True
        submission = await coordinator.submit("terminal", "/resume")
        assert submission.result.final
        assert submission.say_outputs[-1].content == str(int(prior_cell))
        assert provider.requests[-1].context.messages == saved.messages
        assert provider.requests[-1].context.images == saved.images
        assert dict(provider.requests[-1].options) == saved_options
        assert coordinator.recovery is None
        assert len(submission.executions) == 1  # Only the newly generated cell.
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_nested_activity_is_visible_and_cleared():
    class Provider:
        model = "openai/gpt-4o"
        async def generate(self, request):
            active.set()
            await release.wait()
            return ModelResponse("plain text")
    coordinator = cli._build_coordinator("litelm", model="openai/gpt-4o")
    coordinator.provider = Provider()
    active, release = asyncio.Event(), asyncio.Event()
    await coordinator.start()
    task = asyncio.create_task(coordinator.submit("terminal", "@print(llm('nested'))"))
    try:
        await asyncio.wait_for(active.wait(), 3)
        assert coordinator.activity.label == "LLM subcall"
        assert "attempt 1/5" in coordinator.activity.text()
        from prompt_toolkit.output import DummyOutput

        from py_agent.plain_terminal import PlainTerminal
        terminal = PlainTerminal(coordinator, output=DummyOutput())
        assert "LLM subcall" in str(terminal._toolbar())
        release.set()
        result = await asyncio.wait_for(task, 3)
        assert result.result.status == "success"
        assert coordinator.activity is None
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await coordinator.close()


def test_activity_metadata_contains_no_prompts_or_credentials():
    activity = Activity("LLM subcall retry", 2, 5, retry_at=10)
    assert "attempt 2/5" in activity.text()
    assert "retry in" in activity.text()


@pytest.mark.asyncio
async def test_recovery_discard_and_uncertain_execution_never_replay(monkeypatch):
    from py_agent.contracts import ExecutionResult
    class Provider:
        model = "openai/gpt-4o"
        async def generate(self, request):
            return ModelResponse("print('potential side effect')")
    class Executor:
        calls = 0
        async def start(self):
            pass
        async def execute(self, request):
            self.calls += 1
            return ExecutionResult(request.origin, "uncertain", error="worker gone")
        async def interrupt(self):
            pass
        async def store_collapsed(self, value):
            return 1
        async def close(self):
            pass
    coordinator = cli._build_coordinator("litelm", model="openai/gpt-4o")
    coordinator.provider = Provider()
    executor = Executor()
    coordinator.executor = executor
    await coordinator.start()
    try:
        result = await coordinator.submit("terminal", "Do work")
        assert result.result.status == "uncertain"
        assert coordinator.recovery is None
        assert "side effects may have occurred" in coordinator.execution_outcome
        with pytest.raises(RuntimeError):
            await coordinator.submit("terminal", "/resume")
        assert executor.calls == 1
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_recovery_can_be_discarded_and_new_python_invalidates_it(monkeypatch):
    class Provider:
        model = "openai/gpt-4o"
        async def generate(self, request):
            raise ProviderError("disconnect", kind="transport")
    coordinator = cli._build_coordinator("litelm", model="openai/gpt-4o")
    coordinator.provider = Provider()
    async def no_wait(*args):
        pass
    monkeypatch.setattr(coordinator, "_wait_provider_retry", no_wait)
    await coordinator.start()
    try:
        with pytest.raises(ProviderError):
            await coordinator.submit("terminal", "Do work")
        assert coordinator.recovery is not None
        discard = await coordinator.submit("terminal", "/recovery discard")
        assert coordinator.recovery is None
        assert "discarded" in discard.message
        with pytest.raises(ProviderError):
            await coordinator.submit("terminal", "Another task")
        assert coordinator.recovery is not None
        await coordinator.submit("terminal", "@print('new side effect')")
        assert coordinator.recovery is None
        assert "Last Python cell completed" in coordinator.execution_outcome
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_nested_retry_activity_does_not_leak_task_text(monkeypatch):
    from py_agent import host_stdlib
    active, release = asyncio.Event(), asyncio.Event()
    class Provider:
        model = "openai/gpt-4o"
        calls = 0
        async def generate(self, request):
            self.calls += 1
            if self.calls == 1:
                raise ProviderError("disconnect", kind="transport")
            return ModelResponse("answer")
    coordinator = cli._build_coordinator("litelm", model="openai/gpt-4o")
    coordinator.provider = Provider()
    real_sleep = asyncio.sleep
    async def wait(delay):
        if delay == 1:
            active.set()
            await release.wait()
        else:
            await real_sleep(delay)
    # Patching module asyncio.sleep is global; avoid progress timing assertions.
    monkeypatch.setattr(host_stdlib.asyncio, "sleep", wait)
    await coordinator.start()
    task = asyncio.create_task(coordinator.submit("terminal", "@print(llm('PRIVATE PROMPT'))"))
    try:
        await asyncio.wait_for(active.wait(), 3)
        assert coordinator.activity.label == "LLM subcall retry"
        assert coordinator.activity.attempt == 2
        assert "PRIVATE PROMPT" not in coordinator.activity.text()
        release.set()
        result = await asyncio.wait_for(task, 3)
        assert result.result.status == "success"
        assert coordinator.activity is None
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await coordinator.close()
