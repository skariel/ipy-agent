"""Explicit-Tab command and bounded workspace-path completion; no file reads."""
from __future__ import annotations

from collections.abc import Callable, Iterator
import os
from pathlib import Path
import re
import time

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document

COMMANDS = {
    "help": "Command help", "login": "Choose a provider and sign in",
    "logout": "Remove a native provider login", "auth": "Credential status",
    "model": "Choose a credential-backed model", "effort": "Reasoning level",
    "think": "Alias for /effort", "status": "Session status",
    "interrupt": "Interrupt active work", "quit": "Quit", "exit": "Quit",
    "config": "Session configuration", "plugins": "Plugin information",
    "resume": "Resume pending model request without replaying cells",
    "recovery": "Show/discard recovery checkpoint", "context": "Save context", "history": "Inspect journal",
}
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}


def matches(query: str, value: str) -> bool:
    """Case-insensitive subsequence matching."""
    iterator = iter(value.casefold())
    return all(any(c == wanted for c in iterator) for wanted in query.casefold())


def providers() -> tuple[str, ...]:
    from litelm._providers import PROVIDERS

    from .model_catalog import MODELS
    return tuple(sorted(set(MODELS) | set(PROVIDERS)))


def model_choices(coordinator: object) -> tuple[str, ...]:
    from .model_catalog import available_models
    adapter = getattr(getattr(coordinator, "provider", None), "adapter", None)
    return available_models(getattr(adapter, "auth_file", None))


class TerminalCompleter(Completer):
    def __init__(self, coordinator: object, enabled: Callable[[], bool] = lambda: True) -> None:
        self.coordinator = coordinator
        self.enabled = enabled
        self._paths: tuple[str, ...] = ()
        self._stamp = 0.0
        self._cwd: Path | None = None

    def workspace_paths(self) -> tuple[str, ...]:
        cwd = Path.cwd()
        now = time.monotonic()
        if self._cwd == cwd and now - self._stamp < 3:
            return self._paths
        paths = set()
        # Walk only names, never contents; no symlink traversal. Bound work even
        # in huge/non-git trees. Root-relative paths work from the current cwd.
        deadline = now + 0.25
        visited = 0
        for base, dirs, files in os.walk(cwd, followlinks=False):
            visited += 1
            relative = Path(base).relative_to(cwd)
            dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith(".")
                             and not (Path(base) / d).is_symlink())
            if len(relative.parts) >= 7:
                dirs[:] = []
            for name in dirs + files:
                if name.startswith(".") or any(ord(c) < 32 or ord(c) == 127 for c in name):
                    continue
                if (Path(base) / name).is_symlink():
                    continue
                path = (relative / name).as_posix()
                paths.add(path + "/" if name in dirs else path)
                if len(paths) >= 6000:
                    break
            if len(paths) >= 6000 or visited >= 2000 or time.monotonic() >= deadline:
                break
        self._cwd, self._stamp, self._paths = cwd, now, tuple(sorted(paths))
        return self._paths

    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Iterator[Completion]:
        if not self.enabled():
            return
        before = document.text_before_cursor
        command = re.fullmatch(r"/([a-z][a-z0-9_-]*|)(?: (.*))?", before)
        candidates: tuple[str, ...] = ()
        metadata = {}
        if command:
            name, arguments = command.groups()
            if arguments is None:
                registry = getattr(getattr(self.coordinator, "command_registry", None), "commands", {})
                external = getattr(self.coordinator, "external_commands", {})
                candidates = tuple("/" + n for n in sorted(set(COMMANDS) | set(registry) | set(external)))
                query = before
                metadata = {"/" + k: v for k, v in COMMANDS.items()}
            else:
                query = arguments
                if name in {"login", "logout"}:
                    if name == "logout":
                        from .native_auth import read_document
                        try:
                            candidates = tuple(read_document())
                        except Exception:
                            return
                    elif " " in arguments:
                        query = arguments.rsplit(" ", 1)[-1]
                        candidates = ("--api-key", "--manual")
                    else:
                        candidates = providers()
                elif name == "model":
                    try:
                        candidates = model_choices(self.coordinator)
                    except Exception:
                        candidates = ()
                elif name in {"think", "effort"}:
                    candidates = EFFORTS
                elif name == "recovery":
                    candidates = ("discard",)
                elif name == "auth":
                    candidates = ("status",)
                elif name == "config":
                    parts = arguments.split(" ")
                    query = parts[-1]
                    if len(parts) == 1:
                        candidates = ("get", "describe", "set", "reset", "diff", "save", "reload")
                    elif parts[0] in {"get", "describe", "set", "reset", "save"}:
                        store = getattr(self.coordinator, "config_store", None)
                        candidates = tuple(getattr(getattr(store, "registry", None), "fields", {}))
                elif name == "plugins":
                    parts = arguments.split(" ")
                    query = parts[-1]
                    if len(parts) == 1:
                        candidates = ("inspect",)
                    elif parts[0] == "inspect" and len(parts) == 2:
                        runtime = getattr(self.coordinator, "_plugin_runtime", None)
                        candidates = tuple(getattr(runtime, "manifests", {}))
                elif name == "history":
                    candidates = ("search", "page")
                elif name == "context":
                    candidates = ("save",)
                if not candidates:
                    return
        else:
            # No special prefix: Tab fuzzy-matches the final token in English,
            # shell or Python. A leading @ is kept intact.
            match = re.search(r"([^\s\"'`]+)$", before)
            if not match:
                return
            query = match.group(1)
            if before.startswith("@") and match.start() == 0:
                query = query[1:]
            if not query or query.startswith(("~", "/", "\\")):
                return  # Do not enumerate outside the workspace.
            candidates = self.workspace_paths()
            if query.startswith("./"):
                candidates = tuple("./" + path for path in candidates)
        ranked = sorted((v for v in candidates if matches(query, v)),
                        key=lambda v: (not v.casefold().startswith(query.casefold()), len(v), v))
        for value in ranked[:100]:
            yield Completion(value, start_position=-len(query),
                             display_meta=metadata.get(value, ""))
