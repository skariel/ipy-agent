"""Opt-in executor that runs the pinned worker behind an explicit wrapper.

The wrapper is an external policy mechanism, not a policy verified by py. This
module does not claim confidentiality or treat a separate process as isolation.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import json
import os
from pathlib import Path
import shutil
import sys
from types import MappingProxyType
from typing import Any, cast

from .configuration import ApplyAt, ConfigField, Scalar
from .local_executor import (
    DEFAULT_OUTPUT_CHARS,
    MAX_FRAME,
    MAX_OUTPUT_CHARS,
    LocalExecutor,
)
from .plugins import Contributions, PluginManifest, Service, hookimpl

PLUGIN_ID = "isolated-executor"
_DEFAULT_WORKER_EXECUTABLE = os.path.abspath(sys.executable)
_DEFAULT_RUNTIME_PARENT = str(Path(__file__).resolve().parent.parent)


def _parse_wrapper_command(value: object) -> tuple[str, ...]:
    """Decode a JSON argv array; never interpret it as shell source."""
    if not isinstance(value, str):
        raise TypeError("wrapper_command must be a JSON array encoded as text")
    try:
        command = json.loads(value)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("wrapper_command must be a valid JSON argv array") from exc
    return _validate_argv(command)


def _validate_argv(command: object) -> tuple[str, ...]:
    if isinstance(command, (str, bytes)) or not isinstance(command, Sequence):
        raise TypeError("wrapper_command must be a nonempty sequence of argument strings")
    argv = tuple(command)
    if not argv or any(type(argument) is not str or "\x00" in argument for argument in argv):
        raise ValueError("wrapper_command must contain nonempty, NUL-free argument strings")
    if not argv[0] or not os.path.isabs(argv[0]):
        raise ValueError("wrapper_command executable must be an absolute path")
    return argv


def _absolute_path(value: object, name: str) -> str:
    if (not isinstance(value, str) or not value or "\x00" in value
            or not os.path.isabs(value)):
        raise ValueError(f"{name} must be an absolute, NUL-free path")
    return value


def _valid_wrapper_command_setting(value: Scalar) -> None:
    if value != "":  # Unconfigured until this optional executor is selected.
        _parse_wrapper_command(value)


def _valid_absolute_path_setting(value: Scalar) -> None:
    _absolute_path(value, "configured path")


class IsolatedExecutor(LocalExecutor):
    """Persistent LocalExecutor protocol transported through an external wrapper.

    The wrapper is started once per executor and remains around the *entire*
    persistent worker lifetime. It is not relaunched for each execution. The
    wrapper must preserve the worker's stdin/stdout byte stream, keep diagnostics
    off stdout, run the appended command, and arrange lifecycle/signal handling.
    The executor does not verify the wrapper's isolation policy.
    """

    def __init__(
        self,
        wrapper_command: Sequence[str],
        *,
        worker_executable: str | None = None,
        runtime_parent: str | None = None,
        timeout: float = 15,
        interrupt_timeout: float = 2,
        input_timeout: float = 300,
        max_output_chars: int = DEFAULT_OUTPUT_CHARS,
    ) -> None:
        self.wrapper_command = _validate_argv(wrapper_command)
        super().__init__(
            executable=(
                _DEFAULT_WORKER_EXECUTABLE if worker_executable is None else worker_executable
            ),
            timeout=timeout,
            interrupt_timeout=interrupt_timeout,
            input_timeout=input_timeout,
            max_output_chars=max_output_chars,
        )
        self.executable = _absolute_path(self.executable, "worker_executable")
        self.runtime_parent = _absolute_path(
            _DEFAULT_RUNTIME_PARENT if runtime_parent is None else runtime_parent,
            "runtime_parent",
        )

    def _resolve_wrapper(self) -> str:
        configured = self.wrapper_command[0]
        found = shutil.which(configured)
        if found is None:
            raise FileNotFoundError(
                f"Configured isolation wrapper executable was not found: {configured!r}"
            )
        try:
            resolved = Path(found).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise FileNotFoundError(
                f"Configured isolation wrapper cannot be resolved: {configured!r}"
            ) from exc
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise PermissionError(
                f"Configured isolation wrapper is not an executable file: {resolved}"
            )
        return str(resolved)

    async def start(self) -> None:
        if self._closed or self._started or self.process is not None:
            raise RuntimeError("Executor already started or closed")
        try:
            wrapper = self._resolve_wrapper()
            worker_path = Path(__file__).resolve().with_name("local_worker.py")
            if not worker_path.is_file():
                raise RuntimeError(f"Pinned runtime worker is missing: {worker_path}")
            bootstrap = (
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "from py_agent.local_worker import main; main()"
            )
            # create_subprocess_exec passes each value as one argv element. No
            # shell is involved; the configured wrapper prefix is followed by
            # the fixed interpreter/bootstrap for the installed worker.
            argv = (
                wrapper,
                *self.wrapper_command[1:],
                self.executable,
                "-I",
                "-c",
                bootstrap,
                self.runtime_parent,
            )
            kwargs: dict[str, Any] = {}
            if sys.platform != "win32":
                kwargs["start_new_session"] = True
            self.process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=MAX_FRAME + 4,
                **kwargs,
            )
            ready = await asyncio.wait_for(self._receive(self.process), timeout=self.timeout)
            if (
                set(ready) != {"type", "version"}
                or ready.get("type") != "ready"
                or type(ready.get("version")) is not int
                or ready["version"] != 1
            ):
                raise RuntimeError(
                    "Isolation wrapper did not deliver the pinned worker's valid ready frame"
                )
            self._started = True
        except BaseException:
            process = self.process
            if process is not None:
                await asyncio.shield(self._stop_process(process))
            else:
                self._closed = True
            raise


def create_isolated_executor(
    wrapper_command: Sequence[str],
    *,
    worker_executable: str | None = None,
    runtime_parent: str | None = None,
    timeout: float = 15,
    interrupt_timeout: float = 2,
    input_timeout: float = 300,
    max_output_chars: int = DEFAULT_OUTPUT_CHARS,
) -> IsolatedExecutor:
    """Construct an isolated executor from an explicit wrapper argv prefix.

    ``runtime_parent`` and ``worker_executable`` are paths *inside the wrapper's
    execution environment*. By default these are the current installation's
    interpreter and package root. The wrapper must expose matching trusted paths
    (typically read-only mounts) and compatible Python/IPython dependencies.
    """
    return IsolatedExecutor(
        wrapper_command,
        worker_executable=worker_executable,
        runtime_parent=runtime_parent,
        timeout=timeout,
        interrupt_timeout=interrupt_timeout,
        input_timeout=input_timeout,
        max_output_chars=max_output_chars,
    )


def _isolated_executor_from_namespace(namespace: Mapping[str, Scalar]) -> IsolatedExecutor:
    allowed = {
        "wrapper_command", "worker_executable", "runtime_parent", "startup_timeout",
        "interrupt_timeout", "input_timeout", "max_output_chars",
    }
    unknown = set(namespace) - allowed
    if unknown:
        raise ValueError(f"Unknown {PLUGIN_ID} configuration keys: {sorted(unknown)}")
    command = _parse_wrapper_command(namespace.get("wrapper_command", ""))
    return create_isolated_executor(
        command,
        worker_executable=cast(str | None, namespace.get("worker_executable", _DEFAULT_WORKER_EXECUTABLE)),
        runtime_parent=cast(str | None, namespace.get("runtime_parent", _DEFAULT_RUNTIME_PARENT)),
        timeout=cast(float, namespace.get("startup_timeout", 15.0)),
        interrupt_timeout=cast(float, namespace.get("interrupt_timeout", 2.0)),
        input_timeout=cast(float, namespace.get("input_timeout", 300.0)),
        max_output_chars=cast(int, namespace.get("max_output_chars", DEFAULT_OUTPUT_CHARS)),
    )


def isolated_executor_service_factory(
    *, config: Mapping[str, Mapping[str, Scalar]],
) -> IsolatedExecutor:
    """Service factory consuming this plugin's immutable configuration namespace."""
    if not isinstance(config, Mapping):
        raise TypeError("Plugin service config must be a mapping")
    namespace = config.get(PLUGIN_ID)
    if not isinstance(namespace, Mapping):
        raise ValueError(f"Missing {PLUGIN_ID!r} configuration namespace")
    return _isolated_executor_from_namespace(namespace)


class IsolatedExecutorPlugin:
    """Embeddable, non-auto-activated executor service contribution.

    ``config`` optionally supplies this plugin's namespace with unprefixed keys,
    allowing direct ``Coordinator`` embedders to bind a validated snapshot. Hosts
    with shared config injection can omit it and pass the namespace to the service
    factory as usual.
    """

    def __init__(self, *, config: Mapping[str, Scalar] | None = None) -> None:
        if config is not None and not isinstance(config, Mapping):
            raise TypeError("Isolated executor config must be a mapping")
        self._config = None if config is None else MappingProxyType(dict(config))

    def _service_factory(
        self, *, config: Mapping[str, Mapping[str, Scalar]] | None = None,
    ) -> IsolatedExecutor:
        if config is None:
            if self._config is None:
                raise ValueError(
                    "The isolated executor requires an explicit wrapper_command configuration"
                )
            namespace: Mapping[str, Scalar] = self._config
        else:
            if not isinstance(config, Mapping):
                raise TypeError("Plugin service config must be a mapping")
            candidate = config.get(PLUGIN_ID)
            if not isinstance(candidate, Mapping):
                raise ValueError(f"Missing {PLUGIN_ID!r} configuration namespace")
            namespace = candidate
        return _isolated_executor_from_namespace(namespace)

    @hookimpl
    def py_agent_register(self) -> Contributions:
        restart = ApplyAt.RESTART
        fields = (
            ConfigField(
                f"{PLUGIN_ID}.wrapper_command", PLUGIN_ID, str, "",
                "JSON argv array for an explicit isolation wrapper; never shell text.",
                apply_at=restart, validator=_valid_wrapper_command_setting,
            ),
            ConfigField(
                f"{PLUGIN_ID}.worker_executable", PLUGIN_ID, str, _DEFAULT_WORKER_EXECUTABLE,
                "Absolute Python executable path as seen inside the wrapper.",
                apply_at=restart, validator=_valid_absolute_path_setting,
            ),
            ConfigField(
                f"{PLUGIN_ID}.runtime_parent", PLUGIN_ID, str, _DEFAULT_RUNTIME_PARENT,
                "Absolute installed py_agent import root as seen inside the wrapper.",
                apply_at=restart, validator=_valid_absolute_path_setting,
            ),
            ConfigField(
                f"{PLUGIN_ID}.startup_timeout", PLUGIN_ID, float, 15.0,
                "Seconds allowed for the wrapped worker ready frame.",
                apply_at=restart, minimum=0.1, maximum=300.0,
            ),
            ConfigField(
                f"{PLUGIN_ID}.interrupt_timeout", PLUGIN_ID, float, 2.0,
                "Seconds allowed for wrapper/worker interrupt and shutdown.",
                apply_at=restart, minimum=0.1, maximum=60.0,
            ),
            ConfigField(
                f"{PLUGIN_ID}.input_timeout", PLUGIN_ID, float, 300.0,
                "Seconds to wait for one correlated interactive input reply.",
                apply_at=restart, minimum=0.1, maximum=3600.0,
            ),
            ConfigField(
                f"{PLUGIN_ID}.max_output_chars", PLUGIN_ID, int, DEFAULT_OUTPUT_CHARS,
                "Maximum retained stream output per execution.",
                apply_at=restart, minimum=1, maximum=MAX_OUTPUT_CHARS,
            ),
        )
        return Contributions(
            manifest=PluginManifest(PLUGIN_ID),
            services=(Service("executor", "isolated", self._service_factory),),
            config_fields=fields,
        )


plugin = IsolatedExecutorPlugin()
