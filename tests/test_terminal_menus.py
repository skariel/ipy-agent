"""Pi-style terminal interactions; providers and secrets are synthetic."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
import pytest
from test_plain_terminal import CoordinatorStub, Output, until

from py_agent import cli, native_auth
from py_agent.coordinator import State
from py_agent.plain_terminal import PlainTerminal, _TerminalHistory
from py_agent.terminal_completion import TerminalCompleter


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
    from litelm._providers import PROVIDERS

    from py_agent.model_catalog import MODELS, available_models
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


@pytest.mark.asyncio
async def test_async_codex_device_login(home, monkeypatch):
    from test_native_auth import token
    requests = []
    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/usercode"):
            return httpx.Response(200, json={"device_auth_id": "device", "user_code": "CODE", "interval": "0"})
        if request.url.path.endswith("/deviceauth/token"):
            return httpx.Response(200, json={"authorization_code": "code", "code_verifier": "verifier"})
        return httpx.Response(200, json={"access_token": token(), "refresh_token": "private-refresh",
                                       "expires_in": 3600})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    terminal = PlainTerminal(CoordinatorStub(), output=Output())
    async def wait_prompt(*args, **kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(terminal, "_menu_prompt", wait_prompt)
    assert await terminal._handle_command("/login openai-codex")
    assert native_auth.read_document()["openai-codex"]["refresh"] == "private-refresh"
    assert "private-refresh" not in terminal.output.text
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_async_codex_cancel_polling_has_no_late_save(home, monkeypatch):
    from py_agent.terminal_menus import MenuCancelled
    requests = []
    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/usercode"):
            return httpx.Response(200, json={"device_auth_id": "device", "user_code": "CODE", "interval": "1"})
        return httpx.Response(403)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    terminal = PlainTerminal(CoordinatorStub(), output=Output())
    async def cancel_prompt(*args, **kwargs):
        raise MenuCancelled()
    monkeypatch.setattr(terminal, "_menu_prompt", cancel_prompt)
    assert await terminal._handle_command("/login openai-codex")
    count = len(requests)
    await asyncio.sleep(0.03)
    assert len(requests) == count
    assert native_auth.read_document() == {}
    assert "cancelled" in terminal.output.text


@pytest.mark.asyncio
async def test_async_openrouter_pkce_manual(home, monkeypatch):
    import base64
    import hashlib
    import json
    from urllib.parse import parse_qs, urlsplit
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"key": "private-router-key"})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    browser_urls = []
    monkeypatch.setattr("webbrowser.open", lambda url: browser_urls.append(url) or False)
    terminal = PlainTerminal(CoordinatorStub(), output=Output())
    async def paste(*args, **kwargs):
        assert kwargs["password"]
        return "http://localhost/callback?code=private-code"
    monkeypatch.setattr(terminal, "_menu_prompt", paste)
    assert await terminal._handle_command("/login openrouter --manual")
    body = json.loads(requests[0].content)
    assert body["code"] == "private-code"
    expected = base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert not browser_urls  # --manual must not launch a local browser.
    auth_url = terminal.output.text.replace("\r\n", "\n").split(
        "Open this URL to sign in with OpenRouter:\n", 1)[1].splitlines()[0]
    assert parse_qs(urlsplit(auth_url).query)["code_challenge"] == [expected]
    assert native_auth.read_document()["openrouter"]["key"] == "private-router-key"
    assert "private-router-key" not in terminal.output.text
    assert "private-code" not in terminal.output.text


@pytest.mark.asyncio
async def test_menu_rejected_during_work_and_bad_login_args_never_forwarded(home):
    terminal = PlainTerminal(CoordinatorStub(), output=Output())
    terminal.coordinator.state = State.GENERATING
    assert await terminal._handle_command("/login deepseek")
    assert "Wait for active work" in terminal.output.text
    terminal.coordinator.state = State.IDLE
    assert await terminal._handle_command("/login deepseek private-secret")
    assert "private-secret" not in terminal.output.text
    assert not terminal.coordinator.submissions


def test_config_and_plugin_arguments():
    coordinator = SimpleNamespace(
        config_store=SimpleNamespace(registry=SimpleNamespace(fields={"provider.effort": object()})),
        _plugin_runtime=SimpleNamespace(manifests={"sample-plugin": object()}),
    )
    completer = TerminalCompleter(coordinator)
    assert "get" in [v.text for v in complete(completer, "/config g")]
    assert not complete(completer, "/config show")
    assert [v.text for v in complete(completer, "/config set peff")] == ["provider.effort"]
    assert [v.text for v in complete(completer, "/plugins inspect smpl")] == ["sample-plugin"]


@pytest.mark.asyncio
async def test_tab_completion_accepts_before_submit():
    coordinator, output = CoordinatorStub(), Output()
    with create_pipe_input() as pipe:
        terminal = PlainTerminal(coordinator, input=pipe, output=output)
        task = asyncio.create_task(terminal.run())
        try:
            pipe.send_text("/stat")
            pipe.send_bytes(b"\t")
            await until(lambda: terminal.session.default_buffer.complete_state is not None)
            pipe.send_bytes(b"\r")
            await asyncio.sleep(0.04)
            assert not coordinator.submissions
            assert terminal.session.default_buffer.text == "/status"
            pipe.send_bytes(b"\r")
            await until(lambda: "Coordinator state:" in output.text)
            pipe.send_text("/quit\n")
            await asyncio.wait_for(task, 2)
            assert not coordinator.submissions
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("command,label,selection", [
    ("/login", "Provider", "deepseek"),
    ("/model", "Model", "deepseek/deepseek-flash"),
    ("/logout", "Logout provider", "deepseek"),
])
async def test_provider_model_logout_pickers(home, monkeypatch, command, label, selection):
    coordinator, output = CoordinatorStub(), Output()
    native_auth.save("deepseek", native_auth.api_key_entry("private-key"))
    terminal = PlainTerminal(coordinator, output=output)
    prompts = []
    async def select(prompt, choices=(), **kwargs):
        prompts.append((prompt, choices))
        if kwargs.get("password"):
            return "replacement-key"
        return selection
    monkeypatch.setattr(terminal, "_menu_prompt", select)
    assert await terminal._handle_command(command)
    assert label in prompts[0][0]
    assert selection in prompts[0][1]
    assert not coordinator.submissions
    if command == "/login":
        assert native_auth.read_document()["deepseek"]["key"] == "replacement-key"
    elif command == "/logout":
        assert native_auth.read_document() == {}
    else:
        assert terminal._menu_selection == "/model deepseek/deepseek-flash"
    assert "private-key" not in output.text
    assert "replacement-key" not in output.text
