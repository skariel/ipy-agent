"""SessionRuntime journal policy."""
from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING

from .async_commit import settle
from .contracts import ModelRequest, ModelResponse
from .coordinator_support import State
from .session_journal import JournalError

if TYPE_CHECKING:
    from .coordinator_runtime import SessionRuntime


class JournalPolicy:
    """SessionRuntime journal policy; no execution or provider replay."""

    def __init__(self, coordinator: SessionRuntime) -> None:
        self.coordinator = coordinator
        self._usage_record_lock = asyncio.Lock()
        self._cache_totals = {"input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
        self._cache_complete = {name: True for name in self._cache_totals}
        self._cache_reports = 0

    async def _journal_record(self, method: str, *args: object, **kwargs: object) -> None:
        if not self.coordinator.conversation._journal_sensitive_config_ready:
            self.coordinator.lifecycle._journal_failed = True
            self.coordinator.lifecycle._set_state_unless_stopping(State.FAILED)
            raise JournalError(
                "Sensitive configuration exceeds journal redaction limits; operation stopped without replay"
            )
        if self.coordinator.lifecycle._journal_failed:
            raise JournalError("Durable journal failed; the session is fail-closed and will not replay work")
        try:
            from .journal_worker import SQLiteJournalWorker
            if isinstance(self.coordinator.journal, SQLiteJournalWorker):
                await self.coordinator.journal.call(method, *args, **kwargs)
                return
            result = getattr(self.coordinator.journal, method)(*args, **kwargs)
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                raise TypeError("SessionRuntime journal methods must commit synchronously")
        except Exception as exc:
            self.coordinator.lifecycle._journal_failed = True
            self.coordinator.lifecycle._set_state_unless_stopping(State.FAILED)
            raise JournalError(
                "Durable journal failed; the operation stopped without replay"
            ) from exc

    @property
    def cache_summary(self) -> tuple[str, str, str]:
        """Session-weighted cache rate and complete reported read/write totals."""
        rate = "?"
        if (self._cache_reports and self._cache_complete["input_tokens"]
                and self._cache_complete["cache_read_tokens"]
                and self._cache_totals["input_tokens"] > 0):
            total = self._cache_totals["input_tokens"]
            rate = str((self._cache_totals["cache_read_tokens"] * 100 + total // 2) // total)
        read = str(self._cache_totals["cache_read_tokens"]) if (
            self._cache_reports and self._cache_complete["cache_read_tokens"]
        ) else "?"
        write = str(self._cache_totals["cache_write_tokens"]) if (
            self._cache_reports and self._cache_complete["cache_write_tokens"]
        ) else "?"
        return rate, read, write

    async def _record_provider_usage(
        self, request: ModelRequest, response: ModelResponse | None, *, outcome: str,
    ) -> None:
        await settle(self._locked_provider_usage(request, response, outcome=outcome))

    async def _locked_provider_usage(
        self, request: ModelRequest, response: ModelResponse | None, *, outcome: str,
    ) -> None:
        async with self._usage_record_lock:
            await self._commit_provider_usage(request, response, outcome=outcome)

    async def _commit_provider_usage(
        self, request: ModelRequest, response: ModelResponse | None, *, outcome: str,
    ) -> None:
        if self.coordinator.lifecycle.operation.model_request is request and self.coordinator.lifecycle.operation.provider_usage_recorded:
            return
        await self.coordinator.journal_policy._journal_record("record_provider_usage", request, response, outcome=outcome)
        if response is not None:
            reported = response.usage.get("normalized", response.usage)
            counters = reported if hasattr(reported, "get") else {}
            self._cache_reports += 1
            for name in self._cache_totals:
                value = counters.get(name)
                if name == "cache_write_tokens" and (type(value) is not int or value < 0):
                    value = counters.get("cache_creation_tokens")
                if type(value) is int and value >= 0:
                    self._cache_totals[name] += value
                else:
                    self._cache_complete[name] = False
        if self.coordinator.lifecycle.operation.model_request is request:
            self.coordinator.lifecycle.operation.provider_usage_recorded = True

