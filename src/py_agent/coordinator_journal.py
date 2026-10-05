"""Coordinator journal policy."""
from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from .contracts import ModelRequest, ModelResponse
from .coordinator_support import State
from .session_journal import JournalError

if TYPE_CHECKING:
    from .coordinator import Coordinator


class JournalPolicy:
    """Coordinator journal policy; no execution or provider replay."""

    def __init__(self, coordinator: Coordinator) -> None:
        self.coordinator = coordinator
        self._cache_totals = {"input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
        self._cache_complete = {name: True for name in self._cache_totals}
        self._cache_reports = 0

    def _journal_record(self, method: str, *args, **kwargs) -> None:
        if not self.coordinator._conversation._journal_sensitive_config_ready:
            self.coordinator._lifecycle._journal_failed = True
            self.coordinator._set_state_unless_stopping(State.FAILED)
            raise JournalError(
                "Sensitive configuration exceeds journal redaction limits; operation stopped without replay"
            )
        if self.coordinator._lifecycle._journal_failed:
            raise JournalError("Durable journal failed; the session is fail-closed and will not replay work")
        try:
            result = getattr(self.coordinator.journal, method)(*args, **kwargs)
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                raise TypeError("Coordinator journal methods must commit synchronously")
        except Exception as exc:
            self.coordinator._lifecycle._journal_failed = True
            self.coordinator._set_state_unless_stopping(State.FAILED)
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

    def _record_provider_usage(
        self, request: ModelRequest, response: ModelResponse | None, *, outcome: str,
    ) -> None:
        if self.coordinator._lifecycle._active_model_request is request and self.coordinator._lifecycle._active_provider_usage_recorded:
            return
        self.coordinator._journal_record("record_provider_usage", request, response, outcome=outcome)
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
        if self.coordinator._lifecycle._active_model_request is request:
            self.coordinator._lifecycle._active_provider_usage_recorded = True

