"""Pure output redaction shared by the executor and worker.

Always redact before a lossy operation (clipping, archiving, or slicing).
Process-specific validation and transport remain at their respective boundaries.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def redact_text(text: str, secrets: Sequence[str]) -> str:
    marker = "[REDACTED]"
    if any(secret and secret in marker for secret in secrets):
        marker = ""
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            text = text.replace(secret, marker)
    return text


def redact_json(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, list):
        return [redact_json(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_json(item, secrets) for item in value)
    if isinstance(value, Mapping):
        return {
            redact_text(key, secrets) if isinstance(key, str) else key:
            redact_json(item, secrets)
            for key, item in value.items()
        }
    return value


def strip_partial_secret_suffix(text: str, secrets: Sequence[str]) -> str:
    """Drop a possible secret prefix at the end of a truncated capture."""
    partial = 0
    for secret in secrets:
        for length in range(min(len(secret) - 1, len(text)), partial, -1):
            if text.endswith(secret[:length]):
                partial = length
                break
    return text[:-partial] if partial else text


def safe_capture(text: str, secrets: Sequence[str], *, truncated: bool) -> str:
    if truncated:
        text = strip_partial_secret_suffix(text, secrets)
    return redact_text(text, secrets)
