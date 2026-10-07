"""Native auth uses synthetic credentials and mocked provider endpoints only."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from py_agent import cli, native_auth, oauth
from py_agent.codex_auth import read_codex_credentials, read_provider_api_key
from py_agent.provider import ProviderError


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("py_agent.codex_auth.DEFAULT_AUTH_FILE", tmp_path / "pi.json")
    return tmp_path


def token():
    payload = base64.urlsafe_b64encode(json.dumps({
        "https://api.openai.com/auth": {"chatgpt_account_id": "account-123"}
    }).encode()).decode().rstrip("=")
    return "header." + payload + ".signature"


def entry(expired=False):
    return {"type": "oauth", "access": token(), "refresh": "synthetic-refresh",
            "accountId": "account-123", "expires": 1 if expired else 9999999999999}


def mock_client(monkeypatch, handler):
    real_client = httpx.Client
    monkeypatch.setattr(oauth.httpx, "Client",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))


def test_private_atomic_store_and_logout(home):
    native_auth.save("deepseek", native_auth.api_key_entry("synthetic-secret"))
    path = native_auth.auth_path()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert read_provider_api_key("deepseek").key == "synthetic-secret"
    assert "synthetic-secret" not in repr(read_provider_api_key("deepseek"))
    assert native_auth.logout("deepseek")
    assert not native_auth.logout("deepseek")
    assert not list(path.parent.glob(".auth-*"))


def test_concurrent_writes_preserve_providers(home):
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: native_auth.save(f"provider-{i}", native_auth.api_key_entry("key")), range(16)))
    assert len(native_auth.read_document()) == 16


def test_native_precedence_and_pi_read_only_fallback(home):
    pi = home / "pi.json"
    pi.write_text(json.dumps({"deepseek": {"type": "api_key", "key": "pi-key"},
                             "openai-codex": entry()}))
    pi.chmod(0o600)
    before = pi.read_bytes(), pi.stat().st_mtime_ns
    assert read_provider_api_key("deepseek").key == "pi-key"
    assert read_codex_credentials().account_id == "account-123"
    native_auth.save("deepseek", native_auth.api_key_entry("native-key"))
    assert read_provider_api_key("deepseek").key == "native-key"
    assert read_provider_api_key("deepseek", pi).key == "pi-key"
    assert (pi.read_bytes(), pi.stat().st_mtime_ns) == before


def test_bad_native_entry_fails_closed(home):
    native_auth.save("deepseek", {"type": "api_key", "key": "!shell"})
    with pytest.raises(ProviderError):
        read_provider_api_key("deepseek")


def test_store_rejects_symlinks_and_permissions(home):
    elsewhere = home / "elsewhere"
    elsewhere.mkdir()
    (home / ".py").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ProviderError):
        native_auth.save("deepseek", {"type": "api_key", "key": "key"})
    (home / ".py").unlink()
    (home / ".py").mkdir(mode=0o755)
    with pytest.raises(ProviderError):
        native_auth.save("deepseek", {"type": "api_key", "key": "key"})
    (home / ".py").chmod(0o700)
    (home / ".py" / "auth.lock").symlink_to(home / "elsewhere" / "lock")
    with pytest.raises(ProviderError):
        native_auth.save("deepseek", {"type": "api_key", "key": "key"})


def test_refresh_is_saved_and_serialized(home, monkeypatch):
    native_auth.save("openai-codex", entry(expired=True))
    calls = []
    def handler(request):
        calls.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": token(),
                                       "refresh_token": "rotated-refresh", "expires_in": 3600})
    mock_client(monkeypatch, handler)
    with ThreadPoolExecutor(max_workers=3) as pool:
        credentials = list(pool.map(lambda _: read_codex_credentials(), range(3)))
    assert all(c.account_id == "account-123" for c in credentials)
    assert len(calls) == 1
    assert calls[0]["grant_type"] == ["refresh_token"]
    assert native_auth.read_document()["openai-codex"]["refresh"] == "rotated-refresh"


def test_refresh_failure_does_not_write_or_leak(home, monkeypatch):
    native_auth.save("openai-codex", entry(expired=True))
    before = native_auth.auth_path().read_bytes()
    mock_client(monkeypatch, lambda request: httpx.Response(401, text="secret-response"))
    with pytest.raises(ProviderError) as caught:
        read_codex_credentials()
    assert "secret-response" not in str(caught.value)
    assert native_auth.auth_path().read_bytes() == before


def test_device_login_protocol(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/usercode"):
            return httpx.Response(200, json={"device_auth_id": "device-id", "user_code": "ABCD", "interval": "0"})
        if request.url.path.endswith("/deviceauth/token"):
            return httpx.Response(200, json={"authorization_code": "auth-code", "code_verifier": "verifier"})
        return httpx.Response(200, json={"access_token": token(), "refresh_token": "refresh", "expires_in": 3600})
    mock_client(monkeypatch, handler)
    notices = []
    result = oauth.login_codex(notify=notices.append)
    assert result["accountId"] == "account-123"
    assert "ABCD" in notices[0]
    params = parse_qs(requests[-1].content.decode())
    assert params["redirect_uri"] == ["https://auth.openai.com/deviceauth/callback"]
    assert params["code_verifier"] == ["verifier"]


def test_openrouter_pkce_manual(monkeypatch):
    requests = []
    mock_client(monkeypatch, lambda request: requests.append(request) or httpx.Response(200, json={"key": "router-key"}))
    notices = []
    result = oauth.login_openrouter(manual=True, notify=notices.append,
                                    prompt=lambda _: "http://localhost/callback?code=authorization-code")
    assert result == {"type": "api_key", "key": "router-key"}
    body = json.loads(requests[0].content)
    assert body["code"] == "authorization-code"
    import hashlib
    expected = base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    url = notices[0].split("\n")[-1]
    assert parse_qs(urlsplit(url).query)["code_challenge"] == [expected]


def test_commands_no_secrets(home, monkeypatch, capsys):
    monkeypatch.setattr("py_agent.auth_cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("py_agent.auth_cli.getpass.getpass", lambda _: "private-key")
    assert cli.main(["login", "deepseek"]) == 0
    assert cli.main(["auth", "status"]) == 0
    assert "private-key" not in capsys.readouterr().out
    assert cli.main(["logout", "deepseek"]) == 0
    assert native_auth.read_document() == {}


@pytest.mark.parametrize("value", ["bad/provider", "provider\nsecret", "!command", ""])
def test_invalid_provider(value):
    with pytest.raises(ProviderError):
        native_auth.provider_id(value)


def test_openrouter_browser_callback(monkeypatch):
    notices = []
    callback_threads = []
    callback_status = []
    mock_client(monkeypatch, lambda request: httpx.Response(200, json={"key": "browser-key"}))
    # Use stdlib for the callback; httpx is mocked for the token exchange.
    def browser(url):
        import threading
        from urllib.request import urlopen
        callback = parse_qs(urlsplit(url).query)["callback_url"][0]
        def send():
            with urlopen(callback + "?code=browser-code", timeout=5) as response:
                callback_status.append(response.status)
        thread = threading.Thread(target=send)
        thread.start()
        callback_threads.append(thread)
        return True
    monkeypatch.setattr(oauth.webbrowser, "open", browser)
    result = oauth.login_openrouter(notify=notices.append)
    for thread in callback_threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert callback_status == [200]
    assert result["key"] == "browser-key"
    assert "browser-code" not in str(notices)


def test_login_cancelled_does_not_save(home, monkeypatch, capsys):
    def cancelled():
        raise KeyboardInterrupt()
    monkeypatch.setattr(oauth, "login_codex", cancelled)
    assert cli.main(["login", "openai-codex"]) == 130
    assert native_auth.read_document() == {}
    assert "cancelled" in capsys.readouterr().err


def test_explicit_pi_expiry_never_refreshes(home, monkeypatch):
    path = home / "pi.json"
    path.write_text(json.dumps({"openai-codex": entry(expired=True)}))
    path.chmod(0o600)
    before = path.read_bytes()
    monkeypatch.setattr(oauth, "refresh_codex", lambda _: pytest.fail("must not refresh Pi"))
    with pytest.raises(ProviderError, match="expired"):
        read_codex_credentials(path)
    assert path.read_bytes() == before


def test_store_rejects_auth_file_symlink(home):
    target = home / "target.json"
    target.write_text("{}")
    target.chmod(0o600)
    (home / ".py").mkdir(mode=0o700)
    (home / ".py" / "auth.json").symlink_to(target)
    with pytest.raises(ProviderError):
        native_auth.save("deepseek", native_auth.api_key_entry("key"))
    assert target.read_text() == "{}"


def test_cli_auth_alias():
    args = cli.parser().parse_args(["--auth", "/tmp/explicit.json", "--model", "openai/model"])
    assert args.pi_auth == Path("/tmp/explicit.json")


def test_codex_presence_check_is_inside_store_lock(home, monkeypatch):
    native_auth.save("openai-codex", entry())
    from contextlib import contextmanager

    original_lock = native_auth.locked_store
    original_read = native_auth.read_document
    held = False

    @contextmanager
    def tracked_lock():
        nonlocal held
        with original_lock() as directory:
            held = True
            try:
                yield directory
            finally:
                held = False

    def checked_read():
        assert held, "credential presence must not race an atomic refresh"
        return original_read()

    monkeypatch.setattr(native_auth, "locked_store", tracked_lock)
    monkeypatch.setattr(native_auth, "read_document", checked_read)
    assert native_auth.codex_credentials().account_id == "account-123"
