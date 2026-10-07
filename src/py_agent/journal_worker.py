"""Serialized, acknowledged SQLite I/O off the coordinator event loop."""
from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
import threading
from typing import Any

from .async_commit import settle
from .session_journal import JournalError, SQLiteSessionJournal


class SQLiteJournalWorker:
    """Own a connection in one thread; bound admission and acknowledge commits.

    Cancellation waits for the admitted transaction to settle, then propagates.
    No cancelled transaction is replayed. Startup may use synchronous access
    before the event loop runs; runtime recording uses ``call``.
    """
    persisted = True

    def __init__(self, path: str | Path, *, max_pending: int = 64, **options: Any) -> None:
        if type(max_pending) is not int or max_pending < 1:
            raise ValueError("max_pending must be a positive integer")
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="py-agent-journal")
        self._lock = threading.Lock()
        self._pending = 0
        self._max_pending = max_pending
        self._closed = False
        self._sensitive_values: tuple[str, ...] = ()
        try:
            self._journal = self._pool.submit(SQLiteSessionJournal, path, **options).result()
        except BaseException:
            self._pool.shutdown(wait=True)
            raise

    def _submit(self, method: str, *args: Any, **kwargs: Any) -> Future[Any]:
        with self._lock:
            if self._closed:
                raise JournalError("Journal worker is closed")
            if self._pending >= self._max_pending:
                raise JournalError("Journal worker admission limit exceeded")
            self._pending += 1
            try:
                future = self._pool.submit(self._invoke, method, args, kwargs, self._sensitive_values)
            except BaseException:
                self._pending -= 1
                raise
        def released(_: Future[Any]) -> None:
            with self._lock:
                self._pending -= 1
        future.add_done_callback(released)
        return future

    def set_sensitive_values(self, values: tuple[str, ...]) -> None:
        with self._lock:
            if self._closed:
                raise JournalError("Journal worker is closed")
            self._sensitive_values = tuple(values)

    def _invoke(self, method: str, args: tuple[Any, ...], kwargs: dict[str, Any],
                sensitive_values: tuple[str, ...]) -> Any:
        if sensitive_values != self._journal._sensitive_values:
            self._journal.set_sensitive_values(sensitive_values)
        return getattr(self._journal, method)(*args, **kwargs)

    async def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        return await settle(asyncio.wrap_future(self._submit(method, *args, **kwargs)))

    def __getattr__(self, method: str) -> Any:
        # Retain the synchronous journal API for constructor configuration,
        # standalone tooling, and custom frontends.
        if method.startswith("_"):
            raise AttributeError(method)
        target = getattr(self._journal, method)
        if not callable(target):
            return target
        def invoke(*args: Any, **kwargs: Any) -> Any:
            return self._submit(method, *args, **kwargs).result()
        return invoke

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            future = self._pool.submit(self._journal.close)
        try:
            future.result()
        finally:
            self._pool.shutdown(wait=True)

    async def aclose(self) -> None:
        await settle(asyncio.to_thread(self.close))
