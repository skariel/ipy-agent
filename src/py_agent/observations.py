"""Coalesce adjacent stream fragments before the model-facing size check."""

from __future__ import annotations

from copy import deepcopy
from itertools import groupby
from typing import Any


def pack_observations(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Preserve every event and its order without aliasing the input.

    Only adjacent stdout/stderr fragments with matching origins are merged.
    Displays and say events remain boundaries. Historical truncation fields stay
    explicit and false so journal/context readers need no special new envelope.
    """
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise TypeError("events must be a list of dictionaries")

    def key(event: dict[str, Any]) -> object:
        stream = event.get("stream")
        if stream in ("stdout", "stderr") and isinstance(event.get("text"), str):
            return stream, event.get("cell_id"), event.get("asynchronous")
        return object()

    result: list[dict[str, Any]] = []
    for _, run in groupby(events, key):
        pieces = list(run)
        event = deepcopy(pieces[0])
        if len(pieces) > 1:
            event["text"] = "".join(piece["text"] for piece in pieces)
            event["last_id"] = pieces[-1].get("id")
        result.append(event)
    return {"events": result, "truncated": False, "omitted_events": 0}
