"""Synthetic credentials only; no real auth reads, refreshes or network calls."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from py_agent.codex_auth import (
    MAX_AUTH_BYTES,
    read_codex_credentials,
    read_provider_api_key,
)
from py_agent.provider import ProviderError


@pytest.fixture()
def auth(tmp_path, monkeypatch):
    monkeypatch.setattr("py_agent.codex_auth.time.time", lambda: 1000)
    path = tmp_path / "auth.json"
    document = {
        "openai-codex": {
            "type": "oauth",
            "access": "synthetic-private-access",
            "accountId": "account-123",
            "expires": 2_000_000,
            "refresh": "synthetic-private-refresh",
        },
        "unrelated": {"key": "!must-not-be-executed"},
    }
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    return path, document


def test_read_only_and_secrets_not_in_repr(auth):
    path, _ = auth
    before = path.read_bytes(), path.stat().st_mtime_ns
    credential = read_codex_credentials(path)
    assert credential.access == "synthetic-private-access"
    assert credential.account_id == "account-123"
    assert credential.expires == 2_000_000
    assert "synthetic-private" not in repr(credential)
    assert "account-123" not in repr(credential)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_reads_static_keys_for_any_provider_without_leaking_them(auth):
    path, document = auth
    document.update({
        "deepseek": {"type": "api_key", "key": "synthetic-deepseek-key"},
        "openrouter": {"type": "api_key", "key": "synthetic-openrouter-key"},
    })
    path.write_text(json.dumps(document))

    deepseek = read_provider_api_key("deepseek", path)
    openrouter = read_provider_api_key("openrouter", path)
    assert deepseek is not None and deepseek.key == "synthetic-deepseek-key"
    assert openrouter is not None and openrouter.key == "synthetic-openrouter-key"
    assert "synthetic" not in repr(deepseek)
    assert read_provider_api_key("missing", path) is None


def test_static_key_entries_fail_closed_and_never_execute_commands(auth, monkeypatch):
    path, document = auth
    monkeypatch.setattr("subprocess.run", lambda *_args, **_kwargs: pytest.fail("must not execute"))
    for entry in (
        {"type": "oauth", "key": "synthetic-secret"},
        {"type": "api_key", "key": ""},
        {"type": "api_key", "key": "!printf synthetic-secret"},
        {"type": "api_key", "key": "synthetic-secret\nheader"},
    ):
        document["deepseek"] = entry
        path.write_text(json.dumps(document))
        with pytest.raises(ProviderError) as error:
            read_provider_api_key("deepseek", path)
        assert error.value.kind == "authentication"
        assert "synthetic-secret" not in str(error.value)


def test_rereads_after_pi_refresh(auth):
    path, document = auth
    assert read_codex_credentials(path).access == "synthetic-private-access"
    document["openai-codex"]["access"] = "synthetic-new-access"
    replacement = path.with_suffix(".new")
    replacement.write_text(json.dumps(document))
    replacement.chmod(0o600)
    Path(replacement).replace(path)
    assert read_codex_credentials(path).access == "synthetic-new-access"


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", "api_key"),
        ("access", ""),
        ("access", 42),
        ("access", "bad\ntoken"),
        ("access", "x" * 32769),
        ("accountId", None),
        ("accountId", "bad\rheader"),
        ("expires", None),
        ("expires", True),
        ("expires", "future"),
        ("expires", 1000000),
        ("expires", 1029999),
    ],
)
def test_invalid_credentials_do_not_leak(auth, field, value):
    path, document = auth
    document["openai-codex"][field] = value
    path.write_text(json.dumps(document))
    with pytest.raises(ProviderError) as error:
        read_codex_credentials(path)
    assert error.value.kind == "authentication"
    assert "synthetic-private" not in str(error.value)
    assert "account-123" not in str(error.value)


@pytest.mark.parametrize(
    "contents", ["{broken synthetic-private-access", "[]", "{}", '{"a":1,"a":2}', '{"expires":NaN}']
)
def test_bad_json_or_missing_login(auth, contents):
    path, _ = auth
    path.write_text(contents)
    with pytest.raises(ProviderError):
        read_codex_credentials(path)


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o666])
def test_nonprivate_permissions_rejected(auth, mode):
    path, _ = auth
    path.chmod(mode)
    with pytest.raises(ProviderError, match="mode 0600"):
        read_codex_credentials(path)


def test_missing_and_oversized_files(auth):
    path, _ = auth
    path.unlink()
    with pytest.raises(ProviderError, match="/login openai-codex"):
        read_codex_credentials(path)
    path.write_bytes(b" " * (MAX_AUTH_BYTES + 1))
    path.chmod(0o600)
    with pytest.raises(ProviderError, match="1 MiB"):
        read_codex_credentials(path)


def test_symlink_hardlink_fifo_and_directory_rejected(auth):
    path, _ = auth
    alias = path.with_name("alias")
    alias.symlink_to(path)
    with pytest.raises(ProviderError):
        read_codex_credentials(alias)
    alias.unlink()
    os.link(path, alias)
    with pytest.raises(ProviderError):
        read_codex_credentials(alias)
    alias.unlink()
    os.mkfifo(alias, 0o600)
    with pytest.raises(ProviderError):
        read_codex_credentials(alias)
    alias.unlink()
    alias.mkdir()
    with pytest.raises(ProviderError):
        read_codex_credentials(alias)


def test_parent_symlink_rejected(auth):
    path, _ = auth
    alias = path.parent / "parent-link"
    alias.symlink_to(path.parent, target_is_directory=True)
    with pytest.raises(ProviderError):
        read_codex_credentials(alias / path.name)


def test_in_place_read_race_rejected(auth, monkeypatch):
    path, _ = auth
    original = os.fstat
    calls = 0

    def raced(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_text("{}")
        return original(fd)

    monkeypatch.setattr("py_agent.codex_auth.os.fstat", raced)
    with pytest.raises(ProviderError, match="changed during reading"):
        read_codex_credentials(path)
