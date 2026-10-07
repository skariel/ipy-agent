"""Secret-free transient activity and explicit recovery checkpoint metadata."""
from __future__ import annotations

from dataclasses import dataclass, field
import time

from .contracts import ContextSnapshot


@dataclass(frozen=True)
class Activity:
    label: str
    attempt: int = 1
    attempts: int = 1
    started: float = field(default_factory=time.monotonic)
    retry_at: float | None = None

    def text(self) -> str:
        elapsed = max(0, int(time.monotonic() - self.started))
        value = f"{self.label} · attempt {self.attempt}/{self.attempts} · {elapsed}s"
        if self.retry_at is not None:
            value += f" · retry in {max(0, self.retry_at - time.monotonic()):.0f}s"
        return value


@dataclass(frozen=True)
class Recovery:
    source: str = field(repr=False)
    context: ContextSnapshot = field(repr=False)
    committed: bool
    executed_cells: int = 0
    model: str = ""
    options: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    def text(self) -> str:
        return (f"Model request failed; no new source accepted. {self.executed_cells} prior cell(s) "
                "already completed. /resume retries only the pending model request; "
                "completed cells are never replayed. /recovery discard clears it.")
