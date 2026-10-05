"""Bounded, fairly shared previews for one Python cell (not a full-output archive)."""
from __future__ import annotations

from collections.abc import Callable
import sys
import threading


class CellPrinter:
    """Collect at most 32 previews and render at most 6000 characters in total.

    Call like print, with an optional label. Long strings keep their head and
    tail; nonstrings use str(), so conversion itself is not resource bounded.
    The worker creates a fresh instance for each cell and finishes it exactly once.
    Supply ``redact`` at construction to protect text before bounded retention.
    Finish-time redaction is an additional pass, not a substitute for that.
    """

    BUDGET = 6000
    MAX_CALLS = 32
    ITEM_LIMIT = 1000

    def __init__(self, *, redact: Callable[[str], str] = lambda text: text) -> None:
        self._redact = redact
        self._items: list[tuple[str, str]] = []
        self._skipped = 0
        self._active = True
        self._thread = threading.get_ident()

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        marker = "\n… [truncated] …\n"
        if limit <= len(marker):
            return marker[:limit]
        available = limit - len(marker)
        head = (available + 1) // 2
        tail = available // 2
        return text[:head] + marker + (text[-tail:] if tail else "")

    def __call__(self, *values: object, label: str = "", sep: str = " ") -> None:
        if not self._active or threading.get_ident() != self._thread:
            raise RuntimeError("preview() belongs to the active cell's main thread")
        if not isinstance(label, str) or not isinstance(sep, str):
            raise TypeError("preview label and sep must be strings")
        if len(self._items) >= self.MAX_CALLS:
            self._skipped += 1
            return
        frame = sys._getframe(1)
        location = f"line {frame.f_lineno}"
        del frame
        name = self._clip(self._redact(label).replace("\n", " ").replace("\r", " "), 80)
        header = f"[preview {len(self._items) + 1}, {location}" + (f", {name}" if name else "") + "]"
        # Redact before clipping so long passwords cannot become leaked fragments.
        # Bound each conversion's retained text and the join; don't join every
        # argument into an arbitrarily large temporary string.
        sep = self._redact(sep)
        parts = []
        retained = 0
        for value in values:
            part = self._clip(self._redact(str(value)), self.ITEM_LIMIT)
            parts.append(part)
            retained += len(part) + min(len(sep), self.ITEM_LIMIT)
            if retained > self.ITEM_LIMIT:
                parts.append("… [remaining arguments omitted]")
                break
        text = self._clip(sep[:self.ITEM_LIMIT].join(parts), self.ITEM_LIMIT)
        self._items.append((header, text))

    def finish(self, redact: Callable[[str], str] = lambda text: text) -> str:
        """Deactivate and render once; budget includes labels and redaction."""
        if not self._active:
            return ""
        self._active = False
        if not self._items and not self._skipped:
            return ""
        footer = "[preview: bounded excerpts; keep originals in variables]"
        if self._skipped:
            footer += f"\n[preview: {self._skipped} additional calls omitted]"
        footer = self._clip(redact(footer), 300)
        headers = [self._clip(redact(header), 160) for header, _ in self._items]
        remaining = self.BUDGET - len(footer) - 1 - sum(len(h) + 2 for h in headers)
        allowance = max(0, remaining // max(1, len(self._items)))
        chunks = [h + "\n" + self._clip(redact(text), allowance)
                  for h, (_, text) in zip(headers, self._items, strict=True)]
        return "\n".join([*chunks, footer]) + "\n"
