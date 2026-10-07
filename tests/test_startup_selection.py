"""First-run model selection needs no provider call and stores no credentials."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
import pytest

from py_agent import cli
from py_agent import startup_selection as startup


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def prepare(arguments):
    return asyncio.run(startup.prepare_terminal_configuration(cli.parser().parse_args(arguments)))


def picker(monkeypatch, answer):
    calls = []

    async def choose(provider, auth_file):
        calls.append((provider, auth_file))
        return answer

    monkeypatch.setattr(startup, "choose_selection", choose)
    return calls


def test_first_run_remembers_then_reuses(home, monkeypatch):
    calls = picker(monkeypatch, ("litelm", "openai/test-model"))
    first = prepare([])
    assert first.provider == "litelm" and first.model == "openai/test-model"
    assert calls == [("", None)]
    assert json.loads(startup.selection_path().read_text()) == {
        "provider.id": "litelm", "model.name": "openai/test-model",
    }
    assert startup.selection_path().stat().st_mode & 0o777 == 0o600
    assert startup.selection_path().parent.stat().st_mode & 0o777 == 0o700
    calls.clear()
    assert prepare([]).model == "openai/test-model"
    assert calls == []


def test_fake_is_remembered_without_model(home, monkeypatch):
    calls = picker(monkeypatch, ("fake", ""))
    assert prepare([]).provider == "fake"
    assert prepare([]).provider == "fake"
    assert len(calls) == 1


def test_explicit_flags_override_saved_without_rewriting(home, monkeypatch):
    startup.save_selection("codex", "openai-codex/test")
    calls = picker(monkeypatch, ("fake", ""))
    assert prepare(["--provider", "fake"]).provider == "fake"
    assert prepare(["--model", "anthropic/test"]).provider == "litelm"
    assert startup.load_selection() == ("codex", "openai-codex/test")
    assert not calls


def test_explicit_config_wins(home, monkeypatch):
    startup.save_selection("codex", "openai-codex/test")
    config = home / "settings.json"
    config.write_text(json.dumps({"provider.id": "fake"}))
    config.chmod(0o600)
    calls = picker(monkeypatch, ("fake", ""))
    assert prepare(["--config", str(config)]).provider == "fake"
    assert not calls


def test_provider_without_model_filters_picker(home, monkeypatch):
    calls = picker(monkeypatch, ("codex", "openai-codex/test"))
    configured = prepare(["--provider", "codex"])
    assert calls == [("codex", None)]
    assert configured.model == "openai-codex/test"


def test_invalid_explicit_selection_does_not_prompt(home, monkeypatch):
    calls = picker(monkeypatch, ("fake", ""))
    with pytest.raises(ValueError, match="fake provider"):
        prepare(["--provider", "fake", "--model", "openai/test"])
    assert not calls
    assert not startup.selection_path().exists()


def test_cancel_does_not_persist(home, monkeypatch):
    async def cancel(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(startup, "choose_selection", cancel)
    with pytest.raises(KeyboardInterrupt):
        prepare([])
    assert not startup.selection_path().exists()


def test_failed_save_does_not_prevent_startup(home, monkeypatch, capsys):
    picker(monkeypatch, ("fake", ""))

    def fail(*args):
        raise OSError("read-only directory")

    monkeypatch.setattr(startup, "save_selection", fail)
    assert prepare([]).provider == "fake"
    assert "could not remember selection" in capsys.readouterr().err


@pytest.mark.parametrize("values", [
    {"provider.id": "evil-plugin", "model.name": ""},
    {"provider.id": "fake", "model.name": "", "plugins.enabled": "evil"},
    {"provider.id": "codex", "model.name": "openai/test"},
])
def test_bad_saved_preferences_never_activate_plugins(home, values):
    path = startup.selection_path()
    path.parent.mkdir(mode=0o700)
    path.write_text(json.dumps(values))
    path.chmod(0o600)
    with pytest.raises(ValueError):
        startup.load_selection()


def test_symlink_and_nonprivate_selection_rejected(home):
    target = home / "target"
    target.write_text("{}")
    path = startup.selection_path()
    path.parent.mkdir(mode=0o700)
    path.symlink_to(target)
    with pytest.raises(ValueError):
        startup.save_selection("fake", "")
    path.unlink()
    path.write_text('{"provider.id":"fake","model.name":""}')
    path.chmod(0o644)
    with pytest.raises(ValueError):
        startup.load_selection()


def test_actual_picker_rejects_bad_input_then_accepts(home, monkeypatch):
    import prompt_toolkit

    real_session = prompt_toolkit.PromptSession
    monkeypatch.setattr("py_agent.model_catalog.available_models", lambda auth: ("openai/test",))
    with create_pipe_input() as pipe:
        monkeypatch.setattr(prompt_toolkit, "PromptSession",
                            lambda **kwargs: real_session(input=pipe, output=DummyOutput(), **kwargs))
        pipe.send_text("not-a-model\nopenai/test\n")
        assert asyncio.run(startup.choose_selection("", None)) == ("litelm", "openai/test")


def test_noninteractive_entry_points_still_require_explicit_selection(home):
    with pytest.raises(startup.MissingModelSelection):
        cli._prepare_configuration(cli.parser().parse_args([]))


def test_terminal_startup_uses_picker_before_worker(home, monkeypatch):
    calls = picker(monkeypatch, ("fake", ""))

    async def run_terminal(self):
        assert self.coordinator.provider is not None

    monkeypatch.setattr(cli.PlainTerminal, "run", run_terminal)
    args = cli.parser().parse_args(["--executor", "local"])
    assert asyncio.run(cli._run_phase1(args)) == 0
    assert calls == [("", None)]


def test_main_non_tty_never_loads_or_prompts(home, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)

    def unexpected():
        pytest.fail("non-TTY must fail before remembered-default loading")

    monkeypatch.setattr(startup, "load_selection", unexpected)
    assert cli.main([]) == 2
    assert "requires TTY" in capsys.readouterr().err


def test_main_eof_is_clean_cancellation(home, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    async def eof(*args):
        raise EOFError

    monkeypatch.setattr(startup, "choose_selection", eof)
    assert cli.main([]) == 130
    assert "selection cancelled" in capsys.readouterr().err
    assert not startup.selection_path().exists()


def test_real_picker_filters_codex_models(home, monkeypatch):
    import prompt_toolkit

    real_session = prompt_toolkit.PromptSession
    monkeypatch.setattr("py_agent.model_catalog.available_models",
                        lambda auth: ("openai/test", "openai-codex/test"))
    seen = []

    with create_pipe_input() as pipe:
        def session(**kwargs):
            from prompt_toolkit.completion import CompleteEvent
            from prompt_toolkit.document import Document

            seen.extend(item.text for item in kwargs["completer"].get_completions(
                Document(""), CompleteEvent(completion_requested=True)))
            return real_session(input=pipe, output=DummyOutput(), **kwargs)

        monkeypatch.setattr(prompt_toolkit, "PromptSession", session)
        pipe.send_text("openai-codex/test\n")
        assert asyncio.run(startup.choose_selection("codex", None)) == ("codex", "openai-codex/test")
    assert seen == ["openai-codex/test"]
