"""Credential management commands, independent of the interactive agent."""
from __future__ import annotations

import argparse
from collections.abc import Sequence
import getpass
import sys
from typing import Any, cast

from .native_auth import api_key_entry, auth_error, auth_path, logout, provider_id, read_document, save


def status_lines() -> list[str]:
    document = read_document()
    lines = [f"py credential store: {auth_path()}"]
    if not document:
        lines.append("No native credentials configured. Pi/env fallback remains available.")
    for provider in sorted(document):
        provider_id(provider)
        entry = document[provider]
        if not isinstance(entry, dict):
            raise auth_error("invalid credential entry")
        if entry.get("type") == "api_key":
            # api_key_entry performs runtime validation of the JSON value.
            api_key_entry(cast(str, entry.get("key")))
            status = "API key stored"
        elif entry.get("type") == "oauth":
            import math
            import time
            expiry = entry.get("expires")
            if type(expiry) is not int and type(expiry) is not float:
                raise auth_error("invalid OAuth expiry")
            if not math.isfinite(expiry):
                raise auth_error("invalid OAuth expiry")
            status = "OAuth stored" if expiry > time.time() * 1000 + 30000 else "OAuth expired (refresh on use)"
        else:
            raise auth_error("unknown credential type")
        lines.append(f"{provider}: {status}")
    return lines


def main(arguments: Sequence[str] | None) -> int:
    parser = argparse.ArgumentParser(prog="py", description="Manage py provider credentials")
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="Log in or securely enter an API key")
    login.add_argument("provider")
    login.add_argument("--api-key", action="store_true", help="Prompt for an API key rather than OAuth")
    login.add_argument("--manual", action="store_true", help="Paste an OpenRouter redirect URL (remote login)")
    remove = commands.add_parser("logout", help="Remove credentials from py's store only")
    remove.add_argument("provider")
    auth = commands.add_parser("auth", help="Inspect credential status without showing secrets")
    auth.add_subparsers(dest="operation", required=True).add_parser("status")
    args = parser.parse_args(arguments)
    try:
        if args.command == "auth":
            print("\n".join(status_lines()))
            return 0
        provider = provider_id(args.provider)
        if args.command == "logout":
            removed = logout(provider)
            print(f"{provider}: " + ("removed from py" if removed else "no py credential to remove"))
            return 0
        if args.manual and (provider != "openrouter" or args.api_key):
            raise auth_error("--manual is only supported for OpenRouter browser login")
        if provider == "openai-codex" and args.api_key:
            raise auth_error("Codex subscriptions require OAuth; use provider openai for an API key")
        entry: dict[str, Any]
        if provider == "openai-codex":
            from .oauth import login_codex
            entry = login_codex()
        elif provider == "openrouter" and not args.api_key:
            from .oauth import login_openrouter
            entry = login_openrouter(manual=args.manual)
        else:
            if not sys.stdin.isatty():
                raise auth_error("API-key entry requires a terminal; use provider environment variables for automation")
            entry = api_key_entry(getpass.getpass(f"{provider} API key: "))
        save(provider, entry)
        print(f"{provider}: credentials saved to {auth_path()}")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("py: login cancelled.", file=sys.stderr)
        return 130
    except Exception:
        # Only our deliberately secret-free exceptions may be printed.
        from .provider import ProviderError
        exc = sys.exception()
        message = str(exc) if isinstance(exc, ProviderError) else "credential operation failed"
        print("py: " + message, file=sys.stderr)
        return 1
