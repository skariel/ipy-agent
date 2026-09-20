"""Trusted foreground CLI. No workspace configuration grants permissions."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import fields
import json
import math
import os
from pathlib import Path
import stat
import signal
import sys
import tomllib
import uuid

from .limits import Limits
from .terminal import run as run_terminal, sanitize

BUDGET_TYPES = {field.name: field.type for field in fields(Limits)}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="py", description="Persistent IPython agent in a fail-closed Linux srt sandbox.",
        epilog="API keys via litelm; openai-codex models use read-only pi subscription credentials. "
               "Non-TTY sessions are not supported yet. No code runs outside the sandbox.",
    )
    result.add_argument("--version", action="version", version="py-agent 0.1.0")
    result.add_argument("--model", help="Explicit provider/model, including openai-codex/MODEL for pi subscription auth")
    result.add_argument("--context-window-tokens", type=int,
                        help="Configured token capacity for usage percentage and 95%% context resets; defaults to input-tokens budget (not model discovery)")
    result.add_argument("--pi-auth", type=Path, help="Codex only: read-only pi auth file (default: ~/.pi/agent/auth.json)")
    result.add_argument("--api-base", help="Explicit API-key provider endpoint (not allowed for Codex subscription credentials)")
    result.add_argument("--stream", action="store_true", default=None, help="Buffer streamed completion; never execute fragments")
    result.add_argument("--config", type=Path, help="Trusted TOML configuration (default: <host-root>/config.toml)")
    result.add_argument("--host-root", type=Path, default=Path.home() / ".py", help="Private host storage, protected against worker writes")
    result.add_argument("--workspace", type=Path, default=Path.cwd(), help="Anchored writable directory (default: launch directory)")
    result.add_argument("--network", choices=("open", "proxy"), default="open",
                        help="open is requested but unsupported by current srt; proxy is explicit restricted-network consent")
    result.add_argument("--allow-domain", action="append", default=[], metavar="DOMAIN",
                        help="Proxy allowlist entry; repeat as needed. No entries means network denied.")
    result.add_argument("--check-sandbox", action="store_true", help="Run trusted isolation probe without a model/UI; works noninteractively")
    result.add_argument("--fake-responses", type=Path, help="JSON array of deterministic raw code responses; still requires sandboxing")
    result.add_argument("--no-input-history", action="store_true", help="Do not persist composer history (journal still records input)")
    result.add_argument("--vi", action="store_true", help="Vi editing (default: Emacs, including Ctrl-R history search)")
    result.add_argument("--multiline", action="store_true", help="Enter adds a newline; Esc-Enter submits. Paste never auto-submits.")
    result.add_argument("--no-color", action="store_true", help="Disable color (also honors NO_COLOR)")
    for key, type_ in BUDGET_TYPES.items():
        result.add_argument("--" + key.replace("_", "-"), type=type_, default=None,
                            help=("Wall-clock seconds per complete cell (default: 300; timeout kills kernel)"
                                  if key == "cell_seconds" else
                                  "Override provisional " + key.replace("_", " ") + " budget"))
    return result


def _no_symlinks(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f"Trusted path contains a symlink: {part}")
    return path


def _private_directory(path: Path) -> Path:
    path = _no_symlinks(path)
    missing = []
    ancestor = path
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    for part in reversed(missing):
        part.mkdir(mode=0o700)
    info = path.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise ValueError(f"Host storage must be an owned private directory (chmod 700): {path}")
    return path


def _validate_budgets(budgets: dict) -> None:
    if not isinstance(budgets, dict) or set(budgets) - BUDGET_TYPES.keys():
        raise ValueError("Configuration budgets contains unknown fields")
    for key, value in budgets.items():
        valid_type = type(value) in (int, float) if key == "cell_seconds" else type(value) is int
        if not valid_type:
            raise ValueError(f"Budget {key} has the wrong type")
        zero_allowed = key == "generation_retries"
        if not math.isfinite(value) or value < 0 or (value == 0 and not zero_allowed):
            qualifier = "nonnegative" if zero_allowed else "positive"
            raise ValueError(f"Budget {key} must be finite and {qualifier}")


def load_config(path: Path, *, required: bool = False) -> dict:
    """Read only trusted, bounded TOML; never look in the writable workspace."""
    path = _no_symlinks(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if required:
            raise ValueError(f"Configuration file does not exist: {path}") from None
        return {}
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or info.st_nlink != 1):
            raise ValueError("Configuration must be an owned regular file, not group/world writable or multiply linked")
        if info.st_size > 65536:
            raise ValueError("Configuration exceeds 65536 bytes")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(65537)
        if len(data) > 65536:
            raise ValueError("Configuration exceeds 65536 bytes")
        config = tomllib.loads(data.decode("utf-8"))
    finally:
        os.close(fd)
    unknown = set(config) - {"model", "api_base", "stream", "budgets", "context_window_tokens"}
    if unknown:
        raise ValueError(f"Unknown configuration fields: {', '.join(sorted(unknown))}; permissions are CLI-only")
    for key in ("model", "api_base"):
        if key in config and (not isinstance(config[key], str) or not config[key].strip()):
            raise ValueError(f"Configuration {key} must be nonempty text")
    if "stream" in config and type(config["stream"]) is not bool:
        raise ValueError("Configuration stream must be true or false")
    if "context_window_tokens" in config:
        value = config["context_window_tokens"]
        if type(value) is not int or value <= 0:
            raise ValueError("context_window_tokens must be a positive integer")
    _validate_budgets(config.get("budgets", {}))
    return config


def _fake_responses(path: Path) -> list[str]:
    with path.open("rb") as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Fake responses exceed 2 MiB")
    value = json.loads(raw)
    if not isinstance(value, list) or not value or len(value) > 1000 or any(not isinstance(x, str) for x in value):
        raise ValueError("Fake responses must be a nonempty JSON array of at most 1000 raw code strings")
    return value


def _print_policy(sandbox, *, verbose=False) -> None:
    policy = sandbox.policy
    if verbose:
        print("Sandbox policy: " + sanitize(policy))
    else:
        network = policy.get("network", "unknown")
        if network == "proxy":
            domains = policy.get("allowed_domains", [])
            network = f"proxy ({len(domains)} allowed domains)" if domains else "denied"
        print(f"IPython agent | workspace: {sanitize(str(sandbox.workspace))} | worker network: {sanitize(network)}")
    print("Writes allowed in workspace and shared /tmp (except protected paths); readable private data can reach the model/provider. No unsandboxed fallback.")
    if policy.get("broad_root_warning"):
        print("WARNING: broad write root (home or /). Host storage and runtime remain protected, but the writable surface is unusually large.")
    if verbose:
        print("Proxy networking has no transparent DNS, UDP, or localhost access.")
        print("Nested environments can deny namespaces, sockets or mounts needed by srt; no weaker or unsandboxed fallback is enabled.")


def _make_provider(args, config, workspace):
    from .provider import FakeProvider, LitelmProvider
    if args.fake_responses:
        if args.pi_auth:
            raise ValueError("--pi-auth cannot be combined with --fake-responses")
        return FakeProvider(_fake_responses(args.fake_responses))
    model = args.model or config.get("model")
    if not model:
        raise ValueError("Select --model provider/model or set model in trusted config; no model is assumed")
    api_base = args.api_base or config.get("api_base")
    if model.startswith("openai-codex/"):
        from .codex import CodexProvider
        from .codex_auth import DEFAULT_AUTH_FILE, read_codex_credentials
        if api_base:
            raise ValueError("Codex uses a fixed subscription endpoint; --api-base/api_base is forbidden to protect OAuth credentials")
        auth_file = _no_symlinks(args.pi_auth or DEFAULT_AUTH_FILE)
        if auth_file.is_relative_to(workspace) or auth_file.is_relative_to(Path('/tmp')):
            raise ValueError("Codex auth.json must be outside the worker's writable workspace and /tmp")
        read_codex_credentials(auth_file)  # fail before startup, without logging/storing credentials
        print("Codex subscription via read-only pi auth. Reasoning effort: medium. Refresh/login through pi if expired. "
              "Output token limit is local acceptance only; the backend has no server-side output cap.")
        return CodexProvider(model, auth_file=auth_file)
    if args.pi_auth:
        raise ValueError("--pi-auth requires an openai-codex/MODEL model")
    return LitelmProvider(model, api_base=api_base,
                          stream=args.stream if args.stream is not None else config.get("stream", False))


async def _run(args: argparse.Namespace, config: dict) -> int:
    # Lazy imports keep --help and sandbox diagnostics independent of model/UI
    # initialization; no provider request is made before sandbox startup.
    from .sandbox import Sandbox

    workspace = args.workspace.resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("Workspace must be an existing directory")
    host_root = _private_directory(args.host_root)
    if args.config is not None:
        config_path = _no_symlinks(args.config)
        if (config_path.is_relative_to(workspace) or config_path.is_relative_to(Path('/tmp'))) and not config_path.is_relative_to(host_root):
            raise ValueError("Explicit configuration inside the writable workspace or /tmp is unsafe; move it under the protected host root or outside both write roots")
    run_id = uuid.uuid4().hex
    host_session = _private_directory(host_root / "sessions" / run_id)
    workspace_session = workspace / ".py" / "sessions" / run_id
    # Protect the whole host root, not just this run: input history, config and
    # other journals must remain unwritable even with a broad workspace root.
    sandbox = Sandbox(workspace, host_root, workspace_session / "scratch",
                      network=args.network, allowed_domains=tuple(args.allow_domain))
    _print_policy(sandbox, verbose=args.check_sandbox)
    supervisor = journal = None
    try:
        if args.check_sandbox:
            await sandbox.preflight()
            print("Sandbox isolation/stdio probe passed. External networking is not thereby verified.")
            return 0

        from .history import Journal
        from .supervisor import Supervisor

        budget_values = dict(config.get("budgets", {}))
        for key in BUDGET_TYPES:
            value = getattr(args, key)
            if value is not None:
                budget_values[key] = value
        limits = Limits(**budget_values)
        context_window = args.context_window_tokens if args.context_window_tokens is not None else config.get("context_window_tokens", limits.input_tokens)
        if type(context_window) is not int or context_window <= 0:
            raise ValueError("context_window_tokens must be a positive integer")
        provider = _make_provider(args, config, workspace)
        journal = Journal(host_session / "journal.sqlite", run_id)

        def startup_event(event: dict) -> None:
            if event.get("kind") in {"say", "error", "notice", "limit"}:
                print(sanitize(event.get("content", "")))

        supervisor = Supervisor(provider, sandbox, journal, limits=limits, on_event=startup_event,
                                context_window_tokens=context_window)
        print(f"Run {run_id}; configured context capacity: {context_window} tokens. Type /help for help.")
        await supervisor.start()
        await run_terminal(supervisor,
                           history_path=None if args.no_input_history else host_root / "input-history",
                           vi=args.vi, multiline=args.multiline,
                           no_color=args.no_color or "NO_COLOR" in os.environ)
        return 0
    finally:
        try:
            if supervisor is not None:
                await supervisor.close()
        finally:
            try:
                await sandbox.close()
            finally:
                if journal is not None:
                    journal.close()


def _run_bounded(coroutine):
    """Do not let SDK tasks that ignore cancellation hang CLI process shutdown.

    Sandbox cleanup occurs in _run's finally block before this bounded task drain.
    Unlike asyncio.run(), the last drain does not wait forever on orphan SDK work.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    root = loop.create_task(coroutine)
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    installed_sigterm = False
    try:
        try:
            # A normal `kill -TERM` must use the same worker cleanup as /quit.
            def terminate():
                if not root.cancelling():
                    root.cancel()
            loop.add_signal_handler(signal.SIGTERM, terminate)
            installed_sigterm = True
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # e.g. tests embedding the CLI outside the main thread
        return loop.run_until_complete(root)
    finally:
        tasks = asyncio.all_tasks(loop)
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = loop.run_until_complete(asyncio.wait(tasks, timeout=7))
            for task in done:
                if not task.cancelled():
                    task.exception()
            if pending:
                print("py: provider cleanup timed out; usage may be unknown. Exiting without further execution.", file=sys.stderr)
        if installed_sigterm:
            loop.remove_signal_handler(signal.SIGTERM)
            signal.signal(signal.SIGTERM, previous_sigterm)
        loop.close()
        asyncio.set_event_loop(None)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.check_sandbox and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        print("py: interactive mode requires TTY stdin and stdout. Batch/JSON mode is not implemented; use a terminal or --check-sandbox.", file=sys.stderr)
        return 2
    try:
        if args.allow_domain and args.network != "proxy":
            raise ValueError("--allow-domain requires explicit --network proxy")
        if args.fake_responses and (args.model or args.api_base or args.stream):
            raise ValueError("--fake-responses cannot be combined with live provider flags")
        config_path = args.config if args.config is not None else args.host_root / "config.toml"
        config = load_config(config_path, required=args.config is not None)
        if not args.check_sandbox and not args.fake_responses and not (args.model or config.get("model")):
            raise ValueError("Select --model provider/model or set model in trusted config; no model is assumed")
        return _run_bounded(_run(args, config))
    except asyncio.CancelledError:
        print("py: terminated; active work cancelled without replay.", file=sys.stderr)
        return 143
    except KeyboardInterrupt:
        print("py: cancelled; active work was not replayed.", file=sys.stderr)
        return 130
    except Exception as exc:
        print("py: " + sanitize(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
