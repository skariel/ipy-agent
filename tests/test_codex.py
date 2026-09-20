"""Codex adapter tests: fake credentials + MockTransport only; no live calls."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import py_agent.codex as codex
from py_agent.provider import ProviderError

MESSAGES = [{"role": "system", "content": "raw Python only"},
            {"role": "user", "content": "agent memory"},
            {"role": "assistant", "content": "x = 1"},
            {"role": "user", "content": "runtime observation"}]
SECRET = "mock-access-token-do-not-log"
ACCOUNT = "mock-account-id-do-not-log"


def message(text="say('ok', final=True)", **extras):
    return {"type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}], **extras}


def terminal(*, status="completed", output=None, usage=None, **extras):
    return {"id": "resp_fake", "status": status,
            "output": [message()] if output is None else output,
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                      "input_tokens_details": {"cached_tokens": 60},
                      "output_tokens_details": {"reasoning_tokens": 7}} if usage is None else usage,
            **extras}


def done(response=None, kind="response.completed"):
    return {"type": kind, "response": terminal() if response is None else response}


def sse(events, *, delimiter="\n", done_marker=False):
    text = "".join("data: " + json.dumps(event, ensure_ascii=False) + delimiter * 2 for event in events)
    if done_marker:
        text += "data: [DONE]" + delimiter * 2
    return text.encode()


class Bytes(httpx.AsyncByteStream):
    def __init__(self, chunks, *, block=False, failure=None):
        self.chunks = chunks
        self.block = block
        self.failure = failure
        self.started = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        self.started.set()
        for chunk in self.chunks:
            yield chunk
        if self.block:
            await asyncio.Event().wait()
        if self.failure:
            raise self.failure

    async def aclose(self):
        self.closed = True


@pytest.fixture
def auth(monkeypatch):
    reads = []
    def read(path):
        reads.append(path)
        return SimpleNamespace(access=SECRET, account_id=ACCOUNT, expires=9999999999999)
    monkeypatch.setattr(codex, "read_codex_credentials", read)
    return reads


def setup(events=None, *, data=None, stream=None, status=200, headers=None):
    requests = []
    stream = stream or Bytes([data if data is not None else sse(events if events is not None else [done()])])
    def handle(request):
        requests.append(request)
        return httpx.Response(status, headers={"content-type": "text/event-stream", **(headers or {})}, stream=stream)
    provider = codex.CodexProvider("openai-codex/gpt-test", auth_file=Path("/fake/auth.json"),
                                   transport=httpx.MockTransport(handle))
    return provider, requests, stream


async def test_exact_request_no_tools_history_or_unsupported_output_cap(auth):
    provider, requests, stream = setup()
    details = provider.request_details(MESSAGES, max_tokens=30)
    assert not auth  # journaling request configuration must not read credentials
    result = await provider.generate(MESSAGES, max_tokens=30)
    assert result.successful and result.text == "say('ok', final=True)"
    assert stream.closed and auth == [Path("/fake/auth.json")]
    request = requests[0]
    assert str(request.url) == codex.CODEX_URL
    assert request.method == "POST"
    assert request.headers["authorization"] == "Bearer " + SECRET
    assert request.headers["chatgpt-account-id"] == ACCOUNT
    assert request.headers["openai-beta"] == "responses=experimental"
    body = json.loads(request.content)
    assert body == details["body"] == provider.build_request(MESSAGES, max_tokens=30)
    assert body["store"] is False and body["stream"] is True
    assert body["instructions"] == "raw Python only"
    assert body["reasoning"] == {"effort": "medium"}
    assert body["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "agent memory"}]},
        {"type": "message", "role": "assistant", "status": "completed", "phase": "final_answer",
         "content": [{"type": "output_text", "text": "x = 1", "annotations": []}]},
        {"role": "user", "content": [{"type": "input_text", "text": "runtime observation"}]},
    ]
    assert not {"tools", "tool_choice", "previous_response_id", "conversation", "max_output_tokens", "max_tokens"} & body.keys()
    assert details["output_limit_enforcement"] == "none" and details["remote_output_token_cap"] is False
    assert SECRET not in repr(details) + repr(result) + repr(provider)
    assert ACCOUNT not in repr(details) + repr(result) + repr(provider)


async def test_real_system_prompt_and_execution_results_are_not_assistant_output(auth):
    from py_agent.context import Context
    from py_agent.limits import Limits

    context = Context(Limits())
    context.add("user", "inspect the fixture")
    cell = context.add("assistant", "print('fixture evidence')")
    context.observation({"stdout": "fixture evidence", "stderr": "diagnostic", "status": "success"}, group=cell)
    provider, requests, _ = setup()
    await provider.generate(context.messages(), max_tokens=30)
    body = json.loads(requests[0].content)
    assert body["instructions"] == context.contract
    assert body["instructions"].startswith("You are the py coding agent. You speak only Python. Always answer with pure Python code and nothing else.")
    assert body["reasoning"] == {"effort": "medium"}
    assistant = [item for item in body["input"] if item["role"] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["content"] == [{"type": "output_text", "text": "print('fixture evidence')", "annotations": []}]
    observation = body["input"][-1]
    assert observation["role"] == "user"
    assert observation["content"][0]["type"] == "input_text"
    assert observation["content"][0]["text"] == cell.messages[-1]["content"]
    assert '"stdout":"fixture evidence"' in observation["content"][0]["text"]
    assert '"stderr":"diagnostic"' in observation["content"][0]["text"]
    assert all(part["text"] != context.contract for item in body["input"] for part in item["content"])
    assert "tools" not in body


@pytest.mark.parametrize("content_type", ["application/json", "text/plain", "text/html", ""])
async def test_valid_sse_not_rejected_by_proxy_content_type(auth, content_type):
    provider, _, stream = setup(headers={"content-type": content_type})
    result = await provider.generate(MESSAGES, max_tokens=30)
    assert result.successful and result.text == "say('ok', final=True)"
    assert stream.closed


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("content_type", ["application/json", "text/plain", "text/event-stream"])
async def test_native_json_responses_use_same_completion_validation(auth, wrapped, content_type):
    data = done() if wrapped else terminal()
    provider, _, stream = setup(data=json.dumps(data).encode(), headers={"content-type": content_type})
    result = await provider.generate(MESSAGES, max_tokens=30)
    assert result.successful and result.text == "say('ok', final=True)"
    assert result.usage["normalized"]["output_tokens"] == 20
    assert stream.closed


@pytest.mark.parametrize("data", [terminal(status="incomplete"), terminal(status="failed"),
                                  terminal(status="cancelled"), terminal(output=[{"type": "function_call"}]),
                                  terminal(output=[message(content=[{"type": "refusal", "refusal": "no"}])])])
async def test_native_json_never_bypasses_rejection_checks(auth, data):
    provider, _, _ = setup(data=json.dumps(data).encode(), headers={"content-type": "application/json"})
    result = await provider.generate(MESSAGES, max_tokens=30)
    assert not result.successful and not result.text


@pytest.mark.parametrize("error", [{"error": {"message": "model is not supported"}},
                                   {"error": "model is not supported"},
                                   {"detail": "model is not supported"}])
async def test_json_error_exposes_actionable_message_not_a_generic_sse_error(auth, error):
    provider, _, _ = setup(data=json.dumps(error).encode(), headers={"content-type": "application/json"})
    with pytest.raises(ProviderError, match="model is not supported") as failure:
        await provider.generate(MESSAGES, max_tokens=30)
    assert failure.value.kind == "response_error"


async def test_json_error_redacts_credentials_before_error_excerpt(auth):
    provider, _, _ = setup(data=json.dumps({"error": {"message": SECRET + " " + ACCOUNT}}).encode())
    with pytest.raises(ProviderError) as failure:
        await provider.generate(MESSAGES, max_tokens=30)
    assert SECRET not in str(failure.value) and ACCOUNT not in str(failure.value)
    assert "REDACTED" in str(failure.value)


@pytest.mark.parametrize("data", [b'[]', b'{"choices":[]}', b'{"status":"completed","output":[],"output":[]}',
                                  b'{"broken":', b'{"x":NaN}'])
async def test_unsupported_or_malformed_json_rejected(auth, data):
    provider, _, _ = setup(data=data, headers={"content-type": "application/json"})
    with pytest.raises(ProviderError):
        await provider.generate(MESSAGES, max_tokens=30)


async def test_json_complete_response_is_not_rejected_by_local_token_budget(auth):
    provider, _, _ = setup(data=json.dumps(terminal()).encode())
    result = await provider.generate(MESSAGES, max_tokens=1)
    assert result.successful and result.finish_reason == "stop"


def sparse_terminal_events():
    item = message(id="msg_fixture")
    return [
        {"type": "response.output_item.added", "output_index": 0,
         "item": message("", id="msg_fixture", status="in_progress")},
        {"type": "response.output_text.delta", "delta": "never_execute_a_partial_delta("},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        done(terminal(output=[])),
    ]


async def test_recorded_codex_stream_with_empty_terminal_output(auth):
    path = Path(__file__).with_name("fixtures") / "codex_completed_items_empty_terminal.json"
    events = json.loads(path.read_text())
    provider, _, _ = setup(events)
    result = await provider.generate(MESSAGES, max_tokens=100)
    assert result.successful
    assert result.text == 'say("Hi! How can I help?", final=True)'
    assert result.raw["response"]["output"] == []  # preserve original evidence
    assert result.raw["output_source"] == "completed_item_events"
    assert result.usage["normalized"]["output_tokens"] == 30


async def test_complete_item_source_only_after_successful_response_terminal(auth):
    provider, _, _ = setup(sparse_terminal_events())
    result = await provider.generate(MESSAGES, max_tokens=30)
    assert result.successful and result.text == "say('ok', final=True)"
    assert "never_execute" not in result.text


@pytest.mark.parametrize("status", ["failed", "cancelled", "incomplete"])
async def test_complete_item_is_not_success_if_response_fails(auth, status):
    events = sparse_terminal_events()
    events[-1] = done(terminal(status=status, output=[]))
    provider, _, _ = setup(events)
    result = await provider.generate(MESSAGES, max_tokens=30)
    assert not result.successful and not result.text


async def test_complete_item_without_response_terminal_is_never_executable(auth):
    provider, _, _ = setup(sparse_terminal_events()[:-1])
    with pytest.raises(ProviderError, match="without terminal"):
        await provider.generate(MESSAGES, max_tokens=30)


@pytest.mark.parametrize("mutation", ["no_done", "no_added", "duplicate_done", "duplicate_added",
                                     "wrong_id", "wrong_index", "missing_id", "missing_index",
                                     "boolean_index", "negative_index", "index_gap", "incomplete_item"])
async def test_empty_terminal_requires_complete_correlated_item_ledger(auth, mutation):
    events = sparse_terminal_events()
    if mutation == "no_done":
        del events[2]
    elif mutation == "no_added":
        del events[0]
    elif mutation == "duplicate_done":
        events.insert(3, deepcopy(events[2]))
    elif mutation == "duplicate_added":
        events.insert(1, deepcopy(events[0]))
    elif mutation == "wrong_id":
        events[2]["item"]["id"] = "another-message"
    elif mutation == "wrong_index":
        events[2]["output_index"] = 1
    elif mutation == "missing_id":
        del events[2]["item"]["id"]
    elif mutation == "missing_index":
        del events[2]["output_index"]
    elif mutation == "boolean_index":
        events[2]["output_index"] = False
    elif mutation == "negative_index":
        events[2]["output_index"] = -1
    elif mutation == "index_gap":
        events[0]["output_index"] = events[2]["output_index"] = 1
    else:
        events[2]["item"]["status"] = "in_progress"
    provider, _, _ = setup(events)
    result = await provider.generate(MESSAGES, max_tokens=30)
    assert not result.successful and not result.text


@pytest.mark.parametrize("sparse", [False, True])
async def test_first_complete_cell_selected_not_later_unobserved_answer(auth, sparse):
    commentary = message("print('inspect first')", id="msg_commentary", phase="commentary")
    final = message("say('made-up failure before seeing results', final=True)", id="msg_final", phase="final_answer")
    events = []
    for index, item in enumerate((commentary, final)):
        events.extend([
            {"type": "response.output_item.added", "output_index": index,
             "item": {**item, "status": "in_progress", "content": []}},
            {"type": "response.output_item.done", "output_index": index, "item": item},
        ])
    events.append(done(terminal(output=[] if sparse else [commentary, final])))
    provider, _, _ = setup(events)
    result = await provider.generate(MESSAGES, max_tokens=30)
    assert result.successful
    assert result.text == "print('inspect first')"
    assert result.phase == "commentary"
    assert result.raw["discarded_followup_messages"] == 1
    assert "made-up failure" not in result.text


async def test_commentary_only_complete_response_is_a_cell(auth):
    provider, _, _ = setup([done(terminal(output=[message("print(42)", phase="commentary")]))])
    result = await provider.generate(MESSAGES, max_tokens=30)
    assert result.successful and result.text == "print(42)"
    assert result.phase == "commentary"


async def test_executed_phase_replayed_without_falsely_marking_it_final(auth):
    provider, requests, _ = setup()
    messages = deepcopy(MESSAGES)
    messages[2]["phase"] = "commentary"
    await provider.generate(messages, max_tokens=30)
    body = json.loads(requests[0].content)
    assert body["input"][1]["phase"] == "commentary"
    assert body["input"][2]["role"] == "user"
    assert body["input"][2]["content"][0]["type"] == "input_text"


@pytest.mark.parametrize("status", ["failed", "incomplete", "cancelled"])
async def test_early_completed_cell_never_escapes_unsuccessful_response(auth, status):
    first = message("print('not executed')", phase="commentary")
    provider, _, _ = setup([done(terminal(status=status, output=[first, message()]))])
    result = await provider.generate(MESSAGES, max_tokens=30)
    assert not result.successful and not result.text


async def test_prose_in_first_message_is_not_skipped_or_turned_into_code(auth):
    provider, _, _ = setup([done(terminal(output=[message("I will inspect.", phase="commentary"), message()]))])
    result = await provider.generate(MESSAGES, max_tokens=30)
    # The worker, not an extraction heuristic, reports the format error.
    assert result.text == "I will inspect."
    assert "say(" not in result.text


async def test_commentary_does_not_make_multiple_finals_acceptable(auth):
    output = [message("progress", phase="commentary"), message("print(1)", phase="final_answer"),
              message("print(2)", phase="final_answer")]
    provider, _, _ = setup([done(terminal(output=output))])
    result = await provider.generate(MESSAGES, max_tokens=30)
    assert not result.successful and not result.text


async def test_unknown_message_phase_is_still_rejected(auth):
    provider, _, _ = setup([done(terminal(output=[message(phase="unknown")]))])
    result = await provider.generate(MESSAGES, max_tokens=30)
    assert not result.successful and "phase" in result.rejection_reason


async def test_sparse_complete_response_is_not_rejected_by_local_token_budget(auth):
    provider, _, _ = setup(sparse_terminal_events())
    result = await provider.generate(MESSAGES, max_tokens=1)
    assert result.successful and result.finish_reason == "stop"


async def test_usage_preserves_raw_counters_without_cache_arithmetic(auth):
    provider, _, _ = setup()
    result = await provider.generate(MESSAGES, max_tokens=30)
    assert result.usage == {"source": "reported_by_codex", "raw": terminal()["usage"],
                            "normalized": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                                           "cache_read_tokens": 60, "reasoning_tokens": 7}}
    assert "cache_creation_tokens" not in result.usage["normalized"]


async def test_credentials_reread_each_generation(auth):
    def handle(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse([done()]))
    provider = codex.CodexProvider("openai-codex/model", transport=httpx.MockTransport(handle))
    for _ in range(2):
        assert (await provider.generate(MESSAGES, max_tokens=30)).successful
    assert auth == [Path.home() / ".pi/agent/auth.json"] * 2


@pytest.mark.parametrize("kind", ["response.completed", "response.done"])
async def test_reasoning_separate_final_source_not_delta_prefix(auth, kind):
    reasoning = {"type": "reasoning", "id": "rs_fake", "encrypted_content": "opaque",
                 "summary": [{"type": "summary_text", "text": "This is not Python"}]}
    events = [{"type": "response.created", "response": {"id": "resp_fake", "output": []}},
              {"type": "response.output_text.delta", "delta": "do_not_execute_this_prefix("},
              done(terminal(output=[reasoning, message("print('final')")]), kind)]
    provider, _, _ = setup(events)
    result = await provider.generate(MESSAGES, max_tokens=30)
    assert result.successful and result.text == "print('final')"
    assert result.reasoning == "This is not Python"
    assert result.raw["response"]["output"][0] == reasoning


async def test_realistic_event_sequence_and_done_marker(auth):
    item = message()
    events = [
        {"type": "response.created", "response": {"id": "resp_fake"}},
        {"type": "response.in_progress", "response": {"id": "resp_fake"}},
        {"type": "response.output_item.added", "item": message("", status="in_progress")},
        {"type": "response.content_part.added", "part": {"type": "output_text", "text": ""}},
        {"type": "response.output_text.delta", "delta": item["content"][0]["text"]},
        {"type": "response.output_text.done", "text": item["content"][0]["text"]},
        {"type": "response.content_part.done", "part": item["content"][0]},
        {"type": "response.output_item.done", "item": item}, done(),
    ]
    data = sse(events, delimiter="\r\n", done_marker=True)
    provider, _, _ = setup(stream=Bytes([data[i:i + 3] for i in range(0, len(data), 3)]))
    assert (await provider.generate(MESSAGES, max_tokens=30)).successful


async def test_sse_comments_multiline_data_and_split_unicode(auth):
    response = done(terminal(output=[message("print('🌱')")]))
    pretty = json.dumps(response, ensure_ascii=False, indent=2)
    data = (": heartbeat\r\nevent: response.completed\r\n" + "".join("data: " + line + "\r\n" for line in pretty.splitlines()) + "\r\n").encode()
    provider, _, _ = setup(stream=Bytes([bytes([byte]) for byte in data]))
    result = await provider.generate(MESSAGES, max_tokens=30)
    assert result.successful and result.text == "print('🌱')"


@pytest.mark.parametrize("response", [
    terminal(status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
    terminal(status="failed"), terminal(status="cancelled"), terminal(status="in_progress"),
    terminal(status=None), terminal(error={"message": "failure"}),
    terminal(output=[]), terminal(output=[message("")]), terminal(output=[message("   ")]),
    terminal(output=[message(role="user")]), terminal(output=[message(status="in_progress")]),
    terminal(output=[message(), message("second")]),
    terminal(output=[{"type": "function_call", "name": "bash", "arguments": "{}"}]),
    terminal(output=[{"type": "web_search_call"}]), terminal(output=[message(content=[{"type": "refusal", "refusal": "no"}])]),
    terminal(output=[message(content=[{"type": "output_image", "data": "image"}])]),
    terminal(output=[message(tool_calls=[{}])]), terminal(output="not a list"),
    terminal(tools=[{"type": "web_search"}]), terminal(refusal="no"),
])
async def test_unsuccessful_shapes_have_no_executable_text(auth, response):
    provider, _, _ = setup([done(response)])
    result = await provider.generate(MESSAGES, max_tokens=30)
    assert not result.successful and result.text == "" and result.rejection_reason
    assert result.raw["response"] == response


@pytest.mark.parametrize("event", [
    {"type": "response.refusal.delta", "delta": "no"},
    {"type": "response.function_call_arguments.delta", "delta": "{}"},
    {"type": "response.output_item.added", "item": {"type": "custom_tool_call"}},
    {"type": "response.content_part.added", "part": {"type": "refusal", "refusal": "no"}},
    {"type": "response.output_text.delta", "delta": []},
    {"type": "error", "message": "failed"}, {"type": "new_unsupported_kind"},
    {"type": "response.created", "response": {"output": [{"type": "function_call"}]}},
    {"type": "response.in_progress", "response": {"status": "failed"}},
    {"type": "response.created", "response": {"tools": [{"type": "web_search"}]}},
    {"type": "response.created", "response": {"output": "unsupported"}},
])
async def test_rejection_in_earlier_event_cannot_be_overridden_by_good_final(auth, event):
    provider, _, _ = setup([event, done()])
    result = await provider.generate(MESSAGES, max_tokens=30)
    assert not result.successful and result.text == ""


@pytest.mark.parametrize("kind", ["response.failed", "response.incomplete", "response.cancelled"])
async def test_terminal_event_kind_cannot_claim_success_with_completed_status(auth, kind):
    provider, _, _ = setup([done(kind=kind)])
    assert not (await provider.generate(MESSAGES, max_tokens=30)).successful


@pytest.mark.parametrize("events", [[done(), done()], [done(), {"type": "response.output_text.delta", "delta": "late"}]])
async def test_duplicate_terminal_or_late_text_rejected(auth, events):
    provider, _, _ = setup(events)
    result = await provider.generate(MESSAGES, max_tokens=30)
    assert not result.successful and result.text == ""


@pytest.mark.parametrize("data", [
    b"data: [DONE]\n\n", b"", b'data: {"type":"response.output_text.delta","delta":"x=1"}\n\n',
    b'data: {"type":"response.completed"}\n\n', b'data: {"type":"response.completed"}',
    b"data: invalid-json\n\n", b"data: \xff\n\n", b"data: []\n\n",
    b'data: {"type":"response.completed","type":"response.done"}\n\n',
    b'data: {"type":"response.output_text.delta","delta":NaN}\n\n',
    b'event: response.failed\ndata: {"type":"response.completed"}\n\n',
    sse([done()]) + b"data: [DONE]\n\ndata: [DONE]\n\n",
    sse([done()]) + b"data: [DONE]\n\n" + sse([done()]),
])
async def test_broken_or_unfinished_sse_raises_without_source(auth, data):
    provider, _, stream = setup(data=data)
    with pytest.raises(ProviderError):
        await provider.generate(MESSAGES, max_tokens=30)
    assert stream.closed


@pytest.mark.parametrize("tokens,reasoning", [(20, 7), (12000, 11990)])
async def test_complete_response_and_reasoning_are_not_subject_to_local_output_cap(auth, tokens, reasoning):
    provider, requests, _ = setup([done(terminal(usage={"input_tokens": 100, "output_tokens": tokens,
                                                      "output_tokens_details": {"reasoning_tokens": reasoning}}))])
    result = await provider.generate(MESSAGES, max_tokens=19)
    assert result.successful and result.finish_reason == "stop" and result.text == "say('ok', final=True)"
    assert result.usage["normalized"]["output_tokens"] == tokens
    assert result.usage["normalized"]["reasoning_tokens"] == reasoning
    assert result.raw["output_limit_enforcement"] == "none"
    assert result.raw["ignored_max_tokens"] == 19
    assert "max_output_tokens" not in json.loads(requests[0].content)


async def test_missing_usage_is_unknown_not_a_reason_to_reject_complete_code(auth):
    raw = terminal()
    del raw["usage"]
    provider, _, _ = setup([done(raw)])
    result = await provider.generate(MESSAGES, max_tokens=30)
    assert result.successful and result.finish_reason == "stop"
    assert result.usage["source"] == "unknown"


@pytest.mark.parametrize("usage", [{"output_tokens": -1}, {"output_tokens": True}, {"output_tokens": "20"}, []])
async def test_invalid_usage_rejected(auth, usage):
    provider, _, _ = setup([done(terminal(usage=usage))])
    with pytest.raises(ProviderError, match="usage"):
        await provider.generate(MESSAGES, max_tokens=30)


@pytest.mark.parametrize("status", [301, 302, 307, 401, 403, 429, 500])
async def test_http_errors_no_redirects_no_body_or_credential_leak(auth, status):
    provider, requests, stream = setup(status=status, data=(SECRET + ACCOUNT).encode(), headers={"location": "https://untrusted.invalid/"})
    with pytest.raises(ProviderError) as failure:
        await provider.generate(MESSAGES, max_tokens=30)
    assert str(status) in str(failure.value)
    assert SECRET not in repr(failure.value) and ACCOUNT not in repr(failure.value)
    assert len(requests) == 1 and stream.closed
    assert not stream.started.is_set()  # no need to read potentially sensitive error body


async def test_non_sse_success_response_rejected(auth):
    provider, _, stream = setup(headers={"content-type": "text/html"}, data=b"unexpected")
    with pytest.raises(ProviderError, match="non-SSE"):
        await provider.generate(MESSAGES, max_tokens=30)
    assert stream.closed


async def test_transport_failure_is_sanitized_and_stream_closed(auth):
    provider, _, stream = setup(stream=Bytes([], failure=httpx.ReadError(SECRET)))
    with pytest.raises(ProviderError) as failure:
        await provider.generate(MESSAGES, max_tokens=30)
    assert SECRET not in str(failure.value) and "ReadError" in str(failure.value)
    assert stream.closed


async def test_cancellation_closes_stream_no_completion_returned(auth):
    provider, _, stream = setup(stream=Bytes([sse([{"type": "response.output_text.delta", "delta": "x=1"}])], block=True))
    task = asyncio.create_task(provider.generate(MESSAGES, max_tokens=30))
    await stream.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed


@pytest.mark.parametrize("wire_format", ["sse", "json"])
async def test_complete_responses_over_two_mib_are_not_rejected(auth, wire_format):
    source = "# " + "x" * (2 * 1024 * 1024) + "\nsay('done', final=True)"
    final = terminal(output=[message(source)])
    data = sse([done(final)]) if wire_format == "sse" else json.dumps(final).encode()
    provider, requests, stream = setup(data=data)
    result = await provider.generate(MESSAGES)
    assert result.successful and result.text == source
    assert stream.closed
    assert result.raw["ignored_max_tokens"] is None
    assert "max_output_tokens" not in json.loads(requests[0].content)


async def test_complete_stream_accepts_more_than_16384_events(auth):
    events = [{"type": "response.output_text.delta", "delta": "x"}] * 16385 + [done()]
    provider, _, stream = setup(events)
    result = await provider.generate(MESSAGES, max_tokens=None)
    assert result.successful and result.text == "say('ok', final=True)"
    assert result.raw["event_count"] == 16386 and stream.closed


async def test_large_unfinished_response_is_never_returned_early(auth):
    reached_terminal, finish = asyncio.Event(), asyncio.Event()
    source = "# " + "x" * (2 * 1024 * 1024)
    class DeferredTerminal(Bytes):
        async def __aiter__(self):
            yield sse([{"type": "response.output_text.delta", "delta": source}])
            reached_terminal.set()
            await finish.wait()
            yield sse([done(terminal(output=[message(source)]))])
    provider, _, stream = setup(stream=DeferredTerminal([]))
    task = asyncio.create_task(provider.generate(MESSAGES))
    await reached_terminal.wait()
    assert not task.done()
    finish.set()
    result = await task
    assert result.successful and result.text == source and stream.closed


async def test_read_and_write_timeouts_do_not_limit_generation(auth):
    provider, requests, _ = setup()
    assert (await provider.generate(MESSAGES)).successful
    assert requests[0].extensions["timeout"] == {"connect": 20, "read": None, "write": None, "pool": 20}


@pytest.mark.parametrize("secret", [SECRET, ACCOUNT])
async def test_credential_echo_is_redacted_and_never_executable(auth, secret):
    provider, _, _ = setup([done(terminal(output=[message(f"print({secret!r})")]))])
    result = await provider.generate(MESSAGES, max_tokens=30)
    assert not result.successful and result.text == ""
    assert secret not in repr(result) and "[REDACTED]" in repr(result.raw)


@pytest.mark.parametrize("model", ["", "openai/gpt-test", "openai-codex/", "openai-codex/a/b", "openai-codex/test\nheader"])
def test_explicit_codex_model_required(model):
    with pytest.raises(ValueError):
        codex.CodexProvider(model)


def test_request_build_does_not_mutate_context_or_expose_shared_objects(auth):
    original = deepcopy(MESSAGES)
    provider = codex.CodexProvider("openai-codex/gpt-test")
    details = provider.request_details(MESSAGES, max_tokens=30)
    details["body"]["input"][0]["content"][0]["text"] = "modified"
    assert MESSAGES == original
    assert provider.build_request(MESSAGES, max_tokens=30)["input"][0]["content"][0]["text"] == "agent memory"
    assert not auth
    with pytest.raises(ValueError):
        provider.build_request([MESSAGES[1], MESSAGES[0]], max_tokens=30)


async def test_auth_validation_error_does_not_send_request(monkeypatch):
    def expired(path):
        raise ProviderError("Credentials expired; use pi /login openai-codex", kind="authentication")
    monkeypatch.setattr(codex, "read_codex_credentials", expired)
    provider, requests, _ = setup()
    with pytest.raises(ProviderError, match="expired"):
        await provider.generate(MESSAGES, max_tokens=30)
    assert requests == []
