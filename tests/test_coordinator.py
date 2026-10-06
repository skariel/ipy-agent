"""Coordinator journal integration; deterministic fakes only."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from prompt_toolkit.formatted_text import to_plain_text
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
import pytest

from py_agent import cli, plain_terminal
from py_agent.builtin_services import BasicInterpreter, BuiltinPlugin
from py_agent.contracts import (
    ContextSnapshot,
    ExecutionOutput,
    ExecutionRequest,
    ExecutionResult,
    ExecutorCapabilities,
    InputReply,
    InputRequest,
    InputUnavailableError,
    ModelRequest,
    ModelResponse,
    Origin,
    OutputEvent,
    QueueFullError,
    SayOutput,
)
from py_agent.coordinator import MAX_PENDING_ACTIONS, Coordinator, State, Submission
from py_agent.local_executor import LocalExecutor
from py_agent.plugins import (
    CommandContribution,
    Contributions,
    PluginManifest,
    PluginRuntime,
    TransformContribution,
    hookimpl,
)
from py_agent.production_services import ProductionContextAdapter, ProductionObservationAdapter
from py_agent.provider import ProviderError
from py_agent.session_journal import JournalError, NoPersistenceJournal, SQLiteSessionJournal


def test_response_interpreter_unwraps_one_python_fence_and_rejects_mixed_markdown():
    interpreter = BasicInterpreter()
    for label in ("python", "py", "ipython", ""):
        response = ModelResponse(f"```{label}\nsay('2', final=True)\n```")
        decision = interpreter.interpret(response)
        assert decision.kind == "execute"
        assert decision.source == "say('2', final=True)"
    for text in (
        "Here is the code:\n```python\nsay('2', final=True)\n```",
        "```python\nsay('2', final=True)\n```\nmore text",
        "```python\nsay('2', final=True)",
    ):
        assert interpreter.interpret(ModelResponse(text)).kind == "reject"
    assert interpreter.interpret(ModelResponse("say('2', final=True)")).kind == "execute"
    assert interpreter.check_syntax("```python\npass\n```") is not None
    embedded = "doc = '''\n```python\npass\n```\n'''\nsay('2', final=True)"
    assert interpreter.interpret(ModelResponse(embedded)).kind == "execute"
    assert interpreter.interpret(ModelResponse(embedded)).source == embedded
    assert interpreter.check_syntax("!echo hello") is None
    assert interpreter.check_syntax("await coro()") is None


class EffectiveModelTransform:
    async def transform(self, request: ModelRequest) -> ModelRequest:
        context = ContextSnapshot(
            request.context.epoch,
            (*request.context.messages, ("system", "api_key=transform-secret-value")),
            (*request.context.message_phases, None),
            request.context.transform_trace,
        )
        return ModelRequest(
            request.origin,
            context,
            "effective/model",
            {**request.options, "api_key": "transform-secret-value", "temperature": "0.1"},
            request.transform_trace,
        )


class TransformPlugin:
    @hookimpl
    def py_agent_register(self):
        return Contributions(
            PluginManifest("journal-test-transform"),
            transforms=(TransformContribution(
                "model-request", "effective", lambda _config: EffectiveModelTransform(),
            ),),
        )


class CaptureProvider:
    model = "configured/model"

    def __init__(self):
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        return ModelResponse(
            "value = 'api_key=source-secret-value'\nsay('done', final=True)",
            usage={
                "source": "reported",
                "normalized": {"input_tokens": 41},
                "headers": {"authorization": "must-not-persist"},
            },
            provider_id="offline",
            model="effective/model",
            adapter_metadata={"credentials": {"secret": "must-not-persist"}},
        )


class CaptureExecutor:
    capabilities = ExecutorCapabilities(persistent=True, interrupt=True)

    def __init__(self):
        self.requests = []
        self.closed = 0
        self.collapsed = {}

    async def store_collapsed(self, text):
        index = len(self.collapsed) + 1
        self.collapsed[index] = text
        return index

    async def start(self):
        return None

    async def execute(self, request: ExecutionRequest):
        self.requests.append(request)
        if request.author == "agent":
            return ExecutionResult(
                request.origin,
                "success",
                stdout="0123456789",
                say_outputs=(SayOutput("complete", final=True),),
                final=True,
            )
        return ExecutionResult(request.origin, "success", stdout="user output")

    async def interrupt(self):
        return None

    async def close(self):
        self.closed += 1


class InteractiveCaptureExecutor(CaptureExecutor):
    async def execute(self, request: ExecutionRequest):
        self.requests.append(request)
        if request.input_handler is None:
            return ExecutionResult(request.origin, "error", "stdin handler missing")
        self.input_request = InputRequest(
            request.origin, 1, request.origin.frontend_id, "value: ", False,
        )
        self.input_reply = await request.input_handler(self.input_request)
        if request.author == "agent":
            return ExecutionResult(
                request.origin, "success", say_outputs=(SayOutput("done", final=True),), final=True,
            )
        return ExecutionResult(request.origin, "success", stdout="received")


class RichCaptureExecutor(CaptureExecutor):
    capabilities = ExecutorCapabilities(persistent=True, rich_output=True, interrupt=True)

    async def execute(self, request: ExecutionRequest):
        self.requests.append(request)
        if request.author == "agent":
            return ExecutionResult(
                request.origin,
                "success",
                stdout="plain output",
                say_outputs=(SayOutput("complete", final=True),),
                final=True,
                output_events=(
                    ExecutionOutput("stream", {"name": "stdout", "text": "plain output"}),
                    ExecutionOutput("display", {
                        "text/html": "<b>private rich payload</b>",
                        "text/plain": "plain fallback",
                    }),
                ),
            )
        return ExecutionResult(request.origin, "success", stdout="user output")


class CaptureObservations:
    def pack(self, events):
        return {"events": events}


class TwoStepObservationProvider:
    model = "fake/observation"

    def __init__(self):
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        return ModelResponse("pass", provider_id="offline", model=self.model)


class RichOnlyModelExecutor(CaptureExecutor):
    async def execute(self, request: ExecutionRequest):
        self.requests.append(request)
        if len(self.requests) == 1:
            return ExecutionResult(
                request.origin,
                "success",
                output_events=(ExecutionOutput("display", {
                    "text/html": "<b>must not enter the model context</b>",
                    "text/plain": "visible rich fallback",
                }),),
            )
        return ExecutionResult(
            request.origin, "success", say_outputs=(SayOutput("done", final=True),), final=True,
        )


def journal_for(tmp_path, **kwargs):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700, exist_ok=True)
    parent.chmod(0o700)
    return SQLiteSessionJournal(parent / "coordinator.sqlite", **kwargs)


def event_records(journal, session_id):
    rows = journal.db.execute(
        "SELECT kind, config_revision, payload FROM events WHERE session_id=? ORDER BY seq",
        (session_id,),
    ).fetchall()
    return [(kind, revision, json.loads(payload)) for kind, revision, payload in rows]


@pytest.mark.asyncio
async def test_effective_request_usage_attributed_source_and_bounded_result_are_durable(tmp_path):
    runtime = PluginRuntime.load(builtins={
        "builtin": BuiltinPlugin(),
        "journal-test-transform": TransformPlugin(),
    })
    journal = journal_for(tmp_path, max_stream_chars=8)
    coordinator = Coordinator(
        runtime,
        router="default",
        provider="fake",
        interpreter="basic",
        executor="local",
        journal=journal,
        config_revision=12,
    )
    provider, executor = CaptureProvider(), CaptureExecutor()
    coordinator.provider, coordinator.executor = provider, executor

    await coordinator.start()
    try:
        submission = await coordinator.submit("terminal", "please compute privately")
        assert submission.result.final
        direct = await coordinator.submit("terminal", "@user_value = 9")
        assert direct.execution.author == "user"
        assert len(provider.requests) == 1
        effective = provider.requests[0]
        assert effective.model == "effective/model"
        assert effective.options["temperature"] == "0.1"
        assert effective.context.messages[-1] == ("system", "api_key=transform-secret-value")
        assert [request.author for request in executor.requests] == ["agent", "user"]
        session_id = coordinator.session_id
    finally:
        await coordinator.close()

    # Reopening proves the records reached the durable database before shutdown.
    history = SQLiteSessionJournal(journal.path)
    try:
        records = event_records(history, session_id)
    finally:
        history.close()
    assert [kind for kind, _, _ in records] == [
        "session_start", "model_request", "provider_usage", "execution_source",
        "execution_result", "execution_source", "execution_result", "session_end",
    ]
    assert all(revision == 12 for _, revision, _ in records)
    model_request = records[1][2]
    assert model_request["content"]["model"] == "effective/model"
    assert model_request["content"]["context"]["messages"][-1] == [
        "system", "api_key=[REDACTED]",
    ]
    assert model_request["content"]["options"]["api_key"] == "[REDACTED]"
    usage = records[2][2]["content"]
    assert usage["usage"]["normalized"]["input_tokens"] == 41
    assert usage["usage"]["headers"] == "[REDACTED]"
    agent_source = records[3][2]
    assert agent_source["metadata"]["author"] == "agent"
    assert agent_source["metadata"]["generation_id"]
    assert agent_source["metadata"]["execution_id"] == executor.requests[0].origin.execution_id
    assert "source-secret-value" not in json.dumps(agent_source)
    assert records[5][2]["metadata"]["author"] == "user"
    result = records[4][2]["content"]
    assert result["stdout"] == {
        "text": "01234567", "original_chars": 10, "truncated": True, "redacted": False,
    }
    assert records[4][2]["config_revision"] == 12
    stored_bytes = journal.path.read_bytes()
    assert b"must-not-persist" not in stored_bytes
    assert b"transform-secret-value" not in stored_bytes
    assert b"please compute privately" in stored_bytes


@pytest.mark.asyncio
async def test_coordinator_submission_carries_immutable_rich_events_without_model_leakage():
    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
    )
    coordinator.provider = CaptureProvider()
    coordinator.executor = RichCaptureExecutor()
    await coordinator.start()
    try:
        submission = await coordinator.submit("terminal", "show a rich result")
        assert submission.result.final is True
        assert len(submission.events) >= 1
        display = next(event for event in submission.events if event.kind == "display")
        assert display.data["text/html"] == "<b>private rich payload</b>"
        assert display.origin.execution_id == submission.execution.origin.execution_id
        assert display.author == "agent"
        with pytest.raises(TypeError):
            display.data["text/html"] = "mutated"
        assert "private rich payload" not in "\n".join(content for _, content in coordinator._context)
    finally:
        await coordinator.close()


def test_model_observation_projects_ordered_sanitized_bounded_rich_output():
    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
        observations=CaptureObservations(),
    )
    origin = Origin("session", "request", "terminal", 4, "generation", "execution")
    request = ExecutionRequest(origin, "render()", "agent")
    html_payload = "<b>html-secret-payload</b>"
    image_payload = "encoded-image-payload" * 10_000
    result = ExecutionResult(
        origin,
        "success",
        output_events=(
            ExecutionOutput("stream", {"name": "stdout", "text": "before"}),
            ExecutionOutput("display", {
                "text/html": html_payload,
                "text/plain": "safe\x1b]52;c;terminal-secret\x07 fallback",
            }),
            ExecutionOutput("execute_result", {"image/png": image_payload}),
            ExecutionOutput("display", {"text/plain": "x" * 2_000}),
            ExecutionOutput("stream", {"name": "stderr", "text": "after"}),
        ),
    )

    packed = coordinator._packed_observation(request, result)
    events = packed["events"]
    serialized = json.dumps(packed)

    assert [event["event_index"] for event in events] == list(range(5))
    assert [event.get("stream", event.get("output_kind", "stream")) for event in events] == [
        "stdout", "display", "execute_result", "display", "stderr",
    ]
    assert events[1]["display"] == "safe fallback"
    assert events[2]["display"] == "[rich output omitted; available MIME types: image/png]"
    assert len(events[3]["display"]) <= 8_000
    assert all(
        event["execution_id"] == origin.execution_id
        and event["generation_id"] == origin.generation_id
        and event["author"] == "agent"
        for event in events
    )
    assert html_payload not in serialized
    assert image_payload not in serialized
    assert "terminal-secret" not in serialized
    assert "\x1b" not in serialized
    too_large = ExecutionResult(origin, "success", stdout="x" * 9_000)
    rejected = coordinator._packed_observation(request, too_large)
    assert rejected["status"] == "output_too_large"
    assert "x" * 100 not in json.dumps(rejected)
    foreign_origin = Origin(
        "other-session", "request", "terminal", 4, "generation", "other-execution",
    )
    foreign_result = ExecutionResult(
        foreign_origin,
        "success",
        output_events=(ExecutionOutput("display", {"text/plain": "foreign"}),),
    )
    with pytest.raises(RuntimeError, match="different execution origin"):
        coordinator._packed_observation(request, foreign_result)


@pytest.mark.asyncio
async def test_request_progress_is_attributed_ordered_awaited_and_not_replayed():
    class ProgressProvider:
        model = "fake/progress"

        def __init__(self):
            self.calls = 0

        async def generate(self, _request):
            self.calls += 1
            return ModelResponse("pass", reasoning="private chain of thought")

    class ProgressExecutor(CaptureExecutor):
        async def execute(self, request):
            self.requests.append(request)
            if request.author == "user":
                return ExecutionResult(request.origin, "success", stdout="direct")
            if sum(item.author == "agent" for item in self.requests) == 1:
                return ExecutionResult(
                    request.origin, "success",
                    output_events=(
                        ExecutionOutput("stream", {"name": "stdout", "text": "cell one"}),
                        ExecutionOutput("display", {"text/plain": "display one"}),
                    ),
                    say_outputs=(SayOutput("working"),),
                )
            return ExecutionResult(
                request.origin, "success",
                say_outputs=(SayOutput("finished", final=True),), final=True,
            )

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
    )
    provider, executor = ProgressProvider(), ProgressExecutor()
    coordinator.provider, coordinator.executor = provider, executor
    await coordinator.start()
    callback_events = []
    callback_entered = asyncio.Event()
    release_callback = asyncio.Event()

    async def on_progress(event: OutputEvent) -> None:
        callback_events.append(event)
        if event.kind == "progress" and event.data.get("phase") == "generation_start":
            callback_entered.set()
            await release_callback.wait()

    try:
        running = asyncio.create_task(coordinator.submit(
            "progress-client", "do two cells", on_progress=on_progress,
        ))
        await asyncio.wait_for(callback_entered.wait(), timeout=3)
        assert provider.calls == 0  # The next stage waits for callback backpressure.
        release_callback.set()
        submission = await asyncio.wait_for(running, timeout=3)
        assert submission.result.final is True
        assert tuple(callback_events) == submission.events
        assert [event.sequence for event in callback_events] == sorted(
            event.sequence for event in callback_events
        )
        assert [event.data.get("phase") for event in callback_events if event.kind == "progress"] == [
            "generation_start", "execution_start", "cell_complete",
            "generation_start", "execution_start", "cell_complete",
        ]
        assert [event.kind for event in callback_events if event.kind != "progress"] == [
            "stream", "display", "display", "display",
        ]
        assert all(event.origin.request_id == submission.action.origin.request_id for event in callback_events)
        assert "private chain of thought" not in repr(callback_events)

        first_callback_events = tuple(callback_events)
        second_events = []

        async def second_callback(event: OutputEvent) -> None:
            second_events.append(event)

        direct = await coordinator.submit(
            "progress-client", "@value = 3", on_progress=second_callback,
        )
        assert direct.result.status == "success"
        assert second_events
        assert {event.origin.request_id for event in second_events} == {
            direct.action.origin.request_id,
        }
        assert tuple(callback_events) == first_callback_events
    finally:
        release_callback.set()
        await coordinator.close()


@pytest.mark.asyncio
async def test_progress_callback_cancellation_invalidates_generation_without_late_delivery():
    class NeverNeededProvider:
        calls = 0

        async def generate(self, _request):
            self.calls += 1
            return ModelResponse("pass")

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
    )
    provider = NeverNeededProvider()
    coordinator.provider = provider
    coordinator.executor = CaptureExecutor()
    await coordinator.start()
    entered = asyncio.Event()
    delivered = []

    async def blocked_callback(event: OutputEvent) -> None:
        delivered.append(event)
        entered.set()
        await asyncio.Event().wait()

    try:
        running = asyncio.create_task(coordinator.submit(
            "cancel-client", "cancel during progress", on_progress=blocked_callback,
        ))
        await asyncio.wait_for(entered.wait(), timeout=3)
        await coordinator.interrupt()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert provider.calls == 0
        assert len(delivered) == 1
        assert delivered[0].data["phase"] == "generation_start"
        assert coordinator.state is State.IDLE
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_progress_stream_keeps_step_limit_and_error_status():
    class NonFinalProvider:
        async def generate(self, _request):
            return ModelResponse("pass")

    class NonFinalExecutor(CaptureExecutor):
        async def execute(self, request):
            self.requests.append(request)
            return ExecutionResult(request.origin, "success", stdout="cell completed")

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
        max_agent_steps=1,
    )
    coordinator.provider = NonFinalProvider()
    coordinator.executor = NonFinalExecutor()
    await coordinator.start()
    delivered = []

    async def collect(event: OutputEvent) -> None:
        delivered.append(event)

    try:
        submission = await coordinator.submit("status-client", "continue", on_progress=collect)
        limit = next(event for event in delivered if event.data.get("phase") == "step_limit")
        assert limit.data["status"] == "paused"
        assert "paused after 1 agent steps" in limit.data["text"]
        assert limit in submission.events
        assert "paused after 1 agent steps" in submission.message

        class ErrorExecutor(CaptureExecutor):
            async def execute(self, request):
                self.requests.append(request)
                return ExecutionResult(request.origin, "error", error="ValueError: cell failed")

        coordinator.executor = ErrorExecutor()
        error_events = []

        async def collect_error(event: OutputEvent) -> None:
            error_events.append(event)

        failed = await coordinator.submit(
            "status-client", "@raise_error()", on_progress=collect_error,
        )
        assert failed.result.status == "error"
        assert any(
            event.kind == "error" and event.data["evalue"] == "ValueError: cell failed"
            for event in error_events
        )
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_rich_only_fallback_reaches_the_next_model_request():
    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
    )
    provider, executor = TwoStepObservationProvider(), RichOnlyModelExecutor()
    coordinator.provider, coordinator.executor = provider, executor
    await coordinator.start()
    try:
        submission = await coordinator.submit("terminal", "inspect the rich result")
        assert submission.result.final is True
        assert len(provider.requests) == 2
        next_context = provider.requests[1].context.messages
        observations = [content for role, content in next_context if role == "observation"]
        assert any("visible rich fallback" in content for content in observations)
        assert all("must not enter the model context" not in content for content in observations)
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_terminal_uses_only_sanitized_text_plain_rich_fallback(monkeypatch):
    captured = []
    monkeypatch.setattr(
        plain_terminal, "print_formatted_text",
        lambda text, **_kwargs: captured.append(text),
    )
    event = OutputEvent(
        # Rich MIME is never emitted to a terminal; only the plain fallback is considered.
        Origin("session", "request", "terminal", 0),
        1,
        "display",
        {"text/html": "<script>bad()</script>", "text/plain": "safe\x1b]52;c;secret\x07\u202e"},
    )
    submission = type("SubmissionStub", (), {
        "message": "", "executions": (), "execution": None, "result": None,
        "events": (event,),
    })()

    with create_pipe_input() as terminal_input:
        terminal = plain_terminal.PlainTerminal(
            object(), input=terminal_input, output=DummyOutput(),
        )
        await terminal._show_submission(submission)

    rendered = "".join(to_plain_text(part) for part in captured)
    assert "safe" in rendered
    assert "<script>" not in rendered
    assert "secret" not in rendered
    assert "\x1b" not in rendered
    assert "\u202e" not in rendered


@pytest.mark.asyncio
async def test_agent_and_direct_executions_share_the_frontends_correlated_input_handler():
    class AgentInputProvider:
        async def generate(self, _request):
            return ModelResponse(
                "answer = input('agent prompt: ')\nsay('done', final=True)",
                provider_id="offline", model="offline",
            )

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
    )
    executor = InteractiveCaptureExecutor()
    coordinator.executor = executor
    coordinator.provider = AgentInputProvider()
    await coordinator.start()
    owners = []

    async def answer(request: InputRequest) -> InputReply:
        owners.append((request.owner_frontend_id, request.origin, request.password))
        return InputReply(
            request.origin, request.sequence, request.owner_frontend_id,
            value="same-frontend-answer", password=request.password,
        )

    try:
        agent = await coordinator.submit(
            "attached-client", "please ask for a value",
            allow_stdin=True, input_handler=answer,
        )
        direct = await coordinator.submit(
            "attached-client", "@direct_value = input('direct prompt: ')",
            allow_stdin=True, input_handler=answer,
        )
        assert agent.result.final is True
        assert direct.result.status == "success"
        assert [request.author for request in executor.requests] == ["agent", "user"]
        assert [owner for owner, _origin, _password in owners] == [
            "attached-client", "attached-client",
        ]
        assert owners[0][1].generation_id is not None
        assert owners[1][1].generation_id is None
        assert all(origin.execution_id is not None for _owner, origin, _password in owners)
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_terminal_submission_attaches_its_correlated_input_frontend():
    class SubmitCapture:
        async def submit(self, frontend_id, text, *, allow_stdin=False, input_handler=None):
            self.values = (frontend_id, text, allow_stdin, input_handler)
            return "submitted"

    terminal = plain_terminal.PlainTerminal.__new__(plain_terminal.PlainTerminal)
    terminal.coordinator = SubmitCapture()
    terminal.frontend_id = "terminal-a"
    terminal._request_input = lambda request: request

    assert await terminal._submit("@input('prompt')") == "submitted"
    assert terminal.coordinator.values[:3] == ("terminal-a", "@input('prompt')", True)
    assert terminal.coordinator.values[3] is terminal._request_input


@pytest.mark.asyncio
async def test_terminal_reports_input_unavailable_when_another_prompt_is_active():
    terminal = plain_terminal.PlainTerminal.__new__(plain_terminal.PlainTerminal)
    terminal.frontend_id = "terminal"
    terminal._prompt_lock = asyncio.Lock()
    await terminal._prompt_lock.acquire()
    request = InputRequest(
        Origin("session", "request", "terminal", 0, execution_id="execution"),
        1, "terminal", "value: ", False,
    )
    try:
        with pytest.raises(InputUnavailableError, match="already showing another prompt"):
            await terminal._request_input(request)
    finally:
        terminal._prompt_lock.release()


@pytest.mark.asyncio
async def test_input_wait_is_owned_by_its_frontend_and_serializes_other_cells():
    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
    )
    executor = InteractiveCaptureExecutor()
    coordinator.executor = executor
    await coordinator.start()
    prompt_started = asyncio.Event()
    release = asyncio.Event()
    input_replies = []

    async def input_handler(request: InputRequest) -> InputReply:
        prompt_started.set()
        assert coordinator.state is State.WAITING_FOR_INPUT
        input_replies.append(request)
        await release.wait()
        return InputReply(
            request.origin, request.sequence, request.owner_frontend_id,
            value="reply-private", password=request.password,
        )

    try:
        running = asyncio.create_task(coordinator.submit(
            "front-a", "@value = input('value: ')",
            allow_stdin=True, input_handler=input_handler,
        ))
        await asyncio.wait_for(prompt_started.wait(), timeout=3)
        assert coordinator.state is State.WAITING_FOR_INPUT
        with pytest.raises(RuntimeError, match="busy"):
            await coordinator.submit("front-b", "@other = 2")
        release.set()
        submission = await asyncio.wait_for(running, timeout=3)
        assert submission.result.status == "success"
        assert executor.input_request.owner_frontend_id == "front-a"
        assert executor.input_request.origin.execution_id == executor.requests[0].origin.execution_id
        assert executor.input_reply.owner_frontend_id == "front-a"
        assert input_replies[0].origin == executor.requests[0].origin
        assert coordinator.state is State.IDLE
    finally:
        release.set()
        await coordinator.close()


@pytest.mark.asyncio
async def test_password_stdin_value_never_enters_journal_or_visible_output(tmp_path):
    executor = LocalExecutor()
    runtime = PluginRuntime.load(builtins={
        "builtin": BuiltinPlugin(executor_factory=lambda: executor),
    })
    journal = journal_for(tmp_path)
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
        journal=journal,
    )
    secret = "coordinator-private-stdin-password"
    await coordinator.start()

    async def answer(request: InputRequest) -> InputReply:
        return InputReply(
            request.origin, request.sequence, request.owner_frontend_id,
            value=secret, password=request.password,
        )

    try:
        submission = await coordinator.submit(
            "terminal", "@import getpass\npassword = getpass.getpass('Password: ')\npassword",
            allow_stdin=True, input_handler=answer,
        )
        assert submission.result.status == "success"
        assert secret not in submission.result.stdout
        assert secret not in repr(submission.events)
        journal_path = journal.path
    finally:
        await coordinator.close()
    assert secret.encode() not in journal_path.read_bytes()


@pytest.mark.asyncio
async def test_interrupt_during_execution_keeps_the_session_and_namespace_usable():
    executor = LocalExecutor()
    runtime = PluginRuntime.load(builtins={
        "builtin": BuiltinPlugin(executor_factory=lambda: executor),
    })
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
    )
    await coordinator.start()
    running = asyncio.create_task(coordinator.submit(
        "terminal",
        "@import time\ninterrupt_kept_namespace = 41\ntime.sleep(30)",
    ))
    try:
        for _ in range(300):
            if (coordinator.state is State.EXECUTING
                    and executor._active_execution_id is not None):
                break
            await asyncio.sleep(.01)
        assert coordinator.state is State.EXECUTING
        assert executor._active_execution_id is not None
        await coordinator.interrupt()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=10)
        assert coordinator.state is State.IDLE
        follow_up = await asyncio.wait_for(
            coordinator.submit("terminal", "@print(interrupt_kept_namespace + 1)"), timeout=10,
        )
        assert follow_up.result.status == "success"
        assert "42" in follow_up.result.stdout
        assert coordinator.state is State.IDLE
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_journal_failure_prevents_execution_and_fails_session_without_replay(tmp_path):
    journal = journal_for(tmp_path)
    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(
        runtime,
        router="default",
        provider="fake",
        interpreter="basic",
        executor="local",
        journal=journal,
    )
    executor = CaptureExecutor()
    coordinator.executor = executor
    await coordinator.start()
    journal.db.execute(
        "CREATE TRIGGER fail_source BEFORE INSERT ON events WHEN NEW.kind='execution_source' "
        "BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END"
    )
    with pytest.raises(JournalError, match="stopped without replay"):
        await coordinator.submit("terminal", "@side_effect()")
    assert executor.requests == []
    assert coordinator.state is State.FAILED
    await coordinator.close()
    assert executor.closed == 1


def test_coordinator_defaults_to_an_explicit_no_persistence_service():
    coordinator = cli._build_coordinator("fake")
    assert isinstance(coordinator.journal, NoPersistenceJournal)


def test_cli_journal_flag_is_optional_and_forwarded_to_jupyter_kernel():
    path = Path("private/history.sqlite")
    args = cli.parser().parse_args(["--provider", "fake", "--journal", str(path)])
    assert args.journal == path
    assert "private SQLite" in cli.parser().format_help()
    management = cli._management_parser().parse_args([
        "kernel", "start", "--provider", "fake", "--journal", str(path),
    ])
    forwarded = cli._kernel_launch_arguments(management)
    assert forwarded[-2:] == ["--journal", str(path.resolve())]
    no_journal = cli.parser().parse_args(["--provider", "fake"])
    assert no_journal.journal is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["provider", "transport"])
async def test_transient_provider_failure_retries_without_reexecuting_cells(kind):
    class FlakyProvider:
        model = "offline/flaky"

        def __init__(self):
            self.calls = 0

        async def generate(self, _request):
            self.calls += 1
            if self.calls < 3:
                raise ProviderError("temporary outage", kind=kind)
            return ModelResponse("say('done', final=True)", provider_id="offline", model=self.model)

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(runtime, router="default", provider="fake",
                              interpreter="basic", executor="local")
    provider, executor = FlakyProvider(), CaptureExecutor()
    coordinator.provider, coordinator.executor = provider, executor
    await coordinator.start()
    try:
        submission = await coordinator.submit("terminal", "finish")
        assert provider.calls == 3
        assert len(executor.requests) == 1
        assert submission.result.final
        assert [event.data.get("phase") for event in submission.events].count("provider_retry") == 2
    finally:
        await coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["authentication", "configuration"])
async def test_authentication_or_configuration_provider_failure_is_not_retried(kind):
    class AuthFailure:
        model = "offline/auth"

        def __init__(self):
            self.calls = 0

        async def generate(self, _request):
            self.calls += 1
            raise ProviderError("authentication rejected", kind=kind)

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(runtime, router="default", provider="fake",
                              interpreter="basic", executor="local")
    provider, executor = AuthFailure(), CaptureExecutor()
    coordinator.provider, coordinator.executor = provider, executor
    await coordinator.start()
    try:
        with pytest.raises(ProviderError, match="authentication rejected"):
            await coordinator.submit("terminal", "finish")
        assert provider.calls == 1
        assert not executor.requests
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_default_agent_loop_continues_past_sixteen_cells_until_final():
    class RepeatingProvider:
        model = "offline/long-task"

        def __init__(self):
            self.calls = 0

        async def generate(self, _request):
            self.calls += 1
            return ModelResponse("pass", provider_id="offline", model=self.model)

    class EventuallyFinalExecutor(CaptureExecutor):
        async def execute(self, request):
            self.requests.append(request)
            if len(self.requests) == 17:
                return ExecutionResult(request.origin, "success", final=True,
                                       say_outputs=(SayOutput("done", final=True),))
            return ExecutionResult(request.origin, "success")

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(runtime, router="default", provider="fake",
                              interpreter="basic", executor="local")
    provider, executor = RepeatingProvider(), EventuallyFinalExecutor()
    coordinator.provider, coordinator.executor = provider, executor
    assert coordinator.max_agent_steps == 0
    await coordinator.start()
    try:
        submission = await coordinator.submit("terminal", "finish the long investigation")
        assert submission.result.final
        assert len(submission.executions) == provider.calls == 17
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_unlimited_agent_loop_still_bounds_consecutive_invalid_model_cells():
    class MalformedProvider:
        model = "offline/malformed"

        def __init__(self):
            self.calls = 0

        async def generate(self, _request):
            self.calls += 1
            return ModelResponse("Prose\n```python\npass\n```", provider_id="offline", model=self.model)

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(runtime, router="default", provider="fake",
                              interpreter="basic", executor="local")
    provider, executor = MalformedProvider(), CaptureExecutor()
    coordinator.provider, coordinator.executor = provider, executor
    await coordinator.start()
    try:
        submission = await coordinator.submit("terminal", "find a bug")
        assert provider.calls == 3
        assert not executor.requests
        assert "3 consecutive invalid" in submission.message
        assert coordinator.state is State.IDLE
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_long_agent_task_forces_collapse_without_resetting_or_losing_active_request():
    from py_agent.limits import Limits

    class ReportingProvider:
        model = "offline/collapse-required"

        def __init__(self):
            self.requests = []

        async def generate(self, request):
            self.requests.append(request)
            return ModelResponse("pass", usage={"normalized": {"input_tokens": 96}},
                                 provider_id="offline", model=self.model)

    class FinalOnSecondCell(CaptureExecutor):
        async def execute(self, request):
            self.requests.append(request)
            if len(self.requests) == 2:
                return ExecutionResult(request.origin, "success", final=True,
                                       say_outputs=(SayOutput("done", final=True),))
            return ExecutionResult(request.origin, "success")

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
        context=ProductionContextAdapter(limits=Limits(input_tokens=100)),
        observations=ProductionObservationAdapter(),
    )
    provider, executor = ReportingProvider(), FinalOnSecondCell()
    coordinator.provider, coordinator.executor = provider, executor
    await coordinator.start()
    try:
        submission = await coordinator.submit("terminal", "find the bug")
        assert not submission.result.final
        assert "3 consecutive invalid" in submission.message
        assert len(executor.requests) == 1  # Only the cell before forced mode may execute.
        assert [request.context.epoch for request in provider.requests] == [1, 1, 1, 1]
        for request in provider.requests[1:]:
            assert ("user", "[context boundary u1]\nfind the bug") in request.context.messages
            assert ("assistant", "pass") in request.context.messages
            assert any(role == "system" and "FORCED COLLAPSE MODE" in text
                       for role, text in request.context.messages)
        assert coordinator.context_service.force_collapse
        assert not coordinator.context_service.needs_reset()
        assert not executor.collapsed  # Rejected ordinary code never archives or evicts.
    finally:
        await coordinator.close()


def test_default_system_prompt_is_static_and_custom_context_is_not_rewritten():
    default = ProductionContextAdapter().snapshot()
    assert "You do not need to call say() between cells" in " ".join(default.messages[0][1].split())
    custom = ContextSnapshot(1, (("system", "Write one Python cell."),), (None,))
    assert ProductionContextAdapter().provider_messages(custom) == (
        {"role": "system", "content": "Write one Python cell."},
    )


@pytest.mark.asyncio
async def test_toolbar_cache_summary_uses_complete_weighted_reports():
    class UsageProvider:
        model = "offline/cache"

        def __init__(self):
            self.calls = 0

        async def generate(self, _request):
            self.calls += 1
            usage = (
                {"input_tokens": 100, "cache_read_tokens": 50, "cache_write_tokens": 20}
                if self.calls == 1 else
                {"input_tokens": 300, "cache_read_tokens": 270, "cache_write_tokens": 10}
            )
            return ModelResponse("pass", usage={"normalized": usage}, provider_id="offline", model=self.model)

    class TwoCellExecutor(CaptureExecutor):
        async def execute(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                return ExecutionResult(request.origin, "success")
            return ExecutionResult(request.origin, "success", final=True,
                                   say_outputs=(SayOutput("complete", final=True),))

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(runtime, router="default", provider="fake",
                              interpreter="basic", executor="local")
    coordinator.provider, coordinator.executor = UsageProvider(), TwoCellExecutor()
    await coordinator.start()
    try:
        await coordinator.submit("terminal", "calculate cache")
        assert coordinator.cache_summary == ("80", "320", "30")
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_oversized_agent_response_is_rejected_with_model_only_retry():
    class RepairingProvider:
        model = "offline/size-repair"

        def __init__(self):
            self.requests = []

        async def generate(self, request):
            self.requests.append(request)
            source = "#" + "x" * 8_000 if len(self.requests) == 1 else "say('done', final=True)"
            return ModelResponse(source, provider_id="offline", model=self.model)

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
    )
    provider, executor = RepairingProvider(), CaptureExecutor()
    coordinator.provider, coordinator.executor = provider, executor
    await coordinator.start()
    try:
        submission = await coordinator.submit("terminal", "handle this")
        assert submission.result.final
        assert len(provider.requests) == 2
        assert [item.source for item in executor.requests] == ["say('done', final=True)"]
        feedback = "\n".join(content for _, content in provider.requests[1].context.messages)
        assert "Try sending a smaller Python cell" in feedback
        assert "x" * 100 not in feedback
        assert not any(event.kind == "error" for event in submission.events)
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_agent_syntax_error_is_returned_to_model_without_executing_or_displaying_it():
    class RepairingProvider:
        model = "offline/repair"

        def __init__(self):
            self.requests = []

        async def generate(self, request):
            self.requests.append(request)
            source = "if :" if len(self.requests) == 1 else "say('repaired', final=True)"
            return ModelResponse(source, provider_id="offline", model=self.model)

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
    )
    provider, executor = RepairingProvider(), CaptureExecutor()
    coordinator.provider, coordinator.executor = provider, executor
    await coordinator.start()
    try:
        submission = await coordinator.submit("terminal", "fix the syntax")
        assert submission.result.final
        assert len(provider.requests) == 2
        assert [request.source for request in executor.requests] == ["say('repaired', final=True)"]
        assert any(
            "syntax_error" in content and '"executed":false' in content
            for role, content in provider.requests[1].context.messages if role == "observation"
        )
        assert not any(event.kind == "error" for event in submission.events)
        assert submission.message == "complete"
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_mixed_markdown_response_is_corrected_without_executing_rejected_text():
    class RepairingProvider:
        model = "offline/format-repair"

        def __init__(self):
            self.requests = []

        async def generate(self, request):
            self.requests.append(request)
            source = (
                "Here is the answer:\n```python\nprint('DO_NOT_EXECUTE')\n```"
                if len(self.requests) == 1 else "say('repaired', final=True)"
            )
            return ModelResponse(source, provider_id="offline", model=self.model)

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
    )
    provider, executor = RepairingProvider(), CaptureExecutor()
    coordinator.provider, coordinator.executor = provider, executor
    await coordinator.start()
    try:
        submission = await coordinator.submit("terminal", "complete the task")
        assert submission.result.final
        assert len(provider.requests) == 2
        assert [request.source for request in executor.requests] == ["say('repaired', final=True)"]
        next_messages = provider.requests[1].context.messages
        assert any("invalid format" in text for _, text in next_messages)
        assert any(
            "There is no external tool-call API" in text
            and "ordinary assistant message text" in text
            for _, text in next_messages
        )
        assert not any("DO_NOT_EXECUTE" in text for _, text in next_messages)
        assert any(event.kind == "progress" and event.data.get("phase") == "format_retry"
                   for event in submission.events)
        assert submission.message == "complete"
    finally:
        await coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("steering", [
    "Prioritize the failing test first",
    "/py_agent/production_services.py:74: self.context.contract = _PRODUCTION_CONTRACT\n"
    "Request failed: Codex request failed (SSLError); no source accepted",
])
async def test_queued_english_is_added_after_cell_observation_before_next_model_request(steering):
    class BlockingProvider:
        model = "offline/steering"

        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.requests = []
            self.cancelled = False

        async def generate(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                self.started.set()
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
            return ModelResponse("pass", provider_id="offline", model=self.model)

    class TwoCellExecutor(CaptureExecutor):
        async def execute(self, request):
            self.requests.append(request)
            if request.author == "user":
                return ExecutionResult(request.origin, "success", stdout="direct")
            if sum(item.author == "agent" for item in self.requests) == 1:
                return ExecutionResult(request.origin, "success", stdout="first cell")
            return ExecutionResult(
                request.origin, "success",
                say_outputs=(SayOutput("finished", final=True),), final=True,
            )

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
    )
    provider, executor = BlockingProvider(), TwoCellExecutor()
    coordinator.provider, coordinator.executor = provider, executor
    await coordinator.start()
    active = asyncio.create_task(coordinator.submit("terminal", "start task"))
    try:
        await asyncio.wait_for(provider.started.wait(), timeout=3)
        ticket = await coordinator.enqueue("terminal", steering)
        assert ticket.kind == "ask"
        assert ticket.position == 1
        assert ticket.origin.request_id
        assert len(provider.requests) == 1
        assert not provider.cancelled

        provider.release.set()
        submission = await asyncio.wait_for(active, timeout=3)
        outcome = await asyncio.wait_for(ticket.completion, timeout=3)
        assert submission.result.final
        assert outcome.status == "steered"
        assert outcome.origin == ticket.origin
        assert len(provider.requests) == 2
        next_messages = provider.requests[1].context.messages
        assert next_messages[-1] == ("user", steering)
        assert next_messages[-2][0] == "observation"
        assert provider.requests[1].origin.request_id == submission.action.origin.request_id
        assert [request.author for request in executor.requests] == ["agent", "agent"]
        assert all(request.origin.request_id == submission.action.origin.request_id
                   for request in executor.requests)
        assert not provider.cancelled
    finally:
        provider.release.set()
        await coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_transform", [False, True])
async def test_staged_steering_settles_when_next_request_cannot_dispatch(fail_transform):
    class BlockingProvider:
        model = "offline/steering-failure"

        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.requests = []

        async def generate(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                self.started.set()
                await self.release.wait()
            return ModelResponse("pass", provider_id="offline", model=self.model)

    class NonfinalExecutor(CaptureExecutor):
        async def execute(self, request):
            self.requests.append(request)
            return ExecutionResult(request.origin, "success", stdout="first cell")

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
    )
    provider = BlockingProvider()
    coordinator.provider, coordinator.executor = provider, NonfinalExecutor()
    original_transform = coordinator._run_model_transforms
    transforming = asyncio.Event()

    async def transform(request, *args):
        if request.origin.generation_id and len(provider.requests) == 1:
            # The first request has already dispatched; this is the next cell.
            transforming.set()
            if fail_transform:
                raise ValueError("transform rejected steering")
            await asyncio.Event().wait()
        return await original_transform(request, *args)

    coordinator._run_model_transforms = transform
    await coordinator.start()
    active = asyncio.create_task(coordinator.submit("terminal", "initial task"))
    try:
        await asyncio.wait_for(provider.started.wait(), timeout=3)
        ticket = await coordinator.enqueue("terminal", "queued steering")
        provider.release.set()
        await asyncio.wait_for(transforming.wait(), timeout=3)
        if fail_transform:
            with pytest.raises(ValueError, match="transform rejected steering"):
                await active
        else:
            await coordinator.interrupt()
            with pytest.raises(asyncio.CancelledError):
                await active
        outcome = await asyncio.wait_for(ticket.completion, timeout=3)
        assert outcome.status == ("failed" if fail_transform else "interrupted")
        assert len(provider.requests) == 1
        assert all(text != "queued steering" for _, text in coordinator._latest_context().messages)
    finally:
        provider.release.set()
        await coordinator.close()


@pytest.mark.asyncio
async def test_direct_queue_barrier_prevents_later_english_from_overtaking_fifo():
    class BlockingProvider:
        model = "offline/fifo"

        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.requests = []

        async def generate(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                self.started.set()
                await self.release.wait()
            return ModelResponse("pass", provider_id="offline", model=self.model)

    class OrderedExecutor(CaptureExecutor):
        async def execute(self, request):
            self.requests.append(request)
            if request.author == "user":
                return ExecutionResult(request.origin, "success", stdout="direct ran")
            agents = sum(item.author == "agent" for item in self.requests)
            if agents == 1:
                return ExecutionResult(request.origin, "success", stdout="cell one")
            return ExecutionResult(
                request.origin, "success",
                say_outputs=(SayOutput("done", final=True),), final=True,
            )

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
    )
    provider, executor = BlockingProvider(), OrderedExecutor()
    coordinator.provider, coordinator.executor = provider, executor
    await coordinator.start()
    active = asyncio.create_task(coordinator.submit("terminal", "first task"))
    try:
        await asyncio.wait_for(provider.started.wait(), timeout=3)
        direct = await coordinator.enqueue("terminal", "@queued_value = 4")
        later_english = await coordinator.enqueue("terminal", "English queued behind direct")
        assert (direct.kind, later_english.kind) == ("execute", "ask")
        assert (direct.position, later_english.position) == (1, 2)
        provider.release.set()
        await asyncio.wait_for(active, timeout=3)
        direct_outcome, english_outcome = await asyncio.wait_for(asyncio.gather(
            direct.completion, later_english.completion,
        ), timeout=3)

        assert direct_outcome.status == "completed"
        assert english_outcome.status == "steered"
        direct_submission = direct_outcome.submission
        assert isinstance(direct_submission, Submission)
        assert direct_submission.action.origin.request_id == direct.origin.request_id
        assert direct_submission.execution.origin.request_id == direct.origin.request_id
        assert [request.author for request in executor.requests] == ["agent", "user", "agent"]
        assert len(provider.requests) == 2
        assert all(
            "English queued behind direct" not in content
            for _role, content in provider.requests[0].context.messages
        )
        assert provider.requests[1].context.messages[-1] == (
            "user", "English queued behind direct",
        )
    finally:
        provider.release.set()
        await coordinator.close()


@pytest.mark.asyncio
async def test_queued_python_shell_magic_run_at_next_cell_boundary_before_model_call():
    first_generation = asyncio.Event()
    release_first = asyncio.Event()
    second_generation = asyncio.Event()
    release_second = asyncio.Event()
    observed_before_second = []

    class Provider:
        model = "offline/cell-boundary"
        calls = 0

        async def generate(self, _request):
            self.calls += 1
            if self.calls == 1:
                first_generation.set()
                await release_first.wait()
            elif self.calls == 2:
                observed_before_second.extend(request.author for request in executor.requests)
                second_generation.set()
                await release_second.wait()
            return ModelResponse("pass", provider_id="offline", model=self.model)

    class Executor(CaptureExecutor):
        async def execute(self, request):
            self.requests.append(request)
            if request.author == "agent" and sum(
                item.author == "agent" for item in self.requests
            ) == 2:
                return ExecutionResult(
                    request.origin, "success",
                    say_outputs=(SayOutput("done", final=True),), final=True,
                )
            if request.author == "user" and request.source == "len(outputs)":
                return ExecutionResult(request.origin, "success", output_events=(
                    ExecutionOutput("execute_result", {"text/plain": "17"}),
                ))
            return ExecutionResult(request.origin, "success", stdout=request.source)

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(runtime, router="default", provider="fake",
                              interpreter="basic", executor="local")
    executor = Executor()
    coordinator.executor, coordinator.provider = executor, Provider()
    await coordinator.start()
    active = asyncio.create_task(coordinator.submit("terminal", "do a long task"))
    try:
        await asyncio.wait_for(first_generation.wait(), timeout=3)
        sources = ("@len(outputs)", "!echo queued", "%time pass")
        tickets = [await coordinator.enqueue("terminal", text) for text in sources]
        release_first.set()
        outcomes = await asyncio.wait_for(asyncio.gather(
            *(ticket.completion for ticket in tickets),
        ), timeout=3)
        await asyncio.wait_for(second_generation.wait(), timeout=3)
        assert [outcome.status for outcome in outcomes] == ["completed"] * 3
        assert observed_before_second == ["agent", "user", "user", "user"]
        assert outcomes[0].submission.result.output_events[0].data["text/plain"] == "17"
        assert any(event.kind == "execute_result" and event.data["text/plain"] == "17"
                   for event in outcomes[0].submission.events)
        assert [outcome.submission.result.stdout for outcome in outcomes[1:]] == [
            "!echo queued", "%time pass",
        ]
        assert [outcome.submission.execution.origin.request_id for outcome in outcomes] == [
            ticket.origin.request_id for ticket in tickets
        ]
        assert not active.done()
        release_second.set()
        await asyncio.wait_for(active, timeout=3)
    finally:
        release_first.set()
        release_second.set()
        await coordinator.close()


@pytest.mark.asyncio
async def test_queued_command_and_later_direct_cell_run_at_agent_boundary():
    first_started, release_first = asyncio.Event(), asyncio.Event()
    second_started, release_second = asyncio.Event(), asyncio.Event()

    class Provider:
        model = "offline/command-boundary"
        calls = 0

        async def generate(self, _request):
            self.calls += 1
            if self.calls == 1:
                first_started.set()
                await release_first.wait()
            else:
                second_started.set()
                await release_second.wait()
            return ModelResponse("pass")

    class Executor(CaptureExecutor):
        async def execute(self, request):
            self.requests.append(request)
            if request.author == "agent" and sum(
                item.author == "agent" for item in self.requests
            ) == 2:
                return ExecutionResult(request.origin, "success", final=True,
                                       say_outputs=(SayOutput("done", final=True),))
            return ExecutionResult(request.origin, "success")

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(runtime, router="default", provider="fake",
                              interpreter="basic", executor="local")
    executor = Executor()
    coordinator.provider, coordinator.executor = Provider(), executor
    await coordinator.start()
    active = asyncio.create_task(coordinator.submit("terminal", "ongoing"))
    try:
        await asyncio.wait_for(first_started.wait(), 3)
        command = await coordinator.enqueue("terminal", "/unknown")
        direct = await coordinator.enqueue("terminal", "@len(outputs)")
        release_first.set()
        await asyncio.wait_for(second_started.wait(), 3)
        outcomes = await asyncio.wait_for(asyncio.gather(
            command.completion, direct.completion,
        ), 3)
        assert [outcome.status for outcome in outcomes] == ["completed", "completed"]
        assert outcomes[0].submission.action.origin == command.origin
        assert outcomes[0].submission.message == "Command service is not configured"
        assert [request.author for request in executor.requests] == ["agent", "user"]
        assert not active.done()
        assert coordinator.state is State.GENERATING
        release_second.set()
        await asyncio.wait_for(active, 3)
        assert [request.author for request in executor.requests] == ["agent", "user", "agent"]
    finally:
        release_first.set()
        release_second.set()
        await coordinator.close()


@pytest.mark.asyncio
async def test_queued_model_and_effort_commands_affect_next_generation_in_fifo_order():
    first_started, release_first = asyncio.Event(), asyncio.Event()
    second_started, release_second = asyncio.Event(), asyncio.Event()

    class Provider:
        model = "offline/original"

        def __init__(self):
            self.requests = []

        def set_model(self, model):
            self.model = model

        async def generate(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                first_started.set()
                await release_first.wait()
            else:
                second_started.set()
                await release_second.wait()
            return ModelResponse("pass")

    class Executor(CaptureExecutor):
        async def execute(self, request):
            self.requests.append(request)
            final = len(self.requests) == 2
            return ExecutionResult(request.origin, "success", final=final,
                                   say_outputs=(SayOutput("done", final=True),) if final else ())

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(runtime, router="default", provider="fake",
                              interpreter="basic", executor="local")
    provider = Provider()
    coordinator.provider, coordinator.executor = provider, Executor()
    coordinator.provider_id = "litelm"
    coordinator.model = provider.model
    await coordinator.start()
    active = asyncio.create_task(coordinator.submit("terminal", "ongoing"))
    try:
        await asyncio.wait_for(first_started.wait(), 3)
        tickets = [await coordinator.enqueue("other-frontend", source) for source in (
            "/model offline/next", "/model", "/effort high", "continue with new settings",
        )]
        assert provider.model == "offline/original"
        assert not any(ticket.completion.done() for ticket in tickets)
        release_first.set()
        await asyncio.wait_for(second_started.wait(), 3)
        outcomes = await asyncio.wait_for(asyncio.gather(
            *(ticket.completion for ticket in tickets),
        ), 3)
        assert [outcome.status for outcome in outcomes] == ["completed"] * 3 + ["steered"]
        assert outcomes[1].submission.message == "Model: offline/next (provider: litelm)"
        assert [outcome.submission.action.origin for outcome in outcomes[:3]] == [
            ticket.origin for ticket in tickets[:3]
        ]
        assert provider.requests[0].model == "offline/original"
        assert "effort" not in provider.requests[0].options
        assert provider.requests[1].model == "offline/next"
        assert provider.requests[1].options["effort"] == "high"
        assert provider.requests[1].context.messages[-1] == ("user", "continue with new settings")
        assert provider.requests[1].origin.request_id == provider.requests[0].origin.request_id
        assert not active.done()
        release_second.set()
        await asyncio.wait_for(active, 3)
    finally:
        release_first.set()
        release_second.set()
        await coordinator.close()


@pytest.mark.asyncio
async def test_interrupt_queued_async_command_settles_ticket_without_replay():
    first_started, release_first = asyncio.Event(), asyncio.Event()
    command_started = asyncio.Event()
    command_calls = []

    class Provider:
        model = "offline/command-cancellation"
        calls = 0

        async def generate(self, _request):
            self.calls += 1
            first_started.set()
            await release_first.wait()
            return ModelResponse("pass")

    class Executor(CaptureExecutor):
        async def execute(self, request):
            self.requests.append(request)
            return ExecutionResult(request.origin, "success")

    class Command:
        async def execute(self, arguments):
            command_calls.append(arguments)
            command_started.set()
            await asyncio.Event().wait()

    class CommandPlugin:
        @hookimpl
        def py_agent_register(self):
            return Contributions(
                PluginManifest("boundary-command"),
                commands=(CommandContribution("wait-command", lambda _config: Command()),),
            )

    runtime = PluginRuntime.load(builtins={
        "builtin": BuiltinPlugin(), "boundary-command": CommandPlugin(),
    })
    coordinator = Coordinator(runtime, router="default", provider="fake",
                              interpreter="basic", executor="local")
    provider, executor = Provider(), Executor()
    coordinator.provider, coordinator.executor = provider, executor
    await coordinator.start()
    active = asyncio.create_task(coordinator.submit("terminal", "ongoing"))
    try:
        await asyncio.wait_for(first_started.wait(), 3)
        steering = await coordinator.enqueue("terminal", "reserved steering")
        command = await coordinator.enqueue("other-frontend", "/wait-command once")
        direct = await coordinator.enqueue("terminal", "@must_not_run = True")
        release_first.set()
        await asyncio.wait_for(command_started.wait(), 3)
        assert coordinator.state is State.COMMAND
        assert not active.done()
        assert not command.completion.done()
        await coordinator.interrupt()
        with pytest.raises(asyncio.CancelledError):
            await active
        outcomes = await asyncio.wait_for(asyncio.gather(
            steering.completion, command.completion, direct.completion,
        ), 3)
        assert [outcome.status for outcome in outcomes] == ["interrupted"] * 3
        assert outcomes[1].origin == command.origin
        assert command_calls == ["once"]
        assert provider.calls == 1
        assert [request.author for request in executor.requests] == ["agent"]
        assert coordinator.pending_action_count == 0
        assert all(text != "reserved steering" for _, text in coordinator._latest_context().messages)
    finally:
        release_first.set()
        await coordinator.close()


@pytest.mark.asyncio
async def test_queued_direct_routes_and_commands_follow_an_immediate_final_cell():
    class BlockingProvider:
        model = "offline/direct-serialization"

        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def generate(self, _request):
            self.started.set()
            await self.release.wait()
            return ModelResponse("pass", provider_id="offline", model=self.model)

    class CaptureDirectExecutor(CaptureExecutor):
        async def execute(self, request):
            self.requests.append(request)
            if request.author == "agent":
                return ExecutionResult(
                    request.origin, "success",
                    say_outputs=(SayOutput("finished", final=True),), final=True,
                )
            return ExecutionResult(request.origin, "success", stdout=request.source)

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
    )
    provider, executor = BlockingProvider(), CaptureDirectExecutor()
    coordinator.provider, coordinator.executor = provider, executor
    await coordinator.start()
    active = asyncio.create_task(coordinator.submit("terminal", "active"))
    sources = ("@queued_value = 1", "!echo queued", "%time pass", "/unknown")
    try:
        await asyncio.wait_for(provider.started.wait(), timeout=3)
        tickets = [await coordinator.enqueue("terminal", source) for source in sources]
        assert [ticket.kind for ticket in tickets] == ["execute", "execute", "execute", "command"]
        assert executor.requests == []
        provider.release.set()
        await asyncio.wait_for(active, timeout=3)
        outcomes = await asyncio.wait_for(asyncio.gather(
            *(ticket.completion for ticket in tickets),
        ), timeout=3)
        assert all(outcome.status == "completed" for outcome in outcomes)
        user_requests = [request for request in executor.requests if request.author == "user"]
        assert [request.source for request in user_requests] == [
            "queued_value = 1", "!echo queued", "%time pass",
        ]
        assert [request.origin.request_id for request in user_requests] == [
            ticket.origin.request_id for ticket in tickets[:3]
        ]
        assert [request.author for request in executor.requests] == [
            "agent", "user", "user", "user",
        ]
    finally:
        provider.release.set()
        await coordinator.close()


@pytest.mark.asyncio
async def test_queued_english_after_final_cell_becomes_next_independent_request():
    class BlockingProvider:
        model = "offline/final-queue"

        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.requests = []

        async def generate(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                self.started.set()
                await self.release.wait()
            return ModelResponse("pass", provider_id="offline", model=self.model)

    class FinalExecutor(CaptureExecutor):
        async def execute(self, request):
            self.requests.append(request)
            return ExecutionResult(
                request.origin, "success",
                say_outputs=(SayOutput("finished", final=True),), final=True,
            )

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
    )
    provider, executor = BlockingProvider(), FinalExecutor()
    coordinator.provider, coordinator.executor = provider, executor
    await coordinator.start()
    active = asyncio.create_task(coordinator.submit("terminal", "finish this task"))
    try:
        await asyncio.wait_for(provider.started.wait(), timeout=3)
        ticket = await coordinator.enqueue("terminal", "Do a separate follow-up")
        provider.release.set()
        current = await asyncio.wait_for(active, timeout=3)
        outcome = await asyncio.wait_for(ticket.completion, timeout=3)
        assert current.result.final
        assert outcome.status == "completed"
        assert isinstance(outcome.submission, Submission)
        assert outcome.submission.action.origin.request_id == ticket.origin.request_id
        assert len(provider.requests) == 2
        assert provider.requests[0].origin.request_id == current.action.origin.request_id
        assert provider.requests[1].origin.request_id == ticket.origin.request_id
        assert provider.requests[1].context.messages[-1] == ("user", "Do a separate follow-up")
        assert [request.origin.request_id for request in executor.requests] == [
            current.action.origin.request_id, ticket.origin.request_id,
        ]
    finally:
        provider.release.set()
        await coordinator.close()


@pytest.mark.asyncio
async def test_pending_queue_is_bounded_and_interrupt_fails_closed_without_replay():
    class BlockingProvider:
        model = "offline/bounded-queue"

        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = False

        async def generate(self, _request):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
    )
    provider, executor = BlockingProvider(), CaptureExecutor()
    coordinator.provider, coordinator.executor = provider, executor
    await coordinator.start()
    active = asyncio.create_task(coordinator.submit("terminal", "active request"))
    tickets = []
    try:
        await asyncio.wait_for(provider.started.wait(), timeout=3)
        for index in range(MAX_PENDING_ACTIONS):
            tickets.append(await coordinator.enqueue("terminal", f"@queued_{index} = {index}"))
        assert coordinator.pending_action_count == MAX_PENDING_ACTIONS
        with pytest.raises(QueueFullError, match="no action was accepted"):
            await coordinator.enqueue("terminal", "@overflow = True")
        assert not provider.cancelled
        await coordinator.interrupt()
        with pytest.raises(asyncio.CancelledError):
            await active
        outcomes = await asyncio.gather(*(ticket.completion for ticket in tickets))
        assert all(outcome.status == "interrupted" for outcome in outcomes)
        assert provider.cancelled
        assert executor.requests == []
        assert coordinator.pending_action_count == 0
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_repeated_format_failures_do_not_repeat_the_full_contract():
    class AlwaysBadProvider:
        model = "offline/format-loop"

        def __init__(self):
            self.requests = []

        async def generate(self, request):
            self.requests.append(request)
            fence = chr(96) * 3
            return ModelResponse(
                "Here is the code:" + "\n" + fence + "python\npass\n" + fence)

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(
        runtime, router="default", provider="fake", interpreter="basic", executor="local",
    )
    provider, executor = AlwaysBadProvider(), CaptureExecutor()
    coordinator.provider, coordinator.executor = provider, executor
    await coordinator.start()
    try:
        submission = await coordinator.submit("terminal", "complete the task")
        assert len(provider.requests) == 3
        assert not executor.requests
        assert "3 consecutive invalid" in submission.message
        corrections = [
            message
            for request in provider.requests[1:]
            for role, message in request.context.messages
            if role == "observation" and "mixed or malformed Markdown" in message
        ]
        assert len(corrections) >= 2
        full = next(c for c in corrections if "ordinary assistant message text" in c)
        terse = next(c for c in reversed(corrections) if "Still no valid cell" in c)
        assert terse is not full
        assert len(terse) < len(full)
        assert "mixed or malformed Markdown" in terse
    finally:
        await coordinator.close()


def test_coordinator_components_own_state_without_duplicate_facade_storage():
    first = cli._build_coordinator("fake")
    second = cli._build_coordinator("fake")

    for owner in (first._conversation, first._frontend, first._lifecycle,
                  first._journal, first._observations):
        assert owner.coordinator is first
    assert "_context" not in vars(first)
    assert "_pending_actions" not in vars(first)
    assert "_event_sequence" not in vars(first)
    assert "state" not in vars(first)

    first._context.append(("user", "isolated"))
    assert first._conversation._context == [("user", "isolated")]
    assert second._context == []
    first._context = [("assistant", "replacement")]
    assert first._conversation._context == [("assistant", "replacement")]

    first.state = State.IDLE
    assert first._lifecycle.state is State.IDLE
    first._lifecycle.state = State.NEW
    assert first.state is State.NEW
    first.best_effort_observer_failures = 3
    assert first._frontend.best_effort_observer_failures == 3
    assert second.best_effort_observer_failures == 0
    assert first._lock is first._lifecycle._lock
    assert first._lock is not second._lock
    assert "_cache_totals" not in vars(first)
    first._cache_totals["input_tokens"] = 100
    first._cache_totals["cache_read_tokens"] = 25
    first._cache_totals["cache_write_tokens"] = 10
    first._cache_reports = 1
    assert first._journal.cache_summary == ("25", "25", "10")
    assert first.cache_summary == first._journal.cache_summary
    assert second.cache_summary == ("?", "?", "?")


@pytest.mark.asyncio
async def test_coordinator_components_preserve_public_lifecycle_and_dispatch():
    coordinator = cli._build_coordinator("fake")
    executor = CaptureExecutor()
    coordinator.executor = executor
    await coordinator.start()
    try:
        assert coordinator.state is State.IDLE
        assert coordinator._lifecycle.state is State.IDLE
        submission = await coordinator.submit("terminal", "@value = 42")
        assert submission.execution.source == "value = 42"
        assert executor.requests[0].source == "value = 42"
        assert coordinator.state is State.IDLE
        assert not coordinator.queue_active
        assert coordinator.pending_action_count == 0
    finally:
        await coordinator.close()
    assert coordinator._lifecycle.state is State.CLOSED
    assert executor.closed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["connect", "stream"])
@pytest.mark.parametrize("certificate", [False, True])
async def test_codex_tls_retry_is_automatic_and_never_executes_failed_stream(
    monkeypatch, stage, certificate,
):
    import ssl
    from types import SimpleNamespace

    import httpx

    from py_agent import codex
    from py_agent.production_services import ProductionProviderAdapter

    monkeypatch.setattr(codex, "read_codex_credentials", lambda _: SimpleNamespace(
        access="test-secret", account_id="test-account", expires=9999999999999,
    ))
    calls = []

    class Stream(httpx.AsyncByteStream):
        def __init__(self, fail):
            self.fail = fail

        async def __aiter__(self):
            # Even complete-looking source must not execute from a failed stream.
            event = {"type": "response.completed", "response": {
                "id": "mock", "status": "completed",
                "output": [{"type": "message", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": "say('done', final=True)"}]}],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            }}
            yield ("data: " + json.dumps(event) + "\n\n").encode()
            if self.fail:
                error_type = ssl.SSLCertVerificationError if certificate else ssl.SSLError
                raise error_type(ssl.SSL_ERROR_SSL, "private TLS detail test-secret")

        async def aclose(self):
            pass

    def handle(request):
        calls.append(request)
        fail = len(calls) == 1
        if fail and stage == "connect":
            error_type = ssl.SSLCertVerificationError if certificate else ssl.SSLError
            raise error_type(ssl.SSL_ERROR_SSL, "private TLS detail test-secret")
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=Stream(fail))

    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(runtime, router="default", provider="fake",
                              interpreter="basic", executor="local")
    adapter = codex.CodexProvider("openai-codex/gpt-test", transport=httpx.MockTransport(handle))
    coordinator.provider = ProductionProviderAdapter(adapter, provider_id="codex")
    executor = CaptureExecutor()
    coordinator.executor = executor
    await coordinator.start()
    try:
        if certificate:
            with pytest.raises(ProviderError) as caught:
                await coordinator.submit("terminal", "finish")
            assert caught.value.kind == "configuration"
            assert len(calls) == 1
            assert not executor.requests
            assert "test-secret" not in str(caught.value)
            assert "private TLS detail" not in str(caught.value)
        else:
            submission = await coordinator.submit("terminal", "finish")
            retries = [event for event in submission.events if event.data.get("phase") == "provider_retry"]
            assert len(calls) == 2
            assert len(executor.requests) == 1
            assert submission.result.final
            assert len(retries) == 1
            assert "test-secret" not in str(submission.events)
            assert "private TLS detail" not in str(submission.events)
    finally:
        await coordinator.close()
