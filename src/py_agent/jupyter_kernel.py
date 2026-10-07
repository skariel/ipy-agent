"""Optional Jupyter protocol adapter for the configured coordinator.

Cells are submitted once through the same coordinator and persistent executor
as the terminal frontend. This module creates no second Python namespace or
agent loop. Managed-process lifecycle and private connection metadata live in
:mod:`py_agent.kernel_sessions`.
"""
from __future__ import annotations

import argparse
import asyncio
import codeop
from collections.abc import Awaitable, Callable, Mapping
import copy
import importlib
import inspect
import re
import signal
from types import FrameType
from typing import TYPE_CHECKING, Any, Protocol, TypeGuard, cast

from .contracts import (
    MAX_INPUT_VALUE_CHARS,
    CompletenessResult,
    CompletionResult,
    ExecutorCapabilityError,
    InputReply,
    InputRequest,
    InputUnavailableError,
    InspectionResult,
    OutputEvent,
    ProgressCallback,
)

_IPYKERNEL_IMPORT_ERROR: ImportError | None = None

if TYPE_CHECKING:
    class _IPyKernelBase:
        """Typed initializer boundary for the optional notebook runtime."""

        def __init__(self, **kwargs: Any) -> None: ...
else:
    try:
        from ipykernel.kernelbase import Kernel as _IPyKernelBase
    except ImportError as exc:  # Jupyter support is optional.
        _IPyKernelBase = None
        _IPYKERNEL_IMPORT_ERROR = exc
    else:
        _IPYKERNEL_IMPORT_ERROR = None


class _Coordinator(Protocol):
    async def submit(self, frontend_id: str, code: str, **kwargs: object) -> object: ...

    async def interrupt(self) -> None: ...

    async def close(self) -> None: ...


class _ConfigurationStore(Protocol):
    @property
    def snapshot(self) -> Mapping[str, Any]: ...

# Set only by main() before IPKernelApp constructs the protocol kernel.
_COORDINATOR_FACTORY: Callable[[], object] | None = None
MAX_STDIN_REPLY_DISCARDS = 32
JUPYTER_STDIN_TIMEOUT = 300


def jupyter_available() -> bool:
    """Return whether the optional ipykernel protocol runtime is installed."""
    return _IPyKernelBase is not None


def _valid_index(value: object, maximum: int) -> TypeGuard[int]:
    return type(value) is int and 0 <= value <= maximum


def _thaw_json(value: Any) -> Any:
    """Convert immutable contract containers into ordinary protocol JSON data."""
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw_json(item) for item in value]
    return value


def _error_content(error: object, *, default_name: str = "AgentExecutionError") -> dict[str, Any]:
    """Convert coordinator/executor text into the standard Jupyter error shape."""
    if isinstance(error, BaseException):
        name = type(error).__name__
        value = str(error)
        text = f"{name}: {value}" if value else name
    else:
        text = str(error) if error is not None else "Execution failed"
        first_line = text.splitlines()[0] if text.splitlines() else text
        name, separator, value = first_line.partition(":")
        if not separator or not name.isidentifier():
            name, value = default_name, text
        else:
            value = value.lstrip()
    return {
        "ename": name,
        "evalue": value,
        # The existing executor exposes an error string, not a Python traceback.
        "traceback": [text],
    }


def _is_input_unavailable(error: object) -> bool:
    text = str(error).lower()
    return (
        "input is unavailable" in text or "input unavailable" in text
        or "stdin is unavailable" in text or "stdin unavailable" in text
    )


class _History:
    """Small, adapter-local Jupyter request history (not a Python namespace)."""

    SESSION = 1

    def __init__(self) -> None:
        self._entries: list[tuple[int, int, str]] = []

    def add(self, line: int, source: str) -> None:
        self._entries.append((self.SESSION, line, source))

    def query(self, request: Mapping[str, Any]) -> list[tuple[Any, ...]]:
        access_type = request.get("hist_access_type", "tail")
        if access_type not in ("range", "tail", "search"):
            return []
        session = request.get("session", 0)
        if type(session) is not int:
            return []
        entries = list(self._entries)
        if session not in (0, -1):
            entries = [entry for entry in entries if entry[0] == session]

        if access_type == "range":
            start, stop = request.get("start", 1), request.get("stop", 2**63 - 1)
            if type(start) is not int or type(stop) is not int:
                return []
            entries = [entry for entry in entries if start <= entry[1] < stop]
        elif access_type == "tail":
            count = request.get("n", 10)
            if type(count) is not int or count < 0:
                return []
            entries = entries[-count:] if count else []
        else:
            pattern = request.get("pattern", "")
            if not isinstance(pattern, str):
                return []
            try:
                expression = re.compile(pattern)
            except re.error:
                return []
            entries = [entry for entry in entries if expression.search(entry[2])]
            if request.get("unique", False):
                seen: set[str] = set()
                unique_entries = []
                for entry in reversed(entries):
                    if entry[2] not in seen:
                        seen.add(entry[2])
                        unique_entries.append(entry)
                entries = list(reversed(unique_entries))

        if bool(request.get("raw", False)):
            return entries
        # This adapter has no coordinator history/output repository. The
        # protocol's output slot is therefore None, even when requested.
        return [(*entry[:2], (entry[2], None)) for entry in entries]


class _CoordinatorKernelMethods:
    """Protocol methods shared by the real kernel and dependency-free tests."""

    implementation = "py-agent"
    implementation_version = "0.1.0"
    language = "python"
    language_version = "3"
    execution_count: int

    def _initialize_adapter(self, coordinator: object) -> None:
        for method in ("submit", "interrupt", "close"):
            if not callable(getattr(coordinator, method, None)):
                raise TypeError(f"Coordinator must provide {method}()")
        self.coordinator = cast(_Coordinator, coordinator)
        self._execute_lock = asyncio.Lock()
        self._history = _History()
        self._active_execution = False
        self._interrupt_requested = False
        self._interrupt_task: asyncio.Task[None] | None = None
        self._request_sequence = 0
        self._stop_queued_through = 0

    @property
    def language_info(self) -> dict[str, Any]:
        return {
            "name": "python",
            "version": self.language_version,
            "mimetype": "text/x-python",
            "file_extension": ".py",
            "pygments_lexer": "ipython3",
            "codemirror_mode": {"name": "ipython", "version": 3},
        }

    @staticmethod
    def _normalize_parent(parent: object) -> Mapping[str, Any] | None:
        if not isinstance(parent, Mapping):
            return None
        if isinstance(parent.get("header"), Mapping):
            return parent
        if isinstance(parent.get("msg_id"), str):
            return {"header": parent}
        return None

    def _current_parent(self) -> Mapping[str, Any] | None:
        getter = getattr(self, "get_parent", None)
        if callable(getter):
            for channel in ("shell", None):
                try:
                    parent = getter() if channel is None else getter(channel)
                except (TypeError, KeyError, AttributeError):
                    continue
                normalized = self._normalize_parent(parent)
                if normalized is not None:
                    return normalized
        for name in ("_parent", "_parent_header"):
            normalized = self._normalize_parent(getattr(self, name, None))
            if normalized is not None:
                return normalized
        return None

    def _frontend_id(self, parent: Mapping[str, Any] | None) -> str:
        header: object = parent.get("header") if parent is not None else None
        if isinstance(header, Mapping):
            session = header.get("session")
            if isinstance(session, str) and session and "\x00" not in session:
                return "jupyter:" + session[:120]
        return "jupyter:unknown"

    def _send_iopub(
        self,
        message_type: str,
        content: Mapping[str, Any],
        *,
        silent: bool = False,
        parent: Mapping[str, Any] | None = None,
    ) -> None:
        if silent:
            return
        socket = getattr(self, "iopub_socket", None)
        session = getattr(self, "session", None)
        # Build the message with the request's saved parent. This prevents a
        # later shell request from stealing correlation if dispatch is concurrent.
        if parent is not None and session is not None:
            make_message = getattr(session, "msg", None)
            send = getattr(session, "send", None)
            if callable(make_message) and callable(send):
                message = make_message(message_type, dict(content), parent=parent)
                send(socket, message)
                return
        sender = getattr(self, "send_response", None)
        if not callable(sender):
            raise RuntimeError("Jupyter IOPub transport is unavailable")
        sender(socket, message_type, dict(content))

    async def _optional_core_action(self, name: str, *args: object, **kwargs: object) -> object | None:
        action = getattr(self.coordinator, name, None)
        if not callable(action):
            return None
        result = action(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return cast(object, result)

    async def _required_core_action(
        self, name: str, capability: str, *args: object,
    ) -> object:
        action = getattr(self.coordinator, name, None)
        if not callable(action):
            raise ExecutorCapabilityError(capability)
        result = action(*args)
        if inspect.isawaitable(result):
            result = await result
        return cast(object, result)

    @staticmethod
    def _supports_keyword(method: object, keyword: str) -> bool:
        try:
            parameters = inspect.signature(cast(Callable[..., object], method)).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.name == keyword or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )

    async def _submit_with_input(
        self,
        frontend_id: str,
        code: str,
        *,
        allow_stdin: bool,
        input_handler: Callable[[InputRequest], Awaitable[InputReply]] | None,
        on_progress: ProgressCallback | None = None,
    ) -> object:
        submit = self.coordinator.submit
        kwargs: dict[str, object] = {}
        if self._supports_keyword(submit, "allow_stdin"):
            kwargs["allow_stdin"] = allow_stdin
        if self._supports_keyword(submit, "input_handler"):
            kwargs["input_handler"] = input_handler if allow_stdin else None
        if on_progress is not None and self._supports_keyword(submit, "on_progress"):
            kwargs["on_progress"] = on_progress
        return await submit(frontend_id, code, **kwargs)

    async def _stdin_exchange(
        self,
        request: InputRequest,
        parent: Mapping[str, Any],
        ident: object,
    ) -> InputReply:
        """Exchange stdin messages on the kernel loop, never from a worker thread."""
        try:
            import zmq

            zmq_asyncio = getattr(zmq, "asyncio", None)
            if zmq_asyncio is None:
                zmq_asyncio = importlib.import_module("zmq.asyncio")
            poller_type = getattr(zmq_asyncio, "Poller", None)
        except Exception as exc:
            raise InputUnavailableError("Async Jupyter stdin transport is unavailable") from exc
        socket = getattr(self, "stdin_socket", None)
        session = getattr(self, "session", None)
        if socket is None or session is None or ident is None:
            raise InputUnavailableError("Jupyter stdin channel is unavailable for this frontend")
        if not callable(poller_type):
            raise InputUnavailableError("Async Jupyter stdin polling is unavailable")

        def identity_parts(value: object) -> tuple[bytes, ...] | None:
            if isinstance(value, (bytes, bytearray, memoryview)):
                return (bytes(value),)
            if isinstance(value, (tuple, list)) and all(
                isinstance(part, (bytes, bytearray, memoryview)) for part in value
            ):
                return tuple(bytes(part) for part in value)
            return None

        expected_identity = identity_parts(ident)
        if not expected_identity:
            raise InputUnavailableError("Jupyter could not identify the stdin request owner")
        message = session.msg(
            "input_request",
            {"prompt": request.prompt, "password": request.password},
            parent=parent,
        )
        header = message.get("header") if isinstance(message, Mapping) else None
        request_message_id = header.get("msg_id") if isinstance(header, Mapping) else None
        if not isinstance(request_message_id, str) or not request_message_id:
            raise InputUnavailableError("Jupyter could not correlate its stdin request")

        try:
            poller = poller_type()
        except Exception as exc:
            raise InputUnavailableError("Could not initialize async Jupyter stdin polling") from exc
        registered = False
        try:
            registered = True
            try:
                poller.register(socket, zmq.POLLIN)
            except Exception as exc:
                raise InputUnavailableError(
                    "Async Jupyter stdin polling cannot monitor the kernel-owned socket",
                ) from exc
            # Serialize with the Jupyter Session but send nonblocking: a sync
            # Session.send() may wait on ZMQ backpressure and stall the kernel loop.
            serializer = getattr(session, "serialize", None)
            send_raw = getattr(session, "send_raw", None)
            if not callable(serializer) or not callable(send_raw):
                raise InputUnavailableError("Nonblocking Jupyter stdin send is unavailable")
            try:
                adapt_version = getattr(session, "adapt_version", 0)
                if adapt_version:
                    from jupyter_client.adapter import adapt

                    message = adapt(message, adapt_version)
                serialized = serializer(message)
                send_raw(socket, serialized, flags=zmq.DONTWAIT, ident=ident)
            except Exception as exc:
                raise InputUnavailableError("Could not send the Jupyter stdin request nonblocking") from exc
            discarded = 0
            while True:
                try:
                    events = await poller.poll(50)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise InputUnavailableError("Async Jupyter stdin polling failed") from exc
                if not events:
                    continue
                try:
                    received = session.recv(socket, mode=zmq.NOBLOCK)
                except getattr(zmq, "Again", Exception):
                    continue
                if isinstance(received, tuple) and len(received) == 2:
                    identities, reply = received
                else:
                    identities, reply = None, received
                if identity_parts(identities) != expected_identity:
                    discarded += 1
                    if discarded > MAX_STDIN_REPLY_DISCARDS:
                        raise InputUnavailableError("Jupyter stdin replies came from a different frontend")
                    continue
                if not isinstance(reply, Mapping):
                    discarded += 1
                    if discarded > MAX_STDIN_REPLY_DISCARDS:
                        raise InputUnavailableError("Jupyter stdin received too many unrelated replies")
                    continue
                msg_header = reply.get("header")
                parent_header = reply.get("parent_header")
                msg_type = msg_header.get("msg_type") if isinstance(msg_header, Mapping) else None
                reply_parent_id = (
                    parent_header.get("msg_id") if isinstance(parent_header, Mapping) else None
                )
                if msg_type != "input_reply" or reply_parent_id != request_message_id:
                    discarded += 1
                    if discarded > MAX_STDIN_REPLY_DISCARDS:
                        raise InputUnavailableError("Jupyter stdin received too many unrelated replies")
                    continue
                content = reply.get("content")
                if (not isinstance(content, Mapping) or set(content) != {"value"}
                        or not isinstance(content.get("value"), str)
                        or len(content["value"]) > MAX_INPUT_VALUE_CHARS):
                    raise InputUnavailableError("Jupyter returned a malformed or oversized stdin reply")
                return InputReply(
                    request.origin, request.sequence, request.owner_frontend_id,
                    value=content["value"], password=request.password,
                )
        finally:
            if registered:
                try:
                    poller.unregister(socket)
                except Exception:
                    pass

    async def _request_stdin(
        self,
        request: InputRequest,
        *,
        parent: Mapping[str, Any] | None,
        ident: object,
        allow_stdin: bool,
    ) -> InputReply:
        if not allow_stdin:
            raise InputUnavailableError(
                "Interactive input is unavailable because this Jupyter request set allow_stdin=false",
            )
        if parent is None or request.owner_frontend_id != self._frontend_id(parent):
            raise InputUnavailableError("Jupyter stdin request has no matching saved frontend owner")
        try:
            return await asyncio.wait_for(
                self._stdin_exchange(request, parent, ident),
                timeout=JUPYTER_STDIN_TIMEOUT,
            )
        except TimeoutError:
            raise InputUnavailableError("Jupyter stdin reply timed out") from None

    @staticmethod
    def _event_key(event: object) -> tuple[str | None, int | None]:
        origin = getattr(event, "origin", None)
        return (
            getattr(origin, "request_id", None),
            getattr(event, "sequence", None),
        )

    @staticmethod
    def _message_not_already_delivered(
        message: str,
        delivered_events: tuple[object, ...],
        status_events: tuple[object, ...] = (),
    ) -> str:
        say_texts = []
        step_limit_texts = []
        for event in delivered_events:
            data = getattr(event, "data", {})
            metadata = getattr(event, "metadata", {})
            if (getattr(event, "kind", None) in ("display", "execute_result", "update")
                    and getattr(metadata, "get", lambda _key: None)("py_agent_source") == "say"):
                value = data.get("text/plain") if isinstance(data, Mapping) else None
                if isinstance(value, str):
                    say_texts.append(value)
            if (getattr(event, "kind", None) == "progress"
                    and isinstance(data, Mapping) and data.get("phase") == "step_limit"):
                value = data.get("text")
                if isinstance(value, str):
                    step_limit_texts.append(value)
        for event in status_events:
            data = getattr(event, "data", {})
            if (getattr(event, "kind", None) == "progress"
                    and isinstance(data, Mapping) and data.get("phase") == "step_limit"):
                value = data.get("text")
                if isinstance(value, str):
                    step_limit_texts.append(value)
        if say_texts:
            prefix = "\n".join(say_texts)
            if message == prefix:
                message = ""
            elif message.startswith(prefix + "\n"):
                message = message[len(prefix) + 1:]
        for text in step_limit_texts:
            if message == text:
                message = ""
            elif message.endswith("\n" + text):
                message = message[:-(len(text) + 1)]
        return message

    async def do_execute(
        self,
        code: str,
        silent: bool,
        store_history: bool = True,
        user_expressions: Mapping[str, str] | None = None,
        allow_stdin: bool = False,
        stop_on_error: bool = True,
    ) -> dict[str, Any]:
        """Submit one cell and keep the base kernel's busy state until it ends.

        ipykernel's request dispatcher publishes busy/idle and performs the
        correlated shell reply. A single coordinator submission may represent
        an entire provider/execution turn; generated execution is never
        submitted separately by this adapter.
        """
        if not isinstance(allow_stdin, bool):
            allow_stdin = bool(allow_stdin)
        self._request_sequence += 1
        request_sequence = self._request_sequence
        if not isinstance(stop_on_error, bool):
            stop_on_error = bool(stop_on_error)
        if not isinstance(code, str):
            return {"status": "error", "execution_count": self.execution_count,
                    **_error_content("Cell source must be text", default_name="TypeError"),
                    "user_expressions": {}, "payload": []}
        if not isinstance(silent, bool):
            silent = bool(silent)
        if not isinstance(store_history, bool):
            store_history = bool(store_history)
        current_parent = self._current_parent()
        parent = copy.deepcopy(current_parent) if current_parent is not None else None
        parent_ident = copy.deepcopy(getattr(self, "_parent_ident", None))

        async with self._execute_lock:
            if request_sequence <= self._stop_queued_through:
                return {
                    "status": "abort",
                    "execution_count": self.execution_count,
                    "user_expressions": {},
                    "payload": [],
                }
            # Jupyter increments for non-silent submissions. History storage is
            # independently controlled by store_history; silent implies neither.
            if not silent:
                self.execution_count += 1
                self._send_iopub("execute_input", {
                    "code": code,
                    "execution_count": self.execution_count,
                }, parent=parent)
                if store_history:
                    self._history.add(self.execution_count, code)

            self._active_execution = True
            self._interrupt_requested = False
            delivered_progress_events: list[OutputEvent] = []
            event_error_published = False

            async def on_progress(event: OutputEvent) -> None:
                nonlocal event_error_published
                self.publish_output_event(
                    event, parent=parent, silent=silent,
                    execution_count=self.execution_count,
                )
                delivered_progress_events.append(event)
                if event.kind == "error":
                    event_error_published = True

            try:
                if code.strip():
                    # Route exactly as the terminal does: ordinary text asks
                    # the agent; @, ! and % select direct IPython execution.
                    async def input_handler(input_request: InputRequest) -> InputReply:
                        return await self._request_stdin(
                            input_request,
                            parent=parent,
                            ident=parent_ident,
                            allow_stdin=allow_stdin,
                        )

                    submission = await self._submit_with_input(
                        self._frontend_id(parent), code,
                        allow_stdin=allow_stdin, input_handler=input_handler,
                        on_progress=on_progress,
                    )
                else:
                    submission = None
            except asyncio.CancelledError:
                if not self._interrupt_requested:
                    raise
                error = _error_content("Execution interrupted", default_name="KeyboardInterrupt")
                if stop_on_error:
                    self._stop_queued_through = max(self._stop_queued_through, self._request_sequence)
                self._send_iopub("error", error, silent=silent, parent=parent)
                return {
                    "status": "error",
                    "execution_count": self.execution_count,
                    **error,
                    "user_expressions": self._user_expression_errors(user_expressions),
                    "payload": [],
                }
            except Exception as exc:
                error = _error_content(exc)
                if stop_on_error:
                    self._stop_queued_through = max(self._stop_queued_through, self._request_sequence)
                self._send_iopub("error", error, silent=silent, parent=parent)
                return {
                    "status": "error",
                    "execution_count": self.execution_count,
                    **error,
                    "user_expressions": self._user_expression_errors(user_expressions),
                    "payload": [],
                }
            finally:
                self._active_execution = False
                self._interrupt_requested = False

            result = getattr(submission, "result", None) if submission is not None else None
            message = getattr(submission, "message", "") if submission is not None else ""
            raw_events = getattr(submission, "events", ()) if submission is not None else ()
            events = tuple(raw_events) if isinstance(raw_events, (tuple, list)) else ()
            if isinstance(message, str):
                message = self._message_not_already_delivered(
                    message, tuple(delivered_progress_events), status_events=events,
                )
            has_event_flow = bool(events or delivered_progress_events)
            delivered_keys = {
                self._event_key(event) for event in delivered_progress_events
            }
            for event in events:
                if self._event_key(event) in delivered_keys:
                    continue
                metadata = getattr(event, "metadata", {})
                if getattr(metadata, "get", lambda _key: None)("py_agent_source") == "say":
                    continue  # say() retains its existing text-message presentation.
                if getattr(event, "kind", None) == "error":
                    event_error_published = True
                self.publish_output_event(
                    event, parent=parent, silent=silent,
                    execution_count=self.execution_count,
                )

            if result is None:
                if message:
                    self._send_iopub("stream", {
                        "name": "stdout",
                        "text": str(message) + ("" if str(message).endswith("\n") else "\n"),
                    }, silent=silent, parent=parent)
                status = "ok"
                error_fields: dict[str, Any] = {}
            else:
                if not has_event_flow:
                    executions = getattr(submission, "executions", ()) or ((None, result),)
                    for _execution, cell_result in executions:
                        for stream_name in ("stdout", "stderr"):
                            text = getattr(cell_result, stream_name, "")
                            if text:
                                self._send_iopub("stream", {"name": stream_name, "text": text},
                                                 silent=silent, parent=parent)
                if message:
                    self._send_iopub("stream", {
                        "name": "stdout", "text": str(message) + ("" if str(message).endswith("\n") else "\n"),
                    }, silent=silent, parent=parent)
                result_status = getattr(result, "status", "error")
                if result_status == "success":
                    status, error_fields = "ok", {}
                else:
                    if result_status == "cancelled":
                        error_fields = _error_content(
                            getattr(result, "error", None) or "Execution interrupted",
                            default_name="KeyboardInterrupt",
                        )
                    else:
                        error_text = getattr(result, "error", None) or f"Execution ended with status {result_status}"
                        error_fields = _error_content(
                            error_text,
                            default_name="InteractiveInputUnavailable" if _is_input_unavailable(error_text)
                            else "AgentExecutionError",
                        )
                    status = "error"
                    if stop_on_error:
                        self._stop_queued_through = max(self._stop_queued_through, self._request_sequence)
                    if not event_error_published:
                        self._send_iopub("error", error_fields, silent=silent, parent=parent)

            return {
                "status": status,
                "execution_count": self.execution_count,
                **error_fields,
                "user_expressions": self._user_expression_errors(user_expressions),
                "payload": [],
            }

    @staticmethod
    def _user_expression_errors(expressions: Mapping[str, str] | None) -> dict[str, Any]:
        if not isinstance(expressions, Mapping):
            return {}
        return {
            name: {
                "status": "error",
                "ename": "NotImplementedError",
                "evalue": "The coordinator does not expose user-expression evaluation",
                "traceback": [],
            }
            for name in expressions
        }

    async def do_complete(self, code: str, cursor_pos: int | None = None) -> dict[str, Any]:
        if not isinstance(code, str):
            code = ""
        if cursor_pos is None:
            cursor_pos = len(code)
        if not _valid_index(cursor_pos, len(code)):
            cursor_pos = len(code)
        try:
            value = await self._required_core_action("complete", "completion", code, cursor_pos)
            matches: object
            start: object
            end: object
            metadata: object
            if isinstance(value, CompletionResult):
                matches = value.matches
                start, end, metadata = value.cursor_start, value.cursor_end, value.metadata
            elif isinstance(value, Mapping):
                matches = value.get("matches")
                start, end = value.get("cursor_start"), value.get("cursor_end")
                metadata = value.get("metadata", {})
            else:
                raise TypeError("Coordinator completion returned an invalid result")
            if (not isinstance(matches, (list, tuple)) or not all(type(item) is str for item in matches)
                    or not _valid_index(start, len(code)) or not _valid_index(end, len(code))
                    or start > end or not isinstance(metadata, Mapping)):
                raise TypeError("Coordinator completion returned an invalid Jupyter result")
            return {
                "status": "ok",
                "matches": list(matches),
                "cursor_start": start,
                "cursor_end": end,
                "metadata": _thaw_json(metadata),
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return {
                "status": "error", "matches": [], "cursor_start": cursor_pos,
                "cursor_end": cursor_pos, "metadata": {},
                **_error_content(exc, default_name="ExecutorCapabilityError"),
            }

    async def do_inspect(
        self,
        code: str,
        cursor_pos: int,
        detail_level: int = 0,
    ) -> dict[str, Any]:
        if not isinstance(code, str):
            code = ""
        if not _valid_index(cursor_pos, len(code)):
            cursor_pos = len(code)
        if type(detail_level) is not int or detail_level not in (0, 1):
            detail_level = 0
        try:
            value = await self._required_core_action(
                "inspect", "inspection", code, cursor_pos, detail_level,
            )
            found: object
            data: object
            metadata: object
            if isinstance(value, InspectionResult):
                found, data, metadata = value.found, value.data, value.metadata
            elif isinstance(value, Mapping):
                found, data, metadata = value.get("found"), value.get("data"), value.get("metadata", {})
            else:
                raise TypeError("Coordinator inspection returned an invalid result")
            if type(found) is not bool or not isinstance(data, Mapping) or not isinstance(metadata, Mapping):
                raise TypeError("Coordinator inspection returned an invalid Jupyter result")
            if not found and data:
                raise TypeError("Coordinator returned data for an unsuccessful inspection")
            return {
                "status": "ok", "found": found,
                "data": _thaw_json(data), "metadata": _thaw_json(metadata),
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return {
                "status": "error", "found": False, "data": {}, "metadata": {},
                **_error_content(exc, default_name="ExecutorCapabilityError"),
            }

    async def do_is_complete(self, code: str) -> dict[str, Any]:
        if not isinstance(code, str):
            return {"status": "invalid", "indent": ""}
        value = await self._optional_core_action("is_complete", code)
        if isinstance(value, CompletenessResult):
            return {"status": value.status, "indent": value.indent}
        if isinstance(value, Mapping) and value.get("status") in ("complete", "incomplete", "invalid"):
            indent = value.get("indent", "")
            return {"status": value["status"], "indent": indent if isinstance(indent, str) else ""}
        # Match the built-in input routing when an older coordinator omits the
        # completeness action. Normal operation delegates this decision to core.
        if code.startswith("\\") and len(code) > 1 and code[1] in "@!%/":
            return {"status": "complete", "indent": ""}
        if code.startswith("/") or (code and not code.startswith(("@", "!", "%"))):
            return {"status": "complete", "indent": ""}
        if code.startswith("@"):
            code = code[1:]
        if not code.strip():
            return {"status": "complete", "indent": ""}
        # IPython's transformer understands magics and shell escapes without
        # consulting a live shell namespace. Fall back to stdlib if unavailable.
        try:
            from IPython.core.inputtransformer2 import TransformerManager

            status, indent = cast(Callable[[], Any], TransformerManager)().check_complete(code)
            return {"status": status, "indent": " " * min(indent, 80) if type(indent) is int and indent > 0 else ""}
        except ImportError:
            try:
                compiled = codeop.compile_command(code, symbol="exec")
            except (SyntaxError, OverflowError, ValueError):
                return {"status": "invalid", "indent": ""}
            return {"status": "incomplete" if compiled is None else "complete", "indent": ""}
        except (SyntaxError, OverflowError, ValueError):
            return {"status": "invalid", "indent": ""}

    async def do_history(
        self,
        hist_access_type: str,
        output: bool = False,
        raw: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        request = {"hist_access_type": hist_access_type, "output": output, "raw": raw, **kwargs}
        value = await self._optional_core_action("history", request)
        if isinstance(value, Mapping) and isinstance(value.get("history"), (list, tuple)):
            return {"history": list(value["history"])}
        return {"history": self._history.query(request)}

    def publish_output_event(
        self,
        event: OutputEvent,
        *,
        parent: Mapping[str, Any] | None = None,
        silent: bool = False,
        execution_count: int | None = None,
    ) -> None:
        """Publish one immutable submission event using standard IOPub types.

        The parent header is passed explicitly so output remains correlated to
        the submitted cell even if another client request arrives meanwhile.
        """
        if not isinstance(event, OutputEvent):
            raise TypeError("event must be an OutputEvent")
        data = _thaw_json(event.data)
        metadata = _thaw_json(event.metadata)
        if event.kind == "stream":
            name = data.pop("name", "stdout")
            if name not in ("stdout", "stderr"):
                name = "stdout"
            self._send_iopub("stream", {"name": name, "text": data.pop("text", "")},
                             silent=silent, parent=parent)
        elif event.kind in ("display", "execute_result", "update"):
            if metadata.get("py_agent_source") == "say":
                text = str(data.get("text/plain", ""))
                self._send_iopub("stream", {
                    "name": "stdout", "text": text + ("" if text.endswith("\n") else "\n"),
                }, silent=silent, parent=parent)
                return
            message_type = {
                "display": "display_data",
                "execute_result": "execute_result",
                "update": "update_display_data",
            }[event.kind]
            content: dict[str, Any] = {"data": data, "metadata": metadata}
            if event.kind == "execute_result":
                content["execution_count"] = (
                    self.execution_count if execution_count is None else execution_count
                )
            if event.display_id:
                content["transient"] = {"display_id": event.display_id}
            self._send_iopub(message_type, content, silent=silent, parent=parent)
        elif event.kind == "clear":
            wait = data.get("wait", False)
            if isinstance(wait, str):
                wait = wait.lower() == "true"
            self._send_iopub("clear_output", {"wait": bool(wait)}, silent=silent, parent=parent)
        elif event.kind == "error":
            self._send_iopub("error", _error_content(data.get("evalue", "Execution failed"),
                                                      default_name=data.get("ename", "AgentExecutionError")),
                             silent=silent, parent=parent)
        elif event.kind == "progress":
            text = str(data.get("text", ""))
            self._send_iopub("stream", {"name": "stdout", "text": text + ("" if text.endswith("\n") else "\n")},
                             silent=silent, parent=parent)

    def do_interrupt(self) -> dict[str, str]:
        """Schedule coordinator interruption through ipykernel's sync hook."""
        if self._active_execution:
            self._interrupt_requested = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return {"status": "error", "ename": "RuntimeError",
                    "evalue": "Coordinator interruption requires the kernel event loop"}
        self._interrupt_task = loop.create_task(self._interrupt_coordinator())
        return {"status": "ok"}

    async def _interrupt_coordinator(self) -> None:
        try:
            await self.coordinator.interrupt()
        except Exception:
            logger = getattr(self, "log", None)
            if logger is not None:
                logger.exception("Coordinator interrupt failed")

    async def do_shutdown(self, restart: bool) -> dict[str, Any]:
        """Close the selected coordinator; restart does not restore its namespace."""
        task = self._interrupt_task
        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)
        await self.coordinator.close()
        return {"status": "ok", "restart": bool(restart)}


if _IPyKernelBase is None:

    class JupyterKernel(_CoordinatorKernelMethods):
        """Unavailable unless the optional ipykernel dependency is installed."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            raise ImportError(
                "JupyterKernel requires the optional 'ipykernel' package; install ipykernel and jupyter_client"
            ) from _IPYKERNEL_IMPORT_ERROR

else:

    class JupyterKernel(_CoordinatorKernelMethods, _IPyKernelBase):  # type: ignore[no-redef]
        """ipykernel protocol frontend that delegates every cell to a Coordinator."""

        def __init__(self, coordinator: object | None = None, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            if coordinator is None:
                if not callable(_COORDINATOR_FACTORY):
                    raise RuntimeError(
                        "No coordinator configured; launch this kernel through py kernel install/start "
                        "with an explicit provider selection"
                    )
                coordinator = _COORDINATOR_FACTORY()
            self._initialize_adapter(coordinator)
            self.execution_count = 0


def _kernel_argument_parser() -> argparse.ArgumentParser:
    from .cli import _coordinator_options

    parser = argparse.ArgumentParser(
        prog="py-agent-kernel", add_help=False,
        description="Configured py-agent Jupyter kernel (provider selection is required).",
    )
    _coordinator_options(parser)
    return parser


def _interrupt_signal_handler(
    app: Any, kernel: JupyterKernel,
) -> Callable[[int, FrameType | None], None]:
    """Forward stock Jupyter signal-mode interrupts to the selected executor."""
    def handle_signal(_signum: int, _frame: FrameType | None) -> None:
        app.io_loop.add_callback(kernel.do_interrupt)

    return handle_signal


def _graceful_signal_handler(
    app: Any, kernel: JupyterKernel,
) -> Callable[[int, FrameType | None], None]:
    """Close the coordinator on SIGTERM before stopping this owned process."""
    shutting_down = False

    def handle_signal(_signum: int, _frame: FrameType | None) -> None:
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True

        async def close_and_stop() -> None:
            try:
                await kernel.do_shutdown(restart=False)
            finally:
                app.io_loop.stop()

        app.io_loop.add_callback(lambda: asyncio.create_task(close_and_stop()))

    return handle_signal


def main(argv: list[str] | None = None) -> int:
    """Run an owned IPKernelApp around the same coordinator used by ``py``."""
    coordinator = None
    app: Any = None
    journal = None
    if not jupyter_available():
        print(
            "py-agent Jupyter support requires the optional 'ipykernel' and 'jupyter_client' packages; "
            "install py-agent[jupyter]",
            file=__import__("sys").stderr,
        )
        return 2
    from . import kernel_sessions

    try:
        kernel_sessions.require_jupyter_runtime()
        args, kernel_arguments = _kernel_argument_parser().parse_known_args(argv)
        from . import cli

        selected = cli._prepare_configuration(args)
        store = cast(_ConfigurationStore, selected.store)
        if args.journal is not None:
            from .journal_worker import SQLiteJournalWorker
            journal = SQLiteJournalWorker(args.journal)
        coordinator = cli._build_coordinator(
            selected.provider,
            model=selected.model,
            api_base=selected.api_base,
            stream=selected.stream,
            effort=selected.effort,
            auth_file=args.pi_auth,
            context_window_tokens=cast(int, store.snapshot.get("context.window_tokens")),
            startup_timeout=cast(float, store.snapshot.get("executor.startup_timeout")),
            max_output_chars=cast(int, store.snapshot.get("executor.max_output_chars")),
            max_agent_steps=store.snapshot.get("agent.max_steps"),
            config_store=selected.store,
            config_path=selected.config_path,
            runtime=selected.runtime,
            router=selected.router,
            interpreter=selected.interpreter,
            executor=selected.executor,
            executor_wrappers=selected.executor_wrappers,
            journal=journal,
        )
        global _COORDINATOR_FACTORY
        _COORDINATOR_FACTORY = lambda: coordinator
        from ipykernel.kernelapp import IPKernelApp

        app = IPKernelApp.instance()
        app.kernel_class = JupyterKernel
        app.initialize(argv=kernel_arguments)
        kernel = app.kernel
        # Start the coordinator on the kernel's own asyncio/Tornado loop before
        # accepting requests; LocalExecutor subprocess transports stay on the
        # same loop for the complete session lifetime.
        app.io_loop.run_sync(coordinator.start)
        if hasattr(signal, "SIGINT"):
            signal.signal(signal.SIGINT, _interrupt_signal_handler(app, kernel))
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _graceful_signal_handler(app, kernel))
        app.start()
        if getattr(coordinator, "state", None) is not None and coordinator.state.value != "closed":
            app.io_loop.run_sync(coordinator.close)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"py-agent kernel: {exc}", file=__import__("sys").stderr)
        return 1
    finally:
        if coordinator is not None:
            state = getattr(coordinator, "state", None)
            if state is not None and getattr(state, "value", state) != "closed":
                try:
                    if app is not None and getattr(app, "io_loop", None) is not None:
                        app.io_loop.run_sync(coordinator.close)
                    else:
                        asyncio.run(coordinator.close())
                except Exception:
                    pass
        if coordinator is None and journal is not None:
            journal.close()
        _COORDINATOR_FACTORY = None


if __name__ == "__main__":
    raise SystemExit(main())
