"""Explicit Pluggy activation and public plugin registration contracts.

Discovery never imports plugins. Registration hooks are synchronous; selected
services, pipeline transformations, observers, and wrappers own runtime work.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
import inspect
import re
from types import MappingProxyType
from typing import Callable, Literal, Mapping

import pluggy

from .configuration import ConfigError, ConfigField, ConfigRegistry, Scalar

API_VERSION = 1
ENTRY_POINT_GROUP = "ipy_agent.plugins"
hookspec = pluggy.HookspecMarker("ipy_agent")
hookimpl = pluggy.HookimplMarker("ipy_agent")


class PluginError(ValueError):
    """Invalid plugin configuration; never silently select another backend."""


def _valid_name(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip() and "\x00" not in value


def _sequence(value: object, description: str) -> tuple:
    if isinstance(value, (str, bytes)):
        raise PluginError(f"{description} must be a sequence")
    try:
        return tuple(value)
    except TypeError:
        raise PluginError(f"{description} must be a sequence") from None


def _plugin_config(values: Mapping[str, Scalar]) -> Mapping[str, Scalar]:
    return MappingProxyType(dict(values))


@dataclass(frozen=True)
class PluginManifest:
    id: str
    api_min: int = API_VERSION
    api_max: int = API_VERSION
    requires: tuple[str, ...] = ()

    def __post_init__(self):
        if not _valid_name(self.id):
            raise PluginError("Plugin ID must be nonempty, trimmed text")
        if (type(self.api_min) is not int or type(self.api_max) is not int
                or self.api_min < 1 or self.api_max < 1 or self.api_min > self.api_max):
            raise PluginError("API version range must contain positive ordered integers")
        if not self.api_min <= API_VERSION <= self.api_max:
            raise PluginError(f"Plugin {self.id} does not support API {API_VERSION}")
        requirements = _sequence(self.requires, "Plugin requirements")
        if any(not _valid_name(requirement) for requirement in requirements):
            raise PluginError("Plugin requirements must be nonempty plugin IDs")
        if len(requirements) != len(set(requirements)):
            raise PluginError(f"Plugin {self.id} declares duplicate dependencies")
        object.__setattr__(self, "requires", tuple(sorted(requirements)))


@dataclass(frozen=True)
class Service:
    kind: str
    id: str
    factory: Callable

    def __post_init__(self):
        if not _valid_name(self.kind) or not _valid_name(self.id):
            raise PluginError("Service kind and ID must be nonempty, trimmed text")
        if not callable(self.factory):
            raise PluginError(f"Service {self.kind}/{self.id} requires a callable factory")


@dataclass(frozen=True)
class CommandContribution:
    """A command factory receiving this plugin's immutable config namespace.

    The returned object must expose ``execute(arguments: str)``. Its result may
    be text or an awaitable producing text. Command names are slash-command
    identifiers and are registered without a leading slash.
    """

    name: str
    factory: Callable[[Mapping[str, Scalar]], object]
    summary: str = ""
    usage: str = ""

    def __post_init__(self):
        if not isinstance(self.name, str) or re.fullmatch(r"[a-z][a-z0-9_-]*", self.name) is None:
            raise PluginError("Command names must be lowercase identifiers")
        if not callable(self.factory):
            raise PluginError(f"Command {self.name} requires a callable factory")
        if not isinstance(self.summary, str) or not isinstance(self.usage, str):
            raise PluginError("Command summary and usage must be text")


@dataclass(frozen=True)
class TransformContribution:
    """Named async context or model-request pipeline stage.

    Factories receive immutable config values from the owning namespace, with
    the plugin-ID prefix removed from each key.
    ``before`` and ``after`` reference stage names in the same pipeline.
    """

    kind: Literal["context", "model-request"]
    name: str
    factory: Callable[[Mapping[str, Scalar]], object]
    before: tuple[str, ...] = ()
    after: tuple[str, ...] = ()

    def __post_init__(self):
        if self.kind not in ("context", "model-request"):
            raise PluginError("Transform kind must be 'context' or 'model-request'")
        if not _valid_name(self.name):
            raise PluginError("Transform name must be nonempty, trimmed text")
        if not callable(self.factory):
            raise PluginError(f"Transform {self.name} requires a callable factory")
        for attribute in ("before", "after"):
            names = _sequence(getattr(self, attribute), f"Transform {attribute} constraints")
            if any(not _valid_name(name) for name in names):
                raise PluginError("Transform ordering constraints must be valid stage names")
            if len(names) != len(set(names)):
                raise PluginError("Transform ordering constraints cannot contain duplicates")
            if self.name in names:
                raise PluginError(f"Transform {self.name} cannot order itself")
            object.__setattr__(self, attribute, tuple(sorted(names)))


@dataclass(frozen=True)
class ObserverContribution:
    """An async output observer and its explicit failure policy.

    Observer ``observe(event)`` calls are awaited sequentially by the
    coordinator, providing bounded backpressure without an unbounded task queue.
    """

    name: str
    factory: Callable[[Mapping[str, Scalar]], object]
    critical: bool = True

    def __post_init__(self):
        if not _valid_name(self.name):
            raise PluginError("Observer name must be nonempty, trimmed text")
        if not callable(self.factory):
            raise PluginError(f"Observer {self.name} requires a callable factory")
        if type(self.critical) is not bool:
            raise PluginError("Observer criticality must be boolean")


@dataclass(frozen=True)
class ExecutorWrapperContribution:
    """A named decorator factory explicitly selected by the Coordinator."""

    name: str
    factory: Callable[[object], object]

    def __post_init__(self):
        if not _valid_name(self.name):
            raise PluginError("Executor wrapper name must be nonempty, trimmed text")
        if not callable(self.factory):
            raise PluginError(f"Executor wrapper {self.name} requires a callable factory")


@dataclass(frozen=True)
class Contributions:
    manifest: PluginManifest
    services: tuple[Service, ...] = ()
    config_fields: tuple[ConfigField, ...] = ()
    commands: tuple[CommandContribution, ...] = ()
    transforms: tuple[TransformContribution, ...] = ()
    observers: tuple[ObserverContribution, ...] = ()
    executor_wrappers: tuple[ExecutorWrapperContribution, ...] = ()

    def __post_init__(self):
        if not isinstance(self.manifest, PluginManifest):
            raise PluginError("Contributions require a PluginManifest")
        for attribute in ("services", "config_fields", "commands", "transforms",
                          "observers", "executor_wrappers"):
            object.__setattr__(self, attribute, _sequence(
                getattr(self, attribute), f"Contribution {attribute}"))


@dataclass(frozen=True)
class RegisteredCommand:
    plugin_id: str
    contribution: CommandContribution

    @property
    def name(self) -> str:
        return self.contribution.name

    @property
    def factory(self):
        return self.contribution.factory

    def create(self, config: Mapping[str, Scalar]):
        return self.factory(_plugin_config(config))


@dataclass(frozen=True)
class RegisteredTransform:
    plugin_id: str
    contribution: TransformContribution

    @property
    def kind(self) -> str:
        return self.contribution.kind

    @property
    def name(self) -> str:
        return self.contribution.name

    @property
    def factory(self):
        return self.contribution.factory

    @property
    def before(self) -> tuple[str, ...]:
        return self.contribution.before

    @property
    def after(self) -> tuple[str, ...]:
        return self.contribution.after

    @property
    def qualified_name(self) -> str:
        return f"{self.plugin_id}:{self.name}"

    def create(self, config: Mapping[str, Scalar]):
        return self.factory(_plugin_config(config))


@dataclass(frozen=True)
class RegisteredObserver:
    plugin_id: str
    contribution: ObserverContribution

    @property
    def name(self) -> str:
        return self.contribution.name

    @property
    def critical(self) -> bool:
        return self.contribution.critical

    @property
    def qualified_name(self) -> str:
        return f"{self.plugin_id}:{self.name}"

    def create(self, config: Mapping[str, Scalar]):
        return self.contribution.factory(_plugin_config(config))


@dataclass(frozen=True)
class RegisteredExecutorWrapper:
    plugin_id: str
    contribution: ExecutorWrapperContribution

    @property
    def name(self) -> str:
        return self.contribution.name

    @property
    def qualified_name(self) -> str:
        return f"{self.plugin_id}:{self.name}"

    @property
    def factory(self):
        return self.contribution.factory


class Hooks:
    @hookspec
    def py_agent_register(self) -> Contributions:
        """Return declarative contributions; do not start resources here."""


@dataclass(frozen=True)
class DiscoveredPlugin:
    name: str
    target: str
    distribution: str | None


def _entry_points():
    try:
        return tuple(metadata.entry_points(group=ENTRY_POINT_GROUP))
    except Exception:
        raise PluginError("Unable to discover plugin entry points") from None


def _dependency_order(manifests: Mapping[str, PluginManifest]) -> tuple[str, ...]:
    remaining = {name: set(manifest.requires) for name, manifest in manifests.items()}
    for name, requirements in remaining.items():
        missing = requirements - manifests.keys()
        if missing:
            raise PluginError(f"Plugin {name} requires disabled plugins: {sorted(missing)}")
    order = []
    while remaining:
        ready = sorted(name for name, needs in remaining.items() if not needs)
        if not ready:
            raise PluginError(f"Plugin dependency cycle: {sorted(remaining)}")
        for name in ready:
            order.append(name)
            del remaining[name]
        for needs in remaining.values():
            needs.difference_update(ready)
    return tuple(order)


def _ordered_transforms(transforms: tuple[RegisteredTransform, ...]) -> Mapping[str, tuple[RegisteredTransform, ...]]:
    ordered = {}
    for kind in ("context", "model-request"):
        stages = {item.name: item for item in transforms if item.kind == kind}
        if len(stages) != sum(item.kind == kind for item in transforms):
            raise PluginError(f"Duplicate {kind} transform name")
        edges = {name: set() for name in stages}
        for name, item in stages.items():
            references = (*item.before, *item.after)
            missing = set(references) - stages.keys()
            if missing:
                raise PluginError(f"Transform {name} references unknown {kind} stages: {sorted(missing)}")
            for successor in item.before:
                edges[name].add(successor)
            for predecessor in item.after:
                edges[predecessor].add(name)
        remaining = {name: set() for name in stages}
        for before, afters in edges.items():
            for after in afters:
                remaining[after].add(before)
        result = []
        while remaining:
            ready = sorted(name for name, needs in remaining.items() if not needs)
            if not ready:
                raise PluginError(f"{kind} transform ordering cycle: {sorted(remaining)}")
            for name in ready:
                result.append(stages[name])
                del remaining[name]
            for needs in remaining.values():
                needs.difference_update(ready)
        ordered[kind] = tuple(result)
    return MappingProxyType(ordered)


def discover() -> tuple[DiscoveredPlugin, ...]:
    """List installed entry-point metadata without executing entry-point code."""
    found = []
    for ep in _entry_points():
        name, target = getattr(ep, "name", None), getattr(ep, "value", None)
        if not _valid_name(name) or not _valid_name(target):
            raise PluginError("Invalid plugin entry-point metadata")
        distribution = getattr(getattr(ep, "dist", None), "name", None)
        if distribution is not None and not isinstance(distribution, str):
            raise PluginError("Invalid plugin distribution metadata")
        found.append(DiscoveredPlugin(name, target, distribution))
    return tuple(sorted(found, key=lambda item: (item.name, item.target, item.distribution or "")))


class PluginRuntime:
    """A fully validated, immutable registry. Build a new one to change plugins."""

    __slots__ = (
        "manifests", "services", "config", "order", "commands", "transforms",
        "observers", "executor_wrappers", "_sealed",
    )

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise AttributeError("PluginRuntime is immutable")
        object.__setattr__(self, name, value)

    def __init__(self, manifests, services, order, config_fields=(), *, commands=(),
                 transforms=(), observers=(), executor_wrappers=()):
        if not isinstance(manifests, Mapping) or not isinstance(services, Mapping):
            raise PluginError("Plugin registries must be mappings")
        manifest_copy = dict(manifests)
        service_copy = dict(services)
        if any(not _valid_name(name) or not isinstance(item, PluginManifest) or item.id != name
               for name, item in manifest_copy.items()):
            raise PluginError("Invalid plugin manifest registry")
        if any(not isinstance(key, tuple) or len(key) != 2 or not isinstance(item, Service)
               or key != (item.kind, item.id) for key, item in service_copy.items()):
            raise PluginError("Invalid service registry")
        try:
            order_copy = tuple(order)
        except TypeError:
            raise PluginError("Plugin order must be a sequence") from None
        if any(not _valid_name(name) for name in order_copy):
            raise PluginError("Plugin order must contain valid plugin IDs")
        if len(order_copy) != len(set(order_copy)) or set(order_copy) != set(manifest_copy):
            raise PluginError("Plugin order must contain every enabled plugin exactly once")
        if order_copy != _dependency_order(manifest_copy):
            raise PluginError("Plugin order is not the deterministic dependency order")

        try:
            fields = tuple(config_fields)
        except TypeError:
            raise PluginError("Configuration fields must be a sequence") from None
        for field in fields:
            if not isinstance(field, ConfigField) or field.owner not in manifest_copy:
                raise PluginError("Configuration fields must be owned by an enabled plugin")
            if not field.name.startswith(field.owner + "."):
                raise PluginError(f"Plugin configuration field {field.name} must use the {field.owner}. namespace")
        try:
            registry = ConfigRegistry(fields)
        except ConfigError as exc:
            raise PluginError(str(exc)) from None

        command_items = list(commands)
        transform_items = list(transforms)
        observer_items = list(observers)
        wrapper_items = list(executor_wrappers)
        plugin_positions = {name: index for index, name in enumerate(order_copy)}
        for item in command_items:
            if (not isinstance(item, RegisteredCommand) or item.plugin_id not in manifest_copy
                    or not isinstance(item.contribution, CommandContribution)):
                raise PluginError("Invalid registered command")
        for item in transform_items:
            if (not isinstance(item, RegisteredTransform) or item.plugin_id not in manifest_copy
                    or not isinstance(item.contribution, TransformContribution)):
                raise PluginError("Invalid registered transform")
        for item in observer_items:
            if (not isinstance(item, RegisteredObserver) or item.plugin_id not in manifest_copy
                    or not isinstance(item.contribution, ObserverContribution)):
                raise PluginError("Invalid registered observer")
        for item in wrapper_items:
            if (not isinstance(item, RegisteredExecutorWrapper) or item.plugin_id not in manifest_copy
                    or not isinstance(item.contribution, ExecutorWrapperContribution)):
                raise PluginError("Invalid registered executor wrapper")

        def unique(items, key, description):
            names = [key(item) for item in items]
            if len(names) != len(set(names)):
                raise PluginError(f"Duplicate {description}")

        unique(command_items, lambda item: item.name, "command name")
        unique(observer_items, lambda item: item.name, "observer name")
        unique(wrapper_items, lambda item: item.qualified_name, "executor wrapper")
        command_items.sort(key=lambda item: item.name)
        observer_items.sort(key=lambda item: (plugin_positions[item.plugin_id], item.name))
        wrapper_items.sort(key=lambda item: item.qualified_name)
        transform_registry = _ordered_transforms(tuple(transform_items))

        object.__setattr__(self, "manifests", MappingProxyType(dict(sorted(manifest_copy.items()))))
        object.__setattr__(self, "services", MappingProxyType(dict(sorted(service_copy.items()))))
        object.__setattr__(self, "config", registry)
        object.__setattr__(self, "order", order_copy)
        object.__setattr__(self, "commands", MappingProxyType({item.name: item for item in command_items}))
        object.__setattr__(self, "transforms", transform_registry)
        object.__setattr__(self, "observers", tuple(observer_items))
        object.__setattr__(self, "executor_wrappers",
                           MappingProxyType({item.qualified_name: item for item in wrapper_items}))
        object.__setattr__(self, "_sealed", True)

    def select(self, kind: str, identifier: str) -> Service:
        if not _valid_name(kind) or not _valid_name(identifier):
            raise PluginError("Service selection requires nonempty, trimmed kind and ID")
        try:
            return self.services[kind, identifier]
        except KeyError:
            raise PluginError(f"No enabled {kind} service named {identifier}") from None

    @classmethod
    def load(cls, *, builtins: Mapping[str, object] | None = None, enabled=()):
        """Activate only named external entry points; builtins are explicit too.

        Validation is transactional for the registry, not for arbitrary import
        side effects. Plugins are trusted code.
        """
        if builtins is not None and not isinstance(builtins, Mapping):
            raise PluginError("Built-in plugins must be supplied as a name-to-plugin mapping")
        plugins = dict(builtins or {})
        if any(not _valid_name(name) for name in plugins):
            raise PluginError("Built-in plugin names must be nonempty, trimmed text")
        names = _sequence(enabled, "Enabled plugins")
        if any(not _valid_name(name) for name in names):
            raise PluginError("Enabled plugin names must be nonempty, trimmed text")
        if len(names) != len(set(names)):
            raise PluginError("Duplicate enabled plugin name")
        names = tuple(sorted(names))

        entries = _entry_points() if names else ()
        selected = []
        # Resolve every conflict before importing any selected external plugin.
        for name in names:
            matches = [ep for ep in entries if getattr(ep, "name", None) == name]
            if name in plugins or len(matches) != 1:
                raise PluginError(f"Missing or ambiguous plugin entry point: {name}")
            selected.append(matches[0])
        for ep in selected:
            try:
                plugins[ep.name] = ep.load()
            except Exception:
                raise PluginError(f"Unable to load enabled plugin {ep.name}") from None

        pm = pluggy.PluginManager("ipy_agent")
        pm.add_hookspecs(Hooks)
        # Declared specs are snapshotted before any plugin registers, so an
        # unknown hook name is still absent from the manager's hook relay.
        known_specs = set(vars(pm.hook))
        for name, plugin in sorted(plugins.items()):
            try:
                registered = pm.register(plugin, name=name)
            except Exception:
                raise PluginError(f"Unable to register plugin {name}") from None
            if registered is None:
                raise PluginError(f"Plugin object is already registered: {name}")
        for name, plugin in sorted(plugins.items()):
            try:
                for attribute in dir(plugin):
                    options = pm.parse_hookimpl_opts(plugin, attribute)
                    if options is not None and attribute not in known_specs:
                        raise PluginError(f"Plugin {name} declares unknown hook {attribute}")
            except PluginError:
                raise
            except Exception:
                raise PluginError(f"Unable to inspect hooks for plugin {name}") from None
        try:
            pm.check_pending()
        except Exception:
            raise PluginError("Plugin declares an unknown or unsupported hook") from None
        for implementation in pm.hook.py_agent_register.get_hookimpls():
            function = implementation.function
            if (getattr(implementation, "hookwrapper", False)
                    or getattr(implementation, "wrapper", False)
                    or inspect.iscoroutinefunction(function)
                    or inspect.isasyncgenfunction(function)
                    or inspect.isgeneratorfunction(function)):
                raise PluginError("Registration hooks must be synchronous, non-wrapper functions")

        manifests, services, config_fields = {}, {}, []
        commands, transforms, observers, wrappers = [], [], [], []
        for name in sorted(plugins):
            caller = pm.subset_hook_caller("py_agent_register", remove_plugins=[
                other for other in plugins.values() if other is not plugins[name]
            ])
            try:
                results = caller()
            except Exception:
                raise PluginError(f"Registration hook failed for plugin {name}") from None
            if len(results) != 1 or not isinstance(results[0], Contributions):
                for result in results:
                    if inspect.iscoroutine(result):
                        result.close()
                raise PluginError(f"Plugin {name} must return Contributions")
            contribution = results[0]
            manifest = contribution.manifest
            if manifest.id != name:
                raise PluginError(f"Plugin manifest ID must match registration name {name}")
            manifests[name] = manifest
            for config_field in contribution.config_fields:
                if not isinstance(config_field, ConfigField) or config_field.owner != name:
                    raise PluginError(f"Invalid configuration field owner for {name}")
                if not config_field.name.startswith(name + "."):
                    raise PluginError(f"Plugin configuration field {config_field.name} must use the {name}. namespace")
                config_fields.append(config_field)
            for service in contribution.services:
                if not isinstance(service, Service):
                    raise PluginError(f"Invalid service contribution from {name}")
                key = (service.kind, service.id)
                if key in services:
                    raise PluginError(f"Duplicate service {key}")
                services[key] = service
                # Preserve the earlier public Service("command", ...) shape.
                if service.kind == "command":
                    legacy = CommandContribution(
                        service.id, lambda _config, factory=service.factory: factory(),
                    )
                    commands.append(RegisteredCommand(name, legacy))
            if any(not isinstance(item, CommandContribution) for item in contribution.commands):
                raise PluginError(f"Invalid command contribution from {name}")
            if any(not isinstance(item, TransformContribution) for item in contribution.transforms):
                raise PluginError(f"Invalid transform contribution from {name}")
            if any(not isinstance(item, ObserverContribution) for item in contribution.observers):
                raise PluginError(f"Invalid observer contribution from {name}")
            if any(not isinstance(item, ExecutorWrapperContribution)
                   for item in contribution.executor_wrappers):
                raise PluginError(f"Invalid executor wrapper contribution from {name}")
            commands.extend(RegisteredCommand(name, item) for item in contribution.commands)
            transforms.extend(RegisteredTransform(name, item) for item in contribution.transforms)
            observers.extend(RegisteredObserver(name, item) for item in contribution.observers)
            wrappers.extend(RegisteredExecutorWrapper(name, item)
                            for item in contribution.executor_wrappers)

        order = _dependency_order(manifests)
        return cls(manifests, services, order, config_fields, commands=commands,
                   transforms=transforms, observers=observers, executor_wrappers=wrappers)
