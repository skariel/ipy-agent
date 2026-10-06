"""Pi-style terminal interactions; providers and secrets are synthetic."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input

from py_agent import cli, native_auth, oauth
from py_agent.plain_terminal import PlainTerminal, _TerminalHistory
from py_agent.terminal_completion import TerminalCompleter
from test_plain_terminal import CoordinatorStub, Output, until


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("py_agent.codex_auth.DEFAULT_AUTH_FILE", tmp_path / "pi.json")
    return tmp_path


def complete(completer, text):
    return list(completer.get_completions(Document(text), CompleteEvent(completion_requested=True)))


def test_commands_arguments_and_plugins():
    coordinator = SimpleNamespace(command_registry=SimpleNamespace(commands={"custom": object()}))
    completer = TerminalCompleter(coordinator)
    assert "/login" in [v.text for v in complete(completer, "/lg")]
    assert "/custom" in [v.text for v in complete(completer, "/cus")]
    assert [v.text for v in complete(completer, "/think xh")] == ["xhigh"]
    assert "deepseek" in [v.text for v in complete(completer, "/login dsk")]
    assert "--api-key" in [v.text for v in complete(completer, "/login openrouter --ap")]
    assert [v.text for v in complete(completer, "/auth st")] == ["status"]


def test_file_fuzzy_paths_are_names_only_bounded_and_keep_python_prefix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "authentication.py").write_text("private contents never loaded")
    (tmp_path / ".env").write_text("private")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "authentication.py").touch()
    (tmp_path / "linked").symlink_to(tmp_path / "src", target_is_directory=True)
    completer = TerminalCompleter(SimpleNamespace())
    for text in ["inspect athpy", "@athpy", "!cat athpy"]:
        values = complete(completer, text)
        assert [v.text for v in values] == ["src/authentication.py"]
        assert values[0].start_position == -len("athpy")
        assert "private" not in str(values)
    assert not complete(completer, "inspect /.env")
    assert not complete(completer, "inspect ~")
    assert not complete(TerminalCompleter(SimpleNamespace(), enabled=lambda: False), "/login")


def test_login_commands_never_retained_in_composer_history():
    history = _TerminalHistory(lambda: True)
    history.append_string("/login deepseek accidental-secret")
    history.append_string("/model openai/model")
    assert history.get_strings() == ["/model openai/model"]


def test_native_credentials_are_in_model_picker(home, monkeypatch):
    from py_agent.model_catalog import MODELS, available_models
    from litelm._providers import PROVIDERS
    for _, env in PROVIDERS.values():
        if env:
            monkeypatch.delenv(env, raising=False)
    native_auth.save("deepseek", native_auth.api_key_entry("private-key"))
    assert available_models() == tuple(sorted("deepseek/" + m for m in MODELS["deepseek"]))


@pytest.mark.asyncio
async def test_login_hidden_prompt_is_not_submitted_or_retained(home):
    coordinator, output = CoordinatorStub(), Output()
    with create_pipe_input() as pipe:
        terminal = PlainTerminal(coordinator, input=pipe, output=output, no_color=True)
        task = asyncio.create_task(terminal.run())
        try:
            pipe.send_text("/login deepseek\n")
            await until(lambda: "API key (hidden)" in output.text)
            pipe.send_text("synthetic-secret\n")
            await until(lambda: "credentials saved" in output.text)
            pipe.send_text("/auth\n")
            await until(lambda: "API key stored" in output.text)
            pipe.send_text("/quit\n")
            await asyncio.wait_for(task, 2)
            assert coordinator.submissions == []
            assert "synthetic-secret" not in output.text
            assert "synthetic-secret" not in str(terminal.session.history.get_strings())
            assert native_auth.read_document()["deepseek"]["key"] == "synthetic-secret"
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_key_entry_returns_to_composer(home):
    coordinator, output = CoordinatorStub(), Output()
    with create_pipe_input() as pipe:
        terminal = PlainTerminal(coordinator, input=pipe, output=output)
        task = asyncio.create_task(terminal.run())
        try:
            pipe.send_text("/login deepseek\n")
            await until(lambda: "API key (hidden)" in output.text)
            pipe.send_bytes(b"\x03")
            await until(lambda: "cancelled" in output.text)
            pipe.send_text("/quit\n")
            await asyncio.wait_for(task, 2)
            assert native_auth.read_document() == {}
            assert coordinator.submissions == []
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_effort_picker_dispatches_only_selected_command():
    coordinator, output = CoordinatorStub(), Output()
    with create_pipe_input() as pipe:
        terminal = PlainTerminal(coordinator, input=pipe, output=output)
        task = asyncio.create_task(terminal.run())
        try:
            pipe.send_text("/think\n")
            await until(lambda: "Reasoning effort" in output.text)
            pipe.send_text("high\n")
            await until(lambda: bool(coordinator.submissions))
            pipe.send_text("/quit\n")
            await asyncio.wait_for(task, 2)
            assert coordinator.submissions == [("terminal", "/think high")]
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


def test_think_alias_for_other_frontends():
    coordinator = cli._build_coordinator("litelm", model="deepseek/deepseek-chat")
    result = asyncio.run(coordinator._dispatch_command("think high", coordinator.config_store.snapshot))
    assert "Effort changed" in result
    assert coordinator._effort_override == "high"
