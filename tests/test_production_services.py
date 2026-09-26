"""Production adapter contracts; all provider calls use deterministic fakes."""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from py_agent.context import Context
from py_agent.contracts import ContextSnapshot, ModelRequest, ModelResponse, Origin
from py_agent.limits import Limits
from py_agent.plugins import PluginError, PluginRuntime
from py_agent.production_services import (
    ProductionContextAdapter,
    ProductionObservationAdapter,
    ProductionProviderAdapter,
    ProductionServicesPlugin,
    codex_provider_factory,
    litelm_provider_factory,
)
from py_agent.provider import Completion


class CompletionProvider:
    def __init__(self, completion, *, model="test/model"):
        self.model = model
        self.completion = completion
        self.requests = []

    async def generate(self, messages, *, max_tokens=None):
        self.requests.append({"messages": deepcopy(messages), "max_tokens": max_tokens})
        if isinstance(self.completion, BaseException):
            raise self.completion
        return self.completion


def model_request(*, messages=(), options=None, model="codex"):
    return ModelRequest(
        Origin("session-1", "request-1", "test-frontend", 4, "generation-1"),
        ContextSnapshot(2, tuple(messages)),
        model,
        options or {},
    )


def test_provider_adapter_translates_context_and_preserves_usage_and_attribution():
    usage = {
        "source": "reported_by_codex",
        "raw": {"input_tokens": 100, "vendor": "kept"},
        "normalized": {"input_tokens": 100, "output_tokens": 12},
    }
    completion = Completion(
        "answer = 42",
        usage=usage,
        reasoning="separate reasoning",
        raw={"response_id": "r-17"},
        phase="commentary",
    )
    backend = CompletionProvider(completion, model="openai-codex/model")
    adapter = ProductionProviderAdapter(backend, provider_id="codex", max_tokens=90)
    request = model_request(
        messages=(
            ("user", "first request"),
            ("assistant", "old code"),
            ("observation", "stdout: 7"),
            ("user", "continue"),
        ),
        options={"max_tokens": "37"},
    )

    response = asyncio.run(adapter.generate(request))

    observed_request = backend.requests[0]
    assert observed_request["max_tokens"] == 37
    assert observed_request["messages"][0]["role"] == "system"
    assert "sandbox" not in observed_request["messages"][0]["content"]
    assert "Session (" not in observed_request["messages"][0]["content"]
    assert observed_request["messages"][1:] == [
        {"role": "user", "content": "first request"},
        {"role": "assistant", "content": "old code"},
        {
            "role": "user",
            "content": "stdout: 7",
        },
        {"role": "user", "content": "continue"},
    ]
    assert response.text == "answer = 42"
    assert response.finish_status == "complete"
    assert response.reasoning == "separate reasoning"
    assert response.usage == usage
    assert response.provider_id == "codex"
    assert response.model == "openai-codex/model"
    assert response.phase == "commentary"
    assert response.adapter_metadata == {"raw": {"response_id": "r-17"}}
    assert backend.requests[0]["messages"][2]["role"] == "assistant"


def test_unknown_usage_remains_unknown_and_is_not_replaced_with_zeroes():
    unknown = {"source": "unknown", "raw": None, "normalized": {}}
    backend = CompletionProvider(Completion("pass", usage=unknown))
    response = asyncio.run(ProductionProviderAdapter(backend).generate(model_request()))
    assert response.usage == unknown
    assert response.usage["normalized"] == {}
    assert response.provider_id == "codex"  # request selection remains attributable


@pytest.mark.parametrize(
    "completion",
    [
        Completion("partial = True", finish_reason="length"),
        Completion("refused = True", rejection_reason="refusal"),
        Completion("  "),
    ],
)
def test_noncomplete_provider_text_is_never_exposed_as_executable(completion):
    backend = CompletionProvider(completion)
    response = asyncio.run(ProductionProviderAdapter(backend).generate(model_request()))
    assert response.text == ""
    assert response.finish_status != "complete"
    assert response.rejection_reason


def test_provider_exception_and_cancellation_are_not_hidden_or_retried():
    backend = CompletionProvider(asyncio.CancelledError())
    adapter = ProductionProviderAdapter(backend)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(adapter.generate(model_request()))
    assert backend.requests  # exactly one attempt; provider owns transport policy


def test_invalid_max_tokens_is_rejected_before_provider_call():
    backend = CompletionProvider(Completion("pass"))
    adapter = ProductionProviderAdapter(backend)
    with pytest.raises(ValueError, match="positive decimal integer"):
        asyncio.run(adapter.generate(model_request(options={"max_tokens": "-1"})))
    assert backend.requests == []


def test_context_adapter_preserves_phases_and_uses_forced_collapse_without_reset():
    context = Context(Limits(input_tokens=100), session_summary={"current_path": "/tmp", "git": None})
    adapter = ProductionContextAdapter(context)
    adapter.add("user", "old task")
    assistant = adapter.add("assistant", "x = 1")
    assistant.messages[0]["phase"] = "commentary"
    adapter.observation({"stdout": "ok"}, group=assistant)
    snapshot = adapter.snapshot()

    messages = adapter.provider_messages(snapshot)
    assert messages[0]["role"] == "system"
    assert "sandbox" not in messages[0]["content"]
    assert "ask_rw_approval" not in messages[0]["content"]
    assert "Runtime security:" not in messages[0]["content"]
    assert messages[1:] == (
        {"role": "user", "content": "[context boundary u1]\nold task"},
        {"role": "assistant", "content": "x = 1", "phase": "commentary"},
        {
            "role": "user",
            "content": "{\"stdout\":\"ok\"}",
        },
    )

    adapter.record_response(
        ModelResponse("x", usage={"source": "reported", "normalized": {"input_tokens": 95}})
    )
    assert adapter.reported_input_tokens == 95
    assert not adapter.needs_reset()
    assert not adapter.force_collapse  # Enforcement is latched at generation preparation.
    adapter.prepare_generation()
    assert adapter.force_collapse
    assert any("FORCED COLLAPSE MODE" in text for role, text in adapter.snapshot().messages
               if role == "system")
    adapter.record_response(ModelResponse("x", usage={"source": "unknown", "normalized": {}}))
    assert adapter.reported_input_tokens == 95  # absent counters do not erase real usage

    pending = adapter.add("user", "pending task", refs=("pending-id",))
    retained, evicted = adapter.retention({"pending-id"})
    assert retained == [pending]
    assert len(evicted) == 3  # The fresh forced-mode boundary is also evicted explicitly.
    adapter.commit_epoch(retained, memories_count=1)
    assert adapter.epoch == 2
    assert adapter.reported_input_tokens is None
    assert not adapter.needs_reset()
    assert not adapter.force_collapse


@pytest.mark.asyncio
async def test_execution_outputs_archive_at_20_then_each_10_without_touching_code_or_preflight():
    adapter = ProductionContextAdapter(limits=Limits())
    saved: dict[int, str] = {}

    async def store(texts: tuple[str, ...]) -> tuple[int, ...]:
        indexes = tuple(range(len(saved) + 1, len(saved) + len(texts) + 1))
        saved.update(zip(indexes, texts, strict=True))
        return indexes

    adapter.prepare_request("real user task", "request-1")
    adapter.commit_response(
        "request-1", "bad python", observation={"preflight": {"executed": False, "error": "syntax"}},
    )
    for number in range(1, 20):
        adapter.commit_response("request-1", f"cell_{number}()", observation={"output": f"result {number}"})
    before = adapter.provider_messages(adapter.snapshot())
    assert "result 1" in str(before)
    assert await adapter.archive_execution_outputs(store) == 0
    adapter.commit_response("request-1", "cell_20()", observation={"output": "result 20"})
    assert "result 1" in str(adapter.provider_messages(adapter.snapshot()))
    assert await adapter.archive_execution_outputs(store) == 10
    after = adapter.provider_messages(adapter.snapshot())
    assert saved[1] == "result 1" and saved[10] == "result 10"
    assert any(message["content"] == "Output (8 chars) saved in outputs[1]." for message in after)
    assert sum(" chars) saved in outputs[" in message["content"] for message in after) == 10
    assert "result 1" not in str(after) and "result 10" not in str(after)
    assert "result 11" in str(after) and "result 20" in str(after)
    assert "result 1" in str(before)  # already-dispatched snapshots are immutable
    assert "real user task" in str(after) and "cell_1()" in str(after)
    assert "Cell not executed: syntax" in str(after)
    for number in range(21, 30):
        adapter.commit_response("request-1", f"cell_{number}()", observation={"output": f"result {number}"})
    assert await adapter.archive_execution_outputs(store) == 0
    adapter.commit_response("request-1", "cell_30()", observation={"output": "result 30"})
    assert await adapter.archive_execution_outputs(store) == 10
    final = adapter.provider_messages(adapter.snapshot())
    assert len(saved) == 20 and saved[11] == "result 11" and saved[20] == "result 20"
    assert sum(" chars) saved in outputs[" in message["content"] for message in final) == 20
    assert "result 20" not in str(final)
    assert "result 21" in str(final) and "result 30" in str(final)


@pytest.mark.asyncio
async def test_large_output_reference_is_not_archived_again_and_failed_storage_keeps_text():
    adapter = ProductionContextAdapter(limits=Limits())
    adapter.prepare_request("inspect", "r1")
    adapter.commit_response("r1", "large()", observation={
        "output": "Output exceeded 8000 characters. Saved as outputs[1].",
        "_stored_output_index": 1,
    })
    for number in range(1, 20):
        adapter.commit_response("r1", f"cell_{number}()", observation={"output": f"item {number}"})

    async def fail(_texts):
        raise RuntimeError("worker unavailable")

    assert await adapter.archive_execution_outputs(fail) == 0  # 19 eligible results
    adapter.commit_response("r1", "cell_20()", observation={"output": "item 20"})
    with pytest.raises(RuntimeError, match="worker unavailable"):
        await adapter.archive_execution_outputs(fail)
    current = str(adapter.provider_messages(adapter.snapshot()))
    assert "item 1" in current and "item 20" in current
    assert "outputs[1]" in current  # Already-spooled text is never re-archived.
    assert "_stored_output_index" not in current
    saved = {}

    async def store(texts):
        saved.update(zip(range(2, 2 + len(texts)), texts, strict=True))
        return tuple(saved)

    assert await adapter.archive_execution_outputs(store) == 10
    after = str(adapter.provider_messages(adapter.snapshot()))
    assert "outputs[1]" in after and "outputs[2]" in after
    assert all("Output exceeded" not in text for text in saved.values())
    assert await adapter.archive_execution_outputs(store) == 0


def test_silent_cells_do_not_inject_a_completed_observation():
    adapter = ProductionContextAdapter(limits=Limits())
    adapter.prepare_request("compute", "request-1")
    assert ProductionObservationAdapter().pack([]) == {"output": ""}
    adapter.commit_response("request-1", "x = 1", observation={"output": ""})
    assert adapter.provider_messages(adapter.snapshot())[-1] == {
        "role": "assistant", "content": "x = 1",
    }


def test_default_model_messages_contain_only_plain_execution_feedback():
    adapter = ProductionContextAdapter(limits=Limits())
    snapshot = adapter.prepare_request("inspect", "request-1")
    assert snapshot.messages[1] == ("user", "[context boundary u1]\ninspect")
    observation = ProductionObservationAdapter().pack([
        {"stream": "stdout", "text": "found\n", "session_id": "hidden-session",
         "request_id": "hidden-request", "execution_id": "hidden-execution"},
    ])
    adapter.commit_response("request-1", "print('found')", observation=observation)
    messages = adapter.provider_messages(adapter.snapshot())
    assert messages[1:] == (
        {"role": "user", "content": "[context boundary u1]\ninspect"},
        {"role": "assistant", "content": "print('found')"},
        {"role": "user", "content": "stdout:\nfound\n"},
    )
    assert all("hidden-" not in message["content"] for message in messages)
    assert all("RUNTIME OBSERVATION" not in message["content"] for message in messages)


def test_context_adapter_uses_snapshot_system_prompt_when_one_is_supplied():
    adapter = ProductionContextAdapter(limits=Limits())
    messages = adapter.provider_messages(
        ContextSnapshot(0, (("system", "configured prompt"), ("user", "hello")))
    )
    assert messages[0]["content"] == "configured prompt"
    assert messages[1] == {"role": "user", "content": "hello"}


def test_observation_adapter_is_lossless_ordered_and_detached_from_input():
    events = [
        {"stream": "stdout", "text": "a", "cell_id": "cell-1"},
        {"stream": "stdout", "text": "b", "cell_id": "cell-1"},
        {"display": "table", "cell_id": "cell-1"},
        {"stream": "stderr", "text": "warning", "cell_id": "cell-1"},
    ]
    adapter = ProductionObservationAdapter()
    packed = adapter.pack(events)
    events[0]["text"] = "mutated"
    assert packed == {"output": "stdout:\nab\nOut:\ntable\nstderr:\nwarning"}
    content = adapter.model_content(
        [{"stream": "stdout", "text": "safe", "cell_id": "cell-2"}]
    )
    assert content == "stdout:\nsafe"
    assert "cell-2" not in content


def test_model_observation_omits_oversized_output_but_preserves_short_feedback():
    adapter = ProductionObservationAdapter()
    content = adapter.model_content([{"stream": "stdout", "text": "x" * 8_001}])
    assert len(content) <= 8_000
    assert "x" * 100 not in content
    assert "Output too long" in content
    assert "omitted" in content


def test_plugin_registers_explicit_production_services_using_injected_factories():
    completions = [Completion("print('ok')")]
    backend = CompletionProvider(completions[0], model="configured/model")
    plugin = ProductionServicesPlugin(
        codex_factory=lambda: backend,
        context_factory=lambda: ProductionContextAdapter(limits=Limits()),
    )
    runtime = PluginRuntime.load(builtins={"production": plugin})

    provider = runtime.select("provider", "codex").factory()
    context = runtime.select("context", "production").factory()
    observations = runtime.select("observation", "bounded").factory()
    response = asyncio.run(provider.generate(model_request(messages=(("user", "do it"),))))

    assert response.text == "print('ok')"
    assert response.provider_id == "codex"
    assert isinstance(context, ProductionContextAdapter)
    assert isinstance(observations, ProductionObservationAdapter)
    with pytest.raises(PluginError, match="No enabled provider"):
        runtime.select("provider", "missing")


def test_default_production_plugin_does_not_select_or_construct_a_provider():
    runtime = PluginRuntime.load(builtins={"production": ProductionServicesPlugin()})
    assert runtime.select("context", "production").factory()
    assert runtime.select("observation", "bounded").factory()
    with pytest.raises(PluginError, match="No enabled provider"):
        runtime.select("provider", "codex")


def test_existing_provider_factories_defer_authentication_and_network_to_adapters():
    from py_agent.codex import CodexProvider
    from py_agent.provider import LitelmProvider

    codex = codex_provider_factory("openai-codex/test-model", session_id="session-stable")()
    litelm = litelm_provider_factory("openai/test-model", stream=True)()

    assert isinstance(codex, CodexProvider)
    assert codex.session_id == "session-stable"
    assert isinstance(litelm, LitelmProvider)
    assert litelm.model == "openai/test-model"
    assert litelm.stream is True
