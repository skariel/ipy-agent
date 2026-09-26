"""Contract tests for the opt-in wrapper-backed executor (not a live sandbox test)."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

from py_agent.configuration import ConfigError
from py_agent.isolated_executor import (
    PLUGIN_ID,
    IsolatedExecutor,
    IsolatedExecutorPlugin,
    create_isolated_executor,
)
from py_agent.builtin_services import BuiltinPlugin
from py_agent.local_executor import LocalExecutor
from py_agent.plugins import PluginError, PluginRuntime


def test_plugin_is_not_activated_without_explicit_registration():
    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})

    assert runtime.select("executor", "local").id == "local"
    with pytest.raises(PluginError, match="No enabled executor service"):
        runtime.select("executor", "isolated")


def test_plugin_registers_namespaced_restart_only_isolated_service():
    runtime = PluginRuntime.load(builtins={PLUGIN_ID: IsolatedExecutorPlugin()})

    service = runtime.select("executor", "isolated")
    assert runtime.manifests[PLUGIN_ID].id == PLUGIN_ID
    assert service.factory.__name__ == "_service_factory"
    assert f"{PLUGIN_ID}.wrapper_command" in runtime.config.fields
    assert runtime.config.fields[f"{PLUGIN_ID}.wrapper_command"].apply_at.value == "restart"
    assert runtime.config.fields[f"{PLUGIN_ID}.wrapper_command"].sensitive is False

    with pytest.raises(ConfigError):
        runtime.config.validate({f"{PLUGIN_ID}.wrapper_command": '["", "--"]'})


def test_service_factory_parses_json_argv_without_shell_interpretation():
    runtime = PluginRuntime.load(builtins={PLUGIN_ID: IsolatedExecutorPlugin()})
    service = runtime.select("executor", "isolated")
    wrapper_argv = ["/tmp/wrapper", "--literal", "$(touch never-run); *", "--"]
    executor = service.factory(config={
        PLUGIN_ID: {"wrapper_command": json.dumps(wrapper_argv)},
    })

    assert isinstance(executor, IsolatedExecutor)
    assert executor.wrapper_command == tuple(wrapper_argv)
    assert isinstance(executor, LocalExecutor)
    assert executor.capabilities.persistent
    assert executor.capabilities.completion
    assert executor.capabilities.inspection
    assert executor.capabilities.rich_output
    assert executor.capabilities.input
    assert executor.capabilities.interrupt


def test_selecting_isolated_service_without_wrapper_fails_instead_of_using_local():
    runtime = PluginRuntime.load(builtins={PLUGIN_ID: IsolatedExecutorPlugin()})
    service = runtime.select("executor", "isolated")

    with pytest.raises(ValueError, match="wrapper_command"):
        service.factory(config={PLUGIN_ID: {"wrapper_command": ""}})


def test_direct_factory_requires_explicit_argv_and_absolute_worker_paths():
    with pytest.raises(TypeError):
        create_isolated_executor("bwrap")
    with pytest.raises(ValueError):
        create_isolated_executor(["/usr/bin/bwrap", "--", "\x00bad"])
    with pytest.raises(ValueError, match="absolute"):
        create_isolated_executor(["/usr/bin/bwrap"], worker_executable="python")


def test_missing_wrapper_fails_before_any_process_is_spawned(monkeypatch):
    executor = create_isolated_executor(["/tmp/not-installed-wrapper", "--"])
    spawned = False

    async def unexpected_spawn(*_argv, **_kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("must not attempt a process launch")

    monkeypatch.setattr("py_agent.isolated_executor.shutil.which", lambda _command: None)
    monkeypatch.setattr("py_agent.isolated_executor.asyncio.create_subprocess_exec", unexpected_spawn)

    async def scenario():
        with pytest.raises(FileNotFoundError, match="isolation wrapper executable"):
            await executor.start()

    asyncio.run(scenario())
    assert spawned is False
    assert executor.process is None
    assert executor._closed is True


@pytest.mark.asyncio
async def test_start_passes_literal_argv_and_pinned_worker_to_wrapper(tmp_path, monkeypatch):
    wrapper = tmp_path / "wrapper"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o700)
    configured = (str(wrapper), "--preserve-env=PATH", "literal ; $(touch never-run)", "--")
    executor = create_isolated_executor(configured)
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = os.getpid()
        returncode = None

    process = FakeProcess()

    async def fake_spawn(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return process

    async def fake_receive(_process):
        return {"type": "ready", "version": 1}

    async def fake_stop(_process):
        executor.process = None
        executor._closed = True

    monkeypatch.setattr("py_agent.isolated_executor.asyncio.create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(executor, "_receive", fake_receive)
    monkeypatch.setattr(executor, "_stop_process", fake_stop)

    await executor.start()

    argv = captured["argv"]
    assert argv == (
        str(wrapper.resolve()),
        "--preserve-env=PATH",
        "literal ; $(touch never-run)",
        "--",
        os.path.abspath(sys.executable),
        "-I",
        "-c",
        "import sys; sys.path.insert(0, sys.argv[1]); from py_agent.local_worker import main; main()",
        str(Path(__file__).resolve().parents[1] / "src"),
    )
    kwargs = captured["kwargs"]
    assert "shell" not in kwargs  # create_subprocess_exec receives argv, never shell text.
    assert executor._started is True
    assert executor.process is process

    await executor.close()
    assert executor.process is None
    assert executor._closed is True


@pytest.mark.asyncio
async def test_bad_wrapped_worker_handshake_fails_and_closes_without_fallback(tmp_path, monkeypatch):
    wrapper = tmp_path / "wrapper"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o700)
    executor = create_isolated_executor([str(wrapper), "--"])
    stopped = []

    class FakeProcess:
        pid = os.getpid()
        returncode = None

    process = FakeProcess()

    async def fake_spawn(*_argv, **_kwargs):
        return process

    async def bad_receive(_process):
        return {"type": "untrusted-wrapper-output", "version": 1}

    async def fake_stop(stopped_process):
        stopped.append(stopped_process)
        executor.process = None
        executor._closed = True

    monkeypatch.setattr("py_agent.isolated_executor.asyncio.create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(executor, "_receive", bad_receive)
    monkeypatch.setattr(executor, "_stop_process", fake_stop)

    with pytest.raises(RuntimeError, match="pinned worker's valid ready frame"):
        await executor.start()

    assert stopped == [process]
    assert executor.process is None
    assert executor._closed is True
    assert executor._started is False
