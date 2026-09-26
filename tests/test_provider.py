"""No keys, network, provider installation, or pytest-asyncio needed."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import sys
from types import SimpleNamespace

import pytest

from py_agent.provider import (
    Completion, FakeProvider, LitelmProvider, ProviderError, _litelm_failure_kind, normalize_usage,
)

MESSAGES = [{"role": "system", "content": "raw Python only"}, {"role": "user", "content": "go"}]


@pytest.mark.parametrize(
    ("status", "expected"),
    [(400, "request"), (401, "authentication"), (403, "authentication"),
     (408, "timeout"), (413, "overflow"), (429, "rate_limit"),
     (500, "provider"), (503, "provider")],
)
def test_litelm_retry_classification_is_explicit(status, expected):
    error = RuntimeError("credentials must never be logged")
    error.status_code = status
    assert _litelm_failure_kind(error) == expected
    assert _litelm_failure_kind(ValueError("local plugin bug")) == "internal"



def response(content="say('ok', final=True)", *, reason="stop", **message):
    return {
        "choices": [
            {"index": 0, "finish_reason": reason, "message": {"role": "assistant", "content": content, **message}}
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 60, "cache_creation_tokens": 10},
            "completion_tokens_details": {"reasoning_tokens": 7},
            "vendor_field": 99,
        },
    }


def chunk(content=None, *, reason=None, **delta):
    return {"choices": [{"index": 0, "delta": {"content": content, **delta}, "finish_reason": reason}]}


def install(monkeypatch, value):
    calls = []

    async def complete(**kwargs):
        calls.append(deepcopy(kwargs))
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setitem(sys.modules, "litelm", SimpleNamespace(acompletion=complete))
    return calls


class Stream:
    def __init__(self, values, gate=None):
        self.values = iter(values)
        self.gate = gate
        self.started = asyncio.Event()
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        value = next(self.values, None)
        if value is None:
            raise StopAsyncIteration
        if isinstance(value, BaseException):
            raise value
        return value

    async def aclose(self):
        self.closed = True


def test_complete_explicit_context_no_tools_or_hidden_history(monkeypatch):
    raw = response(reasoning_content="private reasoning; not executable")
    calls = install(monkeypatch, SimpleNamespace(model_dump=lambda: raw))
    provider = LitelmProvider("openai/configured", api_base="http://localhost:8000/v1")
    result = asyncio.run(provider.generate(MESSAGES, max_tokens=42))
    assert result.successful
    assert result.text == "say('ok', final=True)"
    assert result.reasoning == "private reasoning; not executable"
    assert result.raw == raw
    assert result.usage["raw"] == raw["usage"]
    assert result.usage["normalized"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "cache_read_tokens": 60,
        "cache_creation_tokens": 10,
        "reasoning_tokens": 7,
    }
    assert calls == [
        {
            "model": "openai/configured",
            "api_base": "http://localhost:8000/v1",
            "messages": MESSAGES,
            "max_tokens": 42,
            "stream": False,
            "num_retries": 0,
        }
    ]
    raw["choices"].clear()
    assert len(result.raw["choices"]) == 1  # audit record was copied


@pytest.mark.parametrize(
    "raw",
    [
        response(reason="length"),
        response(reason="content_filter"),
        response(reason="tool_calls"),
        response(reason=None),
        response(""),
        response(" \n"),
        response(None),
        response(tool_calls=[{"id": "t"}]),
        response(function_call={"name": "exec"}),
        response(refusal="I refuse"),
        response(images=["image"]),
        response(audio={"data": "audio"}),
        response([{"type": "text", "text": "x=1"}]),
        response(reasoning_content={"text": "x"}),
        response(provider_specific_fields={"refusal": "no"}),
        response(role="user"),
        {"choices": []},
        {"choices": [response()["choices"][0]] * 2},
        {"choices": [{"index": 1, "message": {"role": "assistant", "content": "x=1"}, "finish_reason": "stop"}]},
        {"error": "server error", **response()},
    ],
)
def test_bad_responses_never_successful(monkeypatch, raw):
    install(monkeypatch, raw)
    result = asyncio.run(LitelmProvider("fake/model").generate(MESSAGES, max_tokens=10))
    assert not result.successful
    assert result.raw == raw


def test_unknown_usage_does_not_become_zero():
    assert normalize_usage(None) == {"source": "unknown", "raw": None, "normalized": {}}
    raw = {"prompt_tokens": -1, "completion_tokens": True, "total_tokens": "4", "vendor_counter": 7}
    assert normalize_usage(raw) == {"source": "reported_by_litelm", "raw": raw, "normalized": {}}


def test_stream_complete_buffer_and_usage(monkeypatch):
    stream = Stream([
        chunk(role="assistant"),
        chunk(reasoning_content="think "),
        chunk("x="),
        chunk("1"),
        chunk(reason="stop"),
        {"choices": [], "usage": response()["usage"]},
    ])
    calls = install(monkeypatch, stream)
    result = asyncio.run(LitelmProvider("openai/m", stream=True).generate(MESSAGES, max_tokens=20))
    assert result.successful
    assert result.text == "x=1"
    assert result.reasoning == "think "
    assert result.usage["normalized"]["cache_read_tokens"] == 60
    assert len(result.raw["chunks"]) == 6
    assert calls[0]["stream_options"] == {"include_usage": True}
    assert stream.closed


@pytest.mark.parametrize(
    "chunks",
    [
        [chunk("x=1")],
        [chunk("x=1", reason="length")],
        [chunk("x=1", tool_calls=[{"index": 0}]), chunk(reason="stop")],
        [chunk("x=1", refusal="no"), chunk(reason="stop")],
        [chunk("x=1", reason="stop"), chunk(";danger()")],
        [chunk("x=1", reason="stop"), chunk(reason="stop")],
        [chunk(reasoning_content="x=1"), chunk(reason="stop")],
        [{"choices": []}, chunk("x=1", reason="stop")],
    ],
)
def test_bad_streams(monkeypatch, chunks):
    stream = Stream(chunks)
    install(monkeypatch, stream)
    result = asyncio.run(LitelmProvider("openai/m", stream=True).generate(MESSAGES, max_tokens=20))
    assert not result.successful
    assert stream.closed


def test_anthropic_partial_usage_keeps_samples_not_false_zero_input(monkeypatch):
    first = {"prompt_tokens": 100, "completion_tokens": 0, "total_tokens": 100}
    last = {"prompt_tokens": 0, "completion_tokens": 20, "total_tokens": 20}
    stream = Stream([
        {**chunk(role="assistant"), "usage": first},
        chunk("x=1"),
        {**chunk(reason="stop"), "usage": last},
    ])
    calls = install(monkeypatch, stream)
    result = asyncio.run(LitelmProvider("anthropic/configured", stream=True).generate(MESSAGES, max_tokens=20))
    assert "stream_options" not in calls[0]
    assert result.successful
    assert result.usage["normalized"] == {"input_tokens": 100, "output_tokens": 20}
    assert result.usage["samples"] == [first, last]
    assert result.usage["raw"] == last


def test_stream_failure_returns_no_partial_source(monkeypatch):
    stream = Stream([chunk("danger()"), RuntimeError("secret token")])
    install(monkeypatch, stream)
    with pytest.raises(ProviderError) as error:
        asyncio.run(LitelmProvider("openai/m", stream=True).generate(MESSAGES, max_tokens=20))
    assert "secret" not in str(error.value)
    assert error.value.usage_unknown
    assert stream.closed


def test_stream_cancellation_propagates_and_closes(monkeypatch):
    async def scenario():
        stream = Stream([chunk("danger()")], gate=asyncio.Event())
        install(monkeypatch, stream)
        task = asyncio.create_task(LitelmProvider("openai/m", stream=True).generate(MESSAGES, max_tokens=20))
        await stream.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed

    asyncio.run(scenario())


def test_nonstream_cancellation_propagates(monkeypatch):
    install(monkeypatch, asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(LitelmProvider("openai/m").generate(MESSAGES, max_tokens=20))


@pytest.mark.parametrize("streaming", [False, True])
def test_complete_responses_larger_than_old_two_mib_quota(monkeypatch, streaming):
    source = "# " + "x" * (2 * 1024 * 1024) + "\nsay('done', final=True)"
    value = Stream([chunk(source), chunk(reason="stop")]) if streaming else response(source)
    calls = install(monkeypatch, value)
    result = asyncio.run(LitelmProvider("openai/m", stream=streaming).generate(MESSAGES))
    assert result.successful
    assert result.text == source
    assert "max_tokens" not in calls[0]
    if streaming:
        assert value.closed


def test_streams_can_have_more_than_16384_chunks(monkeypatch):
    stream = Stream([chunk(" ")] * 16385 + [chunk("x=1"), chunk(reason="stop")])
    install(monkeypatch, stream)
    result = asyncio.run(LitelmProvider("openai/m", stream=True).generate(MESSAGES, max_tokens=None))
    assert result.successful
    assert result.text == " " * 16385 + "x=1"
    assert len(result.raw["chunks"]) == 16387
    assert stream.closed


@pytest.mark.parametrize("arguments", [{}, {"max_tokens": None}])
def test_api_key_provider_has_no_imposed_output_cap(monkeypatch, arguments):
    calls = install(monkeypatch, response())
    assert asyncio.run(LitelmProvider("openai/m").generate(MESSAGES, **arguments)).successful
    assert "max_tokens" not in calls[0]


async def test_large_partial_stream_still_waits_for_finish(monkeypatch):
    ready, finish = asyncio.Event(), asyncio.Event()
    source = "# " + "x" * (2 * 1024 * 1024)

    async def stream():
        yield chunk(source)
        ready.set()
        await finish.wait()
        yield chunk(reason="stop")

    install(monkeypatch, stream())
    task = asyncio.create_task(LitelmProvider("openai/m", stream=True).generate(MESSAGES))
    await ready.wait()
    assert not task.done()
    finish.set()
    result = await task
    assert result.successful
    assert result.text == source


def test_overflow_category_without_credential_logging(monkeypatch):
    overflow = type("ContextWindowExceededError", (Exception,), {})
    install(monkeypatch, overflow("Authorization secret"))
    with pytest.raises(ProviderError) as error:
        asyncio.run(LitelmProvider("openai/m").generate(MESSAGES, max_tokens=20))
    assert error.value.kind == "overflow"
    assert "secret" not in str(error.value)


def test_fake_copies_requests_consumes_cancelled_attempts_and_exhausts():
    async def scenario():
        gate = asyncio.Event()
        fake = FakeProvider(["stale()", Completion("safe()"), ValueError("fixture")], gate=gate)
        messages = deepcopy(MESSAGES)
        task = asyncio.create_task(fake.generate(messages, max_tokens=5))
        await fake.started.wait()
        messages[1]["content"] = "changed"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake.cancelled == 1
        assert fake.requests[0]["messages"] == MESSAGES
        gate.set()
        result = await fake.generate(messages, max_tokens=5)
        assert result.text == "safe()"
        assert result.successful
        with pytest.raises(ValueError, match="fixture"):
            await fake.generate(messages, max_tokens=5)
        with pytest.raises(ProviderError, match="exhausted"):
            await fake.generate(messages, max_tokens=5)

    asyncio.run(scenario())


def test_fake_text_and_unsuccessful_completion():
    async def scenario():
        fake = FakeProvider(["x=1", Completion("x=2", finish_reason="length")])
        assert (await fake.generate(MESSAGES, max_tokens=2)).text == "x=1"
        assert not (await fake.generate(MESSAGES, max_tokens=2)).successful

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "messages,tokens",
    [
        ([], 10),
        (MESSAGES, 0),
        (MESSAGES, True),
        ([{"role": "tool", "content": "result"}], 10),
        ([{"role": "user", "content": "x", "tools": []}], 10),
    ],
)
def test_explicit_text_context_only(messages, tokens):
    with pytest.raises(ValueError):
        asyncio.run(FakeProvider(["x=1"]).generate(messages, max_tokens=tokens))
