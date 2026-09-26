"""Regression coverage for the unrestricted, persistent local IPython executor."""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from py_agent.contracts import ExecutionOutput, ExecutionRequest, InputReply, InputRequest, Origin
from py_agent.local_executor import (
    LocalExecutor, MAX_FRAME, MAX_QUERY_CHARS, _redact_stream_fragments,
)
from py_agent.local_worker import MAX_BINARY_MIME_BYTES, _safe_mime_bundle


def request(
    source: str,
    *,
    author: str = "user",
    execution_id: str | None = None,
    allow_stdin: bool = False,
    input_handler=None,
    output_handler=None,
) -> ExecutionRequest:
    origin = Origin(
        session_id="session-1",
        request_id="request-1",
        frontend_id="test",
        config_revision=3,
        generation_id="generation-1" if author == "agent" else None,
        execution_id=execution_id or "execution-1",
    )
    return ExecutionRequest(
        origin, source, author, allow_stdin=allow_stdin, input_handler=input_handler,
        output_handler=output_handler,
    )


@pytest.mark.asyncio
async def test_short_provisional_stdout_arrives_before_cell_finishes():
    executor = LocalExecutor()
    await executor.start()
    ready = asyncio.Event()
    previews = []

    async def on_output(output):
        previews.append(output)
        ready.set()

    try:
        running = asyncio.create_task(executor.execute(request(
            "print('started', flush=True)\nimport time; time.sleep(0.3)\nprint('finished')",
            output_handler=on_output,
        )))
        await asyncio.wait_for(ready.wait(), timeout=3)
        assert not running.done()
        assert previews[0].metadata["provisional"] is True
        assert "started" in previews[0].data["text"]
        result = await running
        assert result.status == "success"
        assert "finished" in result.stdout
        assert all(not event.metadata.get("provisional") for event in result.output_events)
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_user_and_agent_sources_share_persistent_namespace_and_ipython_history():
    executor = LocalExecutor()
    await executor.start()
    sent_frames: list[dict] = []
    original_send = executor._send

    async def capture_send(process, frame):
        size = int.from_bytes(frame[:4], "big")
        assert size <= MAX_FRAME
        sent_frames.append(json.loads(frame[4:]))
        await original_send(process, frame)

    executor._send = capture_send
    user_source = "answer = 40  # exact user source"
    agent_source = "answer += 2  # exact agent source\nprint(answer)"
    try:
        first = await executor.execute(request(user_source, author="user"))
        second = await executor.execute(request(agent_source, author="agent"))
        history = await executor.execute(request(
            "print(get_ipython().history_manager.input_hist_raw[1] == "
            "'answer = 40  # exact user source')\n"
            "print(get_ipython().history_manager.input_hist_raw[2] == "
            "'answer += 2  # exact agent source\\nprint(answer)')"
        ))
        assert first.status == "success"
        assert second.status == "success"
        assert "42" in second.stdout
        assert history.status == "success"
        assert "True" in history.stdout
        assert sent_frames[0]["author"] == "user"
        assert sent_frames[0]["source"] == user_source
        assert sent_frames[0]["origin"]["request_id"] == "request-1"
        assert sent_frames[1]["author"] == "agent"
        assert sent_frames[1]["source"] == agent_source
        assert sent_frames[1]["origin"]["generation_id"] == "generation-1"
        assert first.origin == request(user_source).origin
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_say_outputs_preserve_typed_messages_and_explicit_final_marker():
    executor = LocalExecutor()
    await executor.start()
    try:
        result = await executor.execute(request(
            "say({'answer': 42}, final=True)", author="agent",
        ))
        assert result.status == "success"
        assert result.final is True
        assert len(result.say_outputs) == 1
        assert result.say_outputs[0].content == {"answer": 42}
        assert result.say_outputs[0].final is True
        assert result.stdout == ""
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_worker_failure_drops_oversized_output_without_a_false_reference():
    executor = LocalExecutor()
    await executor.start()
    try:
        result = await executor.execute(request(
            "import os\nprint('x' * 8100, flush=True)\nos._exit(1)", author="agent",
        ))
        assert result.status == "uncertain"
        assert "no outputs[index] is available" in result.stdout
        assert "x" * 100 not in result.stdout
        assert "x" * 100 not in str(result.output_events)
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_large_stdout_is_replaced_by_persistent_output_reference():
    executor = LocalExecutor()
    await executor.start()
    previews = []

    async def on_output(output):
        previews.append(output)

    try:
        result = await executor.execute(request(
            "print('x' * 8100)", author="agent", output_handler=on_output,
        ))
        assert sum(len(event.data["text"]) for event in previews) <= 512
        assert result.status == "success"
        assert result.output_reference == 1
        assert "outputs[1]" in result.stdout
        assert "Print a smaller slice" in result.stdout
        assert "x" * 100 not in result.stdout
        assert "x" * 100 not in str(result.output_events)
        excerpt = await executor.execute(request("print(outputs[1][100:112])", author="agent"))
        assert excerpt.status == "success"
        assert "x" * 12 in excerpt.stdout
        assert "outputs[1]" not in excerpt.stdout
        indexes = await executor.store_outputs(("old observation", "another old observation"))
        assert indexes == (2, 3)
        archived = await executor.execute(request("print(outputs[2], outputs[3])", author="agent"))
        assert "old observation another old observation" in archived.stdout
        assert archived.output_reference is None
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_agent_say_over_8000_characters_is_rejected_before_publication():
    executor = LocalExecutor()
    await executor.start()
    try:
        result = await executor.execute(request("say('x' * 8001, final=True)", author="agent"))
        assert result.status == "error"
        assert "send something smaller" in result.error
        assert result.say_outputs == ()
        assert not result.final
        # A direct user cell is not subject to the agent's say size cap.
        direct = await executor.execute(request("say('x' * 8001)", author="user"))
        assert direct.status == "success"
        assert len(direct.say_outputs) == 1
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_ipython_rich_output_preserves_mime_bundles_display_ids_and_frame_order():
    executor = LocalExecutor()
    await executor.start()
    try:
        result = await executor.execute(request(
            "from IPython.display import HTML, clear_output, display, update_display\n"
            "print('before')\n"
            "display(HTML('<b>first</b>'), display_id='stable-view')\n"
            "print('between')\n"
            "update_display(HTML('<i>updated</i>'), display_id='stable-view')\n"
            "clear_output(wait=True)\n"
            "40 + 2"
        ))
        assert result.status == "success"
        assert "\x1b" not in result.stdout + result.stderr
        assert [event.kind for event in result.output_events] == [
            "stream", "display", "stream", "update", "clear", "execute_result",
        ]
        assert result.output_events[0].data == {"name": "stdout", "text": "before\n"}
        assert result.output_events[1].data["text/html"] == "<b>first</b>"
        assert result.output_events[1].display_id == "stable-view"
        assert result.output_events[3].kind == "update"
        assert result.output_events[3].data["text/html"] == "<i>updated</i>"
        assert result.output_events[5].kind == "execute_result"
        assert result.output_events[5].data["text/plain"] == "42"
        assert result.output_events[4].data["wait"] is True
    finally:
        await executor.close()


def test_password_redaction_catches_values_split_across_output_frames():
    events = [
        ExecutionOutput("stream", {"name": "stdout", "text": "prefix private-pass"}),
        ExecutionOutput("stream", {"name": "stdout", "text": "word suffix"}),
    ]

    safe = _redact_stream_fragments(events, ["private-password"])

    assert "private-password" not in "".join(event.data["text"] for event in safe)
    assert "[REDACTED]" in "".join(event.data["text"] for event in safe)
    truncated = _redact_stream_fragments(
        [ExecutionOutput("stream", {"name": "stdout", "text": "prefix private-pass"})],
        ["private-password"],
        truncated_streams=frozenset({"stdout"}),
    )
    assert truncated[0].data["text"] == "prefix "


def test_mime_bundle_accepts_bounded_json_and_binary_but_filters_unsafe_or_oversize_data():
    data, metadata = _safe_mime_bundle({
        "text/plain": "safe fallback",
        "application/json": {"items": [1, True, None]},
        "image/png": b"\x89PNG",
        "image/jpeg": b"x" * (MAX_BINARY_MIME_BYTES + 1),
        "application/javascript": "alert('not a renderer')",
        "application/vnd.plotly.v1+json": {"data": [{"x": [1, 2]}]},
        "application/vnd.jupyter.widget-view+json": {"model_id": "host-control"},
        "text/html": "<b>rich</b>",
    }, {"height": 10, "invalid": float("nan")})

    assert data["text/plain"] == "safe fallback"
    assert data["application/json"] == {"items": [1, True, None]}
    assert data["image/png"] == "iVBORw=="
    assert "image/jpeg" not in data
    assert "application/javascript" not in data
    assert data["application/vnd.plotly.v1+json"] == {"data": [{"x": [1, 2]}]}
    assert "application/vnd.jupyter.widget-view+json" not in data
    assert data["text/html"] == "<b>rich</b>"
    assert metadata == {}


@pytest.mark.asyncio
async def test_failed_cell_keeps_final_say_staged_but_never_marks_result_final():
    executor = LocalExecutor()
    await executor.start()
    try:
        failed = await executor.execute(request(
            "say('must not commit', final=True)\nraise ValueError('after say')",
            author="agent",
        ))
        assert failed.status == "error"
        assert failed.final is False
        assert [(item.content, item.final) for item in failed.say_outputs] == [("must not commit", True)]
        success = await executor.execute(request("say('now complete', final=True)", author="agent"))
        assert success.status == "success"
        assert success.final is True
        assert success.say_outputs[0].content == "now complete"
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_say_rejects_non_boolean_final_without_setting_final_marker():
    executor = LocalExecutor()
    await executor.start()
    try:
        result = await executor.execute(request("say('bad', final=1)", author="agent"))
        assert result.status == "error"
        assert result.final is False
        assert result.say_outputs == ()
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_output_is_streamed_in_bounded_frames_and_truncation_is_explicit():
    executor = LocalExecutor(max_output_chars=100)
    await executor.start()
    sent_sizes: list[int] = []
    original_receive = executor._receive

    async def capture_receive(process):
        frame = await original_receive(process)
        if frame.get("type") == "output":
            sent_sizes.append(len(frame["text"]))
        return frame

    executor._receive = capture_receive
    try:
        result = await executor.execute(request(
            "import sys\nprint('x' * 400)\nprint('err', file=sys.stderr)"
        ))
        assert result.status == "success"
        assert result.stdout.startswith("x" * 100)
        assert "stdout truncated: 301 characters omitted" in result.stdout
        assert "stderr truncated: 4 characters omitted" in result.stderr
        assert all(size <= 8_192 for size in sent_sizes)
        assert len(result.stdout) < 200
        assert len(result.stderr) < 100
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_large_unicode_output_round_trips_across_multiple_frames():
    executor = LocalExecutor(max_output_chars=20_000)
    await executor.start()
    try:
        result = await executor.execute(request("print('🙂' * 10_000, end='')"))
        assert result.status == "success"
        assert result.stdout == "🙂" * 10_000
    finally:
        await executor.close()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="Raw descriptor capture uses POSIX pipes")
async def test_native_and_shell_output_cannot_corrupt_or_disappear_from_the_frame_channel():
    executor = LocalExecutor()
    await executor.start()
    try:
        result = await executor.execute(request(
            "import os, sys\nos.write(1, b'native stdout')\n"
            "os.write(2, b'native stderr')\n"
            "sys.stdout.buffer.write(b'buffer stdout')\n"
            "sys.stderr.buffer.write(b'buffer stderr')\n!printf 'shell output'"
        ))
        assert result.status == "success"
        assert "native stdout" in result.stdout
        assert "native stderr" in result.stderr
        assert "buffer stdout" in result.stdout
        assert "buffer stderr" in result.stderr
        assert "shell output" in result.stdout
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_correlated_stdin_handles_input_and_password_without_protocol_pipe_theft():
    executor = LocalExecutor()
    await executor.start()
    requests = []

    async def answer(input_request: InputRequest) -> InputReply:
        requests.append(input_request)
        value = "ordinary answer" if not input_request.password else "stdin-password-never-leak"
        reply = InputReply(
            input_request.origin, input_request.sequence,
            input_request.owner_frontend_id, value=value, password=input_request.password,
        )
        assert value not in repr(reply)
        return reply

    try:
        ordinary = await executor.execute(request(
            "answer = input('Name: ')\nprint(answer)",
            execution_id="stdin-user", allow_stdin=True, input_handler=answer,
        ))
        agent = await executor.execute(request(
            "agent_answer = input('Agent prompt: ')\nprint(agent_answer)",
            author="agent", execution_id="stdin-agent",
            allow_stdin=True, input_handler=answer,
        ))
        password = await executor.execute(request(
            "import getpass\nprotected = getpass.getpass('Password: ')\nprotected",
            execution_id="stdin-password", allow_stdin=True, input_handler=answer,
        ))
        later = await executor.execute(request(
            "print(protected)\n"
            "print('history-safe:', all(protected not in repr(o) "
            "for o in get_ipython().history_manager.outputs))"
        ))

        assert ordinary.status == agent.status == password.status == later.status == "success"
        assert ordinary.stdout == "ordinary answer\n"
        assert agent.stdout == "ordinary answer\n"
        assert [item.sequence for item in requests] == [1, 1, 1]
        assert len({item.origin.execution_id for item in requests}) == 3
        assert requests[1].origin.generation_id == "generation-1"
        assert requests[2].password is True
        assert "stdin-password-never-leak" not in password.stdout
        assert all("stdin-password-never-leak" not in repr(event.data)
                   for event in password.output_events)
        assert "stdin-password-never-leak" not in later.stdout
        assert "history-safe: True" in later.stdout
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_provisional_preview_cannot_leak_password_split_across_frames():
    executor = LocalExecutor()
    await executor.start()
    secret = "preview-password-never-leak"
    previews = []

    async def answer(item):
        return InputReply(item.origin, item.sequence, item.owner_frontend_id,
                          value=secret, password=item.password)

    async def on_output(item):
        previews.append(item.data["text"])

    try:
        result = await executor.execute(request(
            "import getpass, sys\n"
            "pw = getpass.getpass('Password: ')\n"
            "sys.stdout.write(pw[:7]); sys.stdout.flush()\n"
            "sys.stdout.write(pw[7:] + '\\n'); sys.stdout.flush()",
            allow_stdin=True, input_handler=answer, output_handler=on_output,
        ))
        assert result.status == "success"
        assert all("preview" not in text for text in previews)
        assert secret not in result.stdout
        assert secret not in repr(result.output_events)
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_all_stdlib_getpass_variants_and_saved_aliases_request_password_input():
    executor = LocalExecutor()
    await executor.start()
    requests = []
    secret = "getpass-variant-secret"

    async def answer(input_request: InputRequest) -> InputReply:
        requests.append(input_request)
        return InputReply(
            input_request.origin, input_request.sequence,
            input_request.owner_frontend_id, value=secret, password=input_request.password,
        )

    try:
        setup = await executor.execute(request(
            "from getpass import getpass as saved_getpass\n"
            "from getpass import unix_getpass as saved_unix_getpass\n"
            "from getpass import fallback_getpass as saved_fallback_getpass\n"
            "from getpass import default_getpass as saved_default_getpass",
            execution_id="getpass-alias-setup",
        ))
        result = await executor.execute(request(
            "import getpass\n"
            "variants = [getpass.getpass, getpass.unix_getpass, getpass.fallback_getpass, "
            "getpass.default_getpass, saved_getpass, saved_unix_getpass, "
            "saved_fallback_getpass, saved_default_getpass]\n"
            "variants += [getattr(getpass, name) for name in ('win_getpass', '_raw_input') "
            "if callable(getattr(getpass, name, None))]\n"
            "print([method('Password: ') for method in variants])\n"
            "try:\n    getpass.getpass('Password: ', echo_char='*')\n"
            "except RuntimeError:\n    print('custom echo rejected')",
            allow_stdin=True, input_handler=answer, execution_id="getpass-alias-call",
        ))

        assert setup.status == result.status == "success"
        assert requests
        assert [item.sequence for item in requests] == list(range(1, len(requests) + 1))
        assert all(item.password is True for item in requests)
        assert secret not in result.stdout + result.stderr
        assert secret not in repr(result.output_events)
        assert "custom echo rejected" in result.stdout
        assert result.stderr == ""
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_input_fails_clearly_without_consuming_the_control_channel():
    executor = LocalExecutor()
    await executor.start()
    try:
        result = await asyncio.wait_for(executor.execute(request("input('answer: ')")), timeout=5)
        assert result.status == "error"
        assert "Interactive input unavailable" in (result.error or "")
        later = await executor.execute(request("print('worker remains usable')"))
        assert later.status == "success"
        assert "worker remains usable" in later.stdout
        if os.name != "nt":
            shell = await asyncio.wait_for(executor.execute(request("!cat")), timeout=5)
            assert shell.status == "success"
            assert shell.stdout == ""
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_stdin_reply_timeout_is_bounded_and_worker_remains_usable():
    executor = LocalExecutor(input_timeout=0.05)
    await executor.start()

    async def never_reply(_input_request: InputRequest) -> InputReply:
        await asyncio.Future()

    try:
        result = await asyncio.wait_for(executor.execute(request(
            "input('wait: ')", allow_stdin=True, input_handler=never_reply,
        )), timeout=3)
        assert result.status == "error"
        assert "input timed out" in (result.error or "").lower()
        later = await executor.execute(request("print('worker still works')"))
        assert later.status == "success"
        assert "worker still works" in later.stdout
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_interrupt_cancels_the_active_frontend_stdin_request():
    executor = LocalExecutor(interrupt_timeout=1)
    await executor.start()
    started = asyncio.Event()

    async def wait_for_reply(_input_request: InputRequest) -> InputReply:
        started.set()
        await asyncio.Future()

    try:
        running = asyncio.create_task(executor.execute(request(
            "input('interrupt me: ')", allow_stdin=True, input_handler=wait_for_reply,
        )))
        await asyncio.wait_for(started.wait(), timeout=3)
        await asyncio.wait_for(executor.interrupt(), timeout=3)
        result = await asyncio.wait_for(running, timeout=3)
        assert result.status == "cancelled"
        later = await executor.execute(request("print('after stdin interrupt')"))
        assert later.status == "success"
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_concurrent_execute_calls_are_serialized_without_crossed_output():
    executor = LocalExecutor()
    await executor.start()
    try:
        first, second = await asyncio.gather(
            executor.execute(request("import time; print('first'); time.sleep(.05); print('done')")),
            executor.execute(request("print('second')", execution_id="second-request")),
        )
        assert first.status == second.status == "success"
        assert first.stdout == "first\ndone\n"
        assert second.stdout == "second\n"
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_sigint_interrupt_reports_cancellation_and_preserves_namespace_when_handled():
    executor = LocalExecutor(interrupt_timeout=3)
    await executor.start()
    try:
        setup = await executor.execute(request("kept = 'still here'"))
        assert setup.status == "success"
        running = asyncio.create_task(executor.execute(request("while True: pass")))
        for _ in range(200):
            if executor._active_execution_id is not None:
                break
            await asyncio.sleep(.01)
        assert executor._active_execution_id is not None
        await executor.interrupt()
        interrupted = await asyncio.wait_for(running, timeout=5)
        assert interrupted.status == "cancelled"
        following = await executor.execute(request("print(kept)"))
        assert following.status == "success"
        assert "still here" in following.stdout
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_task_cancellation_never_replays_and_discards_uncertain_worker_state():
    executor = LocalExecutor()
    await executor.start()
    running = asyncio.create_task(executor.execute(request("import time; time.sleep(10)")))
    try:
        for _ in range(200):
            if executor._active_execution_id is not None:
                break
            await asyncio.sleep(.01)
        assert executor._active_execution_id is not None
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert executor.process is None
        assert executor._closed
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_completion_and_inspection_use_the_live_namespace_without_evaluating_source():
    executor = LocalExecutor()
    await executor.start()
    try:
        setup = await executor.execute(request(
            "phase5_completion_target = 42\n"
            "phase5_inspection_calls = []\n"
            "def phase5_inspection_target():\n"
            "    phase5_inspection_calls.append('called')\n"
            "class Phase5ReprBomb:\n"
            "    def __repr__(self):\n"
            "        raise AssertionError('repr must not be called')\n"
            "phase5_repr_bomb = Phase5ReprBomb()"
        ))
        assert setup.status == "success"

        prefix = "phase5_completion_tar"
        completion = await executor.complete(prefix, len(prefix))
        assert "phase5_completion_target" in completion.matches
        assert completion.cursor_start == 0
        assert completion.cursor_end == len(prefix)

        target = "phase5_inspection_target"
        inspection = await executor.inspect(target, len(target), detail_level=1)
        assert inspection.found is True
        assert "function" in inspection.data["text/plain"]

        bomb = "phase5_repr_bomb"
        safe_inspection = await executor.inspect(bomb, len(bomb))
        assert safe_inspection.found is True
        assert "Phase5ReprBomb" in safe_inspection.data["text/plain"]
        calls = await executor.execute(request("print(phase5_inspection_calls)"))
        assert calls.stdout.strip() == "[]"
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_query_source_limit_is_enforced_before_worker_creation():
    executor = LocalExecutor()
    oversized = "x" * (MAX_QUERY_CHARS + 1)
    with pytest.raises(ValueError, match="character limit"):
        await executor.complete(oversized, len(oversized))
    with pytest.raises(ValueError, match="character limit"):
        await executor.inspect(oversized, len(oversized))
    assert executor.process is None


@pytest.mark.asyncio
async def test_completion_query_is_serialized_after_an_active_cell():
    executor = LocalExecutor()
    await executor.start()
    try:
        running = asyncio.create_task(executor.execute(request(
            "import time\nphase5_serialized_completion = 1\ntime.sleep(.05)"
        )))
        for _ in range(200):
            if executor._active_execution_id is not None:
                break
            await asyncio.sleep(.01)
        assert executor._active_execution_id is not None
        prefix = "phase5_serialized_comple"
        queued = asyncio.create_task(executor.complete(prefix, len(prefix)))
        result = await asyncio.wait_for(running, timeout=5)
        completion = await asyncio.wait_for(queued, timeout=5)
        assert result.status == "success"
        assert "phase5_serialized_completion" in completion.matches
    finally:
        await executor.close()


def test_executor_limits_are_validated_without_creating_a_worker():
    with pytest.raises(ValueError, match="finite positive"):
        LocalExecutor(timeout=0)
    with pytest.raises(ValueError, match="input_timeout"):
        LocalExecutor(input_timeout=0)
    with pytest.raises(ValueError, match="max_output_chars"):
        LocalExecutor(max_output_chars=MAX_FRAME + 1)
