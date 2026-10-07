"""SessionRuntime frontend component; orchestration remains in SessionRuntime."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable
import inspect
from typing import TYPE_CHECKING, Literal, Protocol, cast
from uuid import uuid4

from .contracts import (
    CompletenessResult,
    CompletionResult,
    ExecutionRequest,
    ExecutionResult,
    ExecutorCapabilityError,
    InspectionResult,
    Origin,
    OutputEvent,
    ProgressCallback,
    RoutedAction,
    UserAction,
)
from .coordinator_support import (
    State,
)

if TYPE_CHECKING:
    from .coordinator_runtime import SessionRuntime


class _InputRouter(Protocol):
    def route(self, action: UserAction, /) -> RoutedAction: ...


class _QueryCapabilities(Protocol):
    @property
    def completion(self) -> bool: ...

    @property
    def inspection(self) -> bool: ...


class _QueryExecutor(Protocol):
    @property
    def capabilities(self) -> _QueryCapabilities: ...


class _OutputObserver(Protocol):
    def observe(self, event: OutputEvent, /) -> Awaitable[None]: ...


class FrontendRouting:
    """Owns frontend state and behavior for one coordinator."""

    def __init__(self, coordinator: SessionRuntime) -> None:
        self.coordinator = coordinator
        self._event_sequence = 0
        self.best_effort_observer_failures = 0

    def _route_query(self, code: str) -> RoutedAction:
        """Apply the configured input router without dispatching an execution."""
        config = self.coordinator.current_config()
        revision = config.revision if config is not None else self.coordinator.config_revision
        origin = Origin(self.coordinator.session_id, uuid4().hex, "inspection", revision)
        if not code:
            return RoutedAction(origin, "ask", "")
        try:
            routed = cast(_InputRouter, self.coordinator.router).route(UserAction(origin, code))
        except ValueError:
            # Prefix-only editor buffers are valid completion/completeness
            # requests even though they are not executable submissions yet.
            if code.startswith("@") and not code[1:].strip():
                return RoutedAction(origin, "execute", code[1:], "ipython")
            if code.startswith("!") and not code[1:].strip():
                return RoutedAction(origin, "execute", code, "ipython")
            if code.startswith("%") and not code[1:].strip():
                return RoutedAction(origin, "execute", code, "ipython")
            if code.startswith("/") and not code[1:].strip():
                return RoutedAction(origin, "command", code[1:])
            raise
        if not isinstance(routed, RoutedAction) or routed.origin != origin:
            raise TypeError("Router returned an invalid query action")
        if routed.kind not in ("ask", "execute", "command") or not isinstance(routed.source, str):
            raise TypeError("Router returned an invalid query action")
        return routed

    async def complete(self, code: str, cursor_pos: int) -> CompletionResult:
        """Complete direct input in the selected executor's persistent namespace."""
        self.coordinator._check_query_cursor(code, cursor_pos)
        async with self.coordinator.lifecycle._lock:
            if (self.coordinator.lifecycle.state is not State.IDLE or self.coordinator.lifecycle._queue_worker is not None
                    or self.coordinator.lifecycle._pending_actions):
                raise RuntimeError("Session busy or unavailable")
            task = asyncio.current_task()
            self.coordinator.lifecycle.operation.task = task
            try:
                routed = self.coordinator.frontend._route_query(code)
                if routed.kind != "execute":
                    return CompletionResult((), cursor_pos, cursor_pos)
                offset = self.coordinator._query_prefix_offset(code, routed)
                if offset and cursor_pos == 0:
                    return CompletionResult((), 0, 0)
                capabilities = cast(_QueryExecutor, self.coordinator.executor).capabilities
                method = getattr(self.coordinator.executor, "complete", None)
                if not capabilities.completion or not callable(method):
                    raise ExecutorCapabilityError("completion")
                result = await method(routed.source, cursor_pos - offset)
                if not isinstance(result, CompletionResult):
                    raise TypeError("Executor completion must return CompletionResult")
                start, end = result.cursor_start + offset, result.cursor_end + offset
                if end > len(code) or start > end:
                    raise ValueError("Executor returned an invalid completion cursor range")
                return CompletionResult(result.matches, start, end, result.metadata)
            finally:
                if self.coordinator.lifecycle.operation.task is task:
                    self.coordinator.lifecycle.operation.task = None

    async def inspect(self, code: str, cursor_pos: int, detail_level: int = 0) -> InspectionResult:
        """Inspect a direct-input name in the selected worker without evaluating code."""
        self.coordinator._check_query_cursor(code, cursor_pos)
        if type(detail_level) is not int or detail_level not in (0, 1):
            raise ValueError("Inspection detail level must be 0 or 1")
        async with self.coordinator.lifecycle._lock:
            if (self.coordinator.lifecycle.state is not State.IDLE or self.coordinator.lifecycle._queue_worker is not None
                    or self.coordinator.lifecycle._pending_actions):
                raise RuntimeError("Session busy or unavailable")
            task = asyncio.current_task()
            self.coordinator.lifecycle.operation.task = task
            try:
                routed = self.coordinator.frontend._route_query(code)
                if routed.kind != "execute":
                    return InspectionResult(False)
                offset = self.coordinator._query_prefix_offset(code, routed)
                if offset and cursor_pos == 0:
                    return InspectionResult(False)
                capabilities = cast(_QueryExecutor, self.coordinator.executor).capabilities
                method = getattr(self.coordinator.executor, "inspect", None)
                if not capabilities.inspection or not callable(method):
                    raise ExecutorCapabilityError("inspection")
                result = await method(routed.source, cursor_pos - offset, detail_level)
                if not isinstance(result, InspectionResult):
                    raise TypeError("Executor inspection must return InspectionResult")
                return result
            finally:
                if self.coordinator.lifecycle.operation.task is task:
                    self.coordinator.lifecycle.operation.task = None

    async def is_complete(self, code: str) -> CompletenessResult:
        """Check routed input completeness without executing it or using the worker."""
        if not isinstance(code, str):
            return CompletenessResult("invalid")
        async with self.coordinator.lifecycle._lock:
            if (self.coordinator.lifecycle.state is not State.IDLE or self.coordinator.lifecycle._queue_worker is not None
                    or self.coordinator.lifecycle._pending_actions):
                raise RuntimeError("Session busy or unavailable")
            task = asyncio.current_task()
            self.coordinator.lifecycle.operation.task = task
            try:
                routed = self.coordinator.frontend._route_query(code)
                if routed.kind != "execute":
                    return CompletenessResult("complete")
                return self.coordinator._parse_completeness(routed.source)
            finally:
                if self.coordinator.lifecycle.operation.task is task:
                    self.coordinator.lifecycle.operation.task = None


    def new_output_event(
        self,
        origin: Origin,
        kind: Literal["stream", "display", "execute_result", "update", "clear", "error", "progress"],
        data: dict[str, object],
        *,
        display_id: str | None = None,
        metadata: dict[str, object] | None = None,
        author: Literal["user", "agent"] | None = None,
    ) -> OutputEvent:
        self._event_sequence += 1
        return OutputEvent(
            origin, self._event_sequence, kind, data, display_id, metadata or {}, author,
        )

    async def _dispatch_output_event(
        self,
        event: OutputEvent,
        *,
        on_progress: ProgressCallback | None,
        operation_id: str,
        expected_state: State,
    ) -> None:
        """Deliver one event serially and reject delivery after request invalidation."""
        if not self.coordinator.lifecycle.operation_is_current(operation_id, expected_state):
            raise asyncio.CancelledError
        for registration in self.coordinator._observer_registrations:
            observer = self.coordinator.output_observers[registration.qualified_name]
            try:
                delivered = cast(_OutputObserver, observer).observe(event)
                if not inspect.isawaitable(delivered):
                    raise TypeError(f"Observer {registration.qualified_name} must be async")
                await delivered
            except asyncio.CancelledError:
                raise
            except Exception:
                if registration.critical:
                    raise
                self.best_effort_observer_failures += 1
            if not self.coordinator.lifecycle.operation_is_current(operation_id, expected_state):
                raise asyncio.CancelledError
        if on_progress is not None:
            delivered = on_progress(event)
            if not inspect.isawaitable(delivered):
                raise TypeError("Request progress callback must be async")
            await delivered
            if not self.coordinator.lifecycle.operation_is_current(operation_id, expected_state):
                raise asyncio.CancelledError

    async def emit_progress(
        self,
        origin: Origin,
        data: dict[str, object],
        *,
        on_progress: ProgressCallback | None,
        operation_id: str,
        expected_state: State,
        author: Literal["user", "agent"] = "agent",
    ) -> OutputEvent:
        event = self.coordinator.frontend.new_output_event(
            origin, "progress", data, author=author,
        )
        await self.coordinator.frontend._dispatch_output_event(
            event, on_progress=on_progress, operation_id=operation_id,
            expected_state=expected_state,
        )
        return event

    async def publish_output(
        self,
        request: ExecutionRequest,
        result: ExecutionResult,
        *,
        on_progress: ProgressCallback | None,
        operation_id: str,
    ) -> tuple[OutputEvent, ...]:
        origin = request.origin
        events: list[OutputEvent] = []

        async def append(
            kind: Literal["stream", "display", "execute_result", "update", "clear", "error", "progress"],
            data: dict[str, object],
            *,
            display_id: str | None = None,
            metadata: dict[str, object] | None = None,
        ) -> None:
            event = self.coordinator.frontend.new_output_event(
                origin, kind, data, display_id=display_id, metadata=metadata,
                author=request.author,
            )
            events.append(event)
            await self.coordinator.frontend._dispatch_output_event(
                event, on_progress=on_progress, operation_id=operation_id,
                expected_state=State.EXECUTING,
            )

        delivered_streams = set()
        for output in result.output_events:
            if output.kind == "stream":
                data = dict(output.data)
                data["author"] = request.author
                delivered_streams.add(data.get("name"))
                await append("stream", data)
            else:
                await append(
                    output.kind, dict(output.data), display_id=output.display_id,
                    metadata=dict(output.metadata),
                )
        for stream, text in (("stdout", result.stdout), ("stderr", result.stderr)):
            if text and stream not in delivered_streams:
                await append("stream", {"name": stream, "text": text, "author": request.author})

        for say_output in result.say_outputs:
            if say_output.final and result.status != "success":
                continue  # Staged finals are not user-visible until successful completion.
            await append(
                "display", {"text/plain": self.coordinator._say_text(say_output.content)},
                metadata={"py_agent_source": "say", "final": say_output.final},
            )
        if result.error or result.status not in ("success", "error"):
            await append("error", {
                "ename": "ExecutionError", "evalue": result.error or result.status,
            })
        elif result.status == "error":
            await append("error", {
                "ename": "ExecutionError", "evalue": result.error or "Execution failed",
            })
        return tuple(events)

