"""Bounded, non-executing inspection primitives for the Python session."""
from __future__ import annotations

from collections.abc import Mapping
from os import PathLike
from pathlib import Path
import re
from typing import TypedDict

MAX_INSPECTION = 6000


class _TestSummary(TypedDict):
    returncode: int
    summary: str
    failures: list[str]
    output_tail: str
    output_chars: int
    truncated: bool


def source(
    path: str | PathLike[str],
    start: int = 1,
    end: int | None = None,
    *,
    limit: int = 6000,
) -> str:
    """Return numbered UTF-8 source lines. Reads incrementally; never executes."""
    if type(start) is not int or start < 1 or (end is not None and (type(end) is not int or end < start)):
        raise ValueError("source requires positive ordered line numbers")
    if type(limit) is not int or not 128 <= limit <= MAX_INSPECTION:
        raise ValueError("source limit must be 128..6000 characters")
    end = min(end if end is not None else start + 79, start + 199)
    result, used = [], 0
    with Path(path).open("r", encoding="utf-8", errors="replace") as stream:
        # readline(size) prevents a huge/minified line from allocating unbounded
        # text. Skip its remainder in bounded chunks.
        number = 0
        scanned = 0
        while number < end:
            line = stream.readline(limit + 1)
            if not line:
                break
            number += 1
            scanned += len(line)
            if scanned > 8_000_000:
                result.append("[Source scan limit reached.]")
                break
            shortened = not line.endswith("\n") and len(line) > limit
            if shortened:
                while True:
                    chunk = stream.readline(limit + 1)
                    scanned += len(chunk)
                    if not chunk or chunk.endswith("\n") or scanned > 8_000_000:
                        break
            if number < start:
                continue
            rendered = f"{number}: {line.rstrip()}" + (" … [line truncated]" if shortened else "")
            remaining = limit - used - 40
            if len(rendered) > remaining:
                result.append(rendered[:max(0, remaining)] + " … [excerpt truncated]")
                break
            result.append(rendered)
            used += len(rendered) + 1
    return "\n".join(result)[:limit]


def test_summary(result: object, *, limit: int = 4000) -> _TestSummary:
    """Summarize a retained subprocess result; never invokes/reruns tests."""
    if type(limit) is not int or not 128 <= limit <= MAX_INSPECTION:
        raise ValueError("test_summary limit must be 128..6000 characters")
    code: object
    stdout: object
    stderr: object
    if isinstance(result, Mapping):
        code, stdout, stderr = result.get("returncode"), result.get("stdout", ""), result.get("stderr", "")
    else:
        code = getattr(result, "returncode", None)
        stdout, stderr = getattr(result, "stdout", ""), getattr(result, "stderr", "")
    if type(code) is not int or not isinstance(stdout, str) or not isinstance(stderr, str):
        raise TypeError("test_summary requires a completed text-mode subprocess result")
    text = stdout + "\n" + stderr
    failures: list[str] = []
    for match in re.finditer(r"(?m)^(?:FAILED|ERROR) [^\r\n]+", text):
        if len(failures) >= 20:
            break
        failures.append(match.group(0)[:180])
    tail = text[-min(limit // 2, 3000):]
    summary = re.findall(r"(?m)^.*\b(?:passed|failed|errors?|skipped)\b.*$", tail)
    return {"returncode": code, "summary": summary[-1][:400] if summary else "",
            "failures": failures[:max(1, limit // 400)],
            "output_tail": tail, "output_chars": len(text),
            "truncated": len(text) > len(tail)}
