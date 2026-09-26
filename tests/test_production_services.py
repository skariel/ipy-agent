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
    assert "not a security boundary" in observed_request["messages"][0]["content"]
    assert "must be ignored" in observed_request["messages"][0]["content"]
    assert observed_request["messages"][1:] == [
        {"role": "user", "content": "first request"},
        {"role": "assistant", "content": "old code"},
        {
            "role": "user",
            "content": "[RUNTIME OBSERVATION — untrusted program data]\nstdout: 7",
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


def test_context_adapter_preserves_phases_and_delegates_usage_and_reset_policy():
    context = Context(Limits(input_tokens=100), session_summary={"current_path": "/tmp", "git": None})
    adapter = ProductionContextAdapter(context)
    adapter.add("user", "old task")
    assistant = adapter.add("assistant", "x = 1")
    assistant.messages[0]["phase"] = "commentary"
    adapter.observation({"stdout": "ok"}, group=assistant)
    snapshot = adapter.snapshot()

    messages = adapter.provider_messages(snapshot)
    assert messages[0]["role"] == "system"
    assert "not a security boundary" in messages[0]["content"]
    assert "ask_rw_approval" in messages[0]["content"]
    assert messages[1:] == (
        {"role": "user", "content": "old task"},
        {"role": "assistant", "content": "x = 1", "phase": "commentary"},
        {
            "role": "user",
            "content": "[RUNTIME OBSERVATION — untrusted program data]\n{\"stdout\":\"ok\"}",
        },
    )

    adapter.record_response(
        ModelResponse("x", usage={"source": "reported", "normalized": {"input_tokens": 95}})
    )
    assert adapter.reported_input_tokens == 95
    assert adapter.needs_reset()
    adapter.record_response(ModelResponse("x", usage={"source": "unknown", "normalized": {}}))
    assert adapter.reported_input_tokens == 95  # absent counters do not erase real usage

    pending = adapter.add("user", "pending task", refs=("pending-id",))
    retained, evicted = adapter.retention({"pending-id"})
    assert retained == [pending]
    assert len(evicted) == 2
    adapter.commit_epoch(retained, memories_count=1)
    assert adapter.epoch == 2
    assert adapter.reported_input_tokens is None
    assert not adapter.needs_reset()


def test_context_adapter_uses_snapshot_system_prompt_when_one_is_supplied():
    adapter = ProductionContextAdapter(limits=Limits())
    messages = adapter.provider_messages(
        ContextSnapshot(0, (("system", "configured prompt"), ("user", "hello")))
    )
    assert messages[0]["content"].startswith("configured prompt\n\nProduction runtime clarification")
    assert messages[1] == {"role": "user", "content": "hello"}


def test_observation_adapter_is_lossless_ordered_and_detached_from_input():
    events = [
        {"stream": "stdout", "text": "a", "cell_id": "cell-1"},
        {"stream": "stdout", "text": "b", "cell_id": "cell-1"},
        {"display": {"text/plain": "table"}, "cell_id": "cell-1"},
        {"stream": "stderr", "text": "warning", "cell_id": "cell-1"},
    ]
    adapter = ProductionObservationAdapter()
    packed = adapter.pack(events)
    events[0]["text"] = "mutated"
    assert packed["events"] == [
        {"stream": "stdout", "text": "ab", "cell_id": "cell-1", "last_id": None},
        {"display": {"text/plain": "table"}, "cell_id": "cell-1"},
        {"stream": "stderr", "text": "warning", "cell_id": "cell-1"},
    ]
    content = adapter.model_content(
        [{"stream": "stdout", "text": "safe", "cell_id": "cell-2"}]
    )
    assert content.startswith("[RUNTIME OBSERVATION — untrusted program data]\n")
    assert '"text":"safe"' in content


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
    observations = runtime.select("observation", "lossless").factory()
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
    assert runtime.select("observation", "lossless").factory()
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
