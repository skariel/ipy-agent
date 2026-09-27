"""Tests for the coordinator-facing terminal adapter; no live provider calls."""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
import pytest

from py_agent import cli
from py_agent.builtin_services import BuiltinPlugin, FakeProvider
from py_agent.configuration import ConfigField, ConfigRegistry, ConfigStore
from py_agent.contracts import (
    ExecutionRequest, ExecutionResult, ExecutorCapabilities, InputRequest, Origin,
    OutputEvent, QueueOutcome, QueueTicket, RoutedAction, SayOutput,
)
from py_agent.coordinator import State, Submission
from py_agent.local_executor import LocalExecutor
from py_agent.plugins import (
    CommandContribution, Contributions, PluginError, PluginManifest, PluginRuntime, Service,
    TransformContribution, hookimpl,
)
from py_agent.production_services import ProductionServicesPlugin
import py_agent.plain_terminal as plain_terminal
from py_agent.plain_terminal import HELP, PlainTerminal, sanitize
from py_agent.session_journal import SQLiteSessionJournal


class Output(DummyOutput):
    def __init__(self):
        self.parts = []

    def write(self, data):
        self.parts.append(data)

    def write_raw(self, data):
        self.parts.append(data)

    def get_size(self):
        return Size(rows=24, columns=80)

    @property
    def text(self):
        return "".join(self.parts)


class InertExecutor:
    capabilities = ExecutorCapabilities(persistent=True, interrupt=True)

    def __init__(self):
        self.requests = []
        self.closed = False
        self.collapsed = {}

    async def store_collapsed(self, text):
        index = len(self.collapsed) + 1
        self.collapsed[index] = text
        return index

    async def start(self):
        return None

    async def execute(self, request):
        self.requests.append(request)
        return ExecutionResult(request.origin, "success")

    async def interrupt(self):
        return None

    async def close(self):
        self.closed = True


class CoordinatorStub:
    def __init__(self):
        self.state = State.IDLE
        self.submissions = []
        self.interruptions = 0

    async def submit(self, frontend_id, text):
        self.submissions.append((frontend_id, text))
        origin = Origin("session", f"request-{len(self.submissions)}", frontend_id, 0)
        action = RoutedAction(origin, "ask", text)
        if text.startswith("/unconfigured"):
            return Submission(action, message="Command service is not configured")
        return Submission(
            action,
            ExecutionResult(origin, "success", stdout="ran: " + text + "\n"),
        )

    async def interrupt(self):
        self.interruptions += 1


async def until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), 3)


def test_output_panels_do_not_prepend_a_plain_blank_line():
    for render in (
        lambda terminal: (terminal._render_stream("stdout", "out\n"), terminal._finish_stream()),
        lambda terminal: (terminal._render_stream("stderr", "err\n"), terminal._finish_stream()),
        lambda terminal: terminal._render_say("message"),
        lambda terminal: terminal._render_display("Out", "result"),
    ):
        output = Output()
        terminal = PlainTerminal(CoordinatorStub(), output=output)
        render(terminal)
        assert output.text and not output.text.startswith("\n")


def test_main_branch_prompt_toolbar_and_markdown_panels_without_trusting_escapes():
    from py_agent.terminal_markdown import markdown_fragments

    coordinator, output = CoordinatorStub(), Output()
    coordinator.model = "fake/deterministic"
    terminal = PlainTerminal(coordinator, output=output)
    assert "In [1]: " in str(terminal._prompt_message())
    assert "IDLE | fake/deterministic" in str(terminal._toolbar())
    terminal._render_say("# Heading\n- **bold** and `code`\n\x1b]52;c;secret\x07")
    assert "Heading" in output.text
    assert "• bold and code" in output.text
    assert "secret" not in output.text
    assert "\x1b" not in output.text
    assert any(text == "bold" and style == "class:md-bold"
               for style, text in markdown_fragments("**bold**"))

    colored = PlainTerminal(coordinator, output=output)
    colored.no_color = False
    rendered = []
    colored._write_raw = rendered.append
    colored._render_stream("stdout", "hello\n")
    colored._finish_stream()
    assert "\x1b[48;2;40;50;40m" in "".join(rendered)
    assert "stdout [?]:" in "".join(rendered)


def test_provisional_stream_is_hidden_and_completed_output_appears_once():
    origin = Origin("session", "request", "terminal", 0, execution_id="exec-1")
    output = Output()
    terminal = PlainTerminal(CoordinatorStub(), output=output)
    terminal._show_live_event(OutputEvent(
        origin, 1, "stream", {"name": "stdout", "text": "early line\n"},
        metadata={"provisional": True},
    ))
    assert output.text == ""
    terminal._show_live_event(OutputEvent(
        origin, 2, "stream", {"name": "stdout", "text": "early line\nlater line\n"},
    ))
    terminal._show_live_event(OutputEvent(origin, 3, "progress", {"phase": "cell_complete", "status": "success"}))
    assert output.text.count("early line") == 1
    assert output.text.count("later line") == 1
    assert output.text.count("stdout [?]:") == 1
    assert "live preview" not in output.text
    assert "continued" not in output.text


def test_sanitize_removes_terminal_control_and_spoofing_characters():
    assert sanitize("safe\x1b]52;c;SECRET\x07\x1b[2J\u202e text") == "safe text"
    assert sanitize("a\r\nb\rc\x00") == "a\nb\nc"


def test_phase1_builder_selects_explicit_builtin_services_and_local_executor():
    coordinator = cli._build_coordinator("fake")

    assert coordinator.router.__class__.__name__ == "DefaultRouter"
    assert isinstance(coordinator.provider, FakeProvider)
    assert coordinator.interpreter.__class__.__name__ == "BasicInterpreter"
    assert isinstance(coordinator.executor, LocalExecutor)
    assert coordinator.executor.capabilities.persistent
    assert coordinator.executor.capabilities.interrupt
    assert {"config", "plugins"} <= coordinator.command_registry.commands.keys()
    assert "history" not in coordinator.command_registry.commands


def test_default_cli_is_coordinator_first_and_requires_explicit_provider():
    args = cli.parser().parse_args([])
    help_text = cli.parser().format_help()
    assert args.provider is None
    assert "--network" not in help_text
    assert "not a language model" in help_text
    with pytest.raises(ValueError, match="Select --provider"):
        cli._initial_config(args)
    custom = cli.parser().parse_args(["--provider", "third-party"])
    with pytest.raises(PluginError, match="No enabled provider"):
        cli._initial_config(custom)


def test_default_cli_offers_explicit_production_providers_without_network_calls():
    args = cli.parser().parse_args(["--provider", "litelm", "--model", "openai/test"])
    store, provider, model, *_ = cli._initial_config(args)
    assert provider == "litelm"
    assert model == "openai/test"
    coordinator = cli._build_coordinator(provider, model=model, config_store=store)
    assert coordinator.provider_id == "litelm"
    assert coordinator.provider.model == "openai/test"


def test_default_cli_activates_only_trusted_config_plugins_and_applies_plugin_config(
    monkeypatch, tmp_path,
):
    plugin_id = "cli-config-example"
    loaded = []

    class GreetingCommand:
        def __init__(self, greeting):
            self.greeting = greeting

        def execute(self, arguments):
            return f"{self.greeting}, {arguments}"

    class GreetingRouter:
        def __init__(self, greeting):
            self.greeting = greeting

        def route(self, action):
            return RoutedAction(action.origin, "ask", action.text)

    class ExamplePlugin:
        @hookimpl
        def py_agent_register(self):
            return Contributions(
                PluginManifest(plugin_id),
                services=(Service(
                    "router", "configured",
                    lambda config: GreetingRouter(config[plugin_id]["greeting"]),
                ),),
                config_fields=(ConfigField(
                    f"{plugin_id}.greeting", plugin_id, str, "hello",
                ),),
                commands=(CommandContribution(
                    "greet", lambda config: GreetingCommand(config["greeting"]),
                ),),
            )

    class EntryPoint:
        name = plugin_id
        value = "cli_config_example:plugin"
        dist = SimpleNamespace(name="cli-config-example-dist")

        def load(self):
            loaded.append(self.name)
            return ExamplePlugin()

    monkeypatch.setattr("py_agent.plugins.metadata.entry_points", lambda **_kwargs: [EntryPoint()])
    config_path = tmp_path / "trusted.json"
    config_path.write_text(
        '{"provider.id":"fake","plugins.enabled":"cli-config-example",'
        '"cli-config-example.greeting":"salut"}\n',
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    args = cli.parser().parse_args(["--config", str(config_path), "--router", "configured"])
    configured = cli._prepare_configuration(args)
    assert loaded == [plugin_id]
    assert configured.store.snapshot.get(f"{plugin_id}.greeting") == "salut"
    assert configured.runtime.order == ("builtin", "cli-config-example", "production")

    coordinator = cli._build_coordinator(
        configured.provider,
        model=configured.model,
        config_store=configured.store,
        config_path=configured.config_path,
        runtime=configured.runtime,
        router=configured.router,
        interpreter=configured.interpreter,
        executor=configured.executor,
        executor_wrappers=configured.executor_wrappers,
    )
    assert coordinator.router.greeting == "salut"
    assert asyncio.run(coordinator._dispatch_command("greet Ada", configured.store.snapshot)) == "salut, Ada"


def test_default_cli_requires_explicit_enabled_services_and_rejects_command_collisions(monkeypatch):
    class CollisionPlugin:
        @hookimpl
        def py_agent_register(self):
            return Contributions(
                PluginManifest("collision-example"),
                commands=(CommandContribution("help", lambda _config: object()),),
            )

    class EntryPoint:
        name = "collision-example"
        value = "collision_example:plugin"
        dist = SimpleNamespace(name="collision-example")

        def load(self):
            return CollisionPlugin()

    monkeypatch.setattr("py_agent.plugins.metadata.entry_points", lambda **_kwargs: [EntryPoint()])
    args = cli.parser().parse_args(["--provider", "fake", "--plugin", "collision-example"])
    configured = cli._prepare_configuration(args)
    with pytest.raises(PluginError, match="collide with built-in commands"):
        cli._build_coordinator(
            configured.provider,
            config_store=configured.store,
            runtime=configured.runtime,
            router=configured.router,
            interpreter=configured.interpreter,
            executor=configured.executor,
        )

    unknown = cli.parser().parse_args(["--provider", "fake", "--executor", "missing"])
    with pytest.raises(PluginError, match="No enabled executor"):
        cli._prepare_configuration(unknown)


def test_default_cli_selects_only_named_plugin_stages(monkeypatch):
    plugin_id = "stage-selection-example"

    class StagePlugin:
        @hookimpl
        def py_agent_register(self):
            return Contributions(
                PluginManifest(plugin_id),
                transforms=(
                    TransformContribution("context", "one", lambda _config: object()),
                    TransformContribution("context", "two", lambda _config: object()),
                ),
            )

    class EntryPoint:
        name = plugin_id
        value = "stage_selection_example:plugin"
        dist = SimpleNamespace(name=plugin_id)

        def load(self):
            return StagePlugin()

    monkeypatch.setattr("py_agent.plugins.metadata.entry_points", lambda **_kwargs: [EntryPoint()])
    selected = cli._prepare_configuration(cli.parser().parse_args([
        "--provider", "fake", "--plugin", plugin_id,
        "--context-transform", f"{plugin_id}:one",
    ]))
    assert tuple(stage.qualified_name for stage in selected.runtime.transforms["context"]) == (
        f"{plugin_id}:one",
    )

    with pytest.raises(PluginError, match="No enabled context transform"):
        cli._prepare_configuration(cli.parser().parse_args([
            "--provider", "fake", "--plugin", plugin_id,
            "--context-transform", f"{plugin_id}:missing",
        ]))


def test_default_cli_rejects_non_tty_without_importing_or_starting_sandbox(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert cli.main([]) == 2
    assert "requires TTY" in capsys.readouterr().err


async def test_frontend_routes_submission_and_renders_result_safely():
    coordinator, output = CoordinatorStub(), Output()
    with create_pipe_input() as pipe:
        terminal = PlainTerminal(coordinator, input=pipe, output=output)
        task = asyncio.create_task(terminal.run())
        await until(lambda: terminal.session.app.is_running)
        pipe.send_text("@answer = 42\r!pwd\rordinary request\r/quit\r")
        await asyncio.wait_for(task, 3)

    assert coordinator.submissions == [
        ("terminal", "@answer = 42"),
        ("terminal", "!pwd"),
        ("terminal", "ordinary request"),
    ]
    assert "ran: @answer = 42" in output.text
    assert "ran: !pwd" in output.text
    assert "ran: ordinary request" in output.text


async def test_terminal_renders_live_progress_once_without_identifier_spam():
    request_origin = Origin("session", "request-long-id", "terminal", 0, "generation-long-id")
    execution_origin = Origin(
        "session", "request-long-id", "terminal", 0,
        "generation-long-id", "execution-long-id",
    )
    events = (
        OutputEvent(request_origin, 1, "progress", {
            "phase": "generation_start", "text": "Agent: requesting a response (step 1).",
        }),
        OutputEvent(execution_origin, 2, "progress", {
            "phase": "execution_start", "step": 1, "text": "Agent: executing cell (step 1).",
        }, author="agent"),
        OutputEvent(execution_origin, 3, "stream", {"name": "stdout", "text": "cell output\n"}, author="agent"),
        OutputEvent(
            execution_origin, 4, "display", {"text/plain": "hello"},
            metadata={"py_agent_source": "say", "final": True}, author="agent",
        ),
    )
    execution = ExecutionRequest(execution_origin, "pass", "agent")
    result = ExecutionResult(
        execution_origin, "success", stdout="cell output\n",
        say_outputs=(SayOutput("hello", final=True),), final=True,
    )
    started, release = asyncio.Event(), asyncio.Event()

    class StreamingCoordinator(CoordinatorStub):
        async def submit(self, frontend_id, text, *, on_progress=None):
            self.submissions.append((frontend_id, text))
            await on_progress(events[0])
            started.set()
            await release.wait()
            for event in events[1:]:
                await on_progress(event)
            origin = Origin("session", "request-long-id", frontend_id, 0)
            action = RoutedAction(origin, "ask", text)
            return Submission(
                action, result=result, message="hello", execution=execution,
                say_outputs=(SayOutput("hello", final=True),),
                executions=((execution, result),), events=events,
            )

    coordinator, output = StreamingCoordinator(), Output()
    with create_pipe_input() as pipe:
        terminal = PlainTerminal(coordinator, input=pipe, output=output)
        task = asyncio.create_task(terminal.run())
        await until(lambda: terminal.session.app.is_running)
        pipe.send_text("do it\r")
        await asyncio.wait_for(started.wait(), timeout=3)
        assert "Agent working…" not in output.text  # state lives in the toolbar
        assert "hello" not in output.text
        pipe.send_text("/quit\r")
        release.set()
        await asyncio.wait_for(task, 3)

    assert output.text.count("hello") == 1
    assert output.text.count("cell output") == 1
    assert "Running agent cell 1…" not in output.text
    assert "Agent: executing cell (step 1)." not in output.text
    assert "request-long-id" not in output.text
    assert "execution-long-id" not in output.text


async def test_frontend_commands_are_local_and_unknown_command_is_not_hidden():
    coordinator, output = CoordinatorStub(), Output()
    with create_pipe_input() as pipe:
        terminal = PlainTerminal(coordinator, input=pipe, output=output)
        task = asyncio.create_task(terminal.run())
        await until(lambda: terminal.session.app.is_running)
        pipe.send_text("/help\r/status\r/interrupt\r/unconfigured\r/quit\r")
        await asyncio.wait_for(task, 3)

    assert coordinator.submissions == [("terminal", "/unconfigured")]
    assert coordinator.interruptions == 1
    assert "@python" in output.text
    assert "Coordinator state: idle" in output.text
    assert "Command service is not configured" in output.text
    assert "help" in HELP
    assert "/history search QUERY" in HELP


async def test_frontend_recovers_from_submission_error_and_sanitizes_untrusted_output():
    coordinator, output = CoordinatorStub(), Output()

    async def submit(frontend_id, text):
        coordinator.submissions.append((frontend_id, text))
        if text == "one":
            raise RuntimeError("submission failed")
        origin = Origin("session", "request-safe", frontend_id, 0)
        action = RoutedAction(origin, "execute", text, "ipython")
        return Submission(
            action,
            ExecutionResult(
                origin,
                "error",
                error="bad\x1b]52;c;hidden\x07",
                stdout="visible\x1b[2J\n",
            ),
        )

    coordinator.submit = submit
    with create_pipe_input() as pipe:
        terminal = PlainTerminal(coordinator, input=pipe, output=output)
        task = asyncio.create_task(terminal.run())
        await until(lambda: terminal.session.app.is_running)
        pipe.send_text("one\rtwo\r/quit\r")
        await asyncio.wait_for(task, 3)

    assert "Request failed: submission failed" in output.text
    assert "visible" in output.text
    assert "Execution error: bad" in output.text
    assert "hidden" not in output.text
    assert "\x1b" not in output.text


async def test_history_without_persistence_is_clear_and_never_executes_history_text():
    coordinator = cli._build_coordinator("fake")
    executor = InertExecutor()
    coordinator.executor = executor
    await coordinator.start()
    try:
        submission = await coordinator.submit("terminal", "/history")
        assert submission.action.kind == "command"
        assert "persistence is disabled" in submission.message
        assert "no-persistence journal" in submission.message
        assert "replayed" in submission.message
        assert executor.requests == []
    finally:
        await coordinator.close()


async def test_history_search_and_pages_redact_sensitive_config_and_are_read_only(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    secret = "sensitive-config-value"
    registry = ConfigRegistry((
        *cli._core_config_registry().fields.values(),
        ConfigField("plugin.private_value", "core", str, secret, sensitive=True),
    ))
    store = ConfigStore(registry)
    journal = SQLiteSessionJournal(private / "history.sqlite")
    coordinator = cli._build_coordinator("fake", config_store=store, journal=journal)
    executor = InertExecutor()
    coordinator.executor = executor
    await coordinator.start()
    event = journal._append(
        "note", coordinator.session_id, "request-old", 0,
        {"text": f"searchable print({secret!r})"},
    )
    try:
        searched = await coordinator.submit("terminal", "/history search searchable")
        assert "excerpt hidden to protect sensitive configuration" in searched.message
        assert secret not in searched.message

        page = await coordinator.submit(
            "terminal", f"/history page {event['id']} 0 4000",
        )
        assert "Journal event" in page.message
        assert secret not in page.message
        assert "[REDACTED]" in page.message
        assert secret.encode() not in journal.path.read_bytes()

        oversized_recent = await coordinator.submit("terminal", "/history recent 21")
        oversized_page = await coordinator.submit(
            "terminal", f"/history page {event['id']} 0 4001",
        )
        oversized_query = await coordinator.submit(
            "terminal", "/history search " + ("q" * 257),
        )
        replay_attempt = await coordinator.submit(
            "terminal", f"/history replay {event['id']}",
        )
        assert "Usage: /history" in replay_attempt.message
        assert "Usage: /history" in oversized_recent.message
        assert "Usage: /history" in oversized_page.message
        assert "Usage: /history" in oversized_query.message
        assert executor.requests == []
    finally:
        await coordinator.close()


def test_history_command_id_cannot_collide_with_external_plugin_command():
    class HistoryPlugin:
        @hookimpl
        def py_agent_register(self):
            return Contributions(
                PluginManifest("history-collision"),
                commands=(CommandContribution("history", lambda _config: object()),),
            )

    runtime = PluginRuntime.load(builtins={
        "builtin": BuiltinPlugin(),
        "production": ProductionServicesPlugin(),
        "history-collision": HistoryPlugin(),
    })
    with pytest.raises(PluginError, match="history"):
        cli._build_coordinator("fake", runtime=runtime)


async def test_default_startup_announces_fake_provider_and_unrestricted_permissions(monkeypatch, capsys):
    calls = []

    class Coordinator:
        async def start(self):
            calls.append("start")

        async def close(self):
            calls.append("close")

    class Terminal:
        def __init__(self, coordinator, **kwargs):
            assert isinstance(coordinator, Coordinator)

        async def run(self):
            calls.append("terminal")

    monkeypatch.setattr(cli, "_build_coordinator", lambda provider, **kwargs: Coordinator())
    monkeypatch.setattr(cli, "PlainTerminal", Terminal)

    assert await cli._run_phase1(SimpleNamespace(provider="fake")) == 0
    output = capsys.readouterr().out
    assert calls == ["start", "terminal", "close"]
    assert "deterministic fake" in output
    assert "no live model request is made" in output
    assert "not a sandbox" in output
    assert "accessible files" in output


async def test_terminal_accepts_fifo_queue_input_while_active_without_interrupting():
    from py_agent.contracts import QueueOutcome

    output = Output()
    started, release = asyncio.Event(), asyncio.Event()

    class QueuingCoordinator:
        def __init__(self):
            self.state = State.IDLE
            self.queued = []
            self.interruptions = 0
            self.completion = None

        async def submit(self, frontend_id, text, *, on_progress=None):
            self.state = State.GENERATING
            started.set()
            await release.wait()
            origin = Origin("session", "active-request", frontend_id, 0)
            self.state = State.IDLE
            if self.completion is not None and not self.completion.done():
                self.completion.set_result(QueueOutcome(self.queued[0][0].origin, "steered"))
            return Submission(RoutedAction(origin, "ask", text), message="active done")

        async def enqueue(self, frontend_id, text, *, on_progress=None, **_kwargs):
            origin = Origin("session", "stable-queued-request", frontend_id, 0)
            completion = asyncio.get_running_loop().create_future()
            self.completion = completion
            ticket = QueueTicket(origin, "ask", 1, completion)
            self.queued.append((ticket, text))
            return ticket

        async def interrupt(self):
            self.interruptions += 1

    coordinator = QueuingCoordinator()
    with create_pipe_input() as pipe:
        terminal = PlainTerminal(coordinator, input=pipe, output=output)
        running = asyncio.create_task(terminal.run())
        await until(lambda: terminal.session.app.is_running)
        pipe.send_text("start work\r")
        await asyncio.wait_for(started.wait(), timeout=3)
        pipe.send_text("please steer this task\r")
        await until(lambda: len(coordinator.queued) == 1)
        release.set()
        await until(lambda: "Steering added to the active turn." in output.text)
        pipe.send_text("/quit\r")
        await asyncio.wait_for(running, 3)

    assert coordinator.queued[0][1] == "please steer this task"
    assert coordinator.interruptions == 0
    assert "Queued steering (position 1).\n\n" in output.text.replace("\r\n", "\n")


async def test_completed_composer_prompt_racing_stdin_is_not_used_as_the_stdin_reply(monkeypatch):
    composer_started, release_composer = asyncio.Event(), asyncio.Event()
    stdin_started = asyncio.Event()
    prompt_calls = []
    active_prompts = 0
    maximum_active_prompts = 0

    class FakePromptSession:
        def __init__(self, *, history, **_kwargs):
            self.history = history
            self.app = SimpleNamespace(
                current_buffer=SimpleNamespace(text=""),
                invalidate=lambda: None,
            )

        async def prompt_async(self, *_args, is_password=False, default="", **_kwargs):
            nonlocal active_prompts, maximum_active_prompts
            prompt_calls.append((is_password, default))
            active_prompts += 1
            maximum_active_prompts = max(maximum_active_prompts, active_prompts)
            try:
                if len(prompt_calls) == 1:
                    composer_started.set()
                    await release_composer.wait()
                    value = "composer-race-draft"
                elif len(prompt_calls) == 2:
                    stdin_started.set()
                    assert default == ""
                    value = "private-stdin-reply"
                else:
                    assert default == "composer-race-draft"
                    value = "/quit"
                self.app.current_buffer.text = value
                self.history.append_string(value)
                self.app.current_buffer.text = ""
                return value
            finally:
                active_prompts -= 1

    monkeypatch.setattr(plain_terminal, "PromptSession", FakePromptSession)
    coordinator, output = CoordinatorStub(), Output()
    terminal = PlainTerminal(coordinator, output=output)
    running = asyncio.create_task(terminal.run())
    await asyncio.wait_for(composer_started.wait(), 3)

    origin = Origin(
        "session", "racing-input", "terminal", 0, execution_id="racing-execution",
    )
    request = InputRequest(origin, 1, "terminal", "Value: ", False)
    # Queue both completions before yielding: the composer returns first, but
    # its result is observed only after the stdin request has become current.
    release_composer.set()
    input_task = asyncio.create_task(terminal._request_input(request))
    await until(lambda: terminal._stdin_request is request)

    reply = await asyncio.wait_for(input_task, 3)
    await asyncio.wait_for(running, 3)

    assert reply.value == "private-stdin-reply"
    assert coordinator.submissions == []
    assert terminal.session.history.get_strings() == ["composer-race-draft", "/quit"]
    assert prompt_calls == [
        (False, ""),
        (False, ""),
        (False, "composer-race-draft"),
    ]
    assert stdin_started.is_set()
    assert maximum_active_prompts == 1
    assert "private-stdin-reply" not in output.text


async def test_stdin_mode_restores_composer_draft_and_excludes_reply_from_history(monkeypatch):
    original_session = plain_terminal.PromptSession
    prompt_calls = []

    class TrackingPromptSession(original_session):
        async def prompt_async(self, *args, **kwargs):
            prompt_calls.append((kwargs.get("is_password", False), kwargs.get("default", "")))
            return await super().prompt_async(*args, **kwargs)

    monkeypatch.setattr(plain_terminal, "PromptSession", TrackingPromptSession)
    output = Output()
    submission_started, request_stdin = asyncio.Event(), asyncio.Event()
    reply_received = asyncio.Event()
    reply_value = None
    draft = "unfinished composer draft"
    private_reply = "plain-private-stdin"

    class InputCoordinator:
        state = State.IDLE

        async def submit(self, frontend_id, text, *, input_handler=None, **_kwargs):
            nonlocal reply_value
            origin = Origin("session", "draft-request", frontend_id, 0)
            execution_origin = Origin(
                "session", origin.request_id, frontend_id, 0,
                execution_id="draft-execution",
            )
            submission_started.set()
            await request_stdin.wait()
            self.state = State.WAITING_FOR_INPUT
            request = InputRequest(execution_origin, 1, frontend_id, "Value: ", False)
            reply = await input_handler(request)
            reply_value = reply.value
            self.state = State.IDLE
            reply_received.set()
            return Submission(
                RoutedAction(origin, "ask", text),
                ExecutionResult(execution_origin, "success"),
            )

        async def interrupt(self):
            self.state = State.IDLE

    coordinator = InputCoordinator()
    with create_pipe_input() as pipe:
        terminal = PlainTerminal(coordinator, input=pipe, output=output)
        running = asyncio.create_task(terminal.run())
        await until(lambda: terminal.session.app.is_running)
        pipe.send_text("start work\r")
        await asyncio.wait_for(submission_started.wait(), 3)
        await until(lambda: len(prompt_calls) >= 2 and terminal.session.app.is_running)
        pipe.send_text(draft)
        await until(lambda: terminal.session.app.current_buffer.text == draft)

        request_stdin.set()
        await until(
            lambda: terminal._stdin_request is not None
            and terminal._composer_draft == draft
            and terminal.session.app.is_running
            and terminal.session.app.current_buffer.text == ""
        )
        pipe.send_text(private_reply + "\r")
        await asyncio.wait_for(reply_received.wait(), 3)
        await until(
            lambda: terminal._stdin_request is None
            and terminal.session.app.is_running
            and terminal.session.app.current_buffer.text == draft
        )
        pipe.send_text("\x01\x0b/quit\r")
        await asyncio.wait_for(running, 3)

    assert reply_value == private_reply
    assert prompt_calls[-2] == (False, "")  # stdin never inherits the composer draft.
    assert prompt_calls[-1] == (False, draft)  # the composer draft returns after stdin.
    assert private_reply not in terminal.session.history.get_strings()


async def test_python_getpass_uses_same_prompt_and_slash_text_is_not_steering(monkeypatch):
    original_session = plain_terminal.PromptSession
    prompt_modes = []
    active_prompts = 0
    maximum_active_prompts = 0

    class TrackingPromptSession(original_session):
        async def prompt_async(self, *args, **kwargs):
            nonlocal active_prompts, maximum_active_prompts
            active_prompts += 1
            maximum_active_prompts = max(maximum_active_prompts, active_prompts)
            prompt_modes.append(kwargs.get("is_password", False))
            try:
                return await super().prompt_async(*args, **kwargs)
            finally:
                active_prompts -= 1

    monkeypatch.setattr(plain_terminal, "PromptSession", TrackingPromptSession)
    output = Output()
    requested, finished = asyncio.Event(), asyncio.Event()
    secret = "stdin-private-value"

    class InputCoordinator:
        state = State.IDLE

        def __init__(self):
            self.queued = []
            self.input_value = None
            self.submitted = []

        async def submit(self, frontend_id, text, *, input_handler=None, **_kwargs):
            self.submitted.append(text)
            origin = Origin("session", "stdin-request", frontend_id, 0)
            execution_origin = Origin(
                "session", origin.request_id, frontend_id, 0,
                execution_id="stdin-execution",
            )
            self.state = State.WAITING_FOR_INPUT
            requested.set()
            request = InputRequest(
                execution_origin, 1, frontend_id, "Password: ", True,
            )
            reply = await input_handler(request)
            self.input_value = reply.value
            self.state = State.IDLE
            finished.set()
            return Submission(
                RoutedAction(origin, "ask", text),
                ExecutionResult(execution_origin, "success"),
            )

        async def enqueue(self, *_args, **_kwargs):
            self.queued.append(_args)
            raise AssertionError("stdin content must not enter the queued steering API")

        async def interrupt(self):
            self.state = State.IDLE

    coordinator = InputCoordinator()
    with create_pipe_input() as pipe:
        terminal = PlainTerminal(coordinator, input=pipe, output=output)
        running = asyncio.create_task(terminal.run())
        await until(lambda: terminal.session.app.is_running)
        pipe.send_text("ask for a password\r")
        await asyncio.wait_for(requested.wait(), timeout=3)
        await until(lambda: True in prompt_modes)
        pipe.send_text(secret + "\r")
        await asyncio.wait_for(finished.wait(), timeout=3)
        await until(lambda: terminal._active_submission_task is None)
        pipe.send_text("/quit\r")
        await asyncio.wait_for(running, 3)

    assert coordinator.submitted == ["ask for a password"]
    assert coordinator.input_value == secret
    assert coordinator.queued == []
    assert secret not in terminal.session.history.get_strings()
    assert secret not in output.text
    assert prompt_modes[0] is False
    assert True in prompt_modes
    assert maximum_active_prompts == 1
