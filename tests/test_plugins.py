"""Plugin registry contract tests; no workers, network or model calls."""

from types import SimpleNamespace

import pytest

from py_agent.configuration import ConfigField
from py_agent.plugins import (
    CommandContribution,
    Contributions,
    ExecutorWrapperContribution,
    ObserverContribution,
    PluginError,
    PluginManifest,
    PluginRuntime,
    Service,
    TransformContribution,
    hookimpl,
)


def plugin(name, *, requires=(), services=(), config_fields=()):
    class Example:
        @hookimpl
        def py_agent_register(self):
            return Contributions(PluginManifest(name, requires=requires), services, config_fields)

    return Example()


def test_explicit_selection_and_dependency_order():
    service = Service("executor", "local", lambda: object())
    entries = {
        "consumer": plugin("consumer", requires=("base",)),
        "base": plugin("base", services=(service,)),
    }
    runtime = PluginRuntime.load(builtins=entries)
    assert runtime.order == ("base", "consumer")
    assert runtime.select("executor", "local") is service
    with pytest.raises(PluginError, match="No enabled"):
        runtime.select("executor", "sandbox")
    with pytest.raises(TypeError):
        runtime.services["executor", "other"] = service
    assert PluginRuntime.load(builtins=dict(reversed(tuple(entries.items())))).order == runtime.order
    with pytest.raises(AttributeError, match="immutable"):
        runtime.order = ()


def test_cycles_and_missing_dependencies_fail():
    with pytest.raises(PluginError, match="cycle"):
        PluginRuntime.load(builtins={
            "a": plugin("a", requires=("b",)),
            "b": plugin("b", requires=("a",)),
        })
    with pytest.raises(PluginError, match="disabled"):
        PluginRuntime.load(builtins={"a": plugin("a", requires=("b",))})


def test_duplicate_services_fail():
    service = Service("executor", "local", lambda: None)
    with pytest.raises(PluginError, match="Duplicate service"):
        PluginRuntime.load(builtins={
            "a": plugin("a", services=(service,)),
            "b": plugin("b", services=(service,)),
        })


def test_discovery_does_not_activate_unrequested_code(monkeypatch):
    def forbidden():
        pytest.fail("Unselected entry point imported")

    monkeypatch.setattr("py_agent.plugins.metadata.entry_points", lambda **kw: [
        SimpleNamespace(name="unselected", load=forbidden),
    ])
    assert PluginRuntime.load().order == ()


def test_async_registration_is_rejected():
    class Invalid:
        @hookimpl
        async def py_agent_register(self):
            return Contributions(PluginManifest("invalid"))

    with pytest.raises(PluginError, match="synchronous"):
        PluginRuntime.load(builtins={"invalid": Invalid()})


def test_optional_unknown_hook_is_still_rejected():
    class Invalid:
        @hookimpl
        def py_agent_register(self):
            return Contributions(PluginManifest("invalid"))

        @hookimpl(optionalhook=True)
        def py_agent_unrecognized(self):
            return None

    with pytest.raises(PluginError, match="unknown hook"):
        PluginRuntime.load(builtins={"invalid": Invalid()})


def test_api_mismatch_is_rejected():
    with pytest.raises(PluginError, match="API"):
        PluginManifest("future", api_min=2, api_max=3)


def test_manifest_and_service_declarations_are_validated():
    with pytest.raises(PluginError, match="requirements"):
        PluginManifest("bad", requires="base")
    with pytest.raises(PluginError, match="version range"):
        PluginManifest("bad", api_min=True)
    with pytest.raises(PluginError, match="callable factory"):
        Service("executor", "local", None)


def test_plugin_configuration_is_owned_and_namespaced():
    field = ConfigField("sample.timeout", "sample", int, 10, minimum=1)
    runtime = PluginRuntime.load(builtins={
        "sample": plugin("sample", config_fields=(field,)),
    })
    assert runtime.config.fields["sample.timeout"] is field

    unnamespaced = ConfigField("timeout", "sample", int, 10)
    with pytest.raises(PluginError, match="namespace"):
        PluginRuntime.load(builtins={"sample": plugin("sample", config_fields=(unnamespaced,))})
    wrong_owner = ConfigField("sample.timeout", "other", int, 10)
    with pytest.raises(PluginError, match="owner"):
        PluginRuntime.load(builtins={"sample": plugin("sample", config_fields=(wrong_owner,))})


def test_named_pipeline_order_is_deterministic_and_validated():
    class Plugin:
        def __init__(self, name, transforms):
            self.name, self.transforms = name, transforms

        @hookimpl
        def py_agent_register(self):
            return Contributions(
                PluginManifest(self.name), transforms=self.transforms,
            )

    make = lambda name, **kwargs: TransformContribution(
        "context", name, lambda _config: object(), **kwargs,
    )
    runtime = PluginRuntime.load(builtins={
        "z-plugin": Plugin("z-plugin", (make("last", after=("first",)),)),
        "a-plugin": Plugin("a-plugin", (make("middle", after=("first",)),)),
        "first-plugin": Plugin("first-plugin", (make("first"),)),
    })
    assert tuple(stage.name for stage in runtime.transforms["context"]) == (
        "first", "last", "middle",
    )
    reverse = PluginRuntime.load(builtins={
        "first-plugin": Plugin("first-plugin", (make("first"),)),
        "a-plugin": Plugin("a-plugin", (make("middle", after=("first",)),)),
        "z-plugin": Plugin("z-plugin", (make("last", after=("first",)),)),
    })
    assert tuple(stage.name for stage in reverse.transforms["context"]) == (
        "first", "last", "middle",
    )

    with pytest.raises(PluginError, match="unknown context stages"):
        PluginRuntime.load(builtins={
            "missing": Plugin("missing", (make("one", before=("not-registered",)),)),
        })
    with pytest.raises(PluginError, match="ordering cycle"):
        PluginRuntime.load(builtins={
            "cycle": Plugin("cycle", (make("one", before=("two",)), make("two", before=("one",)))),
        })


def test_command_observer_and_executor_wrapper_registrations_validate_collisions():
    class Plugin:
        def __init__(self, manifest, **contributions):
            self.manifest, self.contributions = manifest, contributions

        @hookimpl
        def py_agent_register(self):
            return Contributions(self.manifest, **self.contributions)

    with pytest.raises(PluginError, match="Duplicate command"):
        PluginRuntime.load(builtins={
            "first": Plugin(PluginManifest("first"), commands=(
                CommandContribution("help", lambda _config: object()),
            )),
            "second": Plugin(PluginManifest("second"), commands=(
                CommandContribution("help", lambda _config: object()),
            )),
        })
    with pytest.raises(PluginError, match="Duplicate observer"):
        PluginRuntime.load(builtins={
            "first": Plugin(PluginManifest("first"), observers=(
                ObserverContribution("events", lambda _config: object()),
            )),
            "second": Plugin(PluginManifest("second"), observers=(
                ObserverContribution("events", lambda _config: object()),
            )),
        })
    with pytest.raises(PluginError, match="criticality"):
        ObserverContribution("events", lambda _config: object(), critical=1)

    runtime = PluginRuntime.load(builtins={
        "decorator": Plugin(PluginManifest("decorator"), executor_wrappers=(
            ExecutorWrapperContribution("audit", lambda delegate: delegate),
        )),
    })
    assert "decorator:audit" in runtime.executor_wrappers


def test_entrypoint_conflicts_are_found_before_import(monkeypatch):
    imported = []

    def make_entry(name):
        return SimpleNamespace(name=name, value=f"{name}:plugin",
                               load=lambda: imported.append(name))

    monkeypatch.setattr("py_agent.plugins.metadata.entry_points",
                        lambda **kw: [make_entry("first")])
    with pytest.raises(PluginError, match="Missing or ambiguous"):
        PluginRuntime.load(enabled=("first", "missing"))
    assert imported == []
