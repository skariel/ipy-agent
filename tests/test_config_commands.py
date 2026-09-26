"""Contract tests for the Phase 3 config and plugin command service."""

from __future__ import annotations

import json

import pytest

from py_agent.configuration import ApplyAt, ConfigField, ConfigLayer, ConfigRegistry, ConfigStore
from py_agent.config_commands import ConfigCommandService
from py_agent.plugins import Contributions, DiscoveredPlugin, PluginManifest, PluginRuntime, hookimpl


def make_store() -> ConfigStore:
    registry = ConfigRegistry((
        ConfigField("model.effort", "model", str, "medium", choices=("low", "medium", "high")),
        ConfigField("output.preview_lines", "output", int, 8, minimum=1, maximum=100),
        ConfigField("worker.restart_mode", "worker", bool, False, apply_at=ApplyAt.RESTART),
        ConfigField("provider.token", "provider", str, "", sensitive=True, persistable=False),
    ))
    return ConfigStore(registry, (ConfigLayer("user", {"model.effort": "low"}),))


def test_get_describe_set_reset_and_diff_are_typed_and_redacted():
    store = make_store()
    commands = ConfigCommandService(store)

    initial = commands.execute("/config get")
    assert initial.ok
    assert "model.effort = \"low\"" in initial.text
    assert "provider.token = \"<redacted>\"" in initial.text

    description = commands.execute("/config describe worker.restart_mode")
    assert description.ok
    assert "worker.restart_mode (bool)" in description.text
    assert "applies: restart" in description.text
    assert "default: false" in description.text

    changed = commands.execute('/config set model.effort "high"')
    assert changed.ok
    assert store.snapshot.get("model.effort") == "high"
    assert "high" in commands.execute("/config diff").text

    reset = commands.execute("/config reset model.effort")
    assert reset.ok
    assert store.snapshot.get("model.effort") == "low"
    assert store.snapshot.entries["model.effort"].source == "user"

    restart = commands.execute("/config set worker.restart_mode true")
    assert restart.ok
    assert "restart pending: yes" in restart.text
    assert "restart pending" in commands.execute("/config get worker.restart_mode").text


def test_set_parses_json_data_and_failed_change_is_transactional():
    store = make_store()
    commands = ConfigCommandService(store)
    before = store.snapshot

    rejected = commands.execute('/config set output.preview_lines "__import__(\\"os\\").system(\\"false\\")"')
    assert not rejected.ok
    assert store.snapshot is before
    assert store.snapshot.get("output.preview_lines") == 8

    rejected_list = commands.execute('/config set output.preview_lines [1, 2]')
    assert not rejected_list.ok
    assert store.snapshot is before


def test_credentials_are_never_displayed_or_saved(tmp_path):
    store = make_store()
    commands = ConfigCommandService(store)
    secret = "credential-that-must-not-appear"

    changed = commands.execute(f'/config set provider.token {json.dumps(secret)}')
    assert changed.ok
    assert secret not in changed.text
    assert secret not in commands.execute("/config get provider.token").text
    assert secret not in commands.execute("/config describe provider.token").text
    assert secret not in commands.execute("/config diff").text

    target = tmp_path / "settings.json"
    refused = commands.execute(f"/config save --file {target} provider.token")
    assert not refused.ok
    assert not target.exists()
    assert secret not in refused.text


def test_save_writes_only_explicit_session_overrides_atomically(tmp_path):
    store = make_store()
    commands = ConfigCommandService(store)
    commands.set("model.effort", "high")
    commands.set("output.preview_lines", 24)
    target = tmp_path / "nested settings.json"

    response = commands.execute(
        f'/config save --file "{target}" model.effort output.preview_lines'
    )
    assert response.ok
    assert str(target) in response.text
    assert "model.effort" in response.text and "output.preview_lines" in response.text
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "model.effort": "high",
        "output.preview_lines": 24,
    }
    assert target.stat().st_mode & 0o777 == 0o600
    assert "worker.restart_mode" not in target.read_text(encoding="utf-8")
    assert commands.diff().text == "No configuration changes"


def test_save_preserves_other_selected_settings_in_existing_file(tmp_path):
    store = make_store()
    commands = ConfigCommandService(store)
    target = tmp_path / "settings.json"
    target.write_text('{"output.preview_lines":12}\n', encoding="utf-8")
    commands.set("model.effort", "high")
    assert commands.execute(f"/config save --file {target} model.effort").ok
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "model.effort": "high", "output.preview_lines": 12,
    }


def test_failed_atomic_replace_leaves_existing_file_and_override_untouched(tmp_path, monkeypatch):
    store = make_store()
    commands = ConfigCommandService(store)
    commands.set("model.effort", "high")
    target = tmp_path / "settings.json"
    original = b'{"output.preview_lines":12}\n'
    target.write_bytes(original)
    before = store.snapshot

    def fail_replace(_source, _target):
        raise OSError("private details must not be returned")

    monkeypatch.setattr("py_agent.config_commands.os.replace", fail_replace)
    response = commands.execute(f"/config save --file {target} model.effort")

    assert not response.ok
    assert "private details" not in response.text
    assert target.read_bytes() == original
    assert store.snapshot is before
    assert list(tmp_path.glob(".settings.json.*.tmp")) == []


def test_reload_validates_complete_candidate_before_commit_and_keeps_session_override(tmp_path):
    store = make_store()
    path = tmp_path / "settings.json"
    path.write_text('{"model.effort":"high","output.preview_lines":16}\n', encoding="utf-8")
    commands = ConfigCommandService(store, config_path=path)
    commands.set("model.effort", "medium")

    response = commands.execute("/config reload")
    assert response.ok
    assert store.snapshot.get("model.effort") == "medium"
    assert store.snapshot.entries["model.effort"].source == "session"
    assert store.snapshot.get("output.preview_lines") == 16

    before = store.snapshot
    path.write_text('{"model.effort":{"nested":true}}\n', encoding="utf-8")
    rejected = commands.execute("/config reload")
    assert not rejected.ok
    assert store.snapshot is before


def test_reload_rejects_sensitive_file_values_without_echoing_them(tmp_path):
    store = make_store()
    path = tmp_path / "settings.json"
    secret = "file-secret-that-must-not-leak"
    path.write_text(json.dumps({"provider.token": secret}), encoding="utf-8")
    commands = ConfigCommandService(store, config_path=path)
    before = store.snapshot

    response = commands.execute("/config reload")
    assert not response.ok
    assert secret not in response.text
    assert store.snapshot is before


def test_plugin_listing_and_inspection_separate_metadata_from_loaded_manifests():
    class SamplePlugin:
        @hookimpl
        def py_agent_register(self):
            return Contributions(PluginManifest("sample", api_min=1, api_max=1))

    runtime = PluginRuntime.load(builtins={"sample": SamplePlugin()})
    discovered = lambda: (
        DiscoveredPlugin("sample", "sample_plugin:plugin", "sample-dist"),
    )
    commands = ConfigCommandService(make_store(), runtime, discover_plugins=discovered)

    listing = commands.execute("/plugins")
    assert listing.ok
    assert "metadata only; not imported" in listing.text
    assert "sample_plugin:plugin" in listing.text
    assert "Loaded runtime manifests" in listing.text
    assert "sample: loaded, API 1..1" in listing.text

    details = commands.execute("/plugins inspect sample")
    assert details.ok
    assert "discovery: metadata only" in details.text
    assert "runtime: loaded" in details.text
    assert "API range: 1..1" in details.text


def test_discovery_does_not_load_entry_points_and_unknown_inspection_is_safe():
    def discovery():
        return (DiscoveredPlugin("external", "external_pkg:plugin", "external-dist"),)

    commands = ConfigCommandService(make_store(), discover_plugins=discovery)
    result = commands.execute("/plugins inspect external")
    assert result.ok
    assert "metadata only" in result.text
    assert "runtime: not loaded" in result.text

    missing = commands.execute("/plugins inspect secret-like-unknown-id")
    assert not missing.ok
    assert "secret-like-unknown-id" not in missing.text


def test_command_registry_is_typed_and_immutable():
    commands = ConfigCommandService(make_store())
    registry = commands.command_registry

    assert set(registry.commands) == {"config", "plugins"}
    assert registry.commands["config"].usage.startswith("/config")
    with pytest.raises(TypeError):
        registry.commands["unexpected"] = registry.commands["config"]
    with pytest.raises(AttributeError, match="immutable"):
        registry._commands = {}
