"""Durable, run/agent-scoped evidence with bounded retrieval.

The quota counts serialized event bytes, not SQLite's page/index overhead. The
supervisor must place this database in a sandbox-protected private directory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any


class JournalError(RuntimeError):
    pass


class JournalQuotaExceeded(JournalError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _bound(value: int, maximum: int, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return min(value, maximum)


class Journal:
    MAX_PAGE = 8000
    MAX_RESULTS = 100

    def __init__(self, path: str | Path, run_id: str, agent_id: str = "a1",
                 max_bytes: int = 64 * 1024 * 1024):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
            raise ValueError("invalid run ID")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", agent_id):
            raise ValueError("invalid agent ID")
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.path = Path(path).absolute()
        self.run_id, self.agent_id, self.max_bytes = run_id, agent_id, max_bytes
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for part in [self.path, *self.path.parents]:
            if part.is_symlink():
                raise JournalError("journal path must not contain symlinks")
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                event_id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
                cell_id TEXT, payload TEXT NOT NULL, search_text TEXT NOT NULL,
                size INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_scope ON events(run_id, agent_id, seq);
            CREATE INDEX IF NOT EXISTS events_cells ON events(run_id, agent_id, cell_id);
            CREATE TRIGGER IF NOT EXISTS immutable_update BEFORE UPDATE ON events
                BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_delete BEFORE DELETE ON events
                BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
        """)
        runs = self.db.execute("SELECT DISTINCT run_id FROM events").fetchall()
        if runs and runs != [(run_id,)]:
            self.db.close()
            raise JournalError("journal belongs to a different run")

    def append_emergency(self, message: str) -> dict:
        """One bounded termination record from a separate 4 KiB reserve.

        Normal quota accounts for event payloads, not physical SQLite overhead.
        This does not make ENOSPC survivable: the caller must notify the UI if the
        underlying disk cannot accept even the emergency record.
        """
        if self.db.execute("SELECT 1 FROM events WHERE kind='fatal_limit' LIMIT 1").fetchone():
            raise JournalQuotaExceeded("Emergency journal reserve already used")
        original = self.max_bytes
        try:
            self.max_bytes += 4096
            return self.append("fatal_limit", message[:512])
        finally:
            self.max_bytes = original

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def append(self, kind: str, content: Any, cell_id: str | None = None,
               **metadata: Any) -> dict[str, Any]:
        if not isinstance(kind, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", kind):
            raise ValueError("invalid event kind")
        if cell_id is not None and (not isinstance(cell_id, str) or
                                   not cell_id.startswith(self.agent_id + ":")):
            raise ValueError("cell ID is outside this agent")
        reserved = {"id", "seq", "kind", "content", "cell_id", "run_id", "agent_id", "timestamp"}
        if reserved.intersection(metadata):
            raise ValueError("metadata overrides authoritative fields")
        search_text = content if isinstance(content, str) else _json(content)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            seq = self.db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events").fetchone()[0]
            event = {"id": f"{self.agent_id}:e{seq:08d}", "seq": seq,
                     "run_id": self.run_id, "agent_id": self.agent_id,
                     "timestamp": time.time(), "kind": kind, "cell_id": cell_id,
                     "content": content, **metadata}
            payload = _json(event)
            size = len(payload.encode("utf-8")) + len(search_text.encode("utf-8"))
            used = self.db.execute("SELECT COALESCE(SUM(size), 0) FROM events").fetchone()[0]
            if used + size > self.max_bytes:
                raise JournalQuotaExceeded("journal quota exhausted; stop before accepting more evidence")
            self.db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (seq, self.run_id, self.agent_id, event["id"], kind,
                             cell_id, payload, search_text, size))
            self.db.execute("COMMIT")
            return event
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def commit_epoch(self, retained: list[str], evicted: list[str],
                     pending: list[str], *, epoch_id: str | None = None,
                     **metadata: Any) -> dict[str, Any]:
        """Commit the retained conversation window as one indivisible event.

        The caller owns context-epoch identity and serializes this operation with
        dispatch/steering. Variable contents are not persisted; metadata may
        include the worker-reported count frozen into the epoch's system prompt.
        """
        return self.append("epoch_commit", {"retained": retained, "evicted": evicted,
                           "pending": pending, "epoch_id": epoch_id}, **metadata)

    def recent(self, n: int = 10) -> list[dict[str, Any]]:
        n = _bound(n, self.MAX_RESULTS, "n")
        rows = self.db.execute(
            "SELECT event_id,kind,cell_id,search_text FROM events "
            "WHERE run_id=? AND agent_id=? ORDER BY seq DESC LIMIT ?",
            (self.run_id, self.agent_id, n)).fetchall()
        return [{"id": eid, "kind": kind, "cell_id": cell,
                 "excerpt": text[:240], "truncated": len(text) > 240}
                for eid, kind, cell, text in rows]

    def search(self, query: str, *, kind: str | None = None,
               limit: int = 20) -> list[dict[str, Any]]:
        if not isinstance(query, str) or len(query) > 1000:
            raise ValueError("query must be text of at most 1000 characters")
        limit = _bound(limit, 50, "limit")
        if kind is not None and (not isinstance(kind, str) or len(kind) > 100):
            raise ValueError("invalid kind")
        # Python casefold provides consistent Unicode matching across SQLite builds.
        rows = self.db.execute(
            "SELECT event_id,kind,cell_id,search_text FROM events WHERE run_id=? AND agent_id=? "
            + ("AND kind=? " if kind is not None else "AND kind NOT LIKE 'retrieval%' ")
            + "ORDER BY seq DESC", (self.run_id, self.agent_id, kind) if kind is not None
            else (self.run_id, self.agent_id))
        result = []
        needle = query.casefold()
        for eid, event_kind, cell, text in rows:
            if len(result) >= limit:
                break
            index = text.casefold().find(needle)
            if index < 0:
                continue
            # Case folding can change character offsets; excerpts are illustrative,
            # and exact original evidence is available through read().
            start = max(0, index - 80)
            result.append({"id": eid, "kind": event_kind, "cell_id": cell,
                           "excerpt": text[start:start + 400], "offset": start})
        return result

    def read(self, event_or_cell_id: str, *, offset: int = 0,
             limit: int = 8000) -> dict[str, Any]:
        if not isinstance(event_or_cell_id, str) or len(event_or_cell_id) > 200:
            raise ValueError("invalid history ID")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        limit = _bound(limit, self.MAX_PAGE, "limit")
        rows = self.db.execute(
            "SELECT payload FROM events WHERE run_id=? AND agent_id=? "
            "AND (event_id=? OR cell_id=?) ORDER BY seq",
            (self.run_id, self.agent_id, event_or_cell_id, event_or_cell_id)).fetchall()
        if not rows:
            raise KeyError(event_or_cell_id)
        text = "\n".join(row[0] for row in rows)
        page = text[offset:offset + limit]
        next_offset = min(len(text), offset + len(page))
        return {"id": event_or_cell_id, "offset": offset, "content": page,
                "next_offset": next_offset, "total_chars": len(text),
                "truncated": next_offset < len(text)}
