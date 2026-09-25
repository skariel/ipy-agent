"""Coordinator CLI; the explicit legacy mode remains sandboxed."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import signal
import stat
import subprocess
import sys
import tomllib
import uuid
from dataclasses import dataclass, fields
from inspect import Parameter, signature
from pathlib import Path
from types import MappingProxyType

from .limits import Limits
from .plain_terminal import PlainTerminal, sanitize

BUDGET_TYPES = {field.name: int for field in fields(Limits)}
CODEX_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")


def _legacy_parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="py",
        description="Persistent IPython agent in a fail-closed Linux srt sandbox.",
        epilog="API keys via litelm; openai-codex models use read-only pi subscription credentials. "
        "Non-TTY sessions are not supported yet. No code runs outside the sandbox.",
    )
    result.add_argument("--version", action="version", version="py-agent 0.1.0")
    result.add_argument(
        "--model", help="Explicit provider/model, including openai-codex/MODEL for pi subscription auth"
    )
    result.add_argument(
        "--context-window-tokens",
        type=int,
        help="Configured token capacity for usage percentage and 95%% context resets; defaults to input-tokens budget (not model discovery)",
    )
    result.add_argument(
        "--pi-auth", type=Path, help="Codex only: read-only pi auth file (default: ~/.pi/agent/auth.json)"
    )
    result.add_argument(
        "--effort",
        choices=CODEX_EFFORTS,
        help="Codex reasoning effort (default: medium)",
    )
    result.add_argument(
        "--api-base", help="Explicit API-key provider endpoint (not allowed for Codex subscription credentials)"
    )
    result.add_argument(
        "--stream", action="store_true", default=None, help="Buffer streamed completion; never execute fragments"
    )
    result.add_argument("--config", type=Path, help="Trusted TOML configuration (default: <host-root>/config.toml)")
    result.add_argument(
        "--host-root",
        type=Path,
        default=Path.home() / ".py",
        help="Private host storage, protected against worker writes",
    )
    result.add_argument(
        "--workspace", type=Path, default=Path.cwd(), help="Anchored writable directory (default: launch directory)"
    )
    result.add_argument(
        "--network",
        choices=("open", "proxy"),
        default="open",
        help="open is requested but unsupported by current srt; proxy is explicit restricted-network consent",
    )
    result.add_argument(
        "--allow-domain",
        action="append",
        default=[],
        metavar="DOMAIN",
        help="Pre-approved proxy destination; repeat as needed. Other destinations prompt the user.",
    )
    result.add_argument(
        "--check-sandbox",
        action="store_true",
        help="Run trusted isolation probe without a model/UI; works noninteractively",
    )
    result.add_argument(
        "--fake-responses", type=Path, help="JSON array of deterministic raw code responses; still requires sandboxing"
    )
    result.add_argument(
        "--no-input-history", action="store_true", help="Do not persist composer history (journal still records input)"
    )
    result.add_argument(
        "--vi", action="store_true", help="Vi editing (default: Emacs, including Ctrl-R history search)"
    )
    result.add_argument(
        "--multiline", action="store_true", help="Enter adds a newline; Esc-Enter submits. Paste never auto-submits."
    )
    result.add_argument("--no-color", action="store_true", help="Disable color (also honors NO_COLOR)")
    for key, type_ in BUDGET_TYPES.items():
        result.add_argument(
            "--" + key.replace("_", "-"),
            type=type_,
            default=None,
            help=(
                "Optional API-key response token limit; omitted by default and ignored by Codex"
                if key == "output_tokens"
                else "Configure " + key.replace("_", " ")
            ),
        )
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
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"Host storage must be an owned private directory (chmod 700): {path}")
    return path


def _validate_budgets(budgets: dict) -> None:
    if not isinstance(budgets, dict) or set(budgets) - BUDGET_TYPES.keys():
        raise ValueError("Configuration budgets contains unknown fields")
    for key, value in budgets.items():
        valid_type = type(value) is int
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
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022 or info.st_nlink != 1:
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
    unknown = set(config) - {"model", "api_base", "stream", "effort", "budgets", "context_window_tokens"}
    if unknown:
        raise ValueError(f"Unknown configuration fields: {', '.join(sorted(unknown))}; permissions are CLI-only")
    for key in ("model", "api_base"):
        if key in config and (not isinstance(config[key], str) or not config[key].strip()):
            raise ValueError(f"Configuration {key} must be nonempty text")
    if "stream" in config and type(config["stream"]) is not bool:
        raise ValueError("Configuration stream must be true or false")
    if "effort" in config and config["effort"] not in CODEX_EFFORTS:
        raise ValueError("Configuration effort must be one of: " + ", ".join(CODEX_EFFORTS))
    if "context_window_tokens" in config:
        value = config["context_window_tokens"]
        if type(value) is not int or value <= 0:
            raise ValueError("context_window_tokens must be a positive integer")
    _validate_budgets(config.get("budgets", {}))
    return config


def _fake_responses(path: Path) -> list[str]:
    with path.open("rb") as stream:
        value = json.load(stream)
    if not isinstance(value, list) or not value or any(not isinstance(x, str) for x in value):
        raise ValueError("Fake responses must be a nonempty JSON array of raw code strings")
    return value


def _print_policy(sandbox, *, verbose=False) -> None:
    policy = sandbox.policy
    if verbose:
        print("Sandbox policy: " + sanitize(policy))
    else:
        network = policy.get("network", "unknown")
        if network == "proxy":
            domains = policy.get("allowed_domains", [])
            network = f"proxy ({len(domains)} pre-approved; prompts for others)"
        print(f"IPython agent | workspace: {sanitize(str(sandbox.workspace))} | worker network: {sanitize(network)}")
    print(
        "Writes allowed in workspace and shared /tmp (except protected paths); readable private data can reach the model/provider. No unsandboxed fallback."
    )
    if policy.get("broad_root_warning"):
        print(
            "WARNING: broad write root (home or /). Host storage and runtime remain protected, but the writable surface is unusually large."
        )
    if verbose:
        print("Proxy networking has no transparent DNS, UDP, or localhost access.")
        print(
            "Nested environments can deny namespaces, sockets or mounts needed by srt; no weaker or unsandboxed fallback is enabled."
        )


def _make_provider(args, config, workspace, *, session_id=None):
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
            raise ValueError(
                "Codex uses a fixed subscription endpoint; --api-base/api_base is forbidden to protect OAuth credentials"
            )
        auth_file = _no_symlinks(args.pi_auth or DEFAULT_AUTH_FILE)
        if auth_file.is_relative_to(workspace) or auth_file.is_relative_to(Path("/tmp")):
            raise ValueError("Codex auth.json must be outside the worker's writable workspace and /tmp")
        read_codex_credentials(auth_file)  # fail before startup, without logging/storing credentials
        effort = args.effort or config.get("effort", "medium")
        print(
            f"Codex subscription via read-only pi auth. Reasoning effort: {effort}. "
            "Refresh/login through pi if expired. No client-side Codex output-token cap."
        )
        return CodexProvider(model, auth_file=auth_file, session_id=session_id, effort=effort)
    if args.pi_auth:
        raise ValueError("--pi-auth requires an openai-codex/MODEL model")
    if args.effort is not None or "effort" in config:
        raise ValueError("effort is only supported for openai-codex/MODEL")
    return LitelmProvider(
        model, api_base=api_base, stream=args.stream if args.stream is not None else config.get("stream", False)
    )


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
        if (
            config_path.is_relative_to(workspace) or config_path.is_relative_to(Path("/tmp"))
        ) and not config_path.is_relative_to(host_root):
            raise ValueError(
                "Explicit configuration inside the writable workspace or /tmp is unsafe; move it under the protected host root or outside both write roots"
            )
    run_id = uuid.uuid4().hex
    host_session = _private_directory(host_root / "sessions" / run_id)
    workspace_session = workspace / ".py" / "sessions" / run_id
    # Protect the whole host root, not just this run: input history, config and
    # other journals must remain unwritable even with a broad workspace root.
    sandbox = Sandbox(
        workspace,
        host_root,
        workspace_session / "scratch",
        network=args.network,
        allowed_domains=tuple(args.allow_domain),
    )
    _print_policy(sandbox, verbose=args.check_sandbox)
    supervisor = journal = permissions = None
    try:
        if args.check_sandbox:
            await sandbox.preflight()
            print("Sandbox isolation/stdio probe passed. External networking is not thereby verified.")
            return 0

        from .terminal import run as run_terminal
        from .history import Journal
        from .permissions import PermissionManager, PermissionStore
        from .supervisor import Supervisor

        protected_paths = {
            *sandbox.permanent_protected_paths(),
            Path("/tmp"),
            (Path.home() / ".pi" / "agent" / "auth.json").resolve(strict=False),
        }
        if args.config is not None:
            protected_paths.add(_no_symlinks(args.config).resolve(strict=False))
        if args.pi_auth is not None:
            protected_paths.add(_no_symlinks(args.pi_auth).resolve(strict=False))
        permissions = PermissionManager(
            PermissionStore(host_root),
            workspace,
            protected_paths=protected_paths,
        )
        sandbox.network_permission = permissions.request_network

        budget_values = dict(config.get("budgets", {}))
        for key in BUDGET_TYPES:
            value = getattr(args, key)
            if value is not None:
                budget_values[key] = value
        limits = Limits(**budget_values)
        context_window = (
            args.context_window_tokens
            if args.context_window_tokens is not None
            else config.get("context_window_tokens", limits.input_tokens)
        )
        if type(context_window) is not int or context_window <= 0:
            raise ValueError("context_window_tokens must be a positive integer")
        provider = _make_provider(args, config, workspace, session_id=run_id)
        journal = Journal(host_session / "journal.sqlite", run_id)

        def startup_event(event: dict) -> None:
            if event.get("kind") in {"say", "error", "notice", "limit"}:
                print(sanitize(event.get("content", "")))

        supervisor = Supervisor(
            provider,
            sandbox,
            journal,
            limits=limits,
            on_event=startup_event,
            context_window_tokens=context_window,
            permissions=permissions,
        )
        print(f"Run {run_id}; configured context capacity: {context_window} tokens. Type /help for help.")
        await supervisor.start()
        await run_terminal(
            supervisor,
            history_path=None if args.no_input_history else host_root / "input-history",
            vi=args.vi,
            multiline=args.multiline,
            no_color=args.no_color or "NO_COLOR" in os.environ,
        )
        return 0
    finally:
        try:
            if supervisor is not None:
                await supervisor.close()
        finally:
            try:
                await sandbox.close()
            finally:
                if permissions is not None:
                    permissions.close()
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
                print(
                    "py: provider cleanup timed out; usage may be unknown. Exiting without further execution.",
                    file=sys.stderr,
                )
        if installed_sigterm:
            loop.remove_signal_handler(signal.SIGTERM)
            signal.signal(signal.SIGTERM, previous_sigterm)
        loop.close()
        asyncio.set_event_loop(None)


def _legacy_main(argv: list[str] | None = None) -> int:
    args = _legacy_parser().parse_args(argv)
    if not args.check_sandbox and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        print(
            "py: interactive mode requires TTY stdin and stdout. Batch/JSON mode is not implemented; use a terminal or --check-sandbox.",
            file=sys.stderr,
        )
        return 2
    try:
        if args.allow_domain and args.network != "proxy":
            raise ValueError("--allow-domain requires explicit --network proxy")
        if args.fake_responses and (args.model or args.api_base or args.stream or args.effort):
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


def default_parser() -> argparse.ArgumentParser:
    """Arguments for the coordinator-based, sandbox-independent frontend."""
    result = argparse.ArgumentParser(
        prog="py",
        description="Persistent local IPython agent. The default executor is unrestricted.",
        epilog=(
            "Select a provider explicitly with --provider or in the JSON --config file. "
            "The fake provider is deterministic and is not a language model. "
            "No SRT/bwrap sandbox is required or implied; execution is unrestricted. "
            "Use --legacy to opt into the previous sandboxed CLI."
        ),
    )
    result.add_argument("--version", action="version", version="py-agent 0.1.0")
    result.add_argument(
        "--provider", default=None,
        help="Explicit provider service ID; built-in production providers also require --model",
    )
    result.add_argument("--plugin", action="append", default=None, metavar="ID",
                        help="Explicitly activate an installed third-party plugin (repeatable)")
    result.add_argument("--router", default=None, metavar="ID", help="Select an enabled router service")
    result.add_argument("--interpreter", default=None, metavar="ID",
                        help="Select an enabled response interpreter service")
    result.add_argument("--executor", default=None, metavar="ID", help="Select an enabled executor service")
    result.add_argument("--executor-wrapper", action="append", default=None, metavar="ID",
                        help="Select an enabled executor wrapper (repeatable, applied in order)")
    result.add_argument("--context-transform", action="append", default=None, metavar="PLUGIN:STAGE",
                        help="Select an enabled context transform (repeatable)")
    result.add_argument("--model-transform", action="append", default=None, metavar="PLUGIN:STAGE",
                        help="Select an enabled model-request transform (repeatable)")
    result.add_argument("--observer", action="append", default=None, metavar="PLUGIN:OBSERVER",
                        help="Select an enabled output observer (repeatable)")
    result.add_argument("--model", help="Explicit model ID (Codex uses openai-codex/MODEL)")
    result.add_argument("--api-base", help="Explicit API-key provider endpoint; litelm only")
    result.add_argument(
        "--stream", action=argparse.BooleanOptionalAction, default=None,
        help="Buffer a complete litelm stream before interpreting it",
    )
    result.add_argument("--effort", choices=CODEX_EFFORTS, help="Codex reasoning effort")
    result.add_argument("--pi-auth", type=Path, help="Codex only: pi OAuth auth file")
    result.add_argument("--config", type=Path, help="Typed JSON configuration file")
    result.add_argument("--max-tokens", type=int, help="Optional provider response token limit")
    result.add_argument("--max-agent-steps", type=int, help="Maximum generated cells per request")
    result.add_argument("--context-window-tokens", type=int, help="Context reset capacity; applies after restart")
    result.add_argument("--startup-timeout", type=float, help="Local IPython worker startup timeout")
    result.add_argument("--max-output-chars", type=int, help="Maximum retained worker output characters")
    result.add_argument(
        "--journal", type=Path, default=None,
        help="Persist coordinator history to a private SQLite file (prompts/code/output may be sensitive)",
    )
    # Retained for the legacy provider helper and backwards-compatible parsing;
    # the coordinator fake does not read fixture files.
    result.add_argument("--fake-responses", type=Path, help=argparse.SUPPRESS)
    result.add_argument(
        "--legacy", action="store_true",
        help="Explicitly use the previous sandboxed supervisor CLI and its options",
    )
    result.add_argument("--vi", action="store_true", help="Use vi editing mode")
    result.add_argument("--multiline", action="store_true", help="Use multiline input mode")
    result.add_argument("--no-color", action="store_true", help="Disable terminal colors")
    return result


def parser() -> argparse.ArgumentParser:
    """Return the default coordinator CLI parser."""
    return default_parser()


def _core_config_registry():
    from .configuration import ApplyAt, ConfigField, ConfigRegistry

    return ConfigRegistry((
        ConfigField("provider.id", "core", str, "", apply_at=ApplyAt.RESTART,
                    documentation="Explicitly selected provider service ID."),
        ConfigField("model.name", "core", str, "", apply_at=ApplyAt.RESTART,
                    documentation="Provider model identifier, when required by the selected provider."),
        ConfigField("provider.api_base", "core", str, "", apply_at=ApplyAt.RESTART,
                    documentation="Optional litelm API endpoint; never used for Codex OAuth."),
        ConfigField("provider.stream", "core", bool, False, apply_at=ApplyAt.RESTART,
                    documentation="Buffer litelm streaming completions."),
        ConfigField("model.effort", "core", str, "medium", choices=CODEX_EFFORTS,
                    documentation="Codex reasoning effort; sampled at each request."),
        ConfigField("model.max_tokens", "core", int, 0, minimum=0, maximum=1000000,
                    documentation="Optional litelm/API-key output limit; zero means unspecified. Codex has no client-side cap."),
        ConfigField("agent.max_steps", "core", int, 16, minimum=1, maximum=1000,
                    apply_at=ApplyAt.RESTART, documentation="Maximum generated cells per submitted agent request."),
        ConfigField("plugins.enabled", "core", str, "", apply_at=ApplyAt.RESTART,
                    documentation="Comma-separated plugin IDs explicitly activated from the selected config file."),
        ConfigField("plugins.context_transforms", "core", str, "", apply_at=ApplyAt.RESTART,
                    documentation="Comma-separated qualified context transform IDs selected at startup."),
        ConfigField("plugins.model_transforms", "core", str, "", apply_at=ApplyAt.RESTART,
                    documentation="Comma-separated qualified model-request transform IDs selected at startup."),
        ConfigField("plugins.observers", "core", str, "", apply_at=ApplyAt.RESTART,
                    documentation="Comma-separated qualified output observer IDs selected at startup."),
        ConfigField("router.id", "core", str, "default", apply_at=ApplyAt.RESTART,
                    documentation="Explicitly selected router service ID."),
        ConfigField("interpreter.id", "core", str, "basic", apply_at=ApplyAt.RESTART,
                    documentation="Explicitly selected response interpreter service ID."),
        ConfigField("executor.id", "core", str, "local", apply_at=ApplyAt.RESTART,
                    documentation="Explicitly selected executor service ID; selection never falls back."),
        ConfigField("executor.wrappers", "core", str, "", apply_at=ApplyAt.RESTART,
                    documentation="Comma-separated qualified executor wrapper IDs, applied in order."),
        ConfigField("context.window_tokens", "core", int, 272000, minimum=1, maximum=10000000,
                    apply_at=ApplyAt.RESTART, documentation="Reported-usage context reset threshold."),
        ConfigField("executor.startup_timeout", "core", float, 15.0, minimum=0.1, maximum=300.0,
                    apply_at=ApplyAt.RESTART, documentation="Local worker startup timeout in seconds."),
        ConfigField("executor.max_output_chars", "core", int, 262144, minimum=1, maximum=1048576,
                    apply_at=ApplyAt.RESTART, documentation="Maximum retained output characters."),
    ))


def _parse_id_list(value: object, setting: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise ValueError(f"{setting} must be comma-separated text")
    if not value:
        return ()
    values = tuple(value.split(","))
    if any(not item or item != item.strip() or "\x00" in item for item in values):
        raise ValueError(f"{setting} must contain trimmed, nonempty IDs")
    if len(values) != len(set(values)):
        raise ValueError(f"{setting} contains duplicate IDs")
    return values


def _trusted_config_values(path: Path | None) -> dict:
    """Read only an explicitly selected, owner-private flat JSON config file."""
    if path is None or not (path.exists() or path.is_symlink()):
        return {}
    from .config_commands import _read_json_mapping

    return _read_json_mapping(path)


def _default_runtime(enabled: tuple[str, ...], options: dict):
    """Build default built-ins plus only the named external entry points."""
    from .builtin_services import BuiltinPlugin
    from .limits import Limits
    from .local_executor import LocalExecutor
    from .plugins import PluginRuntime
    from .production_services import (
        ProductionContextAdapter, ProductionObservationAdapter, ProductionServicesPlugin,
        codex_provider_factory, litelm_provider_factory,
    )

    def create_executor():
        return LocalExecutor(
            timeout=options["startup_timeout"], max_output_chars=options["max_output_chars"],
        )

    def create_litelm():
        model = options["model"]
        if not model:
            raise ValueError("The selected litelm provider requires a model")
        return litelm_provider_factory(
            model, api_base=options["api_base"], stream=options["stream"],
        )()

    def create_codex():
        model = options["model"]
        if not model:
            raise ValueError("The selected Codex provider requires a model")
        return codex_provider_factory(
            model, auth_file=options["auth_file"], effort=options["effort"],
        )()

    def create_context():
        return ProductionContextAdapter(limits=Limits(input_tokens=options["context_window_tokens"]))

    builtins = {
        "builtin": BuiltinPlugin(executor_factory=create_executor),
        "production": ProductionServicesPlugin(
            litelm_factory=create_litelm,
            codex_factory=create_codex,
            context_factory=create_context,
            observations_factory=ProductionObservationAdapter,
        ),
    }
    return PluginRuntime.load(builtins=builtins, enabled=enabled)


def _select_runtime_stages(runtime, context_ids, model_ids, observer_ids):
    """Build a runtime containing only explicitly selected pipeline stages."""
    from .plugins import PluginError, PluginRuntime

    available_context = {item.qualified_name: item for item in runtime.transforms["context"]}
    available_model = {item.qualified_name: item for item in runtime.transforms["model-request"]}
    available_observers = {item.qualified_name: item for item in runtime.observers}
    selections = (
        ("context transform", set(context_ids), available_context),
        ("model-request transform", set(model_ids), available_model),
        ("observer", set(observer_ids), available_observers),
    )
    for description, selected, available in selections:
        unknown = selected - available.keys()
        if unknown:
            raise PluginError(f"No enabled {description} IDs named {sorted(unknown)}")
    transforms = tuple(
        [available_context[name] for name in sorted(context_ids)]
        + [available_model[name] for name in sorted(model_ids)]
    )
    observers = tuple(available_observers[name] for name in sorted(observer_ids))
    return PluginRuntime(
        runtime.manifests,
        runtime.services,
        runtime.order,
        runtime.config.fields.values(),
        commands=runtime.commands.values(),
        transforms=transforms,
        observers=observers,
        executor_wrappers=runtime.executor_wrappers.values(),
    )


def _with_factory_config(runtime, config_store):
    """Inject immutable plugin namespaces into factories that request ``config``.

    Generic service factories receive a mapping keyed by plugin ID because the
    v1 Service record intentionally carries no owning-plugin field. Wrapper
    factories retain their delegate-first API and may opt in with a named config
    parameter.
    """
    from .plugins import (
        ExecutorWrapperContribution, PluginRuntime, RegisteredExecutorWrapper, Service,
    )

    snapshot = config_store.snapshot
    namespaces = {}
    for name, field in runtime.config.fields.items():
        entry = snapshot.entries.get(name)
        value = field.default if entry is None else entry.value
        namespaces.setdefault(field.owner, {})[name.removeprefix(field.owner + ".")] = value
    immutable_namespaces = MappingProxyType({
        plugin_id: MappingProxyType(values) for plugin_id, values in namespaces.items()
    })

    def config_parameter(factory):
        try:
            return signature(factory).parameters.get("config")
        except (TypeError, ValueError):
            return None

    services = {}
    for key, service in runtime.services.items():
        parameter = config_parameter(service.factory)
        if parameter is None:
            services[key] = service
            continue
        factory = service.factory
        if parameter.kind is Parameter.POSITIONAL_ONLY:
            configured = lambda factory=factory: factory(immutable_namespaces)
        else:
            configured = lambda factory=factory: factory(config=immutable_namespaces)
        services[key] = Service(service.kind, service.id, configured)

    wrappers = []
    for registration in runtime.executor_wrappers.values():
        factory = registration.factory
        parameter = config_parameter(factory)
        if parameter is None:
            wrappers.append(registration)
            continue
        owner_config = immutable_namespaces.get(registration.plugin_id, MappingProxyType({}))
        if parameter.kind is Parameter.POSITIONAL_ONLY:
            configured = lambda delegate, factory=factory, values=owner_config: factory(delegate, values)
        else:
            configured = lambda delegate, factory=factory, values=owner_config: factory(
                delegate, config=values,
            )
        wrappers.append(RegisteredExecutorWrapper(
            registration.plugin_id,
            ExecutorWrapperContribution(registration.name, configured),
        ))

    return PluginRuntime(
        runtime.manifests,
        services,
        runtime.order,
        runtime.config.fields.values(),
        commands=runtime.commands.values(),
        transforms=tuple(
            (*runtime.transforms["context"], *runtime.transforms["model-request"])
        ),
        observers=runtime.observers,
        executor_wrappers=wrappers,
    )


@dataclass(frozen=True)
class _CliConfiguration:
    store: object
    runtime: object
    provider: str
    model: str | None
    api_base: str | None
    stream: bool
    effort: str
    config_path: Path | None
    router: str
    interpreter: str
    executor: str
    executor_wrappers: tuple[str, ...]
    context_transforms: tuple[str, ...]
    model_transforms: tuple[str, ...]
    observers: tuple[str, ...]
    options: dict


def _prepare_configuration(args: argparse.Namespace) -> _CliConfiguration:
    """Activate trusted/flag-selected plugins, then load their merged schema."""
    from .configuration import ConfigRegistry, ConfigStore
    from .config_commands import ConfigCommandService
    from .plugins import PluginError

    config_path = getattr(args, "config", None)
    if config_path is not None and not isinstance(config_path, Path):
        config_path = Path(config_path)
    file_values = _trusted_config_values(config_path)
    configured_plugins = _parse_id_list(file_values.get("plugins.enabled", ""), "plugins.enabled")
    cli_plugins = tuple(getattr(args, "plugin", None) or ())
    if any(not isinstance(item, str) or not item or item != item.strip() for item in cli_plugins):
        raise ValueError("--plugin requires nonempty, trimmed plugin IDs")
    if len(cli_plugins) != len(set(cli_plugins)):
        raise ValueError("Duplicate --plugin ID")
    enabled = tuple(dict.fromkeys((*configured_plugins, *cli_plugins)))

    options = {
        "model": "",
        "api_base": None,
        "stream": False,
        "effort": "medium",
        "auth_file": getattr(args, "pi_auth", None),
        "context_window_tokens": 272000,
        "startup_timeout": 15.0,
        "max_output_chars": 262144,
    }
    runtime = _default_runtime(enabled, options)
    core_registry = _core_config_registry()
    registry = ConfigRegistry((*core_registry.fields.values(), *runtime.config.fields.values()))
    store = ConfigStore(registry)
    temporary = ConfigCommandService(store, runtime, config_path=config_path)
    if config_path is not None and (config_path.exists() or config_path.is_symlink()):
        response = temporary.reload()
        if not response.ok:
            raise ValueError(response.text)

    overrides = {}
    mapping = (
        ("provider", "provider.id"), ("model", "model.name"),
        ("api_base", "provider.api_base"), ("stream", "provider.stream"),
        ("effort", "model.effort"), ("max_tokens", "model.max_tokens"),
        ("max_agent_steps", "agent.max_steps"),
        ("router", "router.id"), ("interpreter", "interpreter.id"),
        ("executor", "executor.id"),
        ("context_window_tokens", "context.window_tokens"),
        ("startup_timeout", "executor.startup_timeout"),
        ("max_output_chars", "executor.max_output_chars"),
    )
    for argument, field_name in mapping:
        value = getattr(args, argument, None)
        if value is not None:
            overrides[field_name] = value
    if cli_plugins:
        overrides["plugins.enabled"] = ",".join(enabled)
    cli_wrappers = tuple(getattr(args, "executor_wrapper", None) or ())
    if any(not isinstance(item, str) or not item or item != item.strip() for item in cli_wrappers):
        raise ValueError("--executor-wrapper requires nonempty, trimmed IDs")
    if len(cli_wrappers) != len(set(cli_wrappers)):
        raise ValueError("Duplicate --executor-wrapper ID")
    if cli_wrappers:
        configured_wrappers = _parse_id_list(
            store.snapshot.get("executor.wrappers"), "executor.wrappers",
        )
        wrappers = (*configured_wrappers, *cli_wrappers)
        if len(wrappers) != len(set(wrappers)):
            raise ValueError("Duplicate selected executor wrapper ID")
        overrides["executor.wrappers"] = ",".join(wrappers)

    stage_ids = {}
    for argument, field_name in (
        ("context_transform", "plugins.context_transforms"),
        ("model_transform", "plugins.model_transforms"),
        ("observer", "plugins.observers"),
    ):
        flag = argument.replace("_", "-")
        requested = tuple(getattr(args, argument, None) or ())
        if any(not isinstance(item, str) or not item or item != item.strip() for item in requested):
            raise ValueError(f"--{flag} requires nonempty, trimmed IDs")
        if len(requested) != len(set(requested)):
            raise ValueError(f"Duplicate --{flag} ID")
        selected_ids = (*_parse_id_list(store.snapshot.get(field_name), field_name), *requested)
        if len(selected_ids) != len(set(selected_ids)):
            raise ValueError(f"Duplicate selected IDs in {field_name}")
        stage_ids[argument] = tuple(selected_ids)
        if requested:
            overrides[field_name] = ",".join(selected_ids)

    if getattr(args, "fake_responses", None) is not None:
        raise ValueError("--fake-responses is only supported by the legacy supervisor CLI")
    if overrides:
        store.set(overrides)

    snapshot = store.snapshot
    provider = snapshot.get("provider.id")
    model = snapshot.get("model.name")
    if not provider:
        raise ValueError("Select --provider ID or set provider.id in the explicitly selected JSON config")
    # Resolve every selected service before constructing any of them. There is
    # no provider/executor/router fallback on an unknown or disabled ID.
    runtime.select("provider", provider)
    router = snapshot.get("router.id")
    interpreter = snapshot.get("interpreter.id")
    executor = snapshot.get("executor.id")
    runtime.select("router", router)
    runtime.select("interpreter", interpreter)
    runtime.select("executor", executor)
    if executor == "isolated":
        for core_field, plugin_field in (
            ("executor.startup_timeout", "isolated-executor.startup_timeout"),
            ("executor.max_output_chars", "isolated-executor.max_output_chars"),
        ):
            if snapshot.entries[core_field].source != "default":
                raise ValueError(
                    f"{core_field} does not configure the isolated executor; set {plugin_field} instead"
                )
    wrappers = _parse_id_list(snapshot.get("executor.wrappers"), "executor.wrappers")
    unknown_wrappers = set(wrappers) - runtime.executor_wrappers.keys()
    if unknown_wrappers:
        raise PluginError(f"No enabled executor wrappers named {sorted(unknown_wrappers)}")

    if provider in {"litelm", "codex"} and not model:
        raise ValueError("Built-in production providers require --model or model.name in the JSON config")
    if provider == "codex" and not model.startswith("openai-codex/"):
        raise ValueError("Codex model must use the explicit openai-codex/MODEL form")
    if provider == "fake" and model:
        raise ValueError("A model name is not used by the fake provider; remove model.name")
    api_base = snapshot.get("provider.api_base")
    stream = snapshot.get("provider.stream")
    effort = snapshot.get("model.effort")
    if provider != "litelm" and api_base:
        raise ValueError("provider.api_base is supported only by the litelm provider")
    if provider != "litelm" and stream:
        raise ValueError("provider.stream is supported only by the litelm provider")
    if provider != "codex" and snapshot.entries["model.effort"].source != "default":
        raise ValueError("model.effort is supported only by the Codex provider")
    if provider == "fake" and snapshot.get("model.max_tokens"):
        raise ValueError("model.max_tokens is not supported by the fake provider")
    if provider == "codex" and snapshot.get("model.max_tokens"):
        raise ValueError("Codex does not support a client-side output token cap")
    if provider not in {"fake", "litelm", "codex"} and snapshot.get("model.max_tokens"):
        raise ValueError("model.max_tokens is not supported by the selected third-party provider")
    if getattr(args, "pi_auth", None) is not None and provider != "codex":
        raise ValueError("--pi-auth requires --provider codex")

    options.update({
        "model": model,
        "api_base": api_base or None,
        "stream": stream,
        "effort": effort,
        "auth_file": getattr(args, "pi_auth", None),
        "context_window_tokens": snapshot.get("context.window_tokens"),
        "startup_timeout": snapshot.get("executor.startup_timeout"),
        "max_output_chars": snapshot.get("executor.max_output_chars"),
    })
    selected_runtime = _select_runtime_stages(
        runtime,
        stage_ids["context_transform"],
        stage_ids["model_transform"],
        stage_ids["observer"],
    )
    return _CliConfiguration(
        store, selected_runtime, provider, model or None, api_base or None, stream, effort,
        config_path, router, interpreter, executor, wrappers,
        stage_ids["context_transform"], stage_ids["model_transform"], stage_ids["observer"], options,
    )


def _initial_config(args: argparse.Namespace):
    """Backward-compatible core tuple, now resolved against enabled plugin schemas."""
    configured = _prepare_configuration(args)
    return (
        configured.store, configured.provider, configured.model, configured.api_base,
        configured.stream, configured.effort, configured.config_path,
    )


def _build_coordinator(
    provider: str = "fake",
    *,
    model: str | None = None,
    api_base: str | None = None,
    stream: bool = False,
    effort: str = "medium",
    auth_file: Path | None = None,
    context_window_tokens: int = 272000,
    startup_timeout: float = 15.0,
    max_output_chars: int = 262144,
    config_store=None,
    config_path: Path | None = None,
    runtime=None,
    router: str = "default",
    interpreter: str = "basic",
    executor: str = "local",
    executor_wrappers: tuple[str, ...] | None = None,
    max_agent_steps: int | None = None,
    journal=None,
):
    """Build the explicitly selected services; unknown IDs never downgrade."""
    from .configuration import ConfigRegistry, ConfigStore
    from .config_commands import ConfigCommandService
    from .coordinator import Coordinator
    from .plugins import PluginError
    from .plain_terminal import RESERVED_COMMANDS

    options = {
        "model": model or "",
        "api_base": api_base,
        "stream": stream,
        "effort": effort,
        "auth_file": auth_file,
        "context_window_tokens": context_window_tokens,
        "startup_timeout": startup_timeout,
        "max_output_chars": max_output_chars,
    }
    if runtime is None:
        runtime = _default_runtime((), options)
    if config_store is None:
        core = _core_config_registry()
        config_store = ConfigStore(ConfigRegistry((*core.fields.values(), *runtime.config.fields.values())))
    elif not set(runtime.config.fields) <= set(config_store.registry.fields):
        raise ValueError("Configuration store is missing schema fields from enabled plugins")
    runtime = _with_factory_config(runtime, config_store)

    # Resolve the complete selection before any service factory can have side effects.
    selected = {
        "router": runtime.select("router", router),
        "provider": runtime.select("provider", provider),
        "interpreter": runtime.select("interpreter", interpreter),
        "executor": runtime.select("executor", executor),
        "context": runtime.select("context", "production"),
        "observation": runtime.select("observation", "lossless"),
    }
    wrappers = executor_wrappers
    if wrappers is None:
        wrappers = _parse_id_list(config_store.snapshot.get("executor.wrappers"), "executor.wrappers")
    wrappers = tuple(wrappers)
    unknown_wrappers = set(wrappers) - runtime.executor_wrappers.keys()
    if unknown_wrappers:
        raise PluginError(f"No enabled executor wrappers named {sorted(unknown_wrappers)}")

    commands = ConfigCommandService(config_store, runtime, config_path=config_path)
    collisions = set(runtime.commands) & (
        set(commands.command_registry.commands) | set(RESERVED_COMMANDS)
    )
    if collisions:
        raise PluginError(f"External commands collide with built-in commands: {sorted(collisions)}")

    context = selected["context"].factory()
    observations = selected["observation"].factory()
    return Coordinator(
        runtime,
        router=router,
        provider=provider,
        interpreter=interpreter,
        executor=executor,
        executor_wrappers=wrappers,
        context=context,
        observations=observations,
        command_registry=commands.command_registry,
        config_store=config_store,
        journal=journal,
        model=model,
        max_agent_steps=max_agent_steps if max_agent_steps is not None else config_store.snapshot.get("agent.max_steps"),
    )


async def _run_phase1(args: argparse.Namespace) -> int:
    """Run the coordinator CLI (kept under its old private name for embedders)."""
    from .session_journal import SQLiteSessionJournal

    configured = _prepare_configuration(args)
    provider, model = configured.provider, configured.model
    journal_path = getattr(args, "journal", None)
    journal = SQLiteSessionJournal(journal_path) if journal_path is not None else None
    coordinator = None
    try:
        coordinator = _build_coordinator(
            provider,
            model=model,
            api_base=configured.api_base,
            stream=configured.stream,
            effort=configured.effort,
            auth_file=getattr(args, "pi_auth", None),
            context_window_tokens=configured.store.snapshot.get("context.window_tokens"),
            startup_timeout=configured.store.snapshot.get("executor.startup_timeout"),
            max_output_chars=configured.store.snapshot.get("executor.max_output_chars"),
            max_agent_steps=configured.store.snapshot.get("agent.max_steps"),
            config_store=configured.store,
            config_path=configured.config_path,
            runtime=configured.runtime,
            router=configured.router,
            interpreter=configured.interpreter,
            executor=configured.executor,
            executor_wrappers=configured.executor_wrappers,
            journal=journal,
        )
        try:
            if provider == "fake":
                print("Provider: deterministic fake — no live model request is made.")
            else:
                print(f"Provider: {provider}; model: {model}. Requests are sent only after submission.")
            print(
                "WARNING: execution is unrestricted as your current user. Python, shell, and agent code can "
                "read/write accessible files, start subprocesses, use the network, and access available credentials. "
                "The worker process is not a sandbox; use an external isolation wrapper if needed."
            )
            if journal is None:
                print("History persistence: disabled explicitly (no-persistence journal).")
            else:
                print(
                    f"History journal: {journal.path}. Prompts, context, code, and output are private data; "
                    "recognizable credentials are redacted, but arbitrary secrets cannot be identified reliably."
                )
            await coordinator.start()
            await PlainTerminal(
                coordinator,
                vi=getattr(args, "vi", False),
                multiline=getattr(args, "multiline", False),
            ).run()
            return 0
        finally:
            try:
                await coordinator.close()
            finally:
                if journal is not None:
                    journal.close()
    finally:
        # A builder failure happens before coordinator ownership is established.
        if coordinator is None and journal is not None:
            journal.close()


def _coordinator_options(result: argparse.ArgumentParser) -> None:
    """Add explicit coordinator configuration to a management subcommand."""
    result.add_argument("--provider", default=None)
    result.add_argument("--plugin", action="append", default=None, metavar="ID")
    result.add_argument("--router", default=None)
    result.add_argument("--interpreter", default=None)
    result.add_argument("--executor", default=None)
    result.add_argument("--executor-wrapper", action="append", default=None, metavar="ID")
    result.add_argument("--context-transform", action="append", default=None, metavar="PLUGIN:STAGE")
    result.add_argument("--model-transform", action="append", default=None, metavar="PLUGIN:STAGE")
    result.add_argument("--observer", action="append", default=None, metavar="PLUGIN:OBSERVER")
    result.add_argument("--model", default=None)
    result.add_argument("--api-base", default=None)
    result.add_argument("--stream", action=argparse.BooleanOptionalAction, default=None)
    result.add_argument("--effort", choices=CODEX_EFFORTS, default=None)
    result.add_argument("--pi-auth", type=Path, default=None)
    result.add_argument("--config", type=Path, default=None)
    result.add_argument("--max-tokens", type=int, default=None)
    result.add_argument("--max-agent-steps", type=int, default=None)
    result.add_argument("--context-window-tokens", type=int, default=None)
    result.add_argument("--startup-timeout", type=float, default=None)
    result.add_argument("--max-output-chars", type=int, default=None)
    result.add_argument("--journal", type=Path, default=None)
    result.add_argument("--fake-responses", type=Path, default=None, help=argparse.SUPPRESS)


def _kernel_launch_arguments(args: argparse.Namespace) -> list[str]:
    """Serialize only explicitly supplied coordinator options for a child kernel."""
    result: list[str] = []
    for name, option in (
        ("provider", "--provider"),
        ("router", "--router"),
        ("interpreter", "--interpreter"),
        ("executor", "--executor"),
        ("model", "--model"),
        ("api_base", "--api-base"),
        ("effort", "--effort"),
        ("pi_auth", "--pi-auth"),
        ("config", "--config"),
        ("max_tokens", "--max-tokens"),
        ("max_agent_steps", "--max-agent-steps"),
        ("context_window_tokens", "--context-window-tokens"),
        ("startup_timeout", "--startup-timeout"),
        ("max_output_chars", "--max-output-chars"),
        ("journal", "--journal"),
    ):
        value = getattr(args, name, None)
        if value is not None:
            if name in ("pi_auth", "config", "journal"):
                value = Path(value).expanduser().resolve()
            result.extend((option, str(value)))
    for name, option in (
        ("plugin", "--plugin"),
        ("executor_wrapper", "--executor-wrapper"),
        ("context_transform", "--context-transform"),
        ("model_transform", "--model-transform"),
        ("observer", "--observer"),
    ):
        for value in getattr(args, name, None) or ():
            result.extend((option, str(value)))
    stream = getattr(args, "stream", None)
    if stream is not None:
        result.append("--stream" if stream else "--no-stream")
    return result


def _management_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="py")
    commands = parser.add_subparsers(dest="command", required=True)
    kernel = commands.add_parser("kernel", help="manage an optional Jupyter kernel")
    kernel_actions = kernel.add_subparsers(dest="kernel_action", required=True)
    install = kernel_actions.add_parser("install", help="install a user Jupyter kernelspec")
    install.add_argument("--name", default="py-agent")
    install.add_argument("--display-name", default="py-agent")
    _coordinator_options(install)
    start = kernel_actions.add_parser("start", help="start an independently managed kernel")
    start.add_argument("--session-root", type=Path, default=None)
    start.add_argument("--wait-timeout", type=float, default=30.0)
    _coordinator_options(start)
    stop = kernel_actions.add_parser("stop", help="stop an owned kernel session")
    stop.add_argument("id")
    stop.add_argument("--session-root", type=Path, default=None)
    stop.add_argument("--shutdown-timeout", type=float, default=5.0)
    kernels = commands.add_parser("kernels", help="list owned live kernel sessions")
    kernels.add_argument("--session-root", type=Path, default=None)
    attach = commands.add_parser("attach", help="attach stock jupyter-console to an owned session")
    attach.add_argument("id")
    attach.add_argument("--session-root", type=Path, default=None)
    return parser


def _management_main(arguments: list[str]) -> int:
    from . import kernel_sessions

    args = _management_parser().parse_args(arguments)
    root = getattr(args, "session_root", None) or kernel_sessions.DEFAULT_SESSION_ROOT
    if args.command == "kernel" and args.kernel_action in {"install", "start"}:
        kernel_sessions.require_jupyter_runtime()
        # Resolve and validate the same configuration as the terminal. A kernel
        # must never silently choose a provider or model.
        _, provider, model, _, _, _, _ = _initial_config(args)
        launch_arguments = _kernel_launch_arguments(args)
        if args.kernel_action == "install":
            location = kernel_sessions.install_kernelspec(
                launch_arguments, name=args.name, display_name=args.display_name,
            )
            print(f"Installed py-agent kernelspec: {location}")
            return 0
        session = kernel_sessions.start_session(
            [
                *kernel_sessions.kernel_argv(), *launch_arguments,
                "-f", "{connection_file}",
            ],
            root=root,
            cwd=Path.cwd(),
            provider=provider,
            model=model,
            wait_timeout=args.wait_timeout,
        )
        print(f"Started kernel session {session.session_id} (pid {session.pid}).")
        attach = "py attach " + session.session_id
        if args.session_root is not None:
            attach += " --session-root " + str(Path(args.session_root).expanduser().resolve())
        print("Attach with: " + attach)
        return 0
    if args.command == "kernels":
        sessions = kernel_sessions.list_sessions(root=root, cleanup_stale=True)
        if not sessions:
            print("No owned live kernel sessions.")
            return 0
        for session in sessions:
            selection = session.provider or "from config"
            if session.model:
                selection += "/" + session.model
            state = "running" if session.connection_ready else "starting"
            print(f"{session.session_id}  pid={session.pid}  {state}  {sanitize(selection)}")
        return 0
    if args.command == "attach":
        kernel_sessions.require_jupyter_runtime(console=True)
        session = kernel_sessions.get_session(args.id, root=root)
        return subprocess.run(
            [sys.executable, "-m", "jupyter_console", "--existing", str(session.connection_file)],
            check=False,
        ).returncode
    if args.command == "kernel" and args.kernel_action == "stop":
        stopped = kernel_sessions.stop_session(
            args.id, root=root, shutdown_timeout=args.shutdown_timeout,
        )
        if stopped:
            print(f"Stopped kernel session {args.id}.")
        else:
            print(f"Cleaned stale kernel session {args.id}.")
        return 0
    raise ValueError("Unsupported kernel management command")


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--legacy" in arguments:
        arguments.remove("--legacy")
        return _legacy_main(arguments)

    if arguments and arguments[0] in {"kernel", "kernels", "attach"}:
        try:
            return _management_main(arguments)
        except asyncio.CancelledError:
            print("py: kernel operation cancelled.", file=sys.stderr)
            return 130
        except KeyboardInterrupt:
            print("py: kernel operation cancelled.", file=sys.stderr)
            return 130
        except Exception as exc:
            print("py: " + sanitize(str(exc)), file=sys.stderr)
            return 1

    args = parser().parse_args(arguments)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "py: the plain terminal frontend requires TTY stdin and stdout. "
            "Batch/JSON mode is not implemented.",
            file=sys.stderr,
        )
        return 2
    try:
        return _run_bounded(_run_phase1(args))
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
