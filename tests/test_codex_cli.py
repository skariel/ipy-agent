"""Codex selection does not silently reuse credentials for API-key endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest

from py_agent.cli import _make_provider, parser
from py_agent.codex import CodexProvider
from py_agent.provider import LitelmProvider


def arguments(*values):
    return parser().parse_args(list(values))


def test_explicit_codex_selection_and_default_auth(tmp_path, monkeypatch, capsys):
    auth = Path("/home/py-test-host/auth.json")  # mocked credentials, outside writable /tmp
    monkeypatch.setattr("py_agent.codex_auth.DEFAULT_AUTH_FILE", auth)
    reads = []
    monkeypatch.setattr("py_agent.codex_auth.read_codex_credentials", lambda path: reads.append(path))
    provider = _make_provider(arguments("--model", "openai-codex/test-model"), {}, tmp_path / "workspace")
    assert isinstance(provider, CodexProvider)
    assert provider.auth_file == auth
    assert reads == [auth]
    assert "No client-side Codex output-token cap" in capsys.readouterr().out


def test_configured_codex_and_explicit_auth_file(tmp_path, monkeypatch):
    auth = Path("/home/py-test-host/credentials.json")  # mocked; never read
    reads = []
    monkeypatch.setattr("py_agent.codex_auth.read_codex_credentials", lambda path: reads.append(path))
    provider = _make_provider(
        arguments("--pi-auth", str(auth)), {"model": "openai-codex/test-model", "stream": False}, tmp_path / "workspace"
    )
    assert provider.model == "openai-codex/test-model"
    assert reads == [auth]


@pytest.mark.parametrize(
    "config,extra", [({}, ["--api-base", "https://untrusted.invalid"]), ({"api_base": "https://untrusted.invalid"}, [])]
)
def test_codex_endpoint_override_rejected_before_reading_credentials(tmp_path, monkeypatch, config, extra):
    monkeypatch.setattr(
        "py_agent.codex_auth.read_codex_credentials", lambda _: pytest.fail("must not read credentials")
    )
    with pytest.raises(ValueError, match="fixed subscription endpoint"):
        _make_provider(arguments("--model", "openai-codex/test-model", *extra), config, tmp_path)


def test_auth_inside_write_root_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "py_agent.codex_auth.read_codex_credentials", lambda _: pytest.fail("must not read credentials")
    )
    with pytest.raises(ValueError, match="outside.*writable workspace"):
        _make_provider(
            arguments("--model", "openai-codex/test-model", "--pi-auth", str(tmp_path / "auth.json")), {}, tmp_path
        )


def test_auth_under_shared_tmp_rejected_before_reading(monkeypatch):
    monkeypatch.setattr(
        "py_agent.codex_auth.read_codex_credentials", lambda _: pytest.fail("must not read credentials")
    )
    with pytest.raises(ValueError, match="outside.*workspace and /tmp"):
        _make_provider(
            arguments("--model", "openai-codex/test-model", "--pi-auth", "/tmp/py-test-auth.json"),
            {},
            Path("/home/py-test-workspace"),
        )


def test_pi_auth_not_sent_to_litelm(tmp_path):
    with pytest.raises(ValueError, match="requires an openai-codex"):
        _make_provider(arguments("--model", "openai/example", "--pi-auth", str(tmp_path / "auth.json")), {}, tmp_path)
    provider = _make_provider(arguments("--model", "openai/example"), {}, tmp_path)
    assert isinstance(provider, LitelmProvider)


def test_pi_auth_not_used_by_fake_provider(tmp_path):
    with pytest.raises(ValueError, match="cannot be combined"):
        _make_provider(
            arguments("--fake-responses", "does-not-exist.json", "--pi-auth", "also-not-read.json"), {}, tmp_path
        )
