"""Terminal startup selection; persist only builtin adapter/model identifiers."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .config_commands import ConfigCommandError, _read_json_mapping

_MODEL = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:/+-]{0,255}")


class MissingModelSelection(ValueError):
    """Configuration is valid so far but needs a terminal selection."""

    def __init__(self, message: str, provider: str = "") -> None:
        super().__init__(message)
        self.provider = provider


def selection_path() -> Path:
    return Path.home() / ".py" / "default-model.json"


def validate_selection(provider: object, model: object) -> tuple[str, str]:
    from .model_catalog import adapter_for_model

    if provider not in {"fake", "codex", "litelm"} or not isinstance(model, str):
        raise ValueError("Invalid remembered model selection")
    if provider == "fake":
        if model:
            raise ValueError("The fake provider does not use a model")
    elif not _MODEL.fullmatch(model) or "/" not in model:
        raise ValueError("Use a trimmed PROVIDER/MODEL identifier")
    elif adapter_for_model(model) != provider:
        raise ValueError("Model does not match the selected adapter")
    return str(provider), model


def load_selection() -> tuple[str, str] | None:
    path = selection_path()
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Remembered selection must be an owner-private regular file")
    values = _read_json_mapping(path)
    if set(values) != {"provider.id", "model.name"}:
        raise ValueError("Remembered selection must contain only provider.id and model.name")
    return validate_selection(values["provider.id"], values["model.name"])


def save_selection(provider: str, model: str) -> None:
    """Atomically store identifiers, never credentials or plugin activation."""
    validate_selection(provider, model)
    path = selection_path()
    # Reuse the private config path checks without constructing a service.
    from .config_commands import _assert_no_symlink_path

    _assert_no_symlink_path(path.parent, allow_missing_final=True)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_no_symlink_path(path, allow_missing_final=True)
    info = path.parent.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ConfigCommandError("Selection directory must be owner-private (chmod 700 ~/.py)")
    if path.exists():
        _read_json_mapping(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".default-model-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"provider.id": provider, "model.name": model}, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def choose_selection(provider: str, auth_file: Path | None) -> tuple[str, str]:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import FuzzyWordCompleter

    from .model_catalog import adapter_for_model, available_models

    choices = list(available_models(auth_file))
    if provider:
        choices = [model for model in choices if adapter_for_model(model) == provider]
    else:
        choices.insert(0, "fake")
    print("Choose a model (type to filter, Tab/↑/↓, Enter; Ctrl-C cancels).")
    print("Use PROVIDER/MODEL for a custom model; fake is a no-network fixture.")
    if len(choices) <= (0 if provider else 1):
        print("No authenticated models found. Log in with `py login PROVIDER` in another terminal,")
        print("or enter a model and configure its credentials before sending a request.")
    session: PromptSession[str] = PromptSession(
        completer=FuzzyWordCompleter(choices, WORD=True), complete_while_typing=True,
    )
    while True:
        value = (await session.prompt_async("Model: ")).strip()
        adapter = provider or ("fake" if value == "fake" else adapter_for_model(value))
        try:
            return validate_selection(adapter, "" if value == "fake" else value)
        except ValueError as exc:
            print(str(exc))


async def prepare_terminal_configuration(args: argparse.Namespace):
    """Explicit selection wins; only terminal startup reads remembered defaults."""
    from .cli import _prepare_configuration

    selected_args = argparse.Namespace(**vars(args))
    if not any(getattr(args, name, None) is not None for name in ("config", "provider", "model")):
        saved = load_selection()
        if saved is not None:
            selected_args.provider, selected_args.model = saved
    try:
        return _prepare_configuration(selected_args)
    except MissingModelSelection as exc:
        provider, model = await choose_selection(exc.provider, getattr(args, "pi_auth", None))
    selected_args.provider = provider
    selected_args.model = model
    configured = _prepare_configuration(selected_args)
    try:
        save_selection(provider, model)
    except (OSError, ValueError) as exc:
        # A failed preference write must not prevent using the chosen model.
        import sys

        from .plain_terminal import sanitize
        print("py: could not remember selection: " + sanitize(str(exc)), file=sys.stderr)
    return configured
