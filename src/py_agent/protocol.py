"""Bounded, versioned JSON-line transport shared by host and sandbox worker.

Validation is structural, not authentication. Worker frames are untrusted even
when they pass it; host callers must also enforce correlation, scope and quotas.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import weakref
from typing import Any, BinaryIO

VERSION = 1
MAX_FRAME_BYTES = 65536  # includes terminating LF
MAX_NAMESPACE_ROWS = 32
MAX_NAMESPACE_NAME_CHARS = 64
MAX_NAMESPACE_TYPE_CHARS = 40
MAX_NAMESPACE_BYTES = 2000
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}\Z")
HOST_TYPES = frozenset({"execute", "broker_response"})
WORKER_TYPES = frozenset({"ready", "output", "say", "broker_request", "cell_end"})


class ProtocolError(ValueError):
    pass


def _integer(value: Any, *, positive: bool = False) -> bool:
    return type(value) is int and (value > 0 if positive else value >= 0)


def _id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def _keys(frame: dict, required: set[str], optional: set[str] = frozenset()) -> None:
    if not required <= frame.keys() or frame.keys() - required - optional:
        raise ProtocolError("Missing or unexpected frame fields")


def validate_namespace_summary(value: Any) -> None:
    """A bounded names/types inventory, never serialized values or reprs."""
    if value is None:
        return  # Missing/unavailable metadata is not an invented empty namespace.
    if not isinstance(value, dict):
        raise ProtocolError("Invalid namespace summary")
    _keys(value, {"variables", "truncated"})
    rows = value["variables"]
    if type(value["truncated"]) is not bool or not isinstance(rows, list) or len(rows) > MAX_NAMESPACE_ROWS:
        raise ProtocolError("Invalid namespace summary")
    for row in rows:
        if not isinstance(row, dict):
            raise ProtocolError("Invalid namespace row")
        _keys(row, {"name", "type"})
        for key, maximum in (("name", MAX_NAMESPACE_NAME_CHARS), ("type", MAX_NAMESPACE_TYPE_CHARS)):
            if not isinstance(row[key], str) or not 0 < len(row[key]) <= maximum:
                raise ProtocolError("Invalid namespace row")
    if len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")) > MAX_NAMESPACE_BYTES:
        raise ProtocolError("Namespace summary exceeds byte limit")


def _arguments(method: str, args: Any) -> None:
    if not isinstance(args, dict):
        raise ProtocolError("Broker args must be an object")
    if method == "recent":
        _keys(args, set(), {"n"})
    elif method == "search":
        _keys(args, {"query"}, {"kind", "limit"})
        if not isinstance(args["query"], str) or (args.get("kind") is not None and not isinstance(args["kind"], str)):
            raise ProtocolError("Invalid search arguments")
    elif method == "read":
        _keys(args, {"event_or_cell_id"}, {"offset", "limit"})
        if not _id(args["event_or_cell_id"]):
            raise ProtocolError("Invalid history ID")
    else:
        raise ProtocolError("Unsupported broker method")
    for key in ("n", "limit", "offset"):
        if key in args and not _integer(args[key]):
            raise ProtocolError("History bounds must be nonnegative integers")


def _json_value(value: Any, depth: int = 0) -> None:
    if depth > 64:
        raise ProtocolError("JSON nesting limit exceeded")
    if value is None or isinstance(value, (str, bool)) or type(value) is int:
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _json_value(item, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _json_value(item, depth + 1)
        return
    raise ProtocolError("Unsupported or nonfinite JSON value")


def validate_frame(frame: Any, *, allowed_types: set[str] | frozenset[str] | None = None) -> dict:
    _json_value(frame)
    if not isinstance(frame, dict) or type(frame.get("v")) is not int or frame["v"] != VERSION:
        raise ProtocolError("Unsupported protocol version or non-object frame")
    kind = frame.get("type")
    if not isinstance(kind, str) or kind not in HOST_TYPES | WORKER_TYPES:
        raise ProtocolError("Unknown frame type")
    if allowed_types is not None and kind not in allowed_types:
        raise ProtocolError("Frame direction is not allowed")
    base = {"v", "type"}
    if kind == "ready":
        _keys(frame, base | {"kernel_pid"})
        if not _integer(frame["kernel_pid"], positive=True):
            raise ProtocolError("Invalid kernel PID")
        return frame
    if not _id(frame.get("cell_id")):
        raise ProtocolError("Invalid cell ID")
    base.add("cell_id")
    if kind == "execute":
        _keys(frame, base | {"source"})
        if not isinstance(frame["source"], str) or not frame["source"].strip():
            raise ProtocolError("Source must be nonempty text")
    elif kind == "output":
        _keys(frame, base | {"stream", "text"})
        if frame["stream"] not in ("stdout", "stderr", "display") or not isinstance(frame["text"], str):
            raise ProtocolError("Invalid output")
    elif kind == "say":
        _keys(frame, base | {"content", "final"})
        if type(frame["final"]) is not bool:
            raise ProtocolError("final must be boolean")
    elif kind == "broker_request":
        _keys(frame, base | {"request_id", "method", "args"})
        if not _id(frame["request_id"]):
            raise ProtocolError("Invalid request ID")
        _arguments(frame["method"], frame["args"])
    elif kind == "broker_response":
        _keys(frame, base | {"request_id"}, {"result", "error"})
        if not _id(frame["request_id"]) or ("result" in frame) == ("error" in frame):
            raise ProtocolError("Response needs a request ID and exactly one result/error")
        if "error" in frame and not isinstance(frame["error"], str):
            raise ProtocolError("Broker error must be text")
    elif kind == "cell_end":
        _keys(frame, base | {"status", "execution_count"}, {"error", "memories_count", "namespace_summary"})
        if frame["status"] not in ("success", "error", "wait", "invalid_control"):
            raise ProtocolError("Invalid cell status")
        if not _integer(frame["execution_count"]) or ("error" in frame and not isinstance(frame["error"], str)):
            raise ProtocolError("Invalid cell result")
        if frame.get("memories_count") is not None and not _integer(frame["memories_count"]):
            raise ProtocolError("Invalid memories count")
        validate_namespace_summary(frame.get("namespace_summary"))
    return frame


def encode_frame(frame: dict) -> bytes:
    validate_frame(frame)
    try:
        data = json.dumps(frame, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii") + b"\n"
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProtocolError("Frame is not bounded JSON data") from exc
    if len(data) > MAX_FRAME_BYTES:
        raise ProtocolError("Frame exceeds 65536-byte limit")
    return data


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ProtocolError("Duplicate JSON key")
        obj[key] = value
    return obj


def decode_frame(data: bytes, *, allowed_types=None) -> dict:
    if not data:
        raise EOFError("Worker transport closed")
    if len(data) > MAX_FRAME_BYTES or not data.endswith(b"\n"):
        raise ProtocolError("Oversized or unterminated frame")
    try:
        frame = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError("Nonfinite JSON number")))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ProtocolError("Malformed JSON frame") from exc
    return validate_frame(frame, allowed_types=allowed_types)


# Bounded read-ahead keeps framing independent of the caller's StreamReader
# limit. One consumer per reader is required, as with asyncio's own read API.
_BUFFERS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


async def read_frame(reader: asyncio.StreamReader, *, allowed_types=None) -> dict:
    buffer = _BUFFERS.setdefault(reader, bytearray())
    while True:
        newline = buffer.find(b"\n")
        if newline >= 0:
            data = bytes(buffer[:newline + 1])
            del buffer[:newline + 1]
            return decode_frame(data, allowed_types=allowed_types)
        if len(buffer) >= MAX_FRAME_BYTES:
            raise ProtocolError("Frame exceeds 65536-byte limit")
        chunk = await reader.read(min(4096, MAX_FRAME_BYTES - len(buffer)))
        if not chunk:
            if not buffer:
                raise EOFError("Worker transport closed")
            raise ProtocolError("Truncated frame")
        buffer.extend(chunk)


async def write_frame(writer: asyncio.StreamWriter, frame: dict) -> None:
    writer.write(encode_frame(frame))
    await writer.drain()


def read_frame_sync(reader: BinaryIO, *, allowed_types=None) -> dict:
    return decode_frame(reader.readline(MAX_FRAME_BYTES + 1), allowed_types=allowed_types)


def write_frame_sync(writer: BinaryIO, frame: dict) -> None:
    writer.write(encode_frame(frame))
    writer.flush()
