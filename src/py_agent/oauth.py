"""OpenRouter PKCE and Codex device login, adapted from MIT-licensed pi-ai.

See THIRD_PARTY_NOTICES.md. No provider response bodies enter error messages.
"""
from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

import httpx

from .native_auth import api_key_entry, auth_error

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_BASE = "https://auth.openai.com"
TOKEN_URL = AUTH_BASE + "/oauth/token"


def request(client, url, *, pending=(), **kwargs):
    try:
        response = client.post(url, **kwargs)
        if response.status_code in pending:
            return None
        if not 200 <= response.status_code < 300:
            raise auth_error(f"provider rejected authentication (HTTP {response.status_code}); retry login")
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (httpx.HTTPError, ValueError):
        raise auth_error("authentication network error or invalid response; retry login") from None


def _token(value, name):
    if (not isinstance(value, str) or not 1 <= len(value) <= 32768
            or any(ord(c) < 33 or ord(c) > 126 for c in value)):
        raise auth_error(f"provider returned an invalid {name}")
    return value


def codex_entry(data, previous=None):
    access = _token(data.get("access_token"), "access token")
    refresh = _token(data.get("refresh_token") or (previous or {}).get("refresh"), "refresh token")
    expires_in = data.get("expires_in")
    if (type(expires_in) not in (int, float) or not math.isfinite(expires_in)
            or not 30 < expires_in <= 365 * 86400):
        raise auth_error("provider returned invalid token expiry")
    try:
        encoded = access.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        account = claims["https://api.openai.com/auth"]["chatgpt_account_id"]
        import re
        if not isinstance(account, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", account):
            raise ValueError()
    except (IndexError, KeyError, ValueError, TypeError):
        raise auth_error("provider token has no valid account ID") from None
    return {"type": "oauth", "access": access, "refresh": refresh,
            "expires": time.time() * 1000 + expires_in * 1000, "accountId": account}


def refresh_codex(entry):
    refresh = _token(entry.get("refresh"), "refresh token")
    with httpx.Client(timeout=30) as client:
        data = request(client, TOKEN_URL, data={
            "grant_type": "refresh_token", "refresh_token": refresh, "client_id": CLIENT_ID})
    return codex_entry(data, entry)


def login_codex(notify=print):
    with httpx.Client(timeout=30) as client:
        device = request(client, AUTH_BASE + "/api/accounts/deviceauth/usercode",
                         json={"client_id": CLIENT_ID})
        device_id = _token(device.get("device_auth_id"), "device ID")
        user_code = _token(device.get("user_code"), "device code")
        try:
            interval = float(device.get("interval"))
            if not math.isfinite(interval) or not 0 <= interval <= 60:
                raise ValueError()
        except (TypeError, ValueError):
            raise auth_error("provider returned an invalid polling interval") from None
        notify("Open https://auth.openai.com/codex/device and enter code: " + user_code)
        deadline = time.monotonic() + 15 * 60
        while time.monotonic() < deadline:
            result = request(client, AUTH_BASE + "/api/accounts/deviceauth/token",
                             json={"device_auth_id": device_id, "user_code": user_code},
                             pending=(403, 404))
            if result is not None:
                data = request(client, TOKEN_URL, data={
                    "grant_type": "authorization_code", "client_id": CLIENT_ID,
                    "code": _token(result.get("authorization_code"), "authorization code"),
                    "code_verifier": _token(result.get("code_verifier"), "code verifier"),
                    "redirect_uri": AUTH_BASE + "/deviceauth/callback"})
                return codex_entry(data)
            time.sleep(max(1, interval))
    raise auth_error("device login timed out; retry py login openai-codex")


def parse_openrouter_code(value):
    value = value.strip()
    if "://" in value:
        try:
            values = parse_qs(urlsplit(value).query).get("code", [])
        except ValueError:
            raise auth_error("invalid redirect URL") from None
        if len(values) != 1:
            raise auth_error("redirect URL has no unique authorization code")
        value = values[0]
    return _token(value, "authorization code")


def login_openrouter(*, manual=False, notify=print, prompt=input):
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    callback_path = "/oauth/callback/" + secrets.token_hex(24)
    received = []
    ready = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Callback URLs contain secrets.

        def do_GET(self):
            if urlsplit(self.path).path != callback_path:
                self.send_error(404)
                return
            try:
                code = parse_openrouter_code("http://localhost" + self.path)
            except Exception:
                self.send_error(400)
                return
            if not ready.is_set():
                received.append(code)
                ready.set()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Authorization received. Return to py.")

    class CallbackServer(HTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(2)
            return connection, address

    try:
        server = CallbackServer(("127.0.0.1", 0), Handler)
    except OSError:
        raise auth_error("cannot start loopback callback server") from None
    server.timeout = 1
    callback = f"http://localhost:{server.server_port}{callback_path}"
    url = "https://openrouter.ai/auth?" + urlencode({
        "callback_url": callback, "code_challenge": challenge, "code_challenge_method": "S256"})
    notify("Open this URL to sign in with OpenRouter:\n" + url)
    try:
        if manual:
            code = parse_openrouter_code(prompt("Paste the final redirect URL or authorization code: "))
        else:
            try:
                webbrowser.open(url)
            except webbrowser.Error:
                pass
            notify("Waiting for browser callback; for remote login use --manual.")
            deadline = time.monotonic() + 300
            while not ready.is_set() and time.monotonic() < deadline:
                server.handle_request()
            if not received:
                raise auth_error("browser login timed out; retry with --manual")
            code = received[0]
        with httpx.Client(timeout=30) as client:
            data = request(client, "https://openrouter.ai/api/v1/auth/keys",
                           json={"code": code, "code_verifier": verifier,
                                 "code_challenge_method": "S256"})
        return api_key_entry(data.get("key"))
    finally:
        server.server_close()
