"""Session-local model and reasoning-effort slash commands."""

from __future__ import annotations

import asyncio
from pathlib import Path

from py_agent import cli


def dispatch(coordinator, source: str) -> str:
    return asyncio.run(coordinator._dispatch_command(source, coordinator.config_store.snapshot))


def test_litelm_model_command_updates_every_active_model_reference():
    coordinator = cli._build_coordinator("litelm", model="deepseek/deepseek-chat")

    assert "deepseek/deepseek-chat" in dispatch(coordinator, "model")
    changed = dispatch(coordinator, "model deepseek/deepseek-flash")

    assert "deepseek/deepseek-chat -> deepseek/deepseek-flash" in changed
    assert coordinator.model == "deepseek/deepseek-flash"
    assert coordinator.provider.model == "deepseek/deepseek-flash"
    assert coordinator.provider.adapter.model == "deepseek/deepseek-flash"
    assert dispatch(coordinator, "model two models") == "Usage: /model [MODEL_ID]"


def test_effort_presets_override_the_next_litelm_request_options():
    coordinator = cli._build_coordinator("litelm", model="deepseek/deepseek-flash")

    assert "provider default" in dispatch(coordinator, "effort")
    changed = dispatch(coordinator, "effort high")

    assert "xhigh to max" in changed
    assert coordinator._model_options(coordinator.config_store.snapshot)["effort"] == "high"
    assert "Effort: high" in dispatch(coordinator, "effort")
    assert "none disables thinking" in dispatch(coordinator, "effort none")
    assert coordinator._model_options(coordinator.config_store.snapshot)["effort"] == "none"
    assert dispatch(coordinator, "effort maximum").startswith("Usage: /effort")
    assert coordinator._model_options(coordinator.config_store.snapshot)["effort"] == "none"


def test_codex_model_validation_and_configured_effort_are_preserved():
    coordinator = cli._build_coordinator("codex", model="openai-codex/one", effort="medium")

    changed = dispatch(coordinator, "model deepseek/deepseek-flash")
    assert "Model changed" in changed
    assert coordinator.model == "deepseek/deepseek-flash"
    assert coordinator.provider_id == "litelm"
    assert "Model changed" in dispatch(coordinator, "model openai-codex/one")
    assert coordinator.provider_id == "codex"
    assert coordinator._model_options(coordinator.config_store.snapshot)["effort"] == "medium"
    assert "openai-codex/one -> openai-codex/two" in dispatch(
        coordinator,
        "model openai-codex/two",
    )
    assert "Effort changed" in dispatch(coordinator, "effort xhigh")
    assert coordinator._model_options(coordinator.config_store.snapshot)["effort"] == "xhigh"


def test_litelm_accepts_an_explicit_pi_auth_file_without_reading_it_at_startup(tmp_path):
    missing = tmp_path / "not-created-yet.json"
    args = cli.parser().parse_args([
        "--provider",
        "litelm",
        "--model",
        "deepseek/deepseek-flash",
        "--pi-auth",
        str(missing),
    ])

    configured = cli._prepare_configuration(args)
    coordinator = cli._build_coordinator(
        configured.provider,
        model=configured.model,
        auth_file=Path(missing),
    )

    assert coordinator.provider.adapter.auth_file == missing
    assert not missing.exists()


def test_fake_provider_reports_live_controls_as_unavailable():
    coordinator = cli._build_coordinator("fake")

    assert "unavailable" in dispatch(coordinator, "model anything")
    assert "unavailable" in dispatch(coordinator, "effort low")


def test_adapter_is_inferred_from_model():
    for model, adapter in [
        ("deepseek/deepseek-flash", "litelm"),
        ("openai-codex/gpt-5.4", "codex"),
    ]:
        args = cli.parser().parse_args(["--model", model, "--effort", "high"])
        configured = cli._prepare_configuration(args)
        assert configured.provider == adapter


def test_model_list_filters_credentials(monkeypatch, tmp_path):
    from py_agent.model_catalog import available_models, MODELS
    from litelm._providers import PROVIDERS
    for _, env in PROVIDERS.values():
        if env:
            monkeypatch.delenv(env, raising=False)
    auth = tmp_path / "auth.json"
    assert available_models(auth) == ()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    assert available_models(auth) == tuple("deepseek/" + m for m in MODELS["deepseek"])
    coordinator = cli._build_coordinator("litelm", model="deepseek/deepseek-flash", auth_file=auth)
    listing = dispatch(coordinator, "model")
    assert "Available models" in listing
    assert "deepseek/deepseek-flash" in listing
    assert "test-only" not in listing


def test_cross_adapter_switch_preserves_effort_and_auth(tmp_path):
    coordinator = cli._build_coordinator(
        "litelm", model="deepseek/deepseek-flash", auth_file=tmp_path / "auth.json"
    )
    dispatch(coordinator, "effort high")
    assert "Model changed" in dispatch(coordinator, "model openai-codex/gpt-5.4")
    assert coordinator.provider.adapter.auth_file == tmp_path / "auth.json"
    assert coordinator._model_options(coordinator.config_store.snapshot)["effort"] == "high"
    assert "Usage:" in dispatch(coordinator, "model bad model")
    assert coordinator.model == "openai-codex/gpt-5.4"
    dispatch(coordinator, "model deepseek/deepseek-flash")
    assert coordinator.provider_id == "litelm"
    assert coordinator._model_options(coordinator.config_store.snapshot)["effort"] == "high"


def test_litelm_configured_effort(monkeypatch):
    from py_agent.configuration import ConfigLayer, ConfigStore
    store = ConfigStore(cli._core_config_registry(), (
        ConfigLayer("test", {"model.effort": "high"}),
    ))
    coordinator = cli._build_coordinator("litelm", model="deepseek/deepseek-flash", config_store=store)
    assert coordinator._model_options(store.snapshot)["effort"] == "high"
    assert "Effort: high" in dispatch(coordinator, "effort")
