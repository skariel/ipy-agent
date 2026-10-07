"""Shared wire limits for the local executor and worker.

Keep transport validation on both sides of the process boundary. Capture and
presentation budgets are separate policies and are deliberately not defined here.
"""
from __future__ import annotations

import json
import math
from typing import Any

PROTOCOL_VERSION = 1
MAX_COMPLETION_MATCHES = 512
MAX_COMPLETION_MATCH_CHARS = 2_048
MAX_ERROR_CHARS = 8_192
MAX_FRAME = 1_048_576
MAX_INSPECTION_CHARS = 16_384
MAX_OUTPUT_FRAME_CHARS = 8_192
MAX_PASSWORD_SECRETS = 128
MAX_PASSWORD_SECRET_CHARS = 1_048_576
MAX_QUERY_CHARS = 65_536
MAX_RICH_OUTPUT_BYTES = 2_097_152
MAX_RICH_OUTPUT_FRAMES = 256
MAX_SAY_MESSAGES = 1_024


class ProtocolError(ValueError):
    """Malformed, oversized, or non-object protocol data."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ProtocolError("Duplicate key in protocol frame")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ProtocolError(f"Invalid JSON number: {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ProtocolError("Invalid JSON number")
    return number


def decode_frame(payload: bytes) -> dict[str, Any]:
    if not 0 < len(payload) <= MAX_FRAME:
        raise ProtocolError("Invalid protocol frame length")
    try:
        frame = json.loads(
            payload.decode("ascii"), object_pairs_hook=_unique_object,
            parse_constant=_reject_constant, parse_float=_finite_float,
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ProtocolError("Malformed protocol frame") from exc
    if not isinstance(frame, dict):
        raise ProtocolError("Protocol frame must be an object")
    return frame


def encode_frame(message: dict[str, Any]) -> bytes:
    if not isinstance(message, dict):
        raise ProtocolError("Protocol frame must be an object")
    try:
        payload = json.dumps(message, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProtocolError("Protocol frame is not valid JSON data") from exc
    if not 0 < len(payload) <= MAX_FRAME:
        raise ProtocolError(f"Protocol frame exceeds the {MAX_FRAME}-byte transport limit")
    return len(payload).to_bytes(4, "big") + payload
