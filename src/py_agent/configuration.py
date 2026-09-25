"""Typed scalar configuration, independent of frontends and service lifecycles.

Snapshots describe desired configuration. Application timing is explicit metadata:
consumers retain their snapshot until their declared boundary; restart-only changes
must not reconfigure an already running service. Validators must be side-effect free.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
import json
import math
from types import MappingProxyType
from typing import Callable


Scalar = str | int | float | bool | None


class ConfigError(ValueError):
    """A rejected candidate leaves the current configuration unchanged."""


def _valid_label(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip() and "\x00" not in value


def _is_scalar(value: object) -> bool:
    return (type(value) in (str, int, float, bool, type(None))
            and not (type(value) is float and not math.isfinite(value)))


def _copy_mapping(values, description: str) -> dict:
    if not isinstance(values, Mapping):
        raise ConfigError(f"{description} must be a mapping")
    try:
        return dict(values)
    except Exception:
        raise ConfigError(f"{description} must be a stable mapping") from None


class ApplyAt(str, Enum):
    IMMEDIATE = "immediate"
    REQUEST = "request"
    CELL = "cell"
    EPOCH = "epoch"
    RESTART = "restart"


@dataclass(frozen=True)
class ConfigField:
    name: str
    owner: str
    value_type: type
    default: Scalar = field(repr=False)
    documentation: str = ""
    apply_at: ApplyAt = ApplyAt.REQUEST
    sensitive: bool = False
    persistable: bool = True
    choices: tuple[Scalar, ...] = field(default=(), repr=False)
    minimum: int | float | None = None
    maximum: int | float | None = None
    validator: Callable[[Scalar], None] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if not _valid_label(self.name) or not _valid_label(self.owner):
            raise ConfigError("Configuration fields require nonempty, trimmed names and owners")
        if self.value_type not in (str, int, float, bool, type(None)):
            raise ConfigError(f"Unsupported scalar type for {self.name}")
        if not isinstance(self.documentation, str):
            raise ConfigError(f"Documentation for {self.name} must be text")
        if not isinstance(self.apply_at, ApplyAt):
            raise ConfigError(f"Invalid application boundary for {self.name}")
        if type(self.sensitive) is not bool or type(self.persistable) is not bool:
            raise ConfigError(f"Sensitivity and persistence flags for {self.name} must be booleans")
        if self.validator is not None and not callable(self.validator):
            raise ConfigError(f"Validator for {self.name} must be callable")
        if isinstance(self.choices, (str, bytes)):
            raise ConfigError(f"Choices for {self.name} must be a sequence of scalar values")
        try:
            choices = tuple(self.choices)
        except TypeError:
            raise ConfigError(f"Choices for {self.name} must be a sequence of scalar values") from None
        object.__setattr__(self, "choices", choices)
        if any(not _is_scalar(value) for value in choices):
            raise ConfigError(f"Choices for {self.name} must be finite scalar values")
        if len(set(choices)) != len(choices):
            raise ConfigError(f"Duplicate choices for {self.name}")
        if (self.minimum is not None or self.maximum is not None) and self.value_type not in (int, float):
            raise ConfigError(f"Numeric bounds are not supported for {self.name}")
        for bound in (self.minimum, self.maximum):
            if bound is not None and (type(bound) not in (int, float)
                                      or (type(bound) is float and not math.isfinite(bound))):
                raise ConfigError(f"Invalid numeric bound for {self.name}")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ConfigError(f"Reversed bounds for {self.name}")
        for value in choices:
            if type(value) is not self.value_type:
                raise ConfigError(f"Invalid choice type for {self.name}")
        self.validate(self.default)
        for value in choices:
            self.validate(value)

    def validate(self, value: Scalar) -> None:
        # bool is deliberately not accepted for integer fields.
        if type(value) is not self.value_type:
            raise ConfigError(f"{self.name} requires {self.value_type.__name__}")
        if isinstance(value, float) and not math.isfinite(value):
            raise ConfigError(f"{self.name} requires a finite number")
        if self.choices and value not in self.choices:
            raise ConfigError(f"{self.name} is not an allowed choice")
        if self.minimum is not None and value < self.minimum:
            raise ConfigError(f"{self.name} is below its minimum")
        if self.maximum is not None and value > self.maximum:
            raise ConfigError(f"{self.name} exceeds its maximum")
        if self.validator is not None:
            try:
                if self.validator(value) is False:
                    raise ValueError("Rejected")
            except Exception:
                # Validators may include secret values in their exception text.
                raise ConfigError(f"Validation failed for {self.name}") from None


@dataclass(frozen=True)
class ConfigLayer:
    source: str
    values: Mapping[str, Scalar] = field(repr=False)

    def __post_init__(self):
        if not _valid_label(self.source) or self.source in ("default", "session"):
            raise ConfigError("Layer source must be named, trimmed, and cannot be default or session")
        copied = _copy_mapping(self.values, "Configuration layer values")
        if any(not _valid_label(name) for name in copied):
            raise ConfigError("Configuration layer keys must be nonempty, trimmed names")
        if any(not _is_scalar(value) for value in copied.values()):
            raise ConfigError("Configuration layers accept only finite scalar data")
        object.__setattr__(self, "values", MappingProxyType(copied))


@dataclass(frozen=True)
class ResolvedValue:
    value: Scalar = field(repr=False)
    source: str
    masked_sources: tuple[str, ...] = ()

    def __post_init__(self):
        if not _is_scalar(self.value) or not _valid_label(self.source):
            raise ConfigError("Invalid resolved configuration value or provenance")
        if isinstance(self.masked_sources, (str, bytes)):
            raise ConfigError("Masked provenance must be a sequence of source names")
        try:
            sources = tuple(self.masked_sources)
        except TypeError:
            raise ConfigError("Masked provenance must be a sequence of source names") from None
        if (any(not _valid_label(source) for source in sources)
                or len(set(sources)) != len(sources) or self.source in sources):
            raise ConfigError("Invalid masked configuration provenance")
        object.__setattr__(self, "masked_sources", sources)


@dataclass(frozen=True)
class ConfigSnapshot:
    revision: int
    entries: Mapping[str, ResolvedValue] = field(repr=False)

    def __post_init__(self):
        if type(self.revision) is not int or self.revision < 0:
            raise ConfigError("Configuration revision must be a nonnegative integer")
        copied = _copy_mapping(self.entries, "Configuration snapshot entries")
        if any(not _valid_label(name) or not isinstance(value, ResolvedValue)
               for name, value in copied.items()):
            raise ConfigError("Invalid configuration snapshot entry")
        object.__setattr__(self, "entries", MappingProxyType(copied))

    def get(self, name: str) -> Scalar:
        return self.entries[name].value


@dataclass(frozen=True)
class ConfigChange:
    before: ConfigSnapshot
    after: ConfigSnapshot
    changed: Mapping[str, ApplyAt]

    def __post_init__(self):
        if (not isinstance(self.before, ConfigSnapshot) or not isinstance(self.after, ConfigSnapshot)
                or self.after.revision <= self.before.revision
                or set(self.before.entries) != set(self.after.entries)):
            raise ConfigError("A configuration change requires compatible, increasing snapshots")
        copied = _copy_mapping(self.changed, "Changed configuration fields")
        if any(name not in self.before.entries or name not in self.after.entries
               or not isinstance(boundary, ApplyAt) for name, boundary in copied.items()):
            raise ConfigError("Invalid changed configuration field")
        object.__setattr__(self, "changed", MappingProxyType(copied))


class ConfigRegistry:
    __slots__ = ("fields", "_sealed")

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise AttributeError("ConfigRegistry is immutable")
        object.__setattr__(self, name, value)

    def __init__(self, fields: Iterable[ConfigField] = ()):
        try:
            supplied = tuple(fields)
        except TypeError:
            raise ConfigError("Configuration schema must be iterable") from None
        collected = {}
        for item in supplied:
            if not isinstance(item, ConfigField):
                raise ConfigError("Expected ConfigField")
            if item.name in collected:
                raise ConfigError(f"Duplicate configuration field: {item.name}")
            collected[item.name] = item
        object.__setattr__(self, "fields", MappingProxyType(dict(sorted(collected.items()))))
        object.__setattr__(self, "_sealed", True)

    def validate(self, values: Mapping[str, Scalar]) -> None:
        copied = _copy_mapping(values, "Configuration values")
        for name, value in copied.items():
            if not _valid_label(name):
                raise ConfigError("Configuration field names must be nonempty, trimmed text")
            if name not in self.fields:
                raise ConfigError(f"Unknown configuration field: {name}")
            self.fields[name].validate(value)

    def _validate_snapshot(self, snapshot: ConfigSnapshot) -> None:
        if not isinstance(snapshot, ConfigSnapshot) or set(snapshot.entries) != set(self.fields):
            raise ConfigError("Snapshot does not match this configuration schema")
        for name, entry in snapshot.entries.items():
            self.fields[name].validate(entry.value)

    def resolve(self, layers: Iterable[ConfigLayer], overrides: Mapping[str, Scalar],
                *, revision: int) -> ConfigSnapshot:
        if type(revision) is not int or revision < 0:
            raise ConfigError("Configuration revision must be a nonnegative integer")
        try:
            layers = tuple(layers)
        except TypeError:
            raise ConfigError("Configuration layers must be iterable") from None
        if any(not isinstance(layer, ConfigLayer) for layer in layers):
            raise ConfigError("Expected ConfigLayer")
        if len({layer.source for layer in layers}) != len(layers):
            raise ConfigError("Duplicate configuration layer source")
        overrides = _copy_mapping(overrides, "Configuration overrides")
        self.validate(overrides)
        entries = {name: ResolvedValue(item.default, "default") for name, item in self.fields.items()}
        for layer in layers:
            self.validate(layer.values)
            for name, value in layer.values.items():
                previous = entries[name]
                entries[name] = ResolvedValue(value, layer.source,
                                              (*previous.masked_sources, previous.source))
        for name, value in overrides.items():
            previous = entries[name]
            entries[name] = ResolvedValue(value, "session",
                                          (*previous.masked_sources, previous.source))
        return ConfigSnapshot(revision, entries)

    def display(self, snapshot: ConfigSnapshot) -> dict[str, dict[str, object]]:
        self._validate_snapshot(snapshot)
        return {
            name: {"value": "<redacted>" if self.fields[name].sensitive else item.value,
                   "source": item.source, "masked_sources": item.masked_sources,
                   "apply_at": self.fields[name].apply_at.value}
            for name, item in snapshot.entries.items()
        }


class ConfigStore:
    """Transactional desired revisions; use from the coordinator's owning loop.

    Layers are supplied in ascending precedence (user file, profile, launch flags).
    Updating settings never starts/stops services or writes a configuration file.
    """

    __slots__ = ("_registry", "_layers", "_overrides", "_snapshot")

    @property
    def registry(self) -> ConfigRegistry:
        return self._registry

    def __init__(self, registry: ConfigRegistry, layers: Iterable[ConfigLayer] = ()):
        if not isinstance(registry, ConfigRegistry):
            raise ConfigError("Expected ConfigRegistry")
        self._registry = registry
        try:
            self._layers = tuple(layers)
        except TypeError:
            raise ConfigError("Configuration layers must be iterable") from None
        self._overrides: dict[str, Scalar] = {}
        self._snapshot = registry.resolve(self._layers, self._overrides, revision=0)

    @property
    def snapshot(self) -> ConfigSnapshot:
        return self._snapshot

    def _commit(self, layers: tuple[ConfigLayer, ...], overrides: dict[str, Scalar]) -> ConfigChange:
        before = self._snapshot
        after = self.registry.resolve(layers, overrides, revision=before.revision + 1)
        change = ConfigChange(before, after, {
            name: self.registry.fields[name].apply_at
            for name in after.entries if before.entries[name] != after.entries[name]
        })
        # Nothing mutates until the complete candidate, provenance and diff validate.
        self._layers, self._overrides, self._snapshot = layers, overrides, after
        return change

    def set(self, values: Mapping[str, Scalar]) -> ConfigChange:
        copied = _copy_mapping(values, "Configuration overrides")
        return self._commit(self._layers, {**self._overrides, **copied})

    def set_json(self, name: str, text: str) -> ConfigChange:
        if not isinstance(text, str):
            raise ConfigError("Expected a JSON scalar value")
        def reject_constant(_value):
            raise ValueError("Non-standard JSON constant")
        try:
            value = json.loads(text, parse_constant=reject_constant)
        except (ValueError, RecursionError):
            raise ConfigError("Expected a JSON scalar value") from None
        if not _is_scalar(value):
            raise ConfigError("Expected a JSON scalar value")
        return self.set({name: value})

    def reset(self, *names: str) -> ConfigChange:
        overrides = dict(self._overrides)
        for name in names:
            if not _valid_label(name) or name not in self.registry.fields:
                raise ConfigError(f"Unknown configuration field: {name}")
            overrides.pop(name, None)
        return self._commit(self._layers, overrides)

    def reload(self, layers: Iterable[ConfigLayer]) -> ConfigChange:
        """Replace file/launch layers atomically, retaining session overrides."""
        try:
            candidate = tuple(layers)
        except TypeError:
            raise ConfigError("Configuration layers must be iterable") from None
        return self._commit(candidate, dict(self._overrides))

    def persistence_values(self, names: Iterable[str]) -> dict[str, Scalar]:
        """Return only explicitly selected session settings, never credentials.

        File format, target selection and atomic writing belong to the config
        command adapter. Nothing is persisted implicitly by this store.
        """
        if isinstance(names, (str, bytes)):
            raise ConfigError("Persisted setting names must be a sequence")
        try:
            selected = tuple(names)
        except TypeError:
            raise ConfigError("Persisted setting names must be a sequence") from None
        result = {}
        for name in selected:
            if not _valid_label(name) or name not in self.registry.fields:
                raise ConfigError(f"Unknown configuration field: {name}")
            spec = self.registry.fields[name]
            if spec.sensitive or not spec.persistable:
                raise ConfigError(f"{name} cannot be persisted")
            if name not in self._overrides:
                raise ConfigError(f"{name} has no session override to save")
            result[name] = self._overrides[name]
        return result
