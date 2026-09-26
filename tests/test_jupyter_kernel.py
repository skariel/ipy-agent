import asyncio
import sys
from types import SimpleNamespace

import pytest

from py_agent import jupyter_kernel
from py_agent.builtin_services import BuiltinPlugin
from py_agent.contracts import (
    CompletionResult, ExecutorCapabilities, ExecutionResult, InspectionResult,
    InputReply, InputRequest, InputUnavailableError, Origin, OutputEvent,
)
from py_agent.coordinator import Coordinator
from py_agent.plugins import PluginRuntime


class FakeCoordinator:
    def __init__(self, *submissions):
        self.submissions = list(submissions)
        self.calls = []
        self.interruptions = 0
        self.closed = 0

    async def submit(self, frontend_id, text):
        self.calls.append((frontend_id, text))
        if self.submissions:
            return self.submissions.pop(0)
        return SimpleNamespace(result=None, message="")

    async def interrupt(self):
        self.interruptions += 1

    async def close(self):
        self.closed += 1


class GatedCoordinator(FakeCoordinator):
    def __init__(self, *submissions):
        super().__init__(*submissions)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def submit(self, frontend_id, text):
        self.started.set()
        await self.release.wait()
        return await super().submit(frontend_id, text)


class KernelHarness(jupyter_kernel._CoordinatorKernelMethods):
    """Exercise protocol translations without requiring optional Jupyter deps."""

    def __init__(self, coordinator):
        self.execution_count = 0
        self.iopub_socket = object()
        self.session = None
        self._parent_header = {"header": {"session": "client-a", "msg_id": "cell-a"}}
        self._parent_ident = [b"client-a"]
        self.stdin_socket = object()
        self.sent = []
        self._initialize_adapter(coordinator)

    def send_response(self, socket, message_type, content):
        self.sent.append((message_type, content, self._parent_header))


class StdinSocket:
    def __init__(self):
        self.messages = []


class StdinAsyncPoller:
    instances = []

    def __init__(self):
        self.socket = None
        self.flags = None
        self.unregistered = False
        self.instances.append(self)

    def register(self, socket, flags):
        self.socket = socket
        self.flags = flags

    async def poll(self, timeout):
        if self.socket.messages:
            return [(self.socket, self.flags)]
        await asyncio.sleep(timeout / 1000)
        return []

    def unregister(self, socket):
        assert socket is self.socket
        self.unregistered = True


class StdinSession:
    def __init__(self, kernel, value="stdin reply", *, loop=None, respond=True):
        self.kernel = kernel
        self.value = value
        self.loop = loop
        self.respond = respond
        self.counter = 0
        self.stdin_messages = []

    def _assert_loop(self):
        if self.loop is not None:
            assert asyncio.get_running_loop() is self.loop

    def msg(self, message_type, content, *, parent):
        self._assert_loop()
        self.counter += 1
        return {
            "header": {"msg_type": message_type, "msg_id": f"message-{self.counter}"},
            "parent_header": parent,
            "content": content,
        }

    def serialize(self, message):
        self._assert_loop()
        return message

    def send_raw(self, socket, message, *, flags, ident):
        assert flags == 4  # ZMQ_DONTWAIT
        self.send(socket, message, ident=ident)

    def send(self, socket, message, ident=None):
        self._assert_loop()
        if socket is self.kernel.stdin_socket:
            self.stdin_messages.append((message, ident))
            if self.respond:
                reply = {
                    "header": {"msg_type": "input_reply", "msg_id": "reply"},
                    "parent_header": {"msg_id": message["header"]["msg_id"]},
                    "content": {"value": self.value},
                }
                unrelated = {
                    **reply,
                    "content": {"value": "cross-client-secret"},
                }
                stale = {
                    **reply,
                    "parent_header": {"msg_id": "stale-request"},
                    "content": {"value": "stale-reply-secret"},
                }
                socket.messages.extend((
                    ([b"client-b"], unrelated), ([b"client-a"], stale), ([b"client-a"], reply),
                ))
        else:
            self.kernel.sent.append((
                message["header"]["msg_type"], message["content"],
                message.get("parent_header"),
            ))

    def recv(self, socket, *, mode):
        self._assert_loop()
        del mode
        return socket.messages.pop(0)


def _submission(*, status="success", stdout="", stderr="", error=None, message=""):
    result = None if status is None else SimpleNamespace(
        status=status,
        stdout=stdout,
        stderr=stderr,
        error=error,
    )
    return SimpleNamespace(result=result, message=message)


@pytest.mark.asyncio
async def test_execute_delegates_cell_once_and_publishes_streams_with_parent():
    coordinator = FakeCoordinator(_submission(stdout="out\n", stderr="err\n"))
    kernel = KernelHarness(coordinator)

    reply = await kernel.do_execute("@decorator\nvalue = 4", False)

    assert reply["status"] == "ok"
    assert reply["execution_count"] == 1
    assert coordinator.calls == [("jupyter:client-a", "@decorator\nvalue = 4")]
    assert [item[0] for item in kernel.sent] == ["execute_input", "stream", "stream"]
    assert kernel.sent[1][1] == {"name": "stdout", "text": "out\n"}
    assert kernel.sent[2][1] == {"name": "stderr", "text": "err\n"}
    assert all(item[2]["header"]["msg_id"] == "cell-a" for item in kernel.sent)


def _install_fake_zmq(monkeypatch):
    StdinAsyncPoller.instances.clear()
    monkeypatch.setitem(sys.modules, "zmq", SimpleNamespace(
        POLLIN=1, NOBLOCK=2, DONTWAIT=4, Again=BlockingIOError,
        asyncio=SimpleNamespace(Poller=StdinAsyncPoller),
    ))


@pytest.mark.asyncio
async def test_progress_iopub_is_immediate_correlated_and_not_replayed():
    origin = Origin(
        "session", "request-progress", "jupyter:client-a", 0, "generation-a",
    )
    execution_origin = Origin(
        "session", "request-progress", "jupyter:client-a", 0,
        "generation-a", "execution-a",
    )
    step_limit = "Agent paused after 2 execution steps without a successful final answer."
    events = (
        OutputEvent(origin, 1, "progress", {
            "phase": "generation_start", "step": 1,
            "text": "Agent: requesting a response (step 1).",
        }),
        OutputEvent(execution_origin, 2, "progress", {
            "phase": "execution_start", "step": 1,
            "text": "Agent: executing cell (step 1).",
        }, author="agent"),
        OutputEvent(execution_origin, 3, "stream", {"name": "stdout", "text": "cell output\n"}, author="agent"),
        OutputEvent(
            execution_origin, 4, "display", {"text/plain": "working"},
            metadata={"py_agent_source": "say", "final": False}, author="agent",
        ),
        OutputEvent(origin, 5, "progress", {
            "phase": "step_limit", "status": "paused", "step_limit": 2, "text": step_limit,
        }),
    )
    entered, release = asyncio.Event(), asyncio.Event()

    class ProgressCoordinator(FakeCoordinator):
        async def submit(self, frontend_id, text, *, allow_stdin=False, input_handler=None, on_progress=None):
            self.calls.append((frontend_id, text, allow_stdin))
            await on_progress(events[0])
            entered.set()
            await release.wait()
            for event in events[1:]:
                await on_progress(event)
            return SimpleNamespace(
                result=SimpleNamespace(status="success", stdout="legacy duplicate", stderr="", error=None),
                message=f"working\n{step_limit}", events=events,
            )

    coordinator = ProgressCoordinator()
    kernel = KernelHarness(coordinator)
    running = asyncio.create_task(kernel.do_execute("long task", False))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        assert [message[0] for message in kernel.sent] == ["execute_input", "stream"]
        assert kernel.sent[-1][1]["text"] == "Agent: requesting a response (step 1)."
        release.set()
        reply = await asyncio.wait_for(running, timeout=3)
        assert reply["status"] == "ok"
        assert [message[0] for message in kernel.sent] == [
            "execute_input", "stream", "stream", "stream", "stream", "stream",
        ]
        output_text = "".join(
            message[1].get("text", "") for message in kernel.sent if message[0] == "stream"
        )
        assert output_text.count("working") == 1
        assert output_text.count(step_limit) == 1
        assert "cell output" in output_text
        assert "legacy duplicate" not in output_text
        assert all(message[2]["header"]["msg_id"] == "cell-a" for message in kernel.sent)
        prior_messages = tuple(kernel.sent)
        silent_reply = await kernel.do_execute("silent long task", True)
        assert silent_reply["status"] == "ok"
        assert tuple(kernel.sent) == prior_messages
    finally:
        release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
async def test_progress_error_is_published_once_but_final_kernel_status_is_error():
    origin = Origin("session", "request-error", "jupyter:client-a", 0, execution_id="execution")
    error_event = OutputEvent(origin, 1, "error", {
        "ename": "ValueError", "evalue": "cell failed",
    }, author="agent")

    class ErrorProgressCoordinator(FakeCoordinator):
        async def submit(self, frontend_id, text, *, allow_stdin=False, input_handler=None, on_progress=None):
            await on_progress(error_event)
            return SimpleNamespace(
                result=SimpleNamespace(status="error", stdout="", stderr="", error="ValueError: cell failed"),
                message="", events=(error_event,),
            )

    kernel = KernelHarness(ErrorProgressCoordinator())
    reply = await kernel.do_execute("fail", False)

    assert reply["status"] == "error"
    assert reply["ename"] == "ValueError"
    assert reply["evalue"] == "cell failed"
    assert [message[0] for message in kernel.sent] == ["execute_input", "error"]
    assert kernel.sent[-1][1]["evalue"] == "cell failed"


@pytest.mark.asyncio
async def test_stdin_request_uses_stdin_channel_saved_parent_and_owner(monkeypatch):
    _install_fake_zmq(monkeypatch)
    kernel = KernelHarness(FakeCoordinator())
    kernel.stdin_socket = StdinSocket()
    session = StdinSession(
        kernel, value="not-rendered-password", loop=asyncio.get_running_loop(),
    )
    kernel.session = session

    class InteractiveCoordinator(FakeCoordinator):
        async def submit(self, frontend_id, text, *, allow_stdin=False, input_handler=None):
            self.calls.append((frontend_id, text, allow_stdin))
            assert allow_stdin is True
            assert input_handler is not None
            # A later shell request must not steal either parent or ROUTER identity.
            kernel._parent_header = {"header": {"session": "client-b", "msg_id": "cell-b"}}
            kernel._parent_ident = [b"client-b"]
            request = InputRequest(
                Origin("session", "request", frontend_id, 0, execution_id="execution"),
                1, frontend_id, "Password: ", True,
            )
            self.input_reply = await input_handler(request)
            return _submission()

    coordinator = InteractiveCoordinator()
    kernel.coordinator = coordinator

    reply = await kernel.do_execute("@getpass()", False, allow_stdin=True)

    assert reply["status"] == "ok"
    assert coordinator.calls == [("jupyter:client-a", "@getpass()", True)]
    assert coordinator.input_reply.value == "not-rendered-password"
    assert coordinator.input_reply.password is True
    assert len(session.stdin_messages) == 1
    message, ident = session.stdin_messages[0]
    assert message["header"]["msg_type"] == "input_request"
    assert message["content"] == {"prompt": "Password: ", "password": True}
    assert message["parent_header"]["header"]["msg_id"] == "cell-a"
    assert ident == [b"client-a"]
    assert all(item[2]["header"]["msg_id"] == "cell-a" for item in kernel.sent)
    assert StdinAsyncPoller.instances[0].unregistered is True
    assert "cross-client-secret" not in repr(kernel.sent)
    assert "stale-reply-secret" not in repr(kernel.sent)
    assert "not-rendered-password" not in repr(kernel.sent)
    assert "not-rendered-password" not in repr(kernel._history.query({"raw": True}))


@pytest.mark.asyncio
async def test_stdin_fails_clearly_when_async_kernel_socket_polling_is_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "zmq", SimpleNamespace(
        POLLIN=1, NOBLOCK=2, DONTWAIT=4, Again=BlockingIOError,
        asyncio=SimpleNamespace(),
    ))
    kernel = KernelHarness(FakeCoordinator())
    kernel.stdin_socket = StdinSocket()
    kernel.session = StdinSession(kernel, loop=asyncio.get_running_loop())
    frontend_id = "jupyter:client-a"
    input_request = InputRequest(
        Origin("session", "request", frontend_id, 0, execution_id="execution"),
        1, frontend_id, "Password: ", True,
    )

    with pytest.raises(InputUnavailableError, match="polling is unavailable"):
        await kernel._request_stdin(
            input_request, parent=kernel._current_parent(),
            ident=kernel._parent_ident, allow_stdin=True,
        )

    assert kernel.session.stdin_messages == []


@pytest.mark.asyncio
async def test_stdin_async_exchange_timeout_is_bounded_and_unregisters_poller(monkeypatch):
    _install_fake_zmq(monkeypatch)
    monkeypatch.setattr(jupyter_kernel, "JUPYTER_STDIN_TIMEOUT", 0.01)
    kernel = KernelHarness(FakeCoordinator())
    kernel.stdin_socket = StdinSocket()
    kernel.session = StdinSession(
        kernel, loop=asyncio.get_running_loop(), respond=False,
    )
    frontend_id = "jupyter:client-a"
    input_request = InputRequest(
        Origin("session", "request", frontend_id, 0, execution_id="execution"),
        1, frontend_id, "Password: ", True,
    )

    with pytest.raises(InputUnavailableError, match="timed out"):
        await kernel._request_stdin(
            input_request, parent=kernel._current_parent(),
            ident=kernel._parent_ident, allow_stdin=True,
        )

    assert len(kernel.session.stdin_messages) == 1
    assert StdinAsyncPoller.instances[0].unregistered is True


@pytest.mark.asyncio
async def test_stdin_async_exchange_cancellation_stays_on_kernel_loop(monkeypatch):
    _install_fake_zmq(monkeypatch)
    kernel = KernelHarness(FakeCoordinator())
    kernel.stdin_socket = StdinSocket()
    kernel.session = StdinSession(
        kernel, loop=asyncio.get_running_loop(), respond=False,
    )
    frontend_id = "jupyter:client-a"
    input_request = InputRequest(
        Origin("session", "request", frontend_id, 0, execution_id="execution"),
        1, frontend_id, "Password: ", True,
    )
    exchange = asyncio.create_task(kernel._request_stdin(
        input_request, parent=kernel._current_parent(),
        ident=kernel._parent_ident, allow_stdin=True,
    ))
    for _ in range(10):
        if kernel.session.stdin_messages:
            break
        await asyncio.sleep(0)
    assert kernel.session.stdin_messages
    exchange.cancel()
    with pytest.raises(asyncio.CancelledError):
        await exchange

    assert StdinAsyncPoller.instances[0].unregistered is True


@pytest.mark.asyncio
async def test_allow_stdin_false_is_forwarded_and_does_not_send_input_request():
    class InputCapabilityCoordinator(FakeCoordinator):
        async def submit(self, frontend_id, text, *, allow_stdin=False, input_handler=None):
            self.calls.append((frontend_id, text, allow_stdin, input_handler))
            return _submission(
                status="error",
                error="RuntimeError: Interactive input unavailable because allow_stdin=false",
            )

    coordinator = InputCapabilityCoordinator()
    kernel = KernelHarness(coordinator)

    reply = await kernel.do_execute("input('name: ')", False, allow_stdin=False)

    assert reply["status"] == "error"
    assert coordinator.calls == [("jupyter:client-a", "input('name: ')", False, None)]
    assert all(message[0] != "input_request" for message in kernel.sent)


@pytest.mark.asyncio
async def test_submission_output_events_map_rich_messages_once_with_saved_parent():
    origin = Origin("session", "request", "jupyter:client-a", 0, execution_id="execution")
    result = SimpleNamespace(status="success", stdout="legacy duplicate", stderr="", error=None)
    submission = SimpleNamespace(
        result=result,
        message="",
        executions=(),
        events=(
            OutputEvent(origin, 1, "stream", {"name": "stdout", "text": "ordered\n"}),
            OutputEvent(origin, 2, "display", {"text/html": "<b>rich</b>"}),
            OutputEvent(
                origin, 3, "execute_result", {"application/json": {"answer": 42}},
                metadata={"application/json": {"expanded": True}},
            ),
            OutputEvent(origin, 4, "update", {"text/plain": "updated"}, "display-1"),
            OutputEvent(origin, 5, "clear", {"wait": True}),
        ),
    )
    kernel = KernelHarness(FakeCoordinator(submission))

    class SessionCapture:
        def msg(self, message_type, content, *, parent):
            return {"type": message_type, "content": content, "parent": parent}

        def send(self, _socket, message):
            kernel.sent.append((message["type"], message["content"], message["parent"]))

    kernel.session = SessionCapture()

    class ParentChangingCoordinator(FakeCoordinator):
        async def submit(self, frontend_id, text):
            kernel._parent_header = {"header": {"session": "client-b", "msg_id": "other-cell"}}
            return await super().submit(frontend_id, text)

    kernel.coordinator = ParentChangingCoordinator(submission)
    reply = await kernel.do_execute("rich cell", False)

    assert reply["status"] == "ok"
    assert [message[0] for message in kernel.sent] == [
        "execute_input", "stream", "display_data", "execute_result",
        "update_display_data", "clear_output",
    ]
    assert kernel.sent[1][1] == {"name": "stdout", "text": "ordered\n"}
    assert kernel.sent[2][1]["data"] == {"text/html": "<b>rich</b>"}
    assert kernel.sent[3][1]["data"] == {"application/json": {"answer": 42}}
    assert kernel.sent[3][1]["execution_count"] == 1
    assert kernel.sent[4][1]["transient"] == {"display_id": "display-1"}
    assert kernel.sent[5][1] == {"wait": True}
    assert all(message[2]["header"]["msg_id"] == "cell-a" for message in kernel.sent)
    assert "legacy duplicate" not in repr(kernel.sent)


@pytest.mark.asyncio
async def test_silent_and_store_history_are_independent_protocol_flags():
    coordinator = FakeCoordinator(_submission(), _submission())
    kernel = KernelHarness(coordinator)

    silent_reply = await kernel.do_execute("x = 1", True, store_history=True)
    unstored_reply = await kernel.do_execute("x = 2", False, store_history=False)

    assert silent_reply["execution_count"] == 0
    assert unstored_reply["execution_count"] == 1
    assert kernel.sent == []
    history = await kernel.do_history("tail", n=10, raw=True)
    assert history == {"history": []}
    assert len(coordinator.calls) == 2


@pytest.mark.asyncio
async def test_history_tracks_stored_submitted_cells_not_generated_subcells():
    coordinator = FakeCoordinator(_submission(), _submission())
    kernel = KernelHarness(coordinator)

    await kernel.do_execute("print('one')", False, store_history=True)
    await kernel.do_execute("print('two')", False, store_history=False)

    history = await kernel.do_history("range", raw=True, start=1, stop=3)
    assert history == {"history": [(1, 1, "print('one')")]}
    history_with_output_shape = await kernel.do_history(
        "tail", raw=False, output=True, n=1,
    )
    assert history_with_output_shape == {"history": [(1, 1, ("print('one')", None))]}


@pytest.mark.asyncio
async def test_executor_error_becomes_jupyter_error_and_exposes_unavailable_stdin():
    coordinator = FakeCoordinator(_submission(
        status="error",
        error="RuntimeError: Interactive input is unavailable in the local executor",
    ))
    kernel = KernelHarness(coordinator)

    reply = await kernel.do_execute("input('name: ')", False, allow_stdin=True)

    assert reply["status"] == "error"
    assert reply["ename"] == "RuntimeError"
    assert "input is unavailable" in reply["evalue"]
    assert kernel.sent[-1][0] == "error"
    assert kernel.sent[-1][1]["ename"] == "RuntimeError"


@pytest.mark.asyncio
async def test_nonexecution_agent_message_is_returned_as_stdout():
    coordinator = FakeCoordinator(_submission(status=None, message="Done"))
    kernel = KernelHarness(coordinator)

    reply = await kernel.do_execute("Explain the result", False)

    assert reply["status"] == "ok"
    assert kernel.sent[-1][0] == "stream"
    assert kernel.sent[-1][1] == {"name": "stdout", "text": "Done\n"}


@pytest.mark.asyncio
async def test_stop_on_error_aborts_cells_already_queued_behind_a_failed_execution():
    coordinator = GatedCoordinator(_submission(status="error", error="ValueError: failed"), _submission())
    kernel = KernelHarness(coordinator)

    failed = asyncio.create_task(kernel.do_execute("raise ValueError()", False, stop_on_error=True))
    await coordinator.started.wait()
    queued = asyncio.create_task(kernel.do_execute("should_not_run = True", False))
    await asyncio.sleep(0)
    coordinator.release.set()
    first, second = await asyncio.gather(failed, queued)

    assert first["status"] == "error"
    assert second["status"] == "abort"
    assert len(coordinator.calls) == 1


@pytest.mark.asyncio
async def test_stop_on_error_false_allows_next_queued_cell():
    coordinator = GatedCoordinator(_submission(status="error", error="ValueError: failed"), _submission())
    kernel = KernelHarness(coordinator)

    failed = asyncio.create_task(kernel.do_execute("raise ValueError()", False, stop_on_error=False))
    await coordinator.started.wait()
    queued = asyncio.create_task(kernel.do_execute("still_runs = True", False))
    await asyncio.sleep(0)
    coordinator.release.set()
    first, second = await asyncio.gather(failed, queued)

    assert first["status"] == "error"
    assert second["status"] == "ok"
    assert len(coordinator.calls) == 2


@pytest.mark.asyncio
async def test_completion_and_inspection_report_missing_coordinator_capability():
    coordinator = FakeCoordinator()
    kernel = KernelHarness(coordinator)

    completion = await kernel.do_complete("value", 5)
    inspection = await kernel.do_inspect("value", 5)
    completeness = await kernel.do_is_complete("@if True:\n")

    assert completion["status"] == "error"
    assert completion["ename"] == "ExecutorCapabilityError"
    assert completion["matches"] == []
    assert completion["cursor_start"] == completion["cursor_end"] == 5
    assert inspection["status"] == "error"
    assert inspection["ename"] == "ExecutorCapabilityError"
    assert inspection["found"] is False
    assert completeness["status"] == "incomplete"
    assert coordinator.calls == []


@pytest.mark.asyncio
async def test_selected_coordinator_routes_at_prefix_offsets_to_its_executor():
    class QueryExecutor:
        capabilities = ExecutorCapabilities(persistent=True, completion=True, inspection=True)

        def __init__(self):
            self.queries = []

        async def start(self):
            return None

        async def execute(self, request):
            return ExecutionResult(request.origin, "success")

        async def complete(self, code, cursor_pos):
            self.queries.append(("complete", code, cursor_pos))
            return CompletionResult(("phase5_name",), 0, cursor_pos)

        async def inspect(self, code, cursor_pos, detail_level=0):
            self.queries.append(("inspect", code, cursor_pos, detail_level))
            return InspectionResult(True, {"text/plain": "phase5_name : int"})

        async def interrupt(self):
            return None

        async def close(self):
            return None

    executor = QueryExecutor()
    runtime = PluginRuntime.load(builtins={
        "builtin": BuiltinPlugin(executor_factory=lambda: executor),
    })
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
    )
    await coordinator.start()
    kernel = KernelHarness(coordinator)
    try:
        code = "@phase5_name"
        completion = await kernel.do_complete(code, len(code))
        inspection = await kernel.do_inspect(code, len(code), detail_level=1)

        assert completion == {
            "status": "ok", "matches": ["phase5_name"],
            "cursor_start": 1, "cursor_end": len(code), "metadata": {},
        }
        assert inspection["status"] == "ok"
        assert inspection["found"] is True
        assert executor.queries == [
            ("complete", "phase5_name", len(code) - 1),
            ("inspect", "phase5_name", len(code) - 1, 1),
        ]
        completeness = await kernel.do_is_complete("%time 1")
        assert completeness["status"] == "complete"
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_selected_executor_without_query_capabilities_returns_protocol_errors():
    class NoQueryExecutor:
        capabilities = ExecutorCapabilities(persistent=True)

        async def start(self):
            return None

        async def execute(self, request):
            return ExecutionResult(request.origin, "success")

        async def interrupt(self):
            return None

        async def close(self):
            return None

    executor = NoQueryExecutor()
    runtime = PluginRuntime.load(builtins={
        "builtin": BuiltinPlugin(executor_factory=lambda: executor),
    })
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
    )
    await coordinator.start()
    kernel = KernelHarness(coordinator)
    try:
        completion = await kernel.do_complete("@value", 6)
        inspection = await kernel.do_inspect("@value", 6)
        assert completion["status"] == "error"
        assert completion["ename"] == "ExecutorCapabilityError"
        assert "completion" in completion["evalue"]
        assert inspection["status"] == "error"
        assert inspection["ename"] == "ExecutorCapabilityError"
        assert "inspection" in inspection["evalue"]
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_completion_and_inspection_use_optional_core_actions_when_available():
    coordinator = FakeCoordinator()

    async def complete(code, cursor_pos):
        assert (code, cursor_pos) == ("pri", 3)
        return {"matches": ["print"], "cursor_start": 0, "cursor_end": 3}

    async def inspect(code, cursor_pos, detail_level):
        assert (code, cursor_pos, detail_level) == ("value", 5, 1)
        return {"found": True, "data": {"text/plain": "42"}, "metadata": {}}

    coordinator.complete = complete
    coordinator.inspect = inspect
    kernel = KernelHarness(coordinator)

    assert (await kernel.do_complete("pri", 3))["matches"] == ["print"]
    assert (await kernel.do_inspect("value", 5, 1))["data"] == {"text/plain": "42"}


def test_output_event_bridge_maps_display_and_update_messages():
    kernel = KernelHarness(FakeCoordinator())
    origin = Origin("session", "request", "jupyter:client-a", 0)

    kernel.publish_output_event(OutputEvent(origin, 1, "display", {"text/plain": "42"}, "view-1"))
    kernel.publish_output_event(OutputEvent(origin, 2, "update", {"text/plain": "43"}, "view-1"))

    assert [message[0] for message in kernel.sent] == ["display_data", "update_display_data"]
    assert kernel.sent[0][1]["data"] == {"text/plain": "42"}
    assert kernel.sent[1][1]["transient"] == {"display_id": "view-1"}


@pytest.mark.asyncio
async def test_interrupt_and_shutdown_delegate_to_coordinator_lifecycle():
    coordinator = FakeCoordinator()
    kernel = KernelHarness(coordinator)
    kernel._active_execution = True

    assert kernel.do_interrupt() == {"status": "ok"}
    await kernel._interrupt_task
    reply = await kernel.do_shutdown(restart=False)

    assert kernel._interrupt_requested is True
    assert coordinator.interruptions == 1
    assert reply == {"status": "ok", "restart": False}
    assert coordinator.closed == 1


def test_jupyter_protocol_runtime_is_optional_at_import_time():
    if jupyter_kernel.jupyter_available():
        pytest.skip("ipykernel is installed; the real Kernel base is available")

    with pytest.raises(ImportError, match="ipykernel"):
        jupyter_kernel.JupyterKernel(object())


def test_managed_kernel_cli_requires_explicit_provider_and_preserves_options():
    from py_agent import cli

    args = cli._management_parser().parse_args([
        "kernel", "start", "--provider", "fake", "--context-window-tokens", "4096",
        "--no-stream", "--session-root", "/private/sessions",
    ])

    assert args.provider == "fake"
    assert args.context_window_tokens == 4096
    assert args.stream is False
    assert cli._kernel_launch_arguments(args) == [
        "--provider", "fake", "--context-window-tokens", "4096", "--no-stream",
    ]


def test_kernel_start_does_not_assume_provider_or_model():
    from py_agent import cli

    args = cli._management_parser().parse_args(["kernel", "start"])
    assert args.provider is None
    with pytest.raises(ValueError, match="Select --provider"):
        cli._initial_config(args)
