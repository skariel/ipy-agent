"""Validation for bounded archive-read provenance carried on display events."""
from __future__ import annotations

from collections.abc import Mapping


def output_read_reference(metadata: Mapping[str, object]) -> dict[str, int] | None:
    ref = metadata.get("py_agent_output_read")
    if (isinstance(ref, Mapping)
            and set(ref) == {"index", "start", "end", "total"}
            and all(type(value) is int for value in ref.values())
            and 1 <= ref["index"] <= 1_000_000_000
            and 0 <= ref["start"] <= ref["end"] <= ref["total"] <= 2**63 - 1
            and ref["end"] - ref["start"] <= 4000):
        return dict(ref)
    return None
