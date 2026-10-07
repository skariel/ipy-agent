"""Single direct-cell phase; no queue or outer-operation cleanup ownership."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Literal
from uuid import uuid4

from .contracts import (
    ExecutionOutput,
    ExecutionRequest,
    InputHandler,
    Origin,
    ProgressCallback,
    RoutedAction,
    Submission,
)
from .coordinator_interfaces import DirectRuntime
from .coordinator_support import State


class DirectExecution:
    def __init__(self, coordinator: DirectRuntime) -> None:
        self.coordinator = coordinator

    async def execute(
        self, routed: RoutedAction, origin: Origin, operation_id: str, *,
        allow_stdin: bool,
        execution_input_handler: Callable[[Origin], InputHandler | None],
        execution_output_handler: Callable[[Origin, Literal["user", "agent"]], Callable[[ExecutionOutput], Awaitable[None]] | None],
        on_progress: ProgressCallback | None,
        llm_handler_factory: Callable[[Origin], Callable[[Any], Awaitable[str]]],
    ) -> Submission:
        """One user cell; caller retains operation cleanup and queue ownership."""
        coordinator = self.coordinator
        # Direct execution bypasses provider and agent policy exactly once.
        author: Literal["user", "agent"] = "user"
        if coordinator.lifecycle.operation.identity != operation_id or coordinator.lifecycle.state is not State.IDLE:
            raise asyncio.CancelledError from None
        coordinator.lifecycle.state = State.EXECUTING
        execution_origin = Origin(
            origin.session_id, origin.request_id, origin.frontend_id,
            origin.config_revision, None, uuid4().hex,
        )
        execution_request = ExecutionRequest(
            execution_origin, routed.source, author,
            routed.language if routed.language else "ipython",
            allow_stdin=allow_stdin,
            input_handler=execution_input_handler(execution_origin),
            output_handler=execution_output_handler(execution_origin, "user"),
            llm_handler=llm_handler_factory(execution_origin),
        )
        published_events = [await coordinator.frontend.emit_progress(
            execution_origin,
            {"phase": "execution_start", "author": author,
             "text": "Executing user cell."},
            on_progress=on_progress, operation_id=operation_id,
            expected_state=State.EXECUTING, author=author,
        )]
        result = await coordinator.lifecycle.execute_dispatched(execution_request)
        if not coordinator.lifecycle.operation_is_current(operation_id, State.EXECUTING):
            raise asyncio.CancelledError from None
        published_events.extend(await coordinator.frontend.publish_output(
            execution_request, result, on_progress=on_progress,
            operation_id=operation_id,
        ))
        published_events.append(await coordinator.frontend.emit_progress(
            execution_origin,
            {
                "phase": "cell_complete", "step": 1, "status": result.status,
                "text": f"User cell completed with status {result.status}.",
            },
            on_progress=on_progress, operation_id=operation_id,
            expected_state=State.EXECUTING, author=author,
        ))
        if not coordinator.lifecycle.operation_is_current(operation_id, State.EXECUTING):
            raise asyncio.CancelledError from None
        coordinator.lifecycle.set_state_unless_stopping(
            State.FAILED if result.status in ("uncertain", "cancelled") else State.IDLE,
        )
        return Submission(
            routed, result=result, message="\n".join(coordinator.visible_says(result)),
            execution=execution_request,
            say_outputs=coordinator.visible_say_outputs(result),
            events=tuple(published_events),
        )
