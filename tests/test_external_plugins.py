"""Contract tests for separately packaged public-API examples.

These tests are deterministic and offline. They intentionally exercise the
coordinator integrations without private imports from the example packages.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

from py_agent.builtin_services import BuiltinPlugin
from py_agent.configuration import ConfigStore
from py_agent.contracts import (
    ContextSnapshot,
    ExecutorCapabilities,
    ExecutionRequest,
    ExecutionResult,
    ModelRequest,
    ModelResponse,
    Origin,
)
from py_agent.coordinator import Coordinator, State
from py_agent.plugins import (
    CommandContribution,
    Contributions,
    ObserverContribution,
    PluginError,
    PluginManifest,
    PluginRuntime,
    TransformContribution,
    hookimpl,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = {
    "plugin_command": ("example_command_context", "example-command-context"),
    "plugin_executor": ("example_executor_wrapper", "example-executor-wrapper"),
    "plugin_observer": ("example_output_observer", "example-output-observer"),
}


def _load_module(distribution: str, package: str) -> ModuleType:
    path = ROOT / "examples" / distribution / "src" / package / "__init__.py"
    module_name = f"_external_contract_test_{package}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def examples():
    return {
        distribution: _load_module(distribution, package)
        for distribution, (package, _plugin_id) in EXAMPLES.items()
    }


def _runtime(examples, *, builtins=None, include=None) -> PluginRuntime:
    selected = EXAMPLES if include is None else include
    plugins = {
        examples[distribution].PLUGIN_ID: examples[distribution].plugin
        for distribution in selected
    }
    plugins.update(builtins or {})
    return PluginRuntime.load(builtins=plugins)


def test_each_example_is_separately_packaged_and_uses_public_modules(examples):
    public_py_agent_modules = {
        "py_agent.configuration",
        "py_agent.contracts",
        "py_agent.plugins",
    }
    for distribution, (package, plugin_id) in EXAMPLES.items():
        root = ROOT / "examples" / distribution
        metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        project = metadata["project"]
        assert project["name"].startswith("py-agent-example-")
        assert "py-agent" in project["dependencies"][0]
        assert metadata["project"]["entry-points"]["ipy_agent.plugins"] == {
            plugin_id: f"{package}:plugin"
        }
        assert examples[distribution].PLUGIN_ID == plugin_id

        source = (root / "src" / package / "__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
            and node.module.startswith("py_agent")
        }
        assert imported <= public_py_agent_modules


def test_only_explicit_external_entry_points_are_activated(examples, monkeypatch):
    class ExampleEntryPoint:
        def __init__(self, name: str, distribution: str):
            self.name = name
            self.value = f"{name}:plugin"
            self.dist = type("Distribution", (), {"name": distribution})()
            self.distribution = distribution

        def load(self):
            return examples[self.distribution].plugin

    entry_points = []
    for distribution, (_package, plugin_id) in EXAMPLES.items():
        entry_points.append(ExampleEntryPoint(plugin_id, distribution))

    loaded = []
    for entry in entry_points:
        original_load = entry.load

        def track_load(load=original_load, name=entry.name):
            loaded.append(name)
            return load()

        entry.load = track_load

    monkeypatch.setattr("py_agent.plugins.metadata.entry_points", lambda **_kwargs: entry_points)
    selected = EXAMPLES["plugin_command"][1]
    runtime = PluginRuntime.load(enabled=(selected,))
    assert runtime.order == (selected,)
    assert loaded == [selected]


def test_command_config_and_async_context_model_stages_are_public(examples):
    runtime = _runtime(examples)
    command_id = "example-command-context"
    assert set(runtime.commands) == {"greet"}
    store = ConfigStore(runtime.config)
    greeting_name = f"{command_id}.greeting"
    prefix_name = f"{command_id}.context_prefix"
    tag_name = f"{command_id}.request_tag"
    assert store.snapshot.get(greeting_name) == "hello"
    assert store.snapshot.get(prefix_name) == "External example context:"
    store.set_json(greeting_name, '"salut"')
    command = runtime.commands["greet"].create({"greeting": store.snapshot.get(greeting_name)})
    assert command.execute("Ada") == "salut, Ada"

    context_stage = runtime.transforms["context"][0]
    original = ContextSnapshot(4, (("system", "base"), ("assistant", "thinking")),
                               (None, "commentary"))
    transformed = asyncio.run(context_stage.create({"context_prefix": "Added externally"}).transform(original))
    assert transformed is not original
    assert transformed.epoch == original.epoch
    assert transformed.messages == (("system", "Added externally"), *original.messages)
    assert transformed.message_phases == (None, *original.message_phases)

    request_stage = runtime.transforms["model-request"][0]
    request = ModelRequest(Origin("s", "r", "ui", 1), original, "test-model")
    transformed_request = asyncio.run(
        request_stage.create({"request_tag": "contract-test"}).transform(request),
    )
    assert transformed_request.options["example_request_tag"] == "contract-test"
    assert transformed_request.origin == request.origin
    assert runtime.config.fields[tag_name].owner == command_id


def test_registration_conflicts_dependencies_and_transform_topology(examples):
    class ConflictingPlugin:
        @hookimpl
        def py_agent_register(self):
            return Contributions(
                PluginManifest("external-conflict"),
                commands=(CommandContribution("greet", lambda _config: object()),),
            )

    plugins = {
        examples[distribution].PLUGIN_ID: examples[distribution].plugin
        for distribution in EXAMPLES
    }
    with pytest.raises(PluginError, match="Duplicate command"):
        PluginRuntime.load(builtins={**plugins, "external-conflict": ConflictingPlugin()})

    class OrderedPlugin:
        @hookimpl
        def py_agent_register(self):
            return Contributions(
                PluginManifest("ordered-plugin"),
                transforms=(TransformContribution(
                    "context", "last", lambda _config: object(), after=("first",),
                ),),
            )

    class FirstPlugin:
        @hookimpl
        def py_agent_register(self):
            return Contributions(
                PluginManifest("first-plugin"),
                transforms=(TransformContribution("context", "first", lambda _config: object()),),
            )

    ordered = PluginRuntime.load(builtins={
        "ordered-plugin": OrderedPlugin(), "first-plugin": FirstPlugin(),
    })
    assert tuple(item.name for item in ordered.transforms["context"]) == ("first", "last")

    class InvalidOrdering:
        @hookimpl
        def py_agent_register(self):
            return Contributions(
                PluginManifest("invalid-ordering"),
                transforms=(TransformContribution(
                    "context", "one", lambda _config: object(), after=("absent",),
                ),),
            )

    with pytest.raises(PluginError, match="unknown context stages"):
        PluginRuntime.load(builtins={"invalid-ordering": InvalidOrdering()})

    class DependentPlugin:
        @hookimpl
        def py_agent_register(self):
            return Contributions(PluginManifest("example-dependent", requires=("example-command-context",)))

    dependent = PluginRuntime.load(builtins={**plugins, "example-dependent": DependentPlugin()})
    assert dependent.order.index("example-command-context") < dependent.order.index("example-dependent")
    with pytest.raises(PluginError, match="No enabled"):
        dependent.select("context-transform", "missing")


class _DelegateExecutor:
    capabilities = ExecutorCapabilities(persistent=True, interrupt=True)

    def __init__(self, *, stdout: str = "delegated", stderr: str = "", block: bool = False):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not block:
            self.release.set()
        self.stdout, self.stderr = stdout, stderr
        self.start_calls = 0
        self.execute_calls = 0
        self.interrupt_calls = 0
        self.close_calls = 0

    async def start(self):
        self.start_calls += 1

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.execute_calls += 1
        self.entered.set()
        await self.release.wait()
        return ExecutionResult(request.origin, "success", stdout=self.stdout, stderr=self.stderr)

    async def interrupt(self):
        self.interrupt_calls += 1
        self.release.set()

    async def close(self):
        self.close_calls += 1


class _RecordingProvider:
    model = "offline/example"

    def __init__(self):
        self.requests = []

    async def generate(self, request: ModelRequest):
        self.requests.append(request)
        return ModelResponse("pass")


def _execution_request() -> ExecutionRequest:
    origin = Origin("session", "request", "frontend", 2, "generation", "execution")
    return ExecutionRequest(origin, "pass", "user", "python")


@pytest.mark.asyncio
async def test_coordinator_selects_external_executor_wrapper_and_transforms(examples):
    delegate = _DelegateExecutor()
    runtime = _runtime(examples, include=("plugin_command", "plugin_executor"), builtins={
        "builtin": BuiltinPlugin(executor_factory=lambda: delegate),
    })
    store = ConfigStore(runtime.config)
    store.set_json("example-command-context.greeting", '"salut"')
    coordinator = Coordinator(
        runtime,
        router="default",
        provider="fake",
        interpreter="basic",
        executor="local",
        executor_wrappers=("example-executor-wrapper:audit",),
        config_store=store,
    )
    provider = _RecordingProvider()
    coordinator.provider = provider
    await coordinator.start()
    try:
        greeted = await coordinator.submit("terminal", "/greet Ada")
        assert greeted.message == "salut, Ada"
        await coordinator.submit("terminal", "inspect the transformed request")
        request = provider.requests[0]
        assert request.context.messages[0] == ("system", "External example context:")
        assert request.context.transform_trace == ("example-command-context:prefix",)
        assert request.options["example_request_tag"] == "external-example"
        assert request.transform_trace == ("example-command-context:tag",)
        assert coordinator.executor.execution_count == delegate.execute_calls == 1
        assert delegate.start_calls == delegate.close_calls == 1
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_executor_wrapper_forwards_available_archive_capabilities(examples):
    class ArchivingDelegate(_DelegateExecutor):
        async def store_collapsed(self, text):
            self.archive = text
            return 7

        async def store_outputs(self, texts):
            self.outputs = texts
            return (1,)

    delegate = ArchivingDelegate()
    wrapper = examples["plugin_executor"].ExecutionAuditWrapper(delegate)
    assert await wrapper.store_collapsed("exact archive") == 7
    assert delegate.archive == "exact archive"
    assert await wrapper.store_outputs(("result",)) == (1,)
    assert delegate.outputs == ("result",)
    assert wrapper.execution_count == 0
    unsupported = examples["plugin_executor"].ExecutionAuditWrapper(_DelegateExecutor())
    assert not hasattr(unsupported, "store_collapsed")


@pytest.mark.asyncio
async def test_executor_wrapper_cancellation_is_forwarded_once_without_replay(examples):
    delegate = _DelegateExecutor(block=True)
    wrapper = examples["plugin_executor"].ExecutionAuditWrapper(delegate)
    task = asyncio.create_task(wrapper.execute(_execution_request()))
    await delegate.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert wrapper.execution_count == delegate.execute_calls == 1
    await wrapper.interrupt()
    assert delegate.interrupt_calls == 1


@pytest.mark.asyncio
async def test_bounded_observer_backpressures_coordinator_and_events_are_immutable(examples):
    delegate = _DelegateExecutor(stdout="one", stderr="two")
    runtime = _runtime(examples, builtins={
        "builtin": BuiltinPlugin(executor_factory=lambda: delegate),
    })
    coordinator = Coordinator(runtime, router="default", provider="fake", interpreter="basic", executor="local")
    observer = coordinator.output_observers["example-output-observer:bounded"]
    await coordinator.start()
    try:
        submission = asyncio.create_task(coordinator.submit("terminal", "@work()"))
        first = await asyncio.wait_for(observer.receive(), timeout=1)
        assert first.kind == "stream" and first.data["name"] == "stdout"
        with pytest.raises(TypeError):
            first.data["text"] = "changed"
        await asyncio.sleep(0)
        assert not submission.done()  # stderr is blocked by the one-slot queue
        observer.acknowledge()
        await submission
        second = await observer.receive()
        assert second.data["name"] == "stderr"
        assert second.sequence == first.sequence + 1
        assert second.origin.request_id == first.origin.request_id
        observer.acknowledge()
        await observer.join()
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_interrupt_cancels_backpressured_observer_without_replaying_execution(examples):
    delegate = _DelegateExecutor(stdout="one", stderr="two")
    runtime = _runtime(examples, builtins={
        "builtin": BuiltinPlugin(executor_factory=lambda: delegate),
    })
    coordinator = Coordinator(runtime, router="default", provider="fake", interpreter="basic", executor="local")
    observer = coordinator.output_observers["example-output-observer:bounded"]
    await coordinator.start()
    try:
        pending = asyncio.create_task(coordinator.submit("terminal", "@side_effect()"))
        await asyncio.wait_for(observer.receive(), timeout=1)
        await asyncio.sleep(0)
        assert not pending.done()
        await coordinator.interrupt()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert coordinator.state is State.FAILED
        assert delegate.execute_calls == 1
        observer.acknowledge()
    finally:
        await coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("critical", "should_fail"), ((True, True), (False, False)))
async def test_coordinator_observer_failure_policy_and_no_execution_replay(examples, critical, should_fail):
    class BrokenObserver:
        async def observe(self, _event):
            raise RuntimeError("observer failed")

    class BrokenObserverPlugin:
        @hookimpl
        def py_agent_register(self):
            return Contributions(
                PluginManifest("broken-observer"),
                observers=(ObserverContribution("broken", lambda _config: BrokenObserver(), critical),),
            )

    delegate = _DelegateExecutor(stdout="done")
    runtime = _runtime(examples, include=("plugin_command", "plugin_executor"), builtins={
        "builtin": BuiltinPlugin(executor_factory=lambda: delegate),
        "broken-observer": BrokenObserverPlugin(),
    })
    coordinator = Coordinator(runtime, router="default", provider="fake", interpreter="basic", executor="local")
    await coordinator.start()
    try:
        if should_fail:
            with pytest.raises(RuntimeError, match="observer failed"):
                await coordinator.submit("terminal", "@side_effect()")
            assert coordinator.state is State.FAILED
        else:
            await coordinator.submit("terminal", "@side_effect()")
            assert coordinator.state is State.IDLE
            assert coordinator.best_effort_observer_failures == 1
        assert delegate.execute_calls == 1
    finally:
        await coordinator.close()
