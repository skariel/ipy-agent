"""Durable history for the NEW coordinator.

The SQLite implementation is append-only, private by default, and commits every
required event synchronously before its side effect is dispatched. It never
stores provider adapter metadata, auth headers, or credential objects. Common
credential forms, secret-named fields, and schema-sensitive config values supplied
by the coordinator are redacted before commit. Arbitrary secrets in natural-language
prompts or source code cannot be identified reliably. Prompts,
context, dispatched code, and output are private data: protect the journal file,
backups, and exports accordingly. A journal is history, not session replay or a
Python namespace snapshot.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from collections.abc import Mapping
from typing import Protocol

from .contracts import ExecutionRequest, ExecutionResult, ModelRequest, ModelResponse, Origin


class JournalError(RuntimeError):
    """A selected durable journal could not commit or read a record."""


MAX_EVENT_BYTES = 8 * 1024 * 1024
MAX_STREAM_CHARS = 65_536
MAX_ERROR_CHARS = 8_192
MAX_SAY_OUTPUTS = 64
MAX_SAY_CHARS = 8_192
MAX_HISTORY_PAGE_CHARS = 64_000
MAX_HISTORY_SEARCH_QUERY_CHARS = 256
MAX_HISTORY_SEARCH_SCAN = 1_000
MAX_HISTORY_SEARCH_BYTES = MAX_EVENT_BYTES

_SENSITIVE_KEY_NAMES = frozenset({
    "authorization", "proxyauthorization", "wwwauthentication", "headers", "cookies",
    "cookie", "setcookie", "apikey", "accesstoken", "refreshtoken", "idtoken",
    "password", "passwd", "secret", "clientsecret", "credential", "credentials", "auth", "oauth",
})
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"api[_-]?secret|private[_-]?key|password|passphrase|passwd|authorization|credential|token)"
    r"\b\s*[:=]\s*)"
    r"(?:(\"|')([^\"']*)(\2)|([^\s,;}&]+))"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_URL_AUTH = re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@")
_COMMON_TOKENS = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{16,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,})\b"
)


def _valid_count(value: int, name: str, *, maximum: int | None = None) -> int:
    if type(value) is not int or value < 0 or (maximum is not None and value > maximum):
        suffix = f" no greater than {maximum}" if maximum is not None else ""
        raise ValueError(f"{name} must be a non-negative integer{suffix}")
    return value


def _redact_text(value: str, sensitive_pattern: re.Pattern[str] | None = None) -> str:
    value = _URL_AUTH.sub(r"\1[REDACTED]@", value)
    value = _BEARER.sub(lambda match: match.group(1) + " [REDACTED]", value)
    value = _SECRET_ASSIGNMENT.sub(
        lambda match: match.group(1) + (match.group(2) + "[REDACTED]" + match.group(4)
                                        if match.group(2) else "[REDACTED]"),
        value,
    )
    value = _COMMON_TOKENS.sub("[REDACTED]", value)
    return sensitive_pattern.sub("[REDACTED]", value) if sensitive_pattern is not None else value


def _sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return (
        normalized in _SENSITIVE_KEY_NAMES
        or any(part in normalized for part in ("header", "cookie", "secret", "credential", "password"))
        or (normalized.startswith("auth") and not normalized.startswith("author"))
        or normalized.endswith((
            "apikey", "accesstoken", "refreshtoken", "idtoken", "sessiontoken",
            "bearertoken", "privatekey", "clientkey", "token",
        ))
    )


def _clean_json(
    value: object, *, depth: int = 0, sensitive_pattern: re.Pattern[str] | None = None,
) -> object:
    """Copy finite JSON data, scrub common/configured secrets, and reject opaque objects."""
    if depth > 64:
        raise JournalError("Journal record exceeds the supported nesting depth")
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise JournalError("Journal record contains a non-finite number")
        return value
    if isinstance(value, str):
        return _redact_text(value, sensitive_pattern)
    if isinstance(value, (list, tuple)):
        return [
            _clean_json(item, depth=depth + 1, sensitive_pattern=sensitive_pattern)
            for item in value
        ]
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise JournalError("Journal object keys must be text")
            safe_key = _redact_text(key, sensitive_pattern)
            result[safe_key] = "[REDACTED]" if _sensitive_key(key) else _clean_json(
                item, depth=depth + 1, sensitive_pattern=sensitive_pattern,
            )
        return result
    raise JournalError("Journal records accept JSON data only; credentials and opaque objects are not stored")


def _json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise JournalError("Journal record is not finite JSON data") from exc


def _origin_payload(origin: Origin) -> dict[str, object]:
    if not isinstance(origin, Origin):
        raise JournalError("Journal event requires a valid operation origin")
    if type(origin.config_revision) is not int or origin.config_revision < 0:
        raise JournalError("Journal event has an invalid configuration revision")
    identities = {
        "session_id": origin.session_id,
        "request_id": origin.request_id,
        "frontend_id": origin.frontend_id,
        "generation_id": origin.generation_id,
        "execution_id": origin.execution_id,
    }
    for name, value in identities.items():
        required = name in {"session_id", "request_id", "frontend_id"}
        if ((required and (not isinstance(value, str) or not value))
                or (value is not None and (
                    not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value
                ))):
            raise JournalError(f"Journal event has an invalid {name}")
    return {
        "session_id": _redact_text(origin.session_id),
        "request_id": _redact_text(origin.request_id),
        "frontend_id": _redact_text(origin.frontend_id),
        "generation_id": _redact_text(origin.generation_id) if origin.generation_id is not None else None,
        "execution_id": _redact_text(origin.execution_id) if origin.execution_id is not None else None,
        "config_revision": origin.config_revision,
    }


def _bounded_text(
    value: str | None, maximum: int, sensitive_pattern: re.Pattern[str] | None = None,
) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise JournalError("Journal stream content must be text")
    cleaned = _redact_text(value, sensitive_pattern)
    return {
        "text": cleaned[:maximum],
        "original_chars": len(cleaned),
        "truncated": len(cleaned) > maximum,
        "redacted": cleaned != value,
    }


class JournalService(Protocol):
    """Synchronous commit and bounded history-read interface owned by the coordinator.

    The coordinator installs schema-sensitive values before the first commit; a
    selected journal must apply them to every persisted text field.
    """

    persisted: bool

    def set_sensitive_values(self, values: tuple[str, ...]) -> None: ...
    def start(self, session_id: str, config_revision: int, provider: str, model: str) -> None: ...
    def record_model_request(self, request: ModelRequest) -> None: ...
    def record_provider_usage(
        self, request: ModelRequest, response: ModelResponse | None, *, outcome: str,
    ) -> None: ...
    def record_context_collapse(
        self, request: ModelRequest, source: str, *, outcome: str, detail: str = "",
    ) -> None: ...
    def record_execution_source(self, request: ExecutionRequest) -> None: ...
    def record_execution_result(self, request: ExecutionRequest, result: ExecutionResult) -> None: ...
    def record_uncertain_execution(self, request: ExecutionRequest, reason: str) -> None: ...
    def recent(self, session_id: str, limit: int = 10) -> list[dict[str, object]]: ...
    def search(
        self, session_id: str, query: str, *, kind: str | None = None, limit: int = 20,
        scan_limit: int = MAX_HISTORY_SEARCH_SCAN,
    ) -> list[dict[str, object]]: ...
    def read(
        self, session_id: str, event_id: str, *, offset: int = 0,
        limit: int = MAX_HISTORY_PAGE_CHARS,
    ) -> dict[str, object]: ...
    def end(self, session_id: str, config_revision: int, state: str) -> None: ...
    def close(self) -> None: ...


class NoPersistenceJournal:
    """Explicitly selected no-persistence implementation; records are intentionally discarded."""

    persisted = False

    def set_sensitive_values(self, values: tuple[str, ...]) -> None:
        return None

    def start(self, session_id: str, config_revision: int, provider: str, model: str) -> None:
        return None

    def record_model_request(self, request: ModelRequest) -> None:
        return None

    def record_provider_usage(
        self, request: ModelRequest, response: ModelResponse | None, *, outcome: str,
    ) -> None:
        return None

    def record_context_collapse(
        self, request: ModelRequest, source: str, *, outcome: str, detail: str = "",
    ) -> None:
        return None

    def record_execution_source(self, request: ExecutionRequest) -> None:
        return None

    def record_execution_result(self, request: ExecutionRequest, result: ExecutionResult) -> None:
        return None

    def record_uncertain_execution(self, request: ExecutionRequest, reason: str) -> None:
        return None

    def recent(self, session_id: str, limit: int = 10) -> list[dict[str, object]]:
        return []

    def search(
        self, session_id: str, query: str, *, kind: str | None = None, limit: int = 20,
        scan_limit: int = MAX_HISTORY_SEARCH_SCAN,
    ) -> list[dict[str, object]]:
        return []

    def read(
        self, session_id: str, event_id: str, *, offset: int = 0,
        limit: int = MAX_HISTORY_PAGE_CHARS,
    ) -> dict[str, object]:
        raise KeyError(event_id)

    def end(self, session_id: str, config_revision: int, state: str) -> None:
        return None

    def close(self) -> None:
        return None


class SQLiteSessionJournal:
    """Owner-private append-only SQLite history for coordinator sessions.

    Model requests and dispatched source are rejected rather than truncated if a
    record exceeds ``max_event_bytes``. Execution streams and displays are
    bounded with explicit original lengths and truncation markers.
    """

    persisted = True

    def __init__(
        self,
        path: str | Path,
        *,
        max_event_bytes: int = MAX_EVENT_BYTES,
        max_stream_chars: int = MAX_STREAM_CHARS,
        max_say_outputs: int = MAX_SAY_OUTPUTS,
    ):
        if type(max_event_bytes) is not int or not 1 <= max_event_bytes <= MAX_EVENT_BYTES:
            raise ValueError(f"max_event_bytes must be from 1 to {MAX_EVENT_BYTES}")
        if type(max_stream_chars) is not int or not 1 <= max_stream_chars <= MAX_STREAM_CHARS:
            raise ValueError(f"max_stream_chars must be from 1 to {MAX_STREAM_CHARS}")
        if type(max_say_outputs) is not int or not 1 <= max_say_outputs <= MAX_SAY_OUTPUTS:
            raise ValueError(f"max_say_outputs must be from 1 to {MAX_SAY_OUTPUTS}")
        self.max_event_bytes = max_event_bytes
        self.max_stream_chars = max_stream_chars
        self.max_say_outputs = max_say_outputs
        self.path = Path(os.path.abspath(Path(path).expanduser()))
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for part in (self.path, *self.path.parents):
            if part.is_symlink():
                raise JournalError("Journal path must not contain symlinks")
        parent_info = self.path.parent.stat()
        if (not stat.S_ISDIR(parent_info.st_mode)
                or parent_info.st_mode & 0o077
                or (hasattr(os, "getuid") and parent_info.st_uid != os.getuid())):
            raise JournalError("Journal parent must be an owned private directory (mode 700)")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise JournalError("Unable to securely open journal file") from exc
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
                raise JournalError("Journal must be an owned regular file without hard links")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)

        try:
            self.db = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
            self.db.execute("PRAGMA busy_timeout=5000")
            self.db.execute("PRAGMA journal_mode=DELETE")
            self.db.execute("PRAGMA synchronous=FULL")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            tables = self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if version not in (0, 1) or (version == 0 and tables):
                raise JournalError("Unsupported or non-empty journal database")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY,
                    event_id TEXT UNIQUE NOT NULL,
                    session_id TEXT NOT NULL,
                    request_id TEXT,
                    kind TEXT NOT NULL,
                    config_revision INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    payload TEXT NOT NULL,
                    search_text TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_session_seq ON events(session_id, seq);
                CREATE INDEX IF NOT EXISTS events_request ON events(session_id, request_id, seq);
                CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                    BEGIN SELECT RAISE(ABORT, 'journal events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                    BEGIN SELECT RAISE(ABORT, 'journal events are append-only'); END;
            """)
            self.db.execute("PRAGMA user_version=1")
        except (sqlite3.Error, OSError, JournalError) as exc:
            db = getattr(self, "db", None)
            if db is not None:
                db.close()
            if isinstance(exc, JournalError):
                raise
            raise JournalError("Unable to initialize SQLite session journal") from exc
        self._closed = False
        self._sessions_started: set[str] = set()
        self._sensitive_values: tuple[str, ...] = ()
        self._sensitive_pattern: re.Pattern[str] | None = None

    def set_sensitive_values(self, values: tuple[str, ...]) -> None:
        if self._closed:
            raise JournalError("Journal is closed")
        if isinstance(values, (str, bytes)):
            raise JournalError("Sensitive configuration values must be a sequence")
        try:
            selected = tuple(values)
        except TypeError:
            raise JournalError("Sensitive configuration values must be a sequence") from None
        if (len(selected) > 256
                or any(not isinstance(value, str) or not value for value in selected)
                or sum(len(value) for value in selected) > 65_536
                or any(len(value) > 8_192 for value in selected)):
            raise JournalError("Sensitive configuration exceeds the journal redaction limits")
        if len(selected) != len(set(selected)):
            selected = tuple(dict.fromkeys(selected))
        patterns = set()
        for value in selected:
            patterns.add(value)
            escaped = json.dumps(value, ensure_ascii=False)[1:-1]
            if escaped:
                patterns.add(escaped)
        pattern = None
        if patterns:
            pattern = re.compile(
                "|".join(re.escape(item) for item in sorted(patterns, key=len, reverse=True)),
                re.IGNORECASE,
            )
        self._sensitive_values = selected
        self._sensitive_pattern = pattern

    def _append(
        self,
        kind: str,
        session_id: str,
        request_id: str | None,
        config_revision: int,
        content: object,
        **metadata: object,
    ) -> dict[str, object]:
        if self._closed:
            raise JournalError("Journal is closed")
        if (not isinstance(kind, str) or not kind or len(kind) > 100
                or not isinstance(session_id, str) or not session_id or len(session_id) > 512
                or (request_id is not None and (not isinstance(request_id, str) or len(request_id) > 512))
                or type(config_revision) is not int or config_revision < 0):
            raise JournalError("Invalid journal event identity")
        cleaned = _clean_json(content, sensitive_pattern=self._sensitive_pattern)
        event = {
            "kind": kind,
            "session_id": _redact_text(session_id, self._sensitive_pattern),
            "request_id": _redact_text(request_id, self._sensitive_pattern) if request_id is not None else None,
            "config_revision": config_revision,
            "content": cleaned,
        }
        if metadata:
            event["metadata"] = _clean_json(metadata, sensitive_pattern=self._sensitive_pattern)
        created_at = time.time()
        if not isinstance(created_at, (float, int)):
            raise JournalError("Invalid journal timestamp")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            seq = self.db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events").fetchone()[0]
            event_id = f"e{seq:012d}"
            event = {"id": event_id, "seq": seq, "timestamp": created_at, **event}
            payload = _json(event)
            if len(payload.encode("utf-8")) > self.max_event_bytes:
                raise JournalError(f"Journal event exceeds the {self.max_event_bytes}-byte record limit")
            search_text = payload
            self.db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (seq, event_id, event["session_id"], event["request_id"], kind,
                 config_revision, created_at, payload, search_text),
            )
            self.db.execute("COMMIT")
            return event
        except BaseException as exc:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, JournalError):
                raise
            raise JournalError("Unable to commit required journal event") from exc

    @staticmethod
    def _request_identity(request: ModelRequest) -> dict[str, object]:
        identity = _origin_payload(request.origin)
        if identity["generation_id"] is None:
            raise JournalError("Model request is missing its generation identity")
        return identity

    def start(self, session_id: str, config_revision: int, provider: str, model: str) -> None:
        if not isinstance(provider, str) or not isinstance(model, str):
            raise JournalError("Journal session requires text provider and model identifiers")
        if session_id in self._sessions_started:
            raise JournalError("Journal session was already started")
        self._append(
            "session_start", session_id, None, config_revision,
            {"provider": _redact_text(provider), "model": _redact_text(model)},
        )
        self._sessions_started.add(session_id)

    def record_model_request(self, request: ModelRequest) -> None:
        identity = self._request_identity(request)
        content = {
            "model": request.model,
            "options": dict(request.options),
            "context": {
                "epoch": request.context.epoch,
                "messages": [list(message) for message in request.context.messages],
                "message_phases": list(request.context.message_phases),
                "images": [{"message_index": i, **image.record()}
                           for i, image in request.context.images],
                "transform_trace": list(request.context.transform_trace),
            },
            "transform_trace": list(request.transform_trace),
        }
        self._append(
            "model_request", identity["session_id"], identity["request_id"],
            identity["config_revision"], content,
            frontend_id=identity["frontend_id"], generation_id=identity["generation_id"],
        )

    def record_provider_usage(
        self,
        request: ModelRequest,
        response: ModelResponse | None,
        *,
        outcome: str,
    ) -> None:
        identity = self._request_identity(request)
        if outcome not in {"returned", "failed", "cancelled"}:
            raise JournalError("Invalid provider outcome")
        content: dict[str, object] = {"outcome": outcome, "usage": None}
        if response is not None:
            content.update({
                "usage": dict(response.usage),
                "provider_id": response.provider_id,
                "model": response.model,
                "finish_status": response.finish_status,
                "phase": response.phase,
            })
        self._append(
            "provider_usage", identity["session_id"], identity["request_id"],
            identity["config_revision"], content,
            frontend_id=identity["frontend_id"], generation_id=identity["generation_id"],
        )

    def record_context_collapse(
        self, request: ModelRequest, source: str, *, outcome: str, detail: str = "",
    ) -> None:
        """Audit coordinator control cells without claiming Python execution.

        Keep the original call even when active context contains only a receipt.
        Like dispatched source, oversized records are rejected, never truncated.
        """
        identity = self._request_identity(request)
        if outcome not in {"requested", "succeeded", "rejected"}:
            raise JournalError("Invalid context collapse outcome")
        if not isinstance(source, str) or not isinstance(detail, str):
            raise JournalError("Context collapse source and detail must be text")
        self._append(
            "context_collapse", identity["session_id"], identity["request_id"],
            identity["config_revision"],
            {"source": source, "outcome": outcome, "detail": detail},
            frontend_id=identity["frontend_id"], generation_id=identity["generation_id"],
        )

    def record_execution_source(self, request: ExecutionRequest) -> None:
        identity = _origin_payload(request.origin)
        if request.author not in {"user", "agent"}:
            raise JournalError("Invalid execution author")
        if not isinstance(request.source, str):
            raise JournalError("Dispatched execution source must be text")
        self._append(
            "execution_source", identity["session_id"], identity["request_id"],
            identity["config_revision"], {"source": request.source},
            author=request.author,
            language=request.language,
            frontend_id=identity["frontend_id"],
            generation_id=identity["generation_id"],
            execution_id=identity["execution_id"],
        )

    def record_execution_result(self, request: ExecutionRequest, result: ExecutionResult) -> None:
        identity = _origin_payload(request.origin)
        outputs = []
        for output in result.say_outputs[:self.max_say_outputs]:
            cleaned = _clean_json(output.content, sensitive_pattern=self._sensitive_pattern)
            encoded = _json(cleaned)
            if len(encoded) > MAX_SAY_CHARS:
                output_content: object = {
                    "json_prefix": encoded[:MAX_SAY_CHARS],
                    "original_chars": len(encoded),
                    "truncated": True,
                }
            else:
                output_content = cleaned
            outputs.append({"content": output_content, "final": output.final})
        content = {
            "status": result.status,
            "error": _bounded_text(result.error, MAX_ERROR_CHARS, self._sensitive_pattern),
            "stdout": _bounded_text(result.stdout, self.max_stream_chars, self._sensitive_pattern),
            "stderr": _bounded_text(result.stderr, self.max_stream_chars, self._sensitive_pattern),
            "say_outputs": outputs,
            "say_outputs_omitted": max(0, len(result.say_outputs) - len(outputs)),
            "final": result.final,
        }
        self._append(
            "execution_result", identity["session_id"], identity["request_id"],
            identity["config_revision"], content,
            author=request.author,
            language=request.language,
            frontend_id=identity["frontend_id"],
            generation_id=identity["generation_id"],
            execution_id=identity["execution_id"],
        )

    def record_uncertain_execution(self, request: ExecutionRequest, reason: str) -> None:
        """Record that an invoked executor did not yield a trustworthy result."""
        identity = _origin_payload(request.origin)
        self._append(
            "execution_result", identity["session_id"], identity["request_id"],
            identity["config_revision"], {
                "status": "uncertain",
                "error": _bounded_text(reason, MAX_ERROR_CHARS, self._sensitive_pattern),
                "stdout": None,
                "stderr": None,
                "say_outputs": [],
                "say_outputs_omitted": 0,
                "final": False,
            },
            author=request.author,
            language=request.language,
            frontend_id=identity["frontend_id"],
            generation_id=identity["generation_id"],
            execution_id=identity["execution_id"],
        )

    def end(self, session_id: str, config_revision: int, state: str) -> None:
        if session_id in self._sessions_started:
            self._append(
                "session_end", session_id, None, config_revision,
                {"state": _redact_text(state)},
            )
            self._sessions_started.remove(session_id)

    def recent(self, session_id: str, limit: int = 10) -> list[dict[str, object]]:
        limit = _valid_count(limit, "limit", maximum=10_000)
        if not isinstance(session_id, str) or not session_id or len(session_id) > 512 or "\x00" in session_id:
            raise ValueError("session_id must be nonempty bounded text")
        try:
            rows = self.db.execute(
                "SELECT event_id, request_id, kind, search_text FROM events "
                "WHERE session_id=? ORDER BY seq DESC LIMIT ?",
                (_redact_text(session_id), limit),
            ).fetchall()
        except sqlite3.Error as exc:
            raise JournalError("Unable to read journal history") from exc
        return [
            {"id": event_id, "request_id": request_id, "kind": kind,
             "excerpt": text[:240], "truncated": len(text) > 240}
            for event_id, request_id, kind, text in rows
        ]

    def search(
        self,
        session_id: str,
        query: str,
        *,
        kind: str | None = None,
        limit: int = 20,
        scan_limit: int = MAX_HISTORY_SEARCH_SCAN,
    ) -> list[dict[str, object]]:
        if (not isinstance(query, str) or not query.strip() or "\x00" in query
                or len(query) > MAX_HISTORY_SEARCH_QUERY_CHARS):
            raise ValueError(
                f"query must contain 1 to {MAX_HISTORY_SEARCH_QUERY_CHARS} characters"
            )
        if kind is not None and (not isinstance(kind, str) or not kind or len(kind) > 100 or "\x00" in kind):
            raise ValueError("kind must be bounded text or None")
        if not isinstance(session_id, str) or not session_id or len(session_id) > 512 or "\x00" in session_id:
            raise ValueError("session_id must be nonempty bounded text")
        limit = _valid_count(limit, "limit", maximum=10_000)
        scan_limit = _valid_count(scan_limit, "scan_limit", maximum=MAX_HISTORY_SEARCH_SCAN)
        try:
            rows = self.db.execute(
                "SELECT event_id, request_id, kind, length(CAST(search_text AS BLOB)), search_text "
                "FROM events WHERE session_id=? "
                + ("AND kind=? " if kind is not None else "")
                + "ORDER BY seq DESC LIMIT ?",
                (_redact_text(session_id), kind, scan_limit)
                if kind is not None else (_redact_text(session_id), scan_limit),
            )
            result = []
            scanned_bytes = 0
            needle = _redact_text(query).casefold()
            for event_id, request_id, event_kind, payload_bytes, text in rows:
                if len(result) >= limit:
                    break
                if scanned_bytes + payload_bytes > MAX_HISTORY_SEARCH_BYTES:
                    break
                scanned_bytes += payload_bytes
                index = text.casefold().find(needle)
                if index >= 0:
                    start = max(0, index - 80)
                    result.append({
                        "id": event_id, "request_id": request_id, "kind": event_kind,
                        "excerpt": text[start:start + 400], "offset": start,
                    })
            return result
        except sqlite3.Error as exc:
            raise JournalError("Unable to search journal history") from exc

    def read(
        self,
        session_id: str,
        event_id: str,
        *,
        offset: int = 0,
        limit: int = MAX_HISTORY_PAGE_CHARS,
    ) -> dict[str, object]:
        if not isinstance(session_id, str) or not session_id or len(session_id) > 512 or "\x00" in session_id:
            raise ValueError("session_id must be nonempty bounded text")
        if not isinstance(event_id, str) or not event_id or len(event_id) > 128 or "\x00" in event_id:
            raise ValueError("event_id must be nonempty bounded text")
        offset = _valid_count(offset, "offset", maximum=MAX_EVENT_BYTES)
        limit = _valid_count(limit, "limit", maximum=MAX_HISTORY_PAGE_CHARS)
        try:
            row = self.db.execute(
                "SELECT payload FROM events WHERE session_id=? AND event_id=?",
                (_redact_text(session_id), event_id),
            ).fetchone()
        except sqlite3.Error as exc:
            raise JournalError("Unable to read journal history") from exc
        if row is None:
            raise KeyError(event_id)
        text = row[0]
        page = text[offset:offset + limit]
        next_offset = offset + len(page)
        return {
            "id": event_id,
            "offset": offset,
            "content": page,
            "next_offset": next_offset,
            "total_chars": len(text),
            "truncated": next_offset < len(text),
        }

    def close(self) -> None:
        if not getattr(self, "_closed", True):
            try:
                self.db.close()
            except sqlite3.Error as exc:
                raise JournalError("Unable to close SQLite session journal") from exc
            finally:
                self._closed = True

    def __enter__(self) -> SQLiteSessionJournal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
