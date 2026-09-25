"""Typed ``/config`` and ``/plugins`` command services.

This module adapts the configuration and plugin registries without owning
startup, coordinator state, or command routing. Configuration files managed
here are bounded JSON objects containing flat setting names and scalar values.
Only explicitly named session overrides can be saved; sensitive fields are
never serialized or displayed. Plugin inspection uses entry-point metadata and
already-loaded manifests only, and never imports a discovered plugin.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shlex
import stat
import tempfile
from types import MappingProxyType

from .configuration import (
    ApplyAt,
    ConfigError,
    ConfigField,
    ConfigLayer,
    ConfigSnapshot,
    ConfigStore,
    Scalar,
)
from .plugins import DiscoveredPlugin, PluginError, PluginManifest, PluginRuntime, discover

MAX_CONFIG_BYTES = 65536


class ConfigCommandError(ValueError):
    """A safe command or persistence error suitable for a user-facing reply."""


@dataclass(frozen=True)
class CommandResponse:
    ok: bool
    text: str

    def __post_init__(self) -> None:
        if type(self.ok) is not bool or not isinstance(self.text, str):
            raise TypeError("CommandResponse requires a boolean status and text")


CommandHandler = Callable[[str], CommandResponse]


@dataclass(frozen=True)
class CommandDefinition:
    """A typed command contribution, independent of a particular frontend."""

    name: str
    summary: str
    usage: str
    handler: CommandHandler = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", self.name):
            raise ValueError("Command names must be lowercase identifiers")
        if not isinstance(self.summary, str) or not isinstance(self.usage, str):
            raise TypeError("Command summary and usage must be text")
        if not callable(self.handler):
            raise TypeError("Command handler must be callable")


class CommandRegistry:
    """Immutable typed registry of command definitions."""

    __slots__ = ("_commands", "_sealed")

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("CommandRegistry is immutable")
        object.__setattr__(self, name, value)

    def __init__(self, commands: Iterable[CommandDefinition] = ()) -> None:
        items = tuple(commands)
        if any(not isinstance(item, CommandDefinition) for item in items):
            raise TypeError("CommandRegistry accepts CommandDefinition instances")
        if len({item.name for item in items}) != len(items):
            raise ValueError("Duplicate command name")
        object.__setattr__(
            self, "_commands",
            MappingProxyType({item.name: item for item in sorted(items, key=lambda x: x.name)}),
        )
        object.__setattr__(self, "_sealed", True)

    @property
    def commands(self) -> Mapping[str, CommandDefinition]:
        return self._commands

    def dispatch(self, name: str, arguments: str = "") -> CommandResponse:
        if not isinstance(name, str) or not isinstance(arguments, str):
            raise TypeError("Command name and arguments must be text")
        definition = self._commands.get(name)
        if definition is None:
            raise ConfigCommandError("Unknown command")
        return definition.handler(arguments)


LayerLoader = Callable[[Path], Iterable[ConfigLayer]]
Discovery = Callable[[], Iterable[DiscoveredPlugin]]


class ConfigCommandService:
    """Command operations over an existing typed config store and plugin runtime.

    ``config_path`` is an optional explicitly selected user-config target. If
    supplied, bare ``save``/``reload`` commands use it; otherwise a command
    must provide ``--file PATH``. Save always requires an explicit list of
    setting names. A custom ``layer_loader`` may rebuild all non-session
    precedence layers before the store commits a reload.
    """

    __slots__ = (
        "_store",
        "_runtime",
        "_config_path",
        "_layer_source",
        "_layer_loader",
        "_discover_plugins",
        "_baseline",
        "_restart_baseline",
        "_command_registry",
    )

    def __init__(
        self,
        store: ConfigStore,
        runtime: PluginRuntime | None = None,
        *,
        config_path: str | os.PathLike[str] | None = None,
        layer_source: str = "user",
        layer_loader: LayerLoader | None = None,
        discover_plugins: Discovery = discover,
        baseline_layers: Iterable[ConfigLayer] | None = None,
    ) -> None:
        if not isinstance(store, ConfigStore):
            raise TypeError("ConfigCommandService requires a ConfigStore")
        if runtime is not None and not isinstance(runtime, PluginRuntime):
            raise TypeError("runtime must be a PluginRuntime or None")
        if not callable(discover_plugins):
            raise TypeError("discover_plugins must be callable")
        # Validate the source label now rather than at a later reload.
        ConfigLayer(layer_source, {})
        self._store = store
        self._runtime = runtime
        self._config_path = Path(config_path) if config_path is not None else None
        self._layer_source = layer_source
        self._layer_loader = layer_loader or self._load_json_layer
        self._discover_plugins = discover_plugins
        self._restart_baseline = store.snapshot
        if baseline_layers is None:
            self._baseline = store.snapshot
        else:
            try:
                supplied_layers = tuple(baseline_layers)
            except TypeError:
                raise TypeError("baseline_layers must be iterable") from None
            self._baseline = store.registry.resolve(
                supplied_layers, {}, revision=store.snapshot.revision,
            )
        self._command_registry = CommandRegistry((
            CommandDefinition(
                "config", "Inspect and update typed configuration",
                "/config [get|describe|set|reset|diff|save|reload]", self._config_command,
            ),
            CommandDefinition(
                "plugins", "List or inspect discovered and loaded plugins",
                "/plugins [inspect ID]", self._plugins_command,
            ),
        ))

    @property
    def command_registry(self) -> CommandRegistry:
        return self._command_registry

    @property
    def store(self) -> ConfigStore:
        return self._store

    def execute(self, command_line: str) -> CommandResponse:
        """Execute ``/config ...`` or ``/plugins ...`` without evaluating code."""
        if not isinstance(command_line, str):
            return CommandResponse(False, "Command input must be text")
        match = re.fullmatch(r"\s*/?([a-z][a-z0-9_-]*)(?:\s+(.*?))?\s*", command_line, re.DOTALL)
        if match is None:
            return CommandResponse(False, "Expected /config or /plugins")
        return self._dispatch(match.group(1), match.group(2) or "")

    def dispatch(self, name: str, arguments: str = "") -> CommandResponse:
        """Dispatch by typed registry name for use by a shared command router."""
        return self._dispatch(name, arguments)

    def _dispatch(self, name: str, arguments: str) -> CommandResponse:
        try:
            return self._command_registry.dispatch(name, arguments)
        except ConfigError as exc:
            # Config validators deliberately hide rejected values. Field names
            # have already been checked before user-facing operations reach here.
            return CommandResponse(False, str(exc))
        except ConfigCommandError as exc:
            # Command errors contain only fixed text or schema-owned field IDs.
            return CommandResponse(False, str(exc))
        except (PluginError, ValueError):
            # Do not echo config-file contents, parser details, or arbitrary
            # plugin exception messages.
            return CommandResponse(False, "Command rejected; configuration was not changed")
        except Exception:
            # Third-party discovery and filesystem failures are not trusted to
            # produce safe exception messages.
            return CommandResponse(False, "Command failed without exposing configuration values")

    def get(self, name: str | None = None) -> CommandResponse:
        """Show effective value and provenance, with sensitive fields masked."""
        if name is not None:
            self._require_field(name)
            names = (name,)
        else:
            names = tuple(self._store.registry.fields)
        if not names:
            return CommandResponse(True, "No configuration fields are registered")
        rows = [self._value_line(field_name, self._store.snapshot) for field_name in names]
        return CommandResponse(True, "\n".join(rows))

    def describe(self, name: str) -> CommandResponse:
        """Describe schema, constraints, provenance, and application timing."""
        item = self._require_field(name)
        effective = self._store.snapshot.entries[name]
        default: object = "<redacted>" if item.sensitive else item.default
        choices: object = "<redacted>" if item.sensitive else item.choices
        lines = [
            f"{name} ({item.value_type.__name__})",
            f"  owner: {item.owner}",
            f"  documentation: {item.documentation or '(none)'}",
            f"  default: {_render_value(default)}",
            f"  effective: {_render_value(self._display_value(item, effective.value))}",
            f"  source: {effective.source}",
            f"  masked sources: {', '.join(effective.masked_sources) or '(none)'}",
            f"  applies: {item.apply_at.value}",
            f"  restart pending: {'yes' if self._restart_pending(name, effective.value) else 'no'}",
            f"  sensitive: {'yes' if item.sensitive else 'no'}",
            f"  persistable: {'yes' if item.persistable and not item.sensitive else 'no'}",
        ]
        if item.choices:
            lines.append(f"  choices: {_render_value(choices)}")
        if item.minimum is not None:
            lines.append(f"  minimum: {_render_value(item.minimum)}")
        if item.maximum is not None:
            lines.append(f"  maximum: {_render_value(item.maximum)}")
        return CommandResponse(True, "\n".join(lines))

    def set(self, name: str, value: Scalar) -> CommandResponse:
        """Set a typed scalar as a session override; no persistence is implicit."""
        self._require_field(name)
        change = self._store.set({name: value})
        item = self._store.registry.fields[name]
        return CommandResponse(
            True,
            f"Updated {name} to {_render_value(self._display_value(item, change.after.get(name)))}; "
            f"revision {change.after.revision}; applies {item.apply_at.value}; "
            f"restart pending: {'yes' if self._restart_pending(name, change.after.get(name)) else 'no'}.",
        )

    def set_json(self, name: str, scalar_json: str) -> CommandResponse:
        """Parse one JSON scalar as data, then set it transactionally."""
        self._require_field(name)
        change = self._store.set_json(name, scalar_json)
        item = self._store.registry.fields[name]
        return CommandResponse(
            True,
            f"Updated {name} to {_render_value(self._display_value(item, change.after.get(name)))}; "
            f"revision {change.after.revision}; applies {item.apply_at.value}; "
            f"restart pending: {'yes' if self._restart_pending(name, change.after.get(name)) else 'no'}.",
        )

    def reset(self, *names: str) -> CommandResponse:
        """Remove explicit session overrides, restoring lower-precedence values."""
        if not names:
            raise ConfigCommandError("At least one setting name is required")
        for name in names:
            self._require_field(name)
        if len(set(names)) != len(names):
            raise ConfigCommandError("Duplicate setting name")
        change = self._store.reset(*names)
        return CommandResponse(
            True,
            f"Reset {', '.join(names)}; revision {change.after.revision}.",
        )

    def diff(self) -> CommandResponse:
        """Show changes since the initial or most recent save/reload checkpoint."""
        current = self._store.snapshot
        changed = [
            name for name in self._store.registry.fields
            if (current.entries[name].value, current.entries[name].source, current.entries[name].masked_sources)
            != (self._baseline.entries[name].value,
                self._baseline.entries[name].source,
                self._baseline.entries[name].masked_sources)
        ]
        if not changed:
            return CommandResponse(True, "No configuration changes")
        rows = ["Configuration differences since the last checkpoint:"]
        for name in changed:
            item = self._store.registry.fields[name]
            old, new = self._baseline.entries[name], current.entries[name]
            rows.append(
                f"  {name}: {_render_value(self._display_value(item, old.value))} -> "
                f"{_render_value(self._display_value(item, new.value))} "
                f"(source: {old.source} -> {new.source}; applies: {item.apply_at.value})"
            )
        return CommandResponse(True, "\n".join(rows))

    def save(
        self,
        names: Sequence[str],
        path: str | os.PathLike[str] | None = None,
    ) -> CommandResponse:
        """Atomically save only explicitly selected session overrides."""
        selected = self._selected_names(names)
        target = self._selected_path(path)
        try:
            values = self._store.persistence_values(selected)
        except ConfigError as exc:
            # The store never reveals setting values; callers also validate
            # field identifiers before reaching this point.
            raise ConfigCommandError(str(exc)) from None
        # Save is a patch to the chosen file, not a replacement with only the
        # selected keys. Otherwise saving one field would erase the provider,
        # activated plugins, and every other setting in the file.
        try:
            existing = _read_json_mapping(target) if target.exists() else {}
            candidate = {**existing, **values}
            self._store.registry.validate(candidate)
            if any(self._store.registry.fields[name].sensitive for name in candidate):
                raise ConfigCommandError("Sensitive settings must use a credential backend")
        except ConfigCommandError:
            raise
        except (ConfigError, OSError, UnicodeError, ValueError, TypeError):
            raise ConfigCommandError("Existing configuration file is invalid") from None
        content = json.dumps(candidate, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
        self._atomic_write(target, content.encode("utf-8"))
        self._checkpoint(selected)
        return CommandResponse(
            True,
            f"Saved explicitly selected session settings ({', '.join(selected)}) to "
            f"{target}; sensitive and non-persistable settings are excluded.",
        )

    def reload(self, path: str | os.PathLike[str] | None = None) -> CommandResponse:
        """Validate all candidate layers, then atomically replace file layers."""
        target = self._selected_path(path)
        try:
            try:
                layers = tuple(self._layer_loader(target))
            except Exception:
                raise ConfigCommandError("Configuration layer loader failed") from None
            if any(not isinstance(layer, ConfigLayer) for layer in layers):
                raise ConfigCommandError("Configuration loader returned invalid layers")
            for layer in layers:
                for name in layer.values:
                    self._require_field(name)
                    if self._store.registry.fields[name].sensitive:
                        raise ConfigCommandError("Sensitive settings must use a credential backend")
            # Validate the complete base candidate before touching the store.
            candidate = self._store.registry.resolve(
                layers, {}, revision=self._store.snapshot.revision + 1,
            )
            change = self._store.reload(layers)
        except ConfigCommandError:
            raise
        except (ConfigError, OSError, UnicodeError, ValueError, TypeError):
            # Parser/validator errors may contain untrusted file keys or values.
            raise ConfigCommandError("Configuration file is invalid or cannot be read") from None
        self._baseline = ConfigSnapshot(change.after.revision, candidate.entries)
        return CommandResponse(
            True,
            f"Reloaded configuration from {target}; revision {change.after.revision}; "
            "session overrides remain in force.",
        )

    def inspect_plugins(self) -> CommandResponse:
        """List entry-point metadata separately from loaded runtime manifests."""
        discovered = self._discovered()
        lines = ["Discovered entry points (metadata only; not imported):"]
        if discovered:
            for item in discovered:
                distribution = item.distribution or "(unknown distribution)"
                lines.append(f"  {item.name}: {item.target} [{distribution}]")
        else:
            lines.append("  (none)")
        lines.append("Loaded runtime manifests:")
        manifests = self._runtime.manifests if self._runtime is not None else {}
        if manifests:
            for plugin_id, manifest in manifests.items():
                lines.append(self._manifest_line(plugin_id, manifest))
        else:
            lines.append("  (none)")
        return CommandResponse(True, "\n".join(lines))

    def inspect_plugin(self, plugin_id: str) -> CommandResponse:
        """Inspect matching metadata and runtime manifest without activation."""
        if not isinstance(plugin_id, str) or not plugin_id.strip():
            raise ConfigCommandError("Plugin ID is required")
        discovered = [item for item in self._discovered() if item.name == plugin_id]
        manifests = self._runtime.manifests if self._runtime is not None else {}
        manifest = manifests.get(plugin_id)
        if not discovered and manifest is None:
            raise ConfigCommandError("No discovered metadata or loaded manifest matches that plugin")
        lines = [f"Plugin {plugin_id}:"]
        if discovered:
            for item in discovered:
                lines.append("  discovery: metadata only; plugin code was not imported")
                lines.append(f"  entry point: {item.target}")
                lines.append(f"  distribution: {item.distribution or '(unknown distribution)'}")
        else:
            lines.append("  discovery: no matching entry-point metadata")
        if manifest is not None:
            lines.append("  runtime: loaded")
            lines.append(f"  API range: {manifest.api_min}..{manifest.api_max}")
            lines.append(f"  requires: {', '.join(manifest.requires) or '(none)'}")
        else:
            lines.append("  runtime: not loaded (metadata inspection does not activate plugins)")
        return CommandResponse(True, "\n".join(lines))

    def _config_command(self, arguments: str) -> CommandResponse:
        operation, remainder = _split_head(arguments)
        if operation is None:
            return self.get()
        if operation == "get":
            tokens = _tokens(remainder)
            if len(tokens) > 1:
                raise ConfigCommandError("Usage: /config get [NAME]")
            return self.get(tokens[0] if tokens else None)
        if operation == "describe":
            tokens = _tokens(remainder)
            if len(tokens) != 1:
                raise ConfigCommandError("Usage: /config describe NAME")
            return self.describe(tokens[0])
        if operation == "set":
            match = re.fullmatch(r"\s*(\S+)\s+(.+?)\s*", remainder, re.DOTALL)
            if match is None:
                raise ConfigCommandError('Usage: /config set NAME JSON_SCALAR (for example: "high" or 12)')
            return self.set_json(match.group(1), match.group(2))
        if operation == "reset":
            return self.reset(*_tokens(remainder))
        if operation == "diff":
            if remainder.strip():
                raise ConfigCommandError("Usage: /config diff")
            return self.diff()
        if operation == "save":
            tokens = _tokens(remainder)
            path, names = self._parse_file_option(tokens, self._config_path)
            if not names:
                raise ConfigCommandError("Usage: /config save [--file PATH] NAME [NAME ...]")
            return self.save(names, path)
        if operation == "reload":
            tokens = _tokens(remainder)
            path, extra = self._parse_file_option(tokens, self._config_path)
            if extra:
                raise ConfigCommandError("Usage: /config reload [--file PATH]")
            return self.reload(path)
        raise ConfigCommandError("Unknown /config operation")

    def _plugins_command(self, arguments: str) -> CommandResponse:
        operation, remainder = _split_head(arguments)
        if operation is None:
            return self.inspect_plugins()
        if operation != "inspect":
            raise ConfigCommandError("Usage: /plugins [inspect ID]")
        tokens = _tokens(remainder)
        if len(tokens) != 1:
            raise ConfigCommandError("Usage: /plugins inspect ID")
        return self.inspect_plugin(tokens[0])

    def _require_field(self, name: str) -> ConfigField:
        if not isinstance(name, str) or name not in self._store.registry.fields:
            # Do not echo an arbitrary argument: it could itself be a secret.
            raise ConfigCommandError("Unknown configuration field")
        return self._store.registry.fields[name]

    def _selected_names(self, names: Sequence[str]) -> tuple[str, ...]:
        if isinstance(names, (str, bytes)):
            raise ConfigCommandError("Setting names must be an explicit sequence")
        try:
            selected = tuple(names)
        except TypeError:
            raise ConfigCommandError("Setting names must be an explicit sequence") from None
        if not selected:
            raise ConfigCommandError("At least one setting name must be selected")
        for name in selected:
            self._require_field(name)
        if len(set(selected)) != len(selected):
            raise ConfigCommandError("Duplicate setting name")
        return selected

    def _selected_path(self, path: str | os.PathLike[str] | None) -> Path:
        selected = path if path is not None else self._config_path
        if selected is None:
            raise ConfigCommandError("Select a configuration file with --file PATH")
        try:
            raw = os.fspath(selected)
            if not isinstance(raw, str) or not raw or "\x00" in raw:
                raise ValueError
            result = Path(os.path.abspath(raw))
        except (TypeError, ValueError, OSError):
            raise ConfigCommandError("Invalid configuration file path") from None
        return result

    @staticmethod
    def _parse_file_option(
        tokens: list[str], default_path: Path | None,
    ) -> tuple[Path | None, list[str]]:
        if tokens and tokens[0] == "--file":
            if len(tokens) < 2:
                raise ConfigCommandError("--file requires a path")
            return Path(tokens[1]), tokens[2:]
        if default_path is not None:
            return default_path, tokens
        return None, tokens

    def _value_line(self, name: str, snapshot: ConfigSnapshot) -> str:
        item = self._store.registry.fields[name]
        resolved = snapshot.entries[name]
        value = self._display_value(item, resolved.value)
        masked = ", ".join(resolved.masked_sources) or "(none)"
        pending = "; restart pending" if self._restart_pending(name, resolved.value) else ""
        return (
            f"{name} = {_render_value(value)} "
            f"(source: {resolved.source}; masked: {masked}; applies: {item.apply_at.value}{pending})"
        )

    def _restart_pending(self, name: str, value: object) -> bool:
        item = self._store.registry.fields[name]
        return (item.apply_at is ApplyAt.RESTART
                and value != self._restart_baseline.entries[name].value)

    @staticmethod
    def _display_value(item: ConfigField, value: object) -> object:
        return "<redacted>" if item.sensitive else value

    def _checkpoint(self, names: Sequence[str]) -> None:
        entries = dict(self._baseline.entries)
        current = self._store.snapshot
        for name in names:
            entries[name] = current.entries[name]
        self._baseline = ConfigSnapshot(current.revision, entries)

    def _discovered(self) -> tuple[DiscoveredPlugin, ...]:
        try:
            items = tuple(self._discover_plugins())
        except Exception:
            raise ConfigCommandError("Plugin discovery metadata is unavailable") from None
        if any(not isinstance(item, DiscoveredPlugin) for item in items):
            raise ConfigCommandError("Plugin discovery returned invalid metadata")
        for item in items:
            if (not _valid_metadata_text(item.name) or not _valid_metadata_text(item.target)
                    or (item.distribution is not None and not _valid_metadata_text(item.distribution))):
                raise ConfigCommandError("Plugin discovery returned invalid metadata")
        return tuple(sorted(items, key=lambda item: (item.name, item.target, item.distribution or "")))

    @staticmethod
    def _manifest_line(plugin_id: str, manifest: PluginManifest) -> str:
        return (
            f"  {plugin_id}: loaded, API {manifest.api_min}..{manifest.api_max}; "
            f"requires {', '.join(manifest.requires) or '(none)'}"
        )

    def _load_json_layer(self, path: Path) -> tuple[ConfigLayer, ...]:
        values = _read_json_mapping(path)
        try:
            layer = ConfigLayer(self._layer_source, values)
        except ConfigError:
            raise ConfigCommandError("Configuration file is invalid") from None
        return (layer,)

    def _atomic_write(self, path: Path, content: bytes) -> None:
        parent = path.parent
        _assert_no_symlink_path(parent, allow_missing_final=False)
        try:
            parent_info = os.lstat(parent)
            if not stat.S_ISDIR(parent_info.st_mode):
                raise ConfigCommandError("Configuration target parent is not a directory")
            try:
                target_info = os.lstat(path)
            except FileNotFoundError:
                target_info = None
            if target_info is not None:
                _validate_config_file_stat(target_info)
        except ConfigCommandError:
            raise
        except OSError:
            raise ConfigCommandError("Unable to access configuration target") from None

        temp_path: str | None = None
        descriptor: int | None = None
        try:
            descriptor, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
            temp_path = None
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            directory_fd = os.open(parent, flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except ConfigCommandError:
            raise
        except OSError:
            raise ConfigCommandError("Unable to atomically save configuration file") from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass


def _split_head(arguments: str) -> tuple[str | None, str]:
    match = re.match(r"\s*(\S+)(?:\s+(.*))?\s*\Z", arguments, re.DOTALL)
    if match is None:
        return None, ""
    return match.group(1), match.group(2) or ""


def _tokens(arguments: str) -> list[str]:
    try:
        return shlex.split(arguments, posix=True)
    except ValueError:
        raise ConfigCommandError("Invalid command arguments") from None


def _render_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _valid_metadata_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip() and "\x00" not in value


def _assert_no_symlink_path(path: Path, *, allow_missing_final: bool) -> None:
    """Reject symlinked path components before any read/write operation."""
    absolute = Path(os.path.abspath(path))
    parts = absolute.parts
    current = Path(parts[0])
    for index, component in enumerate(parts[1:], start=1):
        current = current / component
        final = index == len(parts) - 1
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if final and allow_missing_final:
                return
            raise ConfigCommandError("Configuration path does not exist") from None
        except OSError:
            raise ConfigCommandError("Unable to inspect configuration path") from None
        if stat.S_ISLNK(info.st_mode):
            raise ConfigCommandError("Configuration paths cannot contain symlinks")
        if not final and not stat.S_ISDIR(info.st_mode):
            raise ConfigCommandError("Configuration path parent is not a directory")


def _validate_config_file_stat(info: os.stat_result) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022 or info.st_nlink != 1):
        raise ConfigCommandError(
            "Configuration file must be an owned regular file, not group/world writable or multiply linked"
        )


def _read_json_mapping(path: Path) -> dict[str, Scalar]:
    _assert_no_symlink_path(path, allow_missing_final=False)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ConfigCommandError("Unable to read configuration file") from None
    try:
        info = os.fstat(descriptor)
        _validate_config_file_stat(info)
        if info.st_size > MAX_CONFIG_BYTES:
            raise ConfigCommandError("Configuration file exceeds the size limit")
        chunks = bytearray()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            chunks.extend(stream.read(MAX_CONFIG_BYTES + 1))
        if len(chunks) > MAX_CONFIG_BYTES:
            raise ConfigCommandError("Configuration file exceeds the size limit")
        text = bytes(chunks).decode("utf-8")
    except ConfigCommandError:
        raise
    except (OSError, UnicodeError):
        raise ConfigCommandError("Configuration file is invalid or cannot be read") from None
    finally:
        os.close(descriptor)

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ConfigCommandError("Configuration file contains duplicate keys")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ConfigCommandError("Configuration file contains a non-finite number")

    try:
        decoded = json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except ConfigCommandError:
        raise
    except (ValueError, RecursionError):
        raise ConfigCommandError("Configuration file is invalid JSON") from None
    if not isinstance(decoded, dict):
        raise ConfigCommandError("Configuration file must be a JSON object")
    try:
        # ConfigLayer performs the final finite-scalar and key validation.
        ConfigLayer("user", decoded)
    except ConfigError:
        raise ConfigCommandError("Configuration file must contain flat scalar settings") from None
    return decoded  # type: ignore[return-value]
