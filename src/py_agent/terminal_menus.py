"""Terminal-only menus and authentication. Secrets never enter coordinator input."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
import shlex
import time
from typing import TYPE_CHECKING, Any, cast, overload
from urllib.parse import urlencode

if TYPE_CHECKING:
    from prompt_toolkit.styles import BaseStyle

    from .coordinator import Coordinator

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import FuzzyWordCompleter
from prompt_toolkit.history import DummyHistory

from .provider import ProviderError
from .terminal_completion import EFFORTS, model_choices, providers


class MenuCancelled(Exception):
    """Cancel a picker without raising KeyboardInterrupt in a child task."""


class TerminalMenus:
    _menu_selection: str | None
    _queue_work_is_active: Callable[[], bool]
    _stdin_request: object | None
    _prompt_lock: asyncio.Lock
    _history_suppressed: bool
    session: PromptSession[str]
    _style: BaseStyle | None
    _write: Callable[[str], None]
    coordinator: Coordinator

    async def _menu_prompt(self, label: str, choices: Sequence[str] = (), *, password: bool = False) -> str:
        if self._queue_work_is_active() or self._stdin_request is not None:
            raise ValueError("Wait for active work to finish before opening a menu.")
        async with self._prompt_lock:
            previous = self._history_suppressed
            self._history_suppressed = True
            try:
                # Fresh, history-less session: never reuse the composer buffer
                # or its completion/history machinery for provider secrets.
                session: PromptSession[str] = PromptSession(input=self.session.input, output=self.session.output,
                                        style=self._style, history=DummyHistory())
                return await session.prompt_async(
                    label, is_password=password, multiline=False,
                    completer=FuzzyWordCompleter(list(choices), WORD=True) if choices and not password else None,
                    complete_while_typing=bool(choices) and not password,
                    enable_history_search=False,
                    pre_run=(lambda: session.default_buffer.start_completion(select_first=False))
                            if choices and not password else None,
                )
            except (KeyboardInterrupt, EOFError):
                raise MenuCancelled() from None
            finally:
                self._history_suppressed = previous

    async def _choose(self, label: str, choices: Sequence[str], *, allow_custom: bool = False) -> str | None:
        if not choices and not allow_custom:
            self._write("No choices configured. Use /login first.")
            return None
        value = (await self._menu_prompt(label + " (type to filter, Tab/↑/↓, Enter; Ctrl-C cancels): ",
                                         choices)).strip()
        if not value:
            return None
        if not allow_custom and value not in choices:
            raise ValueError("Choose a listed value.")
        return value

    async def _login(self, arguments: str) -> None:
        from .native_auth import api_key_entry, provider_id, save
        tokens = shlex.split(arguments)
        if not tokens:
            selected = await self._choose("Provider", providers(), allow_custom=True)
            if selected is None:
                return
            tokens = [selected]
        provider = provider_id(tokens[0])
        flags = tokens[1:]
        if (len(flags) != len(set(flags))
                or any(flag not in {"--manual", "--api-key"} for flag in flags)):
            raise ValueError("Usage: /login [PROVIDER] [--api-key | --manual]. Keys must only be entered in the hidden prompt.")
        if "--manual" in flags and (provider != "openrouter" or "--api-key" in flags):
            raise ValueError("--manual is only supported for OpenRouter browser login.")
        if provider == "openai-codex":
            if flags:
                raise ValueError("Codex subscriptions require device login; no flags.")
            await self._login_codex()
            return
        if provider == "openrouter" and "--api-key" not in flags:
            await self._login_openrouter(manual="--manual" in flags)
            return
        key = await self._menu_prompt(f"{provider} API key (hidden): ", password=True)
        entry = api_key_entry(key)
        await asyncio.to_thread(save, provider, entry)
        self._write(f"{provider}: credentials saved.")

    async def _login_codex(self) -> None:
        # Async variant of the same Pi device protocol; Ctrl-C cancels polling
        # without a lingering background worker that might save credentials.
        import httpx

        from . import oauth
        from .native_auth import auth_error, save
        async with httpx.AsyncClient(timeout=30) as client:
            @overload
            async def post(url: str, *, pending: tuple[()] = (), **kwargs: Any) -> dict[str, Any]: ...
            @overload
            async def post(url: str, *, pending: tuple[int, ...], **kwargs: Any) -> dict[str, Any] | None: ...
            async def post(url: str, *, pending: tuple[int, ...] = (), **kwargs: Any) -> dict[str, Any] | None:
                try:
                    response = await client.post(url, **kwargs)
                    if response.status_code in pending:
                        return None
                    if not 200 <= response.status_code < 300:
                        raise auth_error(f"provider rejected authentication (HTTP {response.status_code})")
                    data = response.json()
                    if not isinstance(data, dict):
                        raise ValueError()
                    return data
                except (httpx.HTTPError, ValueError):
                    raise auth_error("authentication network error or invalid response") from None
            device = await post(oauth.AUTH_BASE + "/api/accounts/deviceauth/usercode",
                                json={"client_id": oauth.CLIENT_ID})
            device_id = oauth._token(device.get("device_auth_id"), "device ID")
            code = oauth._token(device.get("user_code"), "device code")
            import math
            try:
                interval = float(cast(Any, device.get("interval")))
                if not math.isfinite(interval) or not 0 <= interval <= 60:
                    raise ValueError()
            except (TypeError, ValueError):
                raise auth_error("invalid polling interval") from None
            self._write("Open https://auth.openai.com/codex/device and enter code: " + code)
            async def poll() -> dict[str, Any]:
                deadline = time.monotonic() + 900
                while time.monotonic() < deadline:
                    result = await post(oauth.AUTH_BASE + "/api/accounts/deviceauth/token",
                                        json={"device_auth_id": device_id, "user_code": code},
                                        pending=(403, 404))
                    if result is not None:
                        data = await post(oauth.TOKEN_URL, data={
                            "grant_type": "authorization_code", "client_id": oauth.CLIENT_ID,
                            "code": oauth._token(result.get("authorization_code"), "authorization code"),
                            "code_verifier": oauth._token(result.get("code_verifier"), "code verifier"),
                            "redirect_uri": oauth.AUTH_BASE + "/deviceauth/callback"})
                        return oauth.codex_entry(data)
                    await asyncio.sleep(max(1, interval))
                raise auth_error("device login timed out")
            polling = asyncio.create_task(poll())
            cancel = asyncio.create_task(self._menu_prompt("Waiting for login; Ctrl-C cancels: "))
            try:
                while True:
                    done, _ = await asyncio.wait({polling, cancel}, return_when=asyncio.FIRST_COMPLETED)
                    if cancel in done:
                        cancel.result()
                    if polling in done:
                        entry = polling.result()
                        break
                    cancel = asyncio.create_task(self._menu_prompt("Still waiting; Ctrl-C cancels: "))
            finally:
                for task in (polling, cancel):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(polling, cancel, return_exceptions=True)
        await asyncio.to_thread(save, "openai-codex", entry)
        self._write("openai-codex: credentials saved.")

    async def _login_openrouter(self, *, manual: bool = False) -> None:
        import base64
        import hashlib
        import secrets
        import webbrowser

        import httpx

        from . import oauth
        from .native_auth import api_key_entry, auth_error, save

        verifier = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        callback_path = "/oauth/callback/" + secrets.token_hex(24)
        received: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        async def callback(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=2)
                if len(line) > 8192:
                    return
                parts = line.decode("ascii").split(" ")
                from urllib.parse import urlsplit
                valid = len(parts) == 3 and parts[0] == "GET" and urlsplit(parts[1]).path == callback_path
                if not valid:
                    writer.write(b"HTTP/1.0 404 Not Found\r\n\r\n")
                else:
                    code = oauth.parse_openrouter_code("http://localhost" + parts[1])
                    if not received.done():
                        received.set_result(code)
                    writer.write(b"HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\n\r\n"
                                 b"Authorization received. Return to py.")
                await writer.drain()
            except (TimeoutError, ValueError, ProviderError, OSError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

        try:
            server = await asyncio.start_server(callback, "127.0.0.1", 0, limit=8192)
        except OSError:
            raise auth_error("cannot start loopback callback server") from None
        callback_url = f"http://localhost:{server.sockets[0].getsockname()[1]}{callback_path}"
        url = "https://openrouter.ai/auth?" + urlencode({
            "callback_url": callback_url, "code_challenge": challenge, "code_challenge_method": "S256"})
        self._write("Open this URL to sign in with OpenRouter:\n" + url)
        try:
            if not manual:
                try:
                    await asyncio.to_thread(webbrowser.open, url)
                except webbrowser.Error:
                    pass
            async with server:
                # Accept either local callback or remote redirect paste. Keep
                # redirect input hidden and outside chat/history as it is secret.
                prompt = asyncio.create_task(self._menu_prompt(
                    "Waiting for browser, or paste redirect URL/code (hidden); Ctrl-C cancels: ",
                    password=True))
                try:
                    done, _ = await asyncio.wait({received, prompt}, timeout=300,
                                                return_when=asyncio.FIRST_COMPLETED)
                    if prompt in done:
                        code = oauth.parse_openrouter_code(prompt.result())
                    elif received in done:
                        code = received.result()
                    else:
                        raise auth_error("browser login timed out")
                finally:
                    if not prompt.done():
                        prompt.cancel()
                    await asyncio.gather(prompt, return_exceptions=True)
            async with httpx.AsyncClient(timeout=30) as client:
                try:
                    response = await client.post("https://openrouter.ai/api/v1/auth/keys", json={
                        "code": code, "code_verifier": verifier, "code_challenge_method": "S256"})
                    if not 200 <= response.status_code < 300:
                        raise auth_error(f"provider rejected authentication (HTTP {response.status_code})")
                    data = response.json()
                    if not isinstance(data, dict):
                        raise ValueError()
                    entry = api_key_entry(cast(str, data.get("key")))
                except (httpx.HTTPError, ValueError):
                    raise auth_error("authentication network error or invalid response") from None
            await asyncio.to_thread(save, "openrouter", entry)
            self._write("openrouter: credentials saved.")
        finally:
            if not received.done():
                received.cancel()
            server.close()
            await server.wait_closed()

    async def _interactive_command(self, command: str, arguments: str) -> bool:
        """Return True if handled; False leaves coordinator commands queued."""
        if command not in {"login", "logout", "auth", "model", "effort", "think"}:
            return False
        # Explicit model/effort changes use the established queue path.
        if command in {"model", "effort", "think"} and arguments:
            return False
        if self._queue_work_is_active() or self._stdin_request is not None:
            self._write("Wait for active work to finish before using login or selection menus.")
            return True
        try:
            if command == "login":
                await self._login(arguments)
            elif command == "logout":
                from .native_auth import logout, provider_id, read_document
                selected = arguments or await self._choose("Logout provider", tuple(sorted(read_document())))
                if selected:
                    provider_id(selected)
                    removed = await asyncio.to_thread(logout, selected)
                    self._write(f"{selected}: " + ("removed from py." if removed else "no native login."))
                    self._write("Pi/environment fallback may still authenticate.")
            elif command == "auth":
                if arguments not in {"", "status"}:
                    raise ValueError("Usage: /auth [status]")
                from .auth_cli import status_lines
                for line in await asyncio.to_thread(status_lines):
                    self._write(line)
            else:
                choices = await asyncio.to_thread(model_choices, self.coordinator) if command == "model" else EFFORTS
                selected = await self._choose("Model" if command == "model" else "Reasoning effort",
                                              choices, allow_custom=command == "model")
                if selected:
                    # Only non-secret model/effort selections enter the normal
                    # queue, preserving lifecycle and configuration semantics.
                    self._menu_selection = f"/{command} {selected}"
        except (MenuCancelled, KeyboardInterrupt, EOFError):
            self._write("Selection/login cancelled.")
        except ProviderError as exc:
            self._write(str(exc))
        except ValueError:
            self._write("Invalid selection or command arguments. Use /help for usage.")
        except Exception:
            self._write("Selection/login failed; no provider details displayed.")
        return True
