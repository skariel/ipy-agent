"""Session llm is explicit, bounded, host-backed, and data-only."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from py_agent.context import CONTRACT
from py_agent.contracts import ExecutionRequest, ModelResponse, Origin
from py_agent.local_executor import LocalExecutor
from py_agent.stdlib import _payload, llm


def origin():
    return Origin("session", "request", "terminal", 1, "generation", "execution")


def test_system_prompt_documents_session_library():
    for helper in ("say(", "preview(", "read_output(", "collapse(", "llm("):
        assert helper in CONTRACT
    assert "fresh conversation" in CONTRACT
    assert "never executed automatically" in CONTRACT


def test_llm_requires_active_execution():
    with pytest.raises(RuntimeError, match="active py execution"):
        llm("hello")


@pytest.mark.parametrize("kwargs", [
    {"prompt": ""}, {"prompt": "x" * 65537}, {"prompt": "ok", "max_tokens": True},
    {"prompt": "ok", "max_tokens": 0}, {"prompt": "ok", "images": ["path.png"]},
])
def test_llm_payload_limits(kwargs):
    with pytest.raises((ValueError, TypeError)):
        _payload(**kwargs)


@pytest.mark.asyncio
async def test_worker_llm_returns_text_persists_variables_and_imports():
    executor = LocalExecutor()
    calls = []
    async def handler(payload):
        calls.append(payload)
        return "answer; NOT executable"
    await executor.start()
    try:
        result = await executor.execute(ExecutionRequest(
            origin(), "answer = llm('hello')\nprint(answer)", "user", llm_handler=handler))
        assert result.status == "success", result.error
        assert "NOT executable" in result.stdout
        assert calls[0]["prompt"] == "hello"
        result = await executor.execute(ExecutionRequest(
            replace(origin(), execution_id="next"),
            "from py_agent.stdlib import llm as ask\nprint(answer)\nprint(ask('next'))",
            "user", llm_handler=handler))
        assert result.status == "success", result.error
        assert len(calls) == 2
        result = await executor.execute(ExecutionRequest(
            replace(origin(), execution_id="no-handler"), "llm('unavailable')", "user"))
        assert result.status == "error"
        assert "no host model handler" in str(result.error)
        assert len(calls) == 2
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_worker_llm_images_limits_and_secret_free_failure():
    executor = LocalExecutor()
    calls = []
    async def handler(payload):
        calls.append(payload)
        if payload["prompt"] == "fail":
            raise RuntimeError("private credential")
        return "text"
    await executor.start()
    try:
        result = await executor.execute(ExecutionRequest(origin(),
            "from PIL import Image\nprint(llm('image', images=[Image.new('RGB', (3, 2))]))",
            "user", llm_handler=handler))
        assert result.status == "success", result.error
        assert calls[0]["images"][0]["width"] == 3
        result = await executor.execute(ExecutionRequest(replace(origin(), execution_id="fail"),
            "llm('fail')", "user", llm_handler=handler))
        assert result.status == "error"
        assert "private credential" not in str(result.error)
        result = await executor.execute(ExecutionRequest(replace(origin(), execution_id="limit"),
            "[llm('call') for _ in range(9)]", "user", llm_handler=handler))
        assert result.status == "error"
        assert "per-cell call limit" in str(result.error)
        assert len(calls) == 10  # image + failure + eight accepted subcalls
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_worker_llm_interrupt_cancels_host_call_and_recovers():
    executor = LocalExecutor()
    active = asyncio.Event()
    cancelled = asyncio.Event()
    async def handler(payload):
        active.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    await executor.start()
    task = asyncio.create_task(executor.execute(ExecutionRequest(
        origin(), "llm('wait')", "user", llm_handler=handler)))
    try:
        await asyncio.wait_for(active.wait(), 3)
        await executor.interrupt()
        result = await asyncio.wait_for(task, 3)
        assert cancelled.is_set()
        assert result.status in {"interrupted", "cancelled"}
        result = await executor.execute(ExecutionRequest(replace(origin(), execution_id="next"), "print('alive')", "user"))
        assert result.status == "success"
        assert "alive" in result.stdout
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await executor.close()


@pytest.mark.asyncio
async def test_coordinator_llm_uses_current_model_effort_but_fresh_context(tmp_path):
    from py_agent import cli
    from py_agent.contracts import ModelResponse
    from py_agent.session_journal import NoPersistenceJournal

    class RecordingJournal(NoPersistenceJournal):
        def __init__(self):
            self.requests, self.usages = [], []
        def record_model_request(self, request):
            self.requests.append(request)
        def record_provider_usage(self, request, response, *, outcome):
            self.usages.append((request, response, outcome))
    class Provider:
        model = "openai/gpt-4o"
        calls = []
        async def generate(self, request):
            self.calls.append(request)
            return ModelResponse("useful prose, not Python", model=self.model, usage={"input_tokens": 12})
    journal = RecordingJournal()
    coordinator = cli._build_coordinator("litelm", model="openai/gpt-4o", journal=journal)
    provider = Provider()
    coordinator.provider = provider
    coordinator._effort_override = "high"
    coordinator.context_service.add("user", "PRIVATE AGENT HISTORY")
    await coordinator.start()
    try:
        submission = await coordinator.submit("terminal", "@answer = llm('Summarize supplied text')\nprint(answer)")
        assert submission.result.status == "success", submission.result.error
        assert "useful prose" in submission.result.stdout
        assert len(provider.calls) == 1
        request = provider.calls[0]
        assert request.model == "openai/gpt-4o"
        assert request.options["effort"] == "high"
        assert request.options["max_tokens"] == "2048"
        assert len(request.context.messages) == 2
        assert request.context.messages[-1] == ("user", "Summarize supplied text")
        assert "PRIVATE AGENT HISTORY" not in str(request.context.messages)
        assert "Reply with exactly one Python" not in str(request.context.messages)
        assert journal.requests == [request]
        assert journal.usages[0][2] == "returned"
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_agent_can_call_llm_and_only_returned_text_is_data():
    from py_agent import cli
    class Provider:
        model = "openai/gpt-4o"
        calls = []
        async def generate(self, request):
            self.calls.append(request)
            if len(self.calls) == 1:
                return ModelResponse("answer = llm('Give a greeting')\nsay(answer, final=True)")
            return ModelResponse("Hello from the nested model!")
    coordinator = cli._build_coordinator("litelm", model="openai/gpt-4o")
    provider = Provider()
    coordinator.provider = provider
    await coordinator.start()
    try:
        submission = await coordinator.submit("terminal", "Call an LLM")
        assert submission.result.status == "success", submission.result.error
        assert submission.result.final
        assert submission.say_outputs[0].content == "Hello from the nested model!"
        assert len(provider.calls) == 2
        assert len(provider.calls[1].context.messages) == 2
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_production_nested_llm_accepts_prose_and_honors_tokens(monkeypatch, tmp_path):
    import litelm

    from py_agent import cli
    from py_agent.production_services import ProductionProviderAdapter
    from py_agent.provider import LitelmProvider
    captured = []
    async def complete(**kwargs):
        captured.append(kwargs)
        return {"choices": [{"message": {"role": "assistant", "content": "This is prose."},
                             "finish_reason": "stop"}]}
    monkeypatch.setattr(litelm, "acompletion", complete)
    coordinator = cli._build_coordinator("litelm", model="openai/gpt-4o")
    coordinator.provider = ProductionProviderAdapter(
        LitelmProvider("openai/gpt-4o", stream=False, auth_file=tmp_path / "absent.json"),
        provider_id="litelm")
    await coordinator.start()
    try:
        submission = await coordinator.submit("terminal", "@print(llm('Explain', max_tokens=123))")
        assert submission.result.status == "success", submission.result.error
        assert "This is prose." in submission.result.stdout
        assert captured[0]["max_tokens"] == 123
        assert len(captured[0]["messages"]) == 2
    finally:
        await coordinator.close()
