"""Lossless pipe-fragment coalescing and bounded, typed observation excerpts.

The byte budget covers the serialized UTF-8 ``events`` list, not the small outer
metadata dictionary. Originals remain in the journal. No source is evaluated.
"""
from __future__ import annotations

from copy import deepcopy
from itertools import groupby
import json


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _size(value) -> int:
    return len(_json(value).encode("utf-8"))


def _coalesce(events: list[dict]) -> list[dict]:
    def key(event):
        stream = event.get("stream")
        if stream in ("stdout", "stderr") and isinstance(event.get("text"), str):
            return stream, event.get("cell_id"), event.get("asynchronous")
        return object()  # Every say/display/other event is a boundary.

    result = []
    for _, run in groupby(events, key):
        pieces = list(run)
        event = deepcopy(pieces[0])
        if len(pieces) > 1:
            event["text"] = "".join(piece["text"] for piece in pieces)
            event["last_id"] = pieces[-1].get("id")
        result.append(event)
    return result


def _fit(event: dict, budget: int) -> dict | None:
    if _size(event) <= budget:
        return event
    # Excerpt the actual content, not a partly serialized output-event envelope.
    field = "text" if isinstance(event.get("text"), str) else "say" if "say" in event else None
    value = event[field] if field else event
    text = value if isinstance(value, str) else _json(value)
    base = {key: val for key, val in event.items() if key != field} if field else {}
    base.update(truncated=True, excerpt_field=field, original_bytes=len(text.encode("utf-8")))
    if not isinstance(value, str):
        base["excerpt_format"] = "json"

    def excerpt(length):
        head, tail = (length + 1) // 2, length // 2
        return {**base, "head": text[:head], "tail": text[-tail:] if tail else ""}

    if _size(excerpt(0)) > budget:
        return None
    # Character boundaries preserve Unicode; encoded JSON measures escapes too.
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if _size(excerpt(middle)) <= budget:
            low = middle
        else:
            high = middle - 1
    return excerpt(low)


def pack_observations(events: list[dict], max_bytes: int) -> dict:
    """Pack stream runs without modifying inputs or inventing a summary.

    ``omitted_events`` counts whole coalesced runs/events not included. A clipped
    event retains its metadata, replaces text/say with raw ``head``/``tail``, and
    records ``excerpt_field``, ``original_bytes`` and ``truncated``. For a nontext
    say value, excerpts are explicitly labelled JSON. The outer ``truncated``
    also reports omitted events. Budgets below two bytes return an empty list.
    """
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes must be a nonnegative integer")
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise TypeError("events must be a list of dictionaries")
    runs = _coalesce(events)
    if _size(runs) <= max_bytes:
        return {"events": runs, "truncated": False, "omitted_events": 0}
    if not runs or max_bytes < 2:
        return {"events": [], "truncated": bool(runs), "omitted_events": len(runs)}

    # Keep both ends when many alternating streams would consume the budget in
    # metadata alone. This selects by position, never by content or meaning.
    count = max(1, max_bytes // 256)
    if len(runs) > count:
        head, tail = (count + 1) // 2, count // 2
        selected = runs[:head] + (runs[-tail:] if tail else [])
    else:
        selected = runs

    # Small events stay whole; distribute their unused share to larger ones.
    sizes = [_size(event) for event in selected]
    budgets = [0] * len(selected)
    remaining = max(0, max_bytes - 2 - (len(selected) - 1))
    for left, index in zip(range(len(selected), 0, -1), sorted(range(len(selected)), key=sizes.__getitem__)):
        budgets[index] = min(sizes[index], remaining // left)
        remaining -= budgets[index]
    packed = [item for event, budget in zip(selected, budgets) if (item := _fit(event, budget)) is not None]
    return {"events": packed, "truncated": True, "omitted_events": len(runs) - len(packed)}
