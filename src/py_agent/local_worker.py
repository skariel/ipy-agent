"""Unrestricted persistent IPython child used by :mod:`local_executor`.

The process boundary provides lifecycle and transport separation only. It is
not a sandbox: executed code has the current user's permissions.
"""
from __future__ import annotations

import base64
import builtins
import codecs
import getpass as _getpass
import io
import json
import keyword
import math
import locale
import os
import re
import select
import subprocess
import sys
import threading
import types
import inspect as _inspect
from collections.abc import Mapping
from typing import Any

MAX_FRAME = 1_048_576
MAX_OUTPUT_FRAME_CHARS = 8_192
MAX_VISIBLE_OUTPUT_CHARS = 8_000
MAX_STORED_OUTPUT_CHARS = 1_048_576
MAX_ERROR_CHARS = 8_192
MAX_SAY_CHARS = MAX_FRAME
MAX_SAY_MESSAGES = 1_024
MAX_RICH_OUTPUT_FRAMES = 256
MAX_RICH_OUTPUT_BYTES = 2_097_152
MAX_RICH_FRAME_BYTES = MAX_FRAME - 16_384
MAX_MIME_VALUE_BYTES = 512_000
MAX_BINARY_MIME_BYTES = 512_000
MAX_MIME_DEPTH = 16
MAX_MIME_ITEMS = 8_192
MAX_MIME_BUNDLE_ITEMS = 64
MAX_QUERY_CHARS = 65_536
MAX_INPUT_PROMPT_CHARS = 8_192
MAX_INPUT_VALUE_CHARS = 65_536
MAX_INPUT_REQUESTS_PER_EXECUTION = 64
MAX_PASSWORD_SECRET_CHARS = 1_048_576
MAX_PASSWORD_SECRETS = 128
MAX_COMPLETION_MATCHES = 512
MAX_COMPLETION_MATCH_CHARS = 2_048
MAX_INSPECTION_CHARS = 16_384
_ALLOWED_MIME_TYPES = frozenset({
    "text/plain", "text/html", "text/markdown", "text/latex", "text/csv",
    "application/json", "image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml",
    "application/pdf",
})
PROTOCOL_VERSION = 1

_CONTROL_IN: int | None = None
_CONTROL_OUT: int | None = None
_SEND_LOCK = threading.Lock()
_PASSWORD_SECRETS: list[str] = []
_PASSWORD_SECRET_CHARS = 0
_ACTIVE_INPUT_FUNCTIONS: tuple[Any, Any] | None = None
_NEXT_OUTPUT_INDEX = 1


def _dispatch_input(prompt: Any = "") -> str:
    functions = _ACTIVE_INPUT_FUNCTIONS
    if functions is None:
        raise RuntimeError("Interactive input is unavailable outside an active execution")
    return functions[0](prompt)


def _dispatch_getpass(
    prompt: Any = "Password: ", stream: Any = None, *, echo_char: Any = None,
) -> str:
    functions = _ACTIVE_INPUT_FUNCTIONS
    if functions is None:
        raise RuntimeError("Interactive input is unavailable outside an active execution")
    return functions[1](prompt, stream, echo_char=echo_char)


def _stdlib_getpass_dispatch(
    prompt: Any = "Password: ", stream: Any = None, *args: Any, **kwargs: Any,
) -> str:
    """Replacement body for stdlib getpass functions, including saved aliases."""
    if args:
        raise TypeError("Unsupported positional arguments for getpass")
    echo_char = kwargs.pop("echo_char", None)
    if kwargs:
        name = next(iter(kwargs))
        raise TypeError(f"Unexpected getpass argument: {name}")
    # Resolved in the getpass module namespace after code transplantation.
    return __py_agent_dispatch_getpass(prompt, stream, echo_char=echo_char)  # noqa: F821


def _secure_getpass_variants() -> None:
    """Route every stdlib getpass entry point through the password bridge.

    In-place code replacement also secures function objects retained by an
    earlier ``from getpass import ...``. If a stdlib variant cannot be safely
    rewritten, worker startup fails closed rather than leaving an echoing
    fallback reachable.
    """
    namespace = vars(_getpass)
    namespace["__py_agent_dispatch_getpass"] = _dispatch_getpass
    replacement = _stdlib_getpass_dispatch
    names = (
        "getpass", "default_getpass", "unix_getpass", "win_getpass",
        "fallback_getpass", "_raw_input",
    )
    for name in names:
        function = namespace.get(name)
        if isinstance(function, types.FunctionType) and function is not _dispatch_getpass:
            try:
                function.__code__ = replacement.__code__
                function.__defaults__ = replacement.__defaults__
                function.__kwdefaults__ = replacement.__kwdefaults__
            except (AttributeError, TypeError, ValueError) as exc:
                raise RuntimeError("Could not secure a stdlib getpass variant") from exc
        namespace[name] = _dispatch_getpass


class ProtocolError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ProtocolError("Duplicate key in protocol frame")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ProtocolError(f"Invalid JSON number: {value}")


def _read_exact(fd: int, size: int, *, allow_eof: bool = False) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = os.read(fd, size - len(chunks))
        if not chunk:
            if allow_eof and not chunks:
                return None
            raise EOFError("Incomplete protocol frame")
        chunks.extend(chunk)
    return bytes(chunks)


def _decode_frame(payload: bytes) -> dict[str, Any]:
    try:
        frame = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ProtocolError("Malformed protocol frame") from exc
    if not isinstance(frame, dict):
        raise ProtocolError("Protocol frame must be an object")
    return frame


class _UnsafeMimeValue(ValueError):
    pass


def _safe_json_value(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> Any:
    """Copy finite JSON values with strict depth, item, and string limits."""
    if budget is None:
        budget = [MAX_MIME_ITEMS]
    budget[0] -= 1
    if budget[0] < 0 or depth > MAX_MIME_DEPTH:
        raise _UnsafeMimeValue("MIME value exceeds its structural limit")
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _UnsafeMimeValue("MIME numbers must be finite")
        return value
    if isinstance(value, str):
        if (len(value) > MAX_MIME_VALUE_BYTES
                or len(value.encode("utf-8", errors="replace")) > MAX_MIME_VALUE_BYTES):
            raise _UnsafeMimeValue("MIME text exceeds its byte limit")
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_json_value(item, depth=depth + 1, budget=budget) for item in value]
    if isinstance(value, Mapping):
        copied = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 256:
                raise _UnsafeMimeValue("MIME object keys must be short text")
            copied[key] = _safe_json_value(item, depth=depth + 1, budget=budget)
        return copied
    raise _UnsafeMimeValue("MIME values must be finite JSON data")


def _allowed_mime_type(mime_type: str) -> bool:
    if mime_type in _ALLOWED_MIME_TYPES:
        return True
    return (
        mime_type.startswith("application/vnd.")
        and mime_type.endswith("+json")
        and "jupyter.widget" not in mime_type
    )


def _safe_mime_bundle(data: Any, metadata: Any = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Filter and bound a formatter bundle for the worker's JSON transport."""
    if not isinstance(data, Mapping):
        return {}, {}
    safe_data: dict[str, Any] = {}
    value_budget = [MAX_MIME_ITEMS]
    for index, (mime_type, value) in enumerate(data.items()):
        if index >= MAX_MIME_BUNDLE_ITEMS:
            break
        if (not isinstance(mime_type, str) or len(mime_type) > 256
                or not _allowed_mime_type(mime_type)):
            continue
        try:
            if mime_type.startswith("image/") and isinstance(value, (bytes, bytearray, memoryview)):
                binary_size = value.nbytes if isinstance(value, memoryview) else len(value)
                if binary_size > MAX_BINARY_MIME_BYTES:
                    continue
                raw = bytes(value)
                if len(raw) > MAX_BINARY_MIME_BYTES:
                    continue
                safe_value = base64.b64encode(raw).decode("ascii")
            else:
                safe_value = _safe_json_value(value, budget=value_budget)
            trial = dict(safe_data)
            trial[mime_type] = safe_value
            if len(json.dumps(trial, ensure_ascii=True, allow_nan=False, separators=(",", ":"))) > MAX_RICH_FRAME_BYTES - 65_536:
                continue
            safe_data[mime_type] = safe_value
        except (TypeError, ValueError, OverflowError, RecursionError):
            continue
    safe_metadata: dict[str, Any] = {}
    if isinstance(metadata, Mapping):
        try:
            safe_metadata = _safe_json_value(metadata)
            if not isinstance(safe_metadata, dict):
                safe_metadata = {}
            if len(json.dumps(safe_metadata, ensure_ascii=True, allow_nan=False,
                              separators=(",", ":"))) > 65_536:
                safe_metadata = {}
        except (TypeError, ValueError, OverflowError, RecursionError):
            safe_metadata = {}
    return safe_data, safe_metadata


def _encode_payload(frame: dict[str, Any]) -> bytes:
    try:
        payload = json.dumps(frame, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProtocolError("Protocol frame is not JSON data") from exc
    if not 0 < len(payload) <= MAX_FRAME:
        raise ProtocolError("Protocol frame exceeds the byte limit")
    return len(payload).to_bytes(4, "big") + payload


def _send(frame: dict[str, Any]) -> None:
    if _CONTROL_OUT is None:
        raise RuntimeError("Worker control channel is not initialized")
    payload = _encode_payload(frame)
    view = memoryview(payload)
    # Writes to pipes can be short. A single lock also keeps frames from worker
    # threads from interleaving with the main execution loop.
    with _SEND_LOCK:
        while view:
            written = os.write(_CONTROL_OUT, view)
            if written <= 0:
                raise BrokenPipeError("Worker control channel closed")
            view = view[written:]


def _receive() -> dict[str, Any] | None:
    if _CONTROL_IN is None:
        raise RuntimeError("Worker control channel is not initialized")
    header = _read_exact(_CONTROL_IN, 4, allow_eof=True)
    if header is None:
        return None
    size = int.from_bytes(header, "big")
    if not 0 < size <= MAX_FRAME:
        raise ProtocolError("Invalid protocol frame length")
    payload = _read_exact(_CONTROL_IN, size)
    assert payload is not None
    return _decode_frame(payload)


def _is_id(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 128 and all(
        char.isascii() and (char.isalnum() or char in "_.:-") for char in value
    )


def _valid_origin(value: Any) -> bool:
    fields = {"session_id", "request_id", "frontend_id", "config_revision", "generation_id", "execution_id"}
    if not isinstance(value, dict) or set(value) != fields:
        return False
    for key in ("session_id", "request_id", "frontend_id"):
        identity = value[key]
        if not isinstance(identity, str) or not 0 < len(identity) <= 128 or "\x00" in identity:
            return False
    for key in ("generation_id", "execution_id"):
        identity = value[key]
        if identity is not None and (
            not isinstance(identity, str) or not 0 < len(identity) <= 128 or "\x00" in identity
        ):
            return False
    revision = value["config_revision"]
    return type(revision) is int and revision >= 0


def _validate_request(frame: dict[str, Any]) -> tuple[Any, ...] | None:
    if (
        set(frame) == {"type", "version"}
        and frame.get("type") == "close"
        and type(frame.get("version")) is int
        and frame["version"] == PROTOCOL_VERSION
    ):
        return None
    if type(frame.get("version")) is not int or frame["version"] != PROTOCOL_VERSION:
        raise ProtocolError("Unsupported protocol version")
    if frame.get("type") == "execute":
        expected = {"type", "version", "execution_id", "origin", "author", "source"}
        if set(frame) != expected:
            raise ProtocolError("Invalid execution request fields")
        execution_id = frame.get("execution_id")
        author = frame.get("author")
        source = frame.get("source")
        origin = frame.get("origin")
        if (
            not _is_id(execution_id)
            or author not in ("user", "agent")
            or not isinstance(source, str)
            or not _valid_origin(origin)
        ):
            raise ProtocolError("Invalid execution request")
        return "execute", execution_id, author, source, origin

    kind = frame.get("type")
    if kind == "store_collapsed":
        if set(frame) != {"type", "version", "request_id", "offset", "text", "final"}:
            raise ProtocolError("Invalid collapsed storage request fields")
        request_id, offset, text, final = (
            frame["request_id"], frame["offset"], frame["text"], frame["final"],
        )
        if (not _is_id(request_id) or type(offset) is not int or offset < 0
                or not isinstance(text, str) or len(text) > 32_768
                or type(final) is not bool or (not text and not final)):
            raise ProtocolError("Invalid collapsed storage request")
        return "store_collapsed", request_id, offset, text, final
    if kind == "store_outputs":
        if set(frame) != {"type", "version", "request_id", "texts"}:
            raise ProtocolError("Invalid output storage request fields")
        request_id, texts = frame["request_id"], frame["texts"]
        if (not _is_id(request_id) or not isinstance(texts, list)
                or not 1 <= len(texts) <= 10
                or any(not isinstance(text, str) or len(text) > 16_000 for text in texts)):
            raise ProtocolError("Invalid output storage request")
        return "store_outputs", request_id, texts
    if kind == "complete":
        expected = {"type", "version", "request_id", "code", "cursor_pos"}
    elif kind == "inspect":
        expected = {"type", "version", "request_id", "code", "cursor_pos", "detail_level"}
    else:
        raise ProtocolError("Invalid worker request type")
    if set(frame) != expected:
        raise ProtocolError("Invalid query request fields")
    request_id, code, cursor_pos = frame.get("request_id"), frame.get("code"), frame.get("cursor_pos")
    if (
        not _is_id(request_id)
        or not isinstance(code, str)
        or len(code) > MAX_QUERY_CHARS
        or type(cursor_pos) is not int
        or not 0 <= cursor_pos <= len(code)
    ):
        raise ProtocolError("Invalid query request")
    if kind == "inspect":
        detail_level = frame.get("detail_level")
        if type(detail_level) is not int or detail_level not in (0, 1):
            raise ProtocolError("Invalid inspection detail level")
        return "inspect", request_id, code, cursor_pos, detail_level
    return "complete", request_id, code, cursor_pos


def _namespace_root(shell: Any, name: str) -> tuple[bool, Any]:
    namespace = shell.user_ns
    if type(namespace) is dict and name in namespace:
        return True, namespace[name]
    builtins_namespace = builtins.__dict__
    if name in builtins_namespace:
        return True, builtins_namespace[name]
    return False, None


def _static_attribute(value: Any, name: str) -> tuple[bool, Any]:
    try:
        return True, _inspect.getattr_static(value, name)
    except (AttributeError, TypeError):
        return False, None
    except Exception:
        # Custom metaclasses and unusual proxy types can make static lookup fail;
        # inspection must not fall back to dynamic getattr/evaluation.
        return False, None


def _resolve_static(shell: Any, expression: str) -> tuple[bool, Any]:
    parts = expression.split(".")
    found, value = _namespace_root(shell, parts[0])
    if not found:
        return False, None
    for part in parts[1:]:
        found, value = _static_attribute(value, part)
        if not found:
            return False, None
    return True, value


def _static_names(value: Any) -> set[str]:
    """Return names from dictionaries/class tables without calling ``dir``."""
    names: set[str] = set()
    if type(value) is types.ModuleType:
        try:
            namespace = types.ModuleType.__getattribute__(value, "__dict__")
            names.update(key for key in namespace if type(key) is str)
        except Exception:
            pass

    instance_namespace = _static_attribute(value, "__dict__")
    if instance_namespace[0] and type(instance_namespace[1]) is dict:
        names.update(key for key in instance_namespace[1] if type(key) is str)

    classes = []
    if type(value) is type or _inspect.isclass(value):
        classes.append(value)
    else:
        try:
            classes.append(type(value))
        except Exception:
            pass
    for cls in classes:
        try:
            mro = type.__getattribute__(cls, "__mro__")
        except Exception:
            mro = (cls,)
        for base in mro:
            try:
                namespace = type.__getattribute__(base, "__dict__")
                names.update(key for key in namespace if type(key) is str)
            except Exception:
                continue
    return names


def _magic_matches(shell: Any, line: str, cursor_pos: int) -> tuple[list[str], int] | None:
    stripped_start = len(line) - len(line.lstrip())
    content = line[stripped_start:]
    marker = "%%" if content.startswith("%%") else "%" if content.startswith("%") else ""
    if not marker:
        return None
    prefix = content[len(marker):]
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9.-]*|", prefix):
        return None
    registry = getattr(shell.magics_manager, "magics", {})
    group = "cell" if marker == "%%" else "line"
    magics = registry.get(group, {}) if isinstance(registry, dict) else {}
    candidates = []
    for name in magics:
        if type(name) is str and name.startswith(prefix):
            candidate = marker + name
            if len(candidate) <= MAX_COMPLETION_MATCH_CHARS:
                candidates.append(candidate)
    return sorted(set(candidates))[:MAX_COMPLETION_MATCHES], cursor_pos - len(marker + prefix)


def _complete(shell: Any, request_id: str, code: str, cursor_pos: int) -> None:
    """Offer bounded static namespace and IPython-magic completions.

    This intentionally avoids evaluating expressions, calling ``dir`` or
    rendering candidate objects. Dotted completion uses static attribute lookup.
    """
    before = code[:cursor_pos]
    line = before.rsplit("\n", 1)[-1]
    magic = _magic_matches(shell, line, cursor_pos)
    if magic is not None:
        matches, start = magic
    elif line.lstrip().startswith("!"):
        # Shell command completion is platform-specific and may consult the
        # filesystem; do not perform it from a protocol inspection request.
        matches, start = [], cursor_pos
    else:
        token = re.search(r"([A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*\.?)$", before)
        matches = []
        start = cursor_pos
        if token is not None:
            chain = token.group(1)
            if "." in chain:
                if chain.endswith("."):
                    expression, prefix = chain[:-1], ""
                else:
                    expression, prefix = chain.rsplit(".", 1)
                found, value = _resolve_static(shell, expression)
                names = _static_names(value) if found else set()
            else:
                prefix = chain
                namespace = shell.user_ns
                names = set()
                if type(namespace) is dict:
                    names.update(key for key in namespace if type(key) is str)
                names.update(key for key in builtins.__dict__ if type(key) is str)
                names.update(keyword.kwlist)
            start = cursor_pos - len(prefix)
            matches = sorted(name for name in names if name.startswith(prefix))[:MAX_COMPLETION_MATCHES]
            matches = [name for name in matches if len(name) <= MAX_COMPLETION_MATCH_CHARS]

    bounded_matches = []
    used_bytes = 0
    for candidate in matches[:MAX_COMPLETION_MATCHES]:
        if type(candidate) is not str or len(candidate) > MAX_COMPLETION_MATCH_CHARS:
            continue
        cost = len(json.dumps(candidate, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
        if used_bytes + cost + len(bounded_matches) >= MAX_FRAME - 4_096:
            break
        bounded_matches.append(candidate)
        used_bytes += cost
    _send({
        "type": "completion",
        "version": PROTOCOL_VERSION,
        "request_id": request_id,
        "matches": bounded_matches,
        "cursor_start": start,
        "cursor_end": cursor_pos,
        "metadata": {},
    })


def _inspection_expression(code: str, cursor_pos: int) -> str | None:
    pattern = re.compile(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*")
    candidates = list(pattern.finditer(code))
    for match in candidates:
        if match.start() <= cursor_pos < match.end():
            return match.group(0)
    for match in reversed(candidates):
        if match.end() == cursor_pos:
            return match.group(0)
    return None


def _safe_type_name(value: Any) -> str:
    cls = type(value)
    try:
        module = type.__getattribute__(cls, "__module__")
        name = type.__getattribute__(cls, "__name__")
    except Exception:
        return "object"
    if type(module) is not str or type(name) is not str:
        return "object"
    return f"{module}.{name}" if module not in ("builtins", "__main__") else name


def _inspect_name(
    shell: Any, request_id: str, code: str, cursor_pos: int, detail_level: int,
) -> None:
    expression = _inspection_expression(code, cursor_pos)
    found, value = _resolve_static(shell, expression) if expression is not None else (False, None)
    data: dict[str, str] = {}
    if found:
        summary = f"{expression} : {_safe_type_name(value)}"
        has_doc, doc = _static_attribute(value, "__doc__")
        if has_doc and type(doc) is str and doc:
            doc = doc[:MAX_INSPECTION_CHARS]
            if detail_level == 0:
                doc = doc.splitlines()[0] if doc.splitlines() else doc
            summary += "\n\n" + doc
        data["text/plain"] = summary[:MAX_INSPECTION_CHARS]
    _send({
        "type": "inspection",
        "version": PROTOCOL_VERSION,
        "request_id": request_id,
        "found": found,
        "data": data,
        "metadata": {},
    })


class _UnavailableInput:
    """Fail accidental stdin reads rather than consuming control frames."""

    encoding = "utf-8"
    errors = "strict"

    @staticmethod
    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Interactive input is unavailable in the local executor")

    read = _raise
    readline = _raise
    readlines = _raise

    def __iter__(self):
        return self

    def __next__(self):
        self._raise()

    def isatty(self) -> bool:
        return False

    def readable(self) -> bool:
        return True


class _OutputBytesWriter:
    """Minimal ``sys.stdout.buffer`` bridge into the bounded text channel."""

    def __init__(self, writer: _OutputWriter):
        self.writer = writer
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._lock = threading.Lock()

    def write(self, data: bytes | bytearray | memoryview) -> int:
        view = memoryview(data).cast("B")
        with self._lock:
            for start in range(0, len(view), MAX_OUTPUT_FRAME_CHARS):
                chunk = view[start : start + MAX_OUTPUT_FRAME_CHARS].tobytes()
                text = self.decoder.decode(chunk)
                if text:
                    self.writer.write(text)
        return len(view)

    def _flush_decoder(self) -> None:
        with self._lock:
            tail = self.decoder.decode(b"", final=True)
            self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
            if tail:
                self.writer.write(tail)

    def flush(self) -> None:
        self._flush_decoder()
        self.writer._flush_text()

    def writable(self) -> bool:
        return True


class _OutputWriter(io.TextIOBase):
    """Stream output in small, bounded, origin-tagged protocol frames."""

    def __init__(self, stream: str, execution_id: str, author: str, origin: dict[str, Any]):
        super().__init__()
        self.stream = stream
        self.execution_id = execution_id
        self.author = author
        self.origin = origin
        self._pending = ""
        self._lock = threading.Lock()
        self._captured: list[str] = []
        self._captured_chars = 0
        self._omitted_chars = 0
        self._binary = _OutputBytesWriter(self)

    @property
    def encoding(self) -> str:
        return "utf-8"

    @property
    def errors(self) -> str:
        return "replace"

    @property
    def buffer(self) -> _OutputBytesWriter:
        return self._binary

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            raise TypeError("write() argument must be str")
        length = len(text)
        with self._lock:
            for start in range(0, length, MAX_OUTPUT_FRAME_CHARS):
                chunk = text[start : start + MAX_OUTPUT_FRAME_CHARS]
                safe = chunk.encode("utf-8", errors="replace").decode("utf-8")
                remaining = max(0, MAX_STORED_OUTPUT_CHARS - self._captured_chars)
                kept = safe[:remaining]
                if kept:
                    self._captured.append(kept)
                    self._captured_chars += len(kept)
                self._omitted_chars += len(safe) - len(kept)
                offset = 0
                if self._pending:
                    needed = MAX_OUTPUT_FRAME_CHARS - len(self._pending)
                    self._pending += safe[:needed]
                    offset = min(needed, len(safe))
                    if len(self._pending) == MAX_OUTPUT_FRAME_CHARS:
                        self._emit(self._pending)
                        self._pending = ""
                while len(safe) - offset >= MAX_OUTPUT_FRAME_CHARS:
                    self._emit(safe[offset : offset + MAX_OUTPUT_FRAME_CHARS])
                    offset += MAX_OUTPUT_FRAME_CHARS
                self._pending += safe[offset:]
                if "\n" in self._pending:
                    self._emit(self._pending)
                    self._pending = ""
        return length

    def flush(self) -> None:
        self._binary._flush_decoder()
        self._flush_text()

    def captured(self) -> tuple[str, int, int]:
        with self._lock:
            return "".join(self._captured), self._captured_chars, self._omitted_chars

    def _flush_text(self) -> None:
        with self._lock:
            if self._pending:
                self._emit(self._pending)
                self._pending = ""

    def _emit(self, text: str) -> None:
        _send({
            "type": "output",
            "version": PROTOCOL_VERSION,
            "execution_id": self.execution_id,
            "origin": self.origin,
            "author": self.author,
            "stream": self.stream,
            "text": _redact_text(text),
        })


class _RawOutputCapture:
    """Frame raw output with per-cell pipes so child output keeps its origin.

    In-process threads that retain and later write the numeric fd 1/2 cannot be
    attributed after those descriptors rotate; direct text-stream references
    and inherited subprocess descriptors retain their original cell writer.
    """

    MAX_RETAINED_STREAMS = 128

    def __init__(self):
        self._readers: dict[int, tuple[str, Any, _OutputWriter]] = {}
        self._current: set[int] = set()
        self._wake_read, self._wake_write = os.pipe()
        os.set_blocking(self._wake_read, False)
        os.set_blocking(self._wake_write, False)
        self._guard = threading.Lock()
        self._barriers: list[threading.Event] = []
        self._failure: BaseException | None = None
        self._stopping = False
        self._thread = threading.Thread(target=self._pump, name="local-worker-output", daemon=True)
        self._thread.start()

    def activate(self, stdout: _OutputWriter, stderr: _OutputWriter) -> None:
        # Reap completed descendants before allocating two more bounded pipes.
        self.barrier()
        stdout_read, stdout_write = os.pipe()
        stderr_read, stderr_write = os.pipe()
        os.set_blocking(stdout_read, False)
        os.set_blocking(stderr_read, False)
        with self._guard:
            if len(self._readers) + 2 > self.MAX_RETAINED_STREAMS:
                for fd in (stdout_read, stdout_write, stderr_read, stderr_write):
                    os.close(fd)
                raise RuntimeError("Too many executions still own delayed output pipes")
            self._readers[stdout_read] = (
                "stdout", codecs.getincrementaldecoder("utf-8")("replace"), stdout
            )
            self._readers[stderr_read] = (
                "stderr", codecs.getincrementaldecoder("utf-8")("replace"), stderr
            )
            self._current = {stdout_read, stderr_read}
        try:
            os.dup2(stdout_write, 1)
            os.dup2(stderr_write, 2)
        finally:
            os.close(stdout_write)
            os.close(stderr_write)
        self._wake()

    def _wake(self) -> None:
        try:
            os.write(self._wake_write, b".")
        except BlockingIOError:
            pass

    def finish_cell(self) -> None:
        try:
            self.barrier()
        finally:
            null_fd = os.open(os.devnull, os.O_RDWR)
            try:
                os.dup2(null_fd, 1)
                os.dup2(null_fd, 2)
            finally:
                os.close(null_fd)
            with self._guard:
                self._current.clear()
            self._wake()
        self.barrier()

    def barrier(self) -> None:
        marker = threading.Event()
        with self._guard:
            if self._failure is not None:
                raise RuntimeError("Raw output capture failed") from self._failure
            self._barriers.append(marker)
            self._wake()
        marker.wait()
        if self._failure is not None:
            raise RuntimeError("Raw output capture failed") from self._failure

    def _emit(self, writer: _OutputWriter, text: str) -> None:
        if text:
            writer.write(text)

    def _drain(self, fd: int, *, until_empty: bool = False) -> None:
        with self._guard:
            reader = self._readers.get(fd)
        if reader is None:
            return
        _, decoder, writer = reader
        iterations = 0
        while until_empty or iterations < 64:
            iterations += 1
            try:
                data = os.read(fd, 16_384)
            except BlockingIOError:
                return
            if not data:
                self._emit(writer, decoder.decode(b"", final=True))
                with self._guard:
                    self._readers.pop(fd, None)
                os.close(fd)
                return
            self._emit(writer, decoder.decode(data))

    def _drain_all(self) -> None:
        with self._guard:
            readers = tuple(self._readers)
        for fd in readers:
            self._drain(fd, until_empty=True)

    def _pump(self) -> None:
        try:
            while True:
                with self._guard:
                    readers = tuple(self._readers)
                readable, _, _ = select.select([*readers, self._wake_read], [], [])
                for fd in readable:
                    if fd != self._wake_read:
                        self._drain(fd)
                if self._wake_read in readable:
                    try:
                        while os.read(self._wake_read, 4096):
                            pass
                    except BlockingIOError:
                        pass
                    self._drain_all()
                    with self._guard:
                        barriers, self._barriers = self._barriers, []
                        current = tuple(self._current)
                        stopping = self._stopping
                    if barriers:
                        for fd in current:
                            with self._guard:
                                reader = self._readers.get(fd)
                            if reader is not None:
                                stream, decoder, writer = reader
                                self._emit(writer, decoder.decode(b"", final=True))
                                with self._guard:
                                    if fd in self._readers:
                                        self._readers[fd] = (
                                            stream,
                                            codecs.getincrementaldecoder("utf-8")("replace"),
                                            writer,
                                        )
                        for marker in barriers:
                            marker.set()
                    if stopping:
                        return
        except BaseException as exc:
            with self._guard:
                self._failure = exc
                barriers, self._barriers = self._barriers, []
            for marker in barriers:
                marker.set()

    def close(self) -> None:
        with self._guard:
            self._stopping = True
            try:
                os.write(self._wake_write, b".")
            except (BlockingIOError, OSError):
                pass
        self._thread.join(timeout=1)
        if not self._thread.is_alive():
            for fd in (*self._readers, self._wake_read, self._wake_write):
                try:
                    os.close(fd)
                except OSError:
                    pass


_RAW_CAPTURE: _RawOutputCapture | None = None


def _redact_text(text: str) -> str:
    marker = "[REDACTED]"
    if any(secret and secret in marker for secret in _PASSWORD_SECRETS):
        marker = ""
    for secret in sorted(_PASSWORD_SECRETS, key=len, reverse=True):
        if secret:
            text = text.replace(secret, marker)
    return text


def _redact_json(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_json(item) for item in value)
    if isinstance(value, Mapping):
        return {_redact_text(key) if isinstance(key, str) else key: _redact_json(item)
                for key, item in value.items()}
    return value


def _error_text(error: BaseException) -> str:
    try:
        message = str(error)
    except BaseException:
        message = "<error message unavailable>"
    name = type(error).__name__
    value = f"{name}: {message}" if message else name
    return _redact_text(value)[:MAX_ERROR_CHARS]


def _initialize_stdio() -> tuple[Any, Any, Any]:
    """Move IPC off fds 0/1/2 before user code or IPython can touch them."""
    global _CONTROL_IN, _CONTROL_OUT, _RAW_CAPTURE
    _CONTROL_IN = os.dup(0)
    _CONTROL_OUT = os.dup(1)
    null_fd = os.open(os.devnull, os.O_RDWR)
    if os.name == "nt":
        # select() cannot monitor anonymous pipes on Windows. Keep protocol fds
        # isolated; Python text streams still use the framed writers below.
        try:
            for fd in (0, 1, 2):
                os.dup2(null_fd, fd)
        finally:
            os.close(null_fd)
        _RAW_CAPTURE = None
    else:
        try:
            for fd in (0, 1, 2):
                os.dup2(null_fd, fd)
        finally:
            os.close(null_fd)
        _RAW_CAPTURE = _RawOutputCapture()

    # Python text output gets explicit framed writers per cell. On POSIX,
    # native and inherited subprocess writes use dedicated pipes, never IPC.
    null_in = _UnavailableInput()
    null_out = os.fdopen(os.dup(1), "w", encoding="utf-8", errors="replace", buffering=1)
    null_err = os.fdopen(os.dup(2), "w", encoding="utf-8", errors="replace", buffering=1)
    sys.stdin = sys.__stdin__ = null_in
    sys.stdout = sys.__stdout__ = null_out
    sys.stderr = sys.__stderr__ = null_err
    builtins.input = _dispatch_input
    _secure_getpass_variants()
    return null_in, null_out, null_err


def _install_noninteractive_system(shell: Any) -> None:
    """Keep IPython ``!`` escapes usable without attaching a stdin prompt."""

    def run_system(command: str) -> None:
        if command.rstrip().endswith("&"):
            raise OSError("Background shell commands are not supported by the local executor")
        expanded = shell.var_expand(command, depth=1)
        process = subprocess.Popen(
            expanded,
            shell=True,
            executable=os.environ.get("SHELL") or None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        if process.stdout is None:
            raise RuntimeError("Shell command output pipe is unavailable")
        decoder = codecs.getincrementaldecoder(locale.getpreferredencoding(False))("replace")
        completed = False
        try:
            while chunk := process.stdout.read(16_384):
                text = decoder.decode(chunk)
                if text:
                    sys.stdout.write(text)
                    sys.stdout.flush()
            tail = decoder.decode(b"", final=True)
            if tail:
                sys.stdout.write(tail)
                sys.stdout.flush()
            shell.user_ns["_exit_code"] = process.wait()
            completed = True
        finally:
            if not completed and process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                process.wait()
            process.stdout.close()

    shell.system = run_system


def _make_input_functions(execution_id: str, author: str, origin: dict[str, Any]):
    """Bridge Python's synchronous input APIs to the framed parent channel."""
    global _PASSWORD_SECRET_CHARS
    owner_thread = threading.get_ident()
    sequence = 0
    active = True

    def request_input(prompt: Any = "", *, password: bool = False) -> str:
        nonlocal sequence
        global _PASSWORD_SECRET_CHARS
        if threading.get_ident() != owner_thread or not active:
            raise RuntimeError("Interactive input is only supported in the active cell's main thread")
        if type(password) is not bool:
            raise TypeError("Interactive input password flag must be a boolean")
        if sequence >= MAX_INPUT_REQUESTS_PER_EXECUTION:
            raise RuntimeError("Interactive input request limit exceeded for this execution")
        text = _redact_text(str(prompt))
        if len(text) > MAX_INPUT_PROMPT_CHARS:
            raise ValueError("Interactive input prompt exceeds its character limit")
        sequence += 1
        _send({
            "type": "input_request",
            "version": PROTOCOL_VERSION,
            "execution_id": execution_id,
            "origin": origin,
            "author": author,
            "sequence": sequence,
            "prompt": text,
            "password": password,
        })
        frame = _receive()
        expected_reply = {
            "type", "version", "execution_id", "origin", "author", "sequence", "password", "value",
        }
        expected_error = {
            "type", "version", "execution_id", "origin", "author", "sequence",
            "password", "error", "cancelled",
        }
        if not isinstance(frame, dict):
            raise RuntimeError("Interactive input channel closed before a reply arrived")
        common_matches = (
            frame.get("version") == PROTOCOL_VERSION
            and type(frame.get("version")) is int
            and frame.get("execution_id") == execution_id
            and frame.get("origin") == origin
            and frame.get("author") == author
            and type(frame.get("sequence")) is int
            and frame.get("sequence") == sequence
        )
        if frame.get("type") == "input_error":
            if (set(frame) != expected_error or not common_matches
                    or type(frame["password"]) is not bool or frame["password"] is not password
                    or not isinstance(frame["error"], str)
                    or len(frame["error"]) > MAX_ERROR_CHARS
                    or type(frame["cancelled"]) is not bool):
                raise ProtocolError("Malformed interactive input failure frame")
            if frame["cancelled"]:
                raise KeyboardInterrupt(frame["error"] or "Interactive input cancelled")
            raise RuntimeError(frame["error"] or "Interactive input is unavailable")
        if (set(frame) != expected_reply or not common_matches
                or type(frame["password"]) is not bool or frame["password"] is not password
                or not isinstance(frame["value"], str)
                or len(frame["value"]) > MAX_INPUT_VALUE_CHARS):
            raise ProtocolError("Interactive input reply does not match its request")
        value = frame["value"]
        if password and value:
            if value not in _PASSWORD_SECRETS:
                if (len(_PASSWORD_SECRETS) >= MAX_PASSWORD_SECRETS
                        or _PASSWORD_SECRET_CHARS + len(value) > MAX_PASSWORD_SECRET_CHARS):
                    raise RuntimeError("Password redaction capacity is exhausted; password was not accepted")
                _PASSWORD_SECRETS.append(value)
                _PASSWORD_SECRET_CHARS += len(value)
        return value

    def interactive_input(prompt: Any = "") -> str:
        return request_input(prompt, password=False)

    def interactive_getpass(prompt: Any = "Password: ", stream: Any = None, *, echo_char: Any = None) -> str:
        del stream
        if echo_char is not None:
            raise RuntimeError("Custom getpass echo characters are unsupported by the active frontend")
        return request_input(prompt, password=True)

    def deactivate() -> None:
        nonlocal active
        active = False

    return interactive_input, interactive_getpass, deactivate


def _make_say(execution_id: str, author: str, origin: dict[str, Any], stdout, stderr):
    owner_thread = threading.get_ident()
    active = True
    final_requested = False
    emitted_chars = 0
    emitted_count = 0

    def say(content: Any, *, final: bool = False) -> None:
        nonlocal final_requested, emitted_chars, emitted_count
        if threading.get_ident() != owner_thread or not active:
            raise RuntimeError("say() is only supported in the active cell's main thread")
        if type(final) is not bool:
            raise TypeError("say(final=...) must be boolean")
        try:
            encoded = json.dumps(content, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError, RecursionError) as exc:
            raise TypeError("say() content must be finite JSON data") from exc
        cost = len(encoded)
        if author == "agent" and emitted_chars + cost > 8_000:
            raise ValueError("say() output exceeds the 8000-character cell limit; send something smaller")
        if emitted_count >= MAX_SAY_MESSAGES:
            raise ValueError("say() emitted too many messages in one cell")
        if emitted_chars + cost > MAX_SAY_CHARS:
            raise ValueError("say() output exceeds the worker's bounded message limit")
        safe_content = _redact_json(content)
        frame = {
            "type": "say",
            "version": PROTOCOL_VERSION,
            "execution_id": execution_id,
            "origin": origin,
            "author": author,
            "content": safe_content,
            "final": final,
        }
        _encode_payload(frame)  # reject oversize or unsupported values before control intent
        stdout.flush()
        stderr.flush()
        if _RAW_CAPTURE is not None:
            _RAW_CAPTURE.barrier()
        _send(frame)
        emitted_chars += cost
        emitted_count += 1
        final_requested = final_requested or final

    def deactivate() -> None:
        nonlocal active
        active = False

    return say, deactivate, lambda: final_requested


def _store_output(shell: Any, text: str) -> int:
    global _NEXT_OUTPUT_INDEX
    outputs = shell.user_ns.get("outputs")
    if type(outputs) is not dict:
        outputs = {}
        shell.user_ns["outputs"] = outputs
    while _NEXT_OUTPUT_INDEX in outputs:
        _NEXT_OUTPUT_INDEX += 1
    index = _NEXT_OUTPUT_INDEX
    outputs[index] = text
    _NEXT_OUTPUT_INDEX += 1
    return index


class _CollapsedStorage:
    """Stage one chunked transaction, publishing only complete lossless strings."""

    def __init__(self) -> None:
        self.archives: dict[int, str] = {}
        self.next_index = 1
        self.request_id: str | None = None
        self.offset = 0
        self.chunks: list[str] = []

    def append(self, request_id: str, offset: int, text: str, final: bool) -> int | None:
        if self.request_id is None:
            if offset != 0:
                raise ProtocolError("Collapsed archive must start at offset zero")
            self.request_id = request_id
        if request_id != self.request_id or offset != self.offset:
            raise ProtocolError("Collapsed archive chunk is out of sequence")
        self.chunks.append(text)
        self.offset += len(text)
        if not final:
            return None
        while self.next_index in self.archives:
            self.next_index += 1
        index = self.next_index
        self.archives[index] = "".join(self.chunks)
        self.next_index += 1
        self.request_id = None
        self.offset = 0
        self.chunks = []
        return index


def _run_cell(shell: Any, execution_id: str, author: str, source: str, origin: dict[str, Any]) -> None:
    global _ACTIVE_INPUT_FUNCTIONS
    null_in, null_out, null_err = _NULL_STREAMS
    stdout = _OutputWriter("stdout", execution_id, author, origin)
    stderr = _OutputWriter("stderr", execution_id, author, origin)
    say, deactivate_say, final_requested = _make_say(
        execution_id, author, origin, stdout, stderr,
    )
    input_fn, getpass_fn, deactivate_input = _make_input_functions(
        execution_id, author, origin,
    )
    _ACTIVE_INPUT_FUNCTIONS = (input_fn, getpass_fn)
    shell.user_ns["say"] = say
    from py_agent.cell_printer import CellPrinter

    cell_printer = CellPrinter()
    shell.user_ns["preview"] = cell_printer
    sys.stdin = sys.__stdin__ = null_in
    sys.stdout = sys.__stdout__ = stdout
    sys.stderr = sys.__stderr__ = stderr
    builtins.input = _dispatch_input
    _secure_getpass_variants()
    if _RAW_CAPTURE is not None:
        _RAW_CAPTURE.activate(stdout, stderr)

    from IPython.core.history import HistoryOutput

    display_pub = shell.display_pub
    original_publish = display_pub.publish
    original_clear_output = display_pub.clear_output
    displayhook_delegate = shell.displayhook
    original_write_format_data = displayhook_delegate.write_format_data
    original_write_output_prompt = displayhook_delegate.write_output_prompt
    original_finish_displayhook = displayhook_delegate.finish_displayhook
    original_log_output = displayhook_delegate.log_output
    safe_execute_bundle: dict[str, Any] = {}
    rich_frame_count = 0
    rich_frame_bytes = 0

    def emit_rich(kind: str, data: Any = None, metadata: Any = None,
                  display_id: str | None = None, wait: bool = False,
                  output_read: dict[str, int] | None = None) -> bool:
        nonlocal rich_frame_count, rich_frame_bytes
        if kind == "clear":
            safe_data, safe_metadata = {"wait": bool(wait)}, {}
        else:
            safe_data, safe_metadata = _safe_mime_bundle(data, metadata)
            safe_data = _redact_json(safe_data)
            safe_metadata = _redact_json(safe_metadata)
            if output_read is not None:
                # These fixed protocol keys and integer offsets are structural,
                # not user text. Password redaction must not corrupt provenance.
                safe_data = {"text/plain": data["text/plain"]}  # already redacted and budgeted
                safe_metadata = {"py_agent_output_read": output_read}
            if not safe_data:
                return False
        frame = {
            "type": "rich_output",
            "version": PROTOCOL_VERSION,
            "execution_id": execution_id,
            "origin": origin,
            "author": author,
            "kind": kind,
            "data": safe_data,
            "metadata": safe_metadata,
            "display_id": _redact_text(display_id) if display_id is not None else None,
        }
        try:
            encoded = _encode_payload(frame)
        except ProtocolError:
            return False
        size = len(encoded) - 4
        if (rich_frame_count >= MAX_RICH_OUTPUT_FRAMES
                or rich_frame_bytes + size > MAX_RICH_OUTPUT_BYTES):
            return False
        stdout.flush()
        stderr.flush()
        if _RAW_CAPTURE is not None:
            _RAW_CAPTURE.barrier()
        _send(frame)
        rich_frame_count += 1
        rich_frame_bytes += size
        return True

    read_output_active = True
    read_output_chars = 0
    read_output_count = 0

    def read_output(index: int, start: int = 0, limit: int = 4000) -> None:
        """Display a bounded archive excerpt without creating another archive."""
        nonlocal read_output_chars, read_output_count
        if not read_output_active:
            raise RuntimeError("read_output() belongs to a completed cell; use the current helper")
        if type(index) is not int or not 1 <= index <= 1_000_000_000:
            raise ValueError("index must be a positive output ID")
        if type(start) is not int or start < 0:
            raise ValueError("start must be a nonnegative character offset")
        if type(limit) is not int or not 1 <= limit <= 4000:
            raise ValueError("limit must be between 1 and 4000 characters")
        outputs = shell.user_ns.get("outputs")
        if type(outputs) is not dict or index not in outputs:
            raise KeyError(f"No archived outputs[{index}] in this namespace")
        text = outputs[index]
        if type(text) is not str:
            raise TypeError(f"outputs[{index}] is not archived text")
        total = len(text)
        start = min(start, total)
        end = min(start + limit, total)
        header = f"outputs[{index}] chars {start}:{end} of {total}"
        excerpt = _redact_text(text[start:end])
        rendered = _redact_text(header) + "\n" + excerpt
        if read_output_count >= 8 or read_output_chars + len(excerpt) > 8000:
            raise ValueError("read_output() cell budget exceeded after redaction; use a smaller limit or another cell")
        if not emit_rich("display", {"text/plain": rendered}, output_read={
            "index": index, "start": start, "end": end, "total": total,
        }):
            raise RuntimeError("Unable to emit archive excerpt within output limits")
        read_output_count += 1
        read_output_chars += len(excerpt)

    shell.user_ns["read_output"] = read_output

    def publish(data: Any, metadata: Any = None, source: Any = None, *,
                transient: Any = None, update: bool = False) -> Any:
        stdout.flush()
        stderr.flush()
        if _RAW_CAPTURE is not None:
            _RAW_CAPTURE.barrier()
        raw_display_id = transient.get("display_id") if isinstance(transient, dict) else None
        display_id = (
            raw_display_id if isinstance(raw_display_id, str) and 0 < len(raw_display_id) <= 128 else None
        )
        kind = "update" if update and display_id is not None else "display"
        safe_data, safe_metadata = _safe_mime_bundle(data, metadata)
        safe_data = _redact_json(safe_data)
        safe_metadata = _redact_json(safe_metadata)
        if not safe_data or not emit_rich(kind, safe_data, safe_metadata, display_id):
            return None

        # Preserve IPython output history without invoking its local renderer,
        # which would print text/plain or terminal clear escapes into stdout.
        shell = display_pub.shell
        try:
            shell.history_manager.outputs[shell.execution_count - 1].append(
                HistoryOutput(output_type="display_data", bundle=safe_data)
            )
        except (AttributeError, IndexError, KeyError):
            pass
        return None

    def clear_output(wait: bool = False) -> None:
        emit_rich("clear", wait=wait)
        return None

    def capture_execute_result(format_dict: Any, md_dict: Any = None) -> None:
        nonlocal safe_execute_bundle
        safe_execute_bundle, safe_metadata = _safe_mime_bundle(format_dict, md_dict)
        safe_execute_bundle = _redact_json(safe_execute_bundle)
        safe_metadata = _redact_json(safe_metadata)
        if not emit_rich("execute_result", safe_execute_bundle, safe_metadata):
            safe_execute_bundle = {}

    def log_execute_result(_format_dict: Any) -> None:
        original_log_output(safe_execute_bundle)

    def suppress_output_prompt() -> None:
        # The terminal fallback consumes text/plain from the event; emitting
        # the legacy Out[n] stream prompt too would duplicate notebook output.
        return None

    def finish_displayhook() -> None:
        displayhook_delegate._is_active = False

    display_pub.publish = publish
    display_pub.clear_output = clear_output
    displayhook_delegate.write_format_data = capture_execute_result
    displayhook_delegate.write_output_prompt = suppress_output_prompt
    displayhook_delegate.finish_displayhook = finish_displayhook
    displayhook_delegate.log_output = log_execute_result

    status = "success"
    error: str | None = None
    try:
        result = shell.run_cell(source, store_history=True)
        cell_error = result.error_before_exec
        if cell_error is None:
            cell_error = result.error_in_exec
        if cell_error is not None:
            status = "cancelled" if isinstance(cell_error, KeyboardInterrupt) else "error"
            error = _error_text(cell_error)
    except KeyboardInterrupt as exc:
        status = "cancelled"
        error = _error_text(exc) or "Execution interrupted"
    except BaseException as exc:
        status = "error"
        error = _error_text(exc)
    finally:
        try:
            preview_text = cell_printer.finish(_redact_text)
            if preview_text:
                stdout.write(preview_text)
            stdout.flush()
            stderr.flush()
            if _RAW_CAPTURE is not None:
                _RAW_CAPTURE.finish_cell()
            stdout.flush()
            stderr.flush()
        finally:
            display_pub.publish = original_publish
            display_pub.clear_output = original_clear_output
            displayhook_delegate.write_format_data = original_write_format_data
            displayhook_delegate.write_output_prompt = original_write_output_prompt
            displayhook_delegate.finish_displayhook = original_finish_displayhook
            displayhook_delegate.log_output = original_log_output
            read_output_active = False
            deactivate_say()
            deactivate_input()
            _ACTIVE_INPUT_FUNCTIONS = None
            # Do not let a cell's reassignment of sys.std* poison later cells.
            sys.stdin = sys.__stdin__ = null_in
            sys.stdout = sys.__stdout__ = null_out
            sys.stderr = sys.__stderr__ = null_err
            builtins.input = _dispatch_input
            _secure_getpass_variants()

    stdout_text, stdout_chars, stdout_omitted = stdout.captured()
    stderr_text, stderr_chars, stderr_omitted = stderr.captured()
    output_reference = None
    original_chars = stdout_chars + stdout_omitted + stderr_chars + stderr_omitted
    if original_chars > MAX_VISIBLE_OUTPUT_CHARS:
        # Model and terminal receive only the reference. Keep bounded text in
        # the persistent Python namespace so the agent can inspect small slices.
        # stdout then stderr when both exist; this is text, not a replay of
        # interleaved stream timing. Do not retain unbounded subprocess output.
        retained = (stdout_text + stderr_text)[:MAX_STORED_OUTPUT_CHARS]
        omitted = original_chars - len(retained)
        output_reference = {
            "index": _store_output(shell, retained),
            "original_chars": original_chars,
            "retained_chars": len(retained),
            "omitted_chars": omitted,
        }

    _send({
        "type": "result",
        "version": PROTOCOL_VERSION,
        "execution_id": execution_id,
        "origin": origin,
        "author": author,
        "status": status,
        "error": error,
        "final_requested": final_requested(),
        "output_reference": output_reference,
    })


_NULL_STREAMS: tuple[Any, Any, Any]


def main() -> None:
    global _NULL_STREAMS
    _NULL_STREAMS = _initialize_stdio()

    from IPython.core.interactiveshell import InteractiveShell
    from traitlets.config import Config

    config = Config()
    config.HistoryManager.hist_file = ":memory:"
    config.HistoryManager.db_cache_size = 0
    config.InteractiveShell.colors = "nocolor"
    collapsed = _CollapsedStorage()
    shell = InteractiveShell.instance(
        config=config,
        user_ns={"say": lambda text, final=False: print(text), "memories": [], "outputs": {},
                 "collapsed": collapsed.archives},
    )
    _install_noninteractive_system(shell)
    _send({"type": "ready", "version": PROTOCOL_VERSION})

    try:
        while True:
            frame = _receive()
            if frame is None:
                break
            request = _validate_request(frame)
            if request is None:
                break
            kind = request[0]
            if kind == "execute":
                _, execution_id, author, source, origin = request
                _run_cell(shell, execution_id, author, source, origin)
            elif kind == "store_collapsed":
                _, request_id, offset, text, final = request
                index = collapsed.append(request_id, offset, text, final)
                if index is None:
                    _send({"type": "collapsed_chunk_stored", "version": PROTOCOL_VERSION,
                           "request_id": request_id, "offset": collapsed.offset})
                else:
                    # Rebinding the public name must not discard earlier archives.
                    shell.user_ns["collapsed"] = collapsed.archives
                    _send({"type": "collapsed_stored", "version": PROTOCOL_VERSION,
                           "request_id": request_id, "index": index})
            elif kind == "store_outputs":
                _, request_id, texts = request
                indexes = [_store_output(shell, _redact_text(text)) for text in texts]
                _send({"type": "outputs_stored", "version": PROTOCOL_VERSION,
                       "request_id": request_id, "indexes": indexes})
            elif kind == "complete":
                _, request_id, code, cursor_pos = request
                _complete(shell, request_id, code, cursor_pos)
            else:
                _, request_id, code, cursor_pos, detail_level = request
                _inspect_name(shell, request_id, code, cursor_pos, detail_level)
    finally:
        if _RAW_CAPTURE is not None:
            _RAW_CAPTURE.close()


if __name__ == "__main__":
    main()
