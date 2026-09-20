"""Read-only pi Codex credentials; never execute key commands or rotate tokens.

Pi remains the owner of OAuth login and refresh. Re-read each generation so a
refresh made by pi is picked up without restarting py. No secrets in repr/errors.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import stat
import time
from dataclasses import dataclass, field

from .provider import ProviderError

MAX_AUTH_BYTES = 1024 * 1024
DEFAULT_AUTH_FILE = Path.home() / ".pi" / "agent" / "auth.json"


@dataclass(frozen=True)
class CodexCredentials:
    access: str = field(repr=False)
    account_id: str = field(repr=False)
    expires: float


def _error(message):
    return ProviderError("Codex authentication: " + message, kind="authentication")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value
    return result


def read_codex_credentials(path: Path | None = None) -> CodexCredentials:
    """Bounded, symlink-safe read of a private auth.json. No writes or networking."""
    path = Path(os.path.abspath(path if path is not None else DEFAULT_AUTH_FILE))
    parent_fd = file_fd = None
    try:
        parent_fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        for component in path.parts[1:-1]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        before = os.fstat(file_fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_mode & 0o077 or before.st_nlink != 1):
            raise _error("auth.json must be an owned regular single-link file with mode 0600 (no symlinks)")
        if before.st_size > MAX_AUTH_BYTES:
            raise _error("auth.json exceeds the 1 MiB read limit")
        with os.fdopen(file_fd, "rb", closefd=False) as stream:
            data = stream.read(MAX_AUTH_BYTES + 1)
        after = os.fstat(file_fd)
        current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if len(data) > MAX_AUTH_BYTES or identity(before) != identity(after) or identity(after) != identity(current):
            raise _error("auth.json changed during reading; retry after pi finishes updating it")
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid number")))
    except ProviderError:
        raise
    except FileNotFoundError:
        raise _error("auth.json not found; log in with pi's /login openai-codex") from None
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise _error("cannot safely read auth.json; check permissions, JSON format and symlinks") from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if parent_fd is not None:
            os.close(parent_fd)

    entry = document.get("openai-codex") if isinstance(document, dict) else None
    if not isinstance(entry, dict) or entry.get("type") != "oauth":
        raise _error("no openai-codex OAuth login; use pi's /login openai-codex")
    access, account_id, expires = entry.get("access"), entry.get("accountId"), entry.get("expires")
    if (not isinstance(access, str) or not 1 <= len(access) <= 32768
            or any(ord(c) < 33 or ord(c) > 126 for c in access)):
        raise _error("invalid access token; log in again through pi")
    if not isinstance(account_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", account_id):
        raise _error("missing or invalid accountId; log in again through pi")
    if type(expires) not in (int, float) or not math.isfinite(expires):
        raise _error("missing or invalid token expiry; log in again through pi")
    if expires <= time.time() * 1000 + 30_000:
        raise _error("pi's token is expired or about to expire. Refresh/login in pi (/login openai-codex), then retry. py reads but never rewrites pi's shared credentials")
    return CodexCredentials(access=access, account_id=account_id, expires=expires)
