"""Private py credentials; Pi compatibility is read-only."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from .provider import ProviderError


def auth_path() -> Path:
    return Path.home() / ".py" / "auth.json"


def auth_error(message: str) -> ProviderError:
    return ProviderError("py authentication: " + message, kind="authentication")


def provider_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", value):
        raise auth_error("invalid provider identifier")
    return value


def read_document() -> dict:
    from .codex_auth import _read_auth_document
    return _read_auth_document(auth_path(), error=auth_error) or {}


@contextmanager
def locked_store():
    """Owned, no-symlink directory and lock; serialize read/modify/refresh/write."""
    path = auth_path()
    directory = lock = None
    try:
        parent = path.parent
        try:
            parent.mkdir(mode=0o700)
        except FileExistsError:
            pass
        absolute = Path(os.path.abspath(parent))
        directory = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
        for component in absolute.parts[1:]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=directory)
            os.close(directory)
            directory = next_fd
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise auth_error("~/.py must be owned by you with mode 0700")
        lock = os.open("auth.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                       0o600, dir_fd=directory)
        info = os.fstat(lock)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise auth_error("unsafe credential lock file")
        deadline = time.monotonic() + 60
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise auth_error("credential store is busy; retry later") from None
                time.sleep(0.05)
        yield directory
    except OSError:
        raise auth_error("cannot safely access credential storage") from None
    finally:
        if lock is not None:
            os.close(lock)
        if directory is not None:
            os.close(directory)


def write_document(directory: int, document: dict) -> None:
    from .codex_auth import MAX_AUTH_BYTES
    data = json.dumps(document, allow_nan=False).encode()
    if len(data) > MAX_AUTH_BYTES:
        raise auth_error("credential store exceeds size limit")
    temporary = ".auth-" + uuid.uuid4().hex
    fd = None
    created = False
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        created = True
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, "auth.json", src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        if fd is not None:
            os.close(fd)
        if created:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def save(provider: str, entry: dict) -> None:
    provider_id(provider)
    with locked_store() as directory:
        document = read_document()
        document[provider] = entry
        write_document(directory, document)


def logout(provider: str) -> bool:
    provider_id(provider)
    with locked_store() as directory:
        document = read_document()
        present = provider in document
        if present:
            del document[provider]
            write_document(directory, document)
        return present


def api_key_entry(key: str) -> dict:
    if (not isinstance(key, str) or not 1 <= len(key) <= 65536
            or key.startswith("!") or any(ord(c) < 33 or ord(c) > 126 for c in key)):
        raise auth_error("invalid API key (command-backed keys are not supported)")
    return {"type": "api_key", "key": key}


def select_path(provider: str) -> Path:
    from .codex_auth import DEFAULT_AUTH_FILE
    return auth_path() if provider in read_document() else DEFAULT_AUTH_FILE


def codex_credentials():
    from .codex_auth import DEFAULT_AUTH_FILE, read_codex_credentials
    if "openai-codex" not in read_document():
        return read_codex_credentials(DEFAULT_AUTH_FILE)
    with locked_store() as directory:
        document = read_document()
        entry = document.get("openai-codex")
        if not isinstance(entry, dict) or entry.get("type") != "oauth":
            raise auth_error("no Codex OAuth login; use py login openai-codex")
        expires = entry.get("expires")
        import math
        if type(expires) not in (int, float) or not math.isfinite(expires):
            raise auth_error("invalid Codex expiry; use py login openai-codex")
        if expires <= time.time() * 1000 + 30_000:
            from .oauth import refresh_codex
            entry = refresh_codex(entry)
            document["openai-codex"] = entry
            write_document(directory, document)
        return read_codex_credentials(auth_path())
