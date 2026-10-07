"""Safe-boundary dispatch of FIFO user cells, commands and steering."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from .configuration import ConfigSnapshot
from .contracts import (
    ExecutionRequest,
    ModelResponse,
    Origin,
    OutputEvent,
    QueueOutcome,
    RoutedAction,
    Submission,
    UserAction,
)
from .coordinator_io import RequestIO
from .coordinator_request import RequestScope
from .coordinator_support import (
    MAX_PENDING_ACTIONS as MAX_PENDING_ACTIONS,
)
from .coordinator_support import (
    State,
    _QueuedAction,
)

if TYPE_CHECKING:
    from .coordinator_interfaces import RequestRuntime


class _Router(Protocol):
    def route(self, action: UserAction) -> object: ...


class _Interpreter(Protocol):
    def interpret(self, response: ModelResponse) -> object: ...


class BoundaryQueue:
    """Reserve steering and dispatch queued cells/commands at safe boundaries."""

    def __init__(self, coordinator: RequestRuntime, scope: RequestScope, io: RequestIO) -> None:
        self.coordinator = coordinator
        self.scope = scope
        self.io = io

    async def run_queued_direct(self, item: _QueuedAction) -> None:
        """Execute a user cell at a model boundary without ending the agent turn."""
        coordinator, scope = self.coordinator, self.scope
        action = item.action
        queued_origin = action.origin
        execution_origin = Origin(
            queued_origin.session_id,
            queued_origin.request_id,
            queued_origin.frontend_id,
            queued_origin.config_revision,
            None,
            uuid4().hex,
        )
        execution_request = ExecutionRequest(
            execution_origin,
            action.source,
            "user",
            action.language or "ipython",
            allow_stdin=item.allow_stdin,
            input_handler=self.io.execution_input_handler(
                execution_origin,
                enabled=item.allow_stdin,
                handler=item.input_handler,
                owner=queued_origin.frontend_id,
            ),
            output_handler=self.io.execution_output_handler(
                execution_origin,
                "user",
                progress=item.on_progress,
            ),
            llm_handler=self.io.llm(execution_origin),
        )
        previous_state = coordinator.lifecycle.state
        coordinator.lifecycle.state = State.EXECUTING
        events: list[OutputEvent] = []
        dispatched = False
        try:
            events.append(
                await coordinator.frontend.emit_progress(
                    execution_origin,
                    {"phase": "execution_start", "author": "user", "text": "Executing queued user cell."},
                    on_progress=item.on_progress,
                    operation_id=scope.operation_id,
                    expected_state=State.EXECUTING,
                    author="user",
                )
            )
            dispatched = True
            result = await coordinator.lifecycle.execute_dispatched(execution_request)
            if not coordinator.lifecycle.operation_is_current(scope.operation_id, State.EXECUTING):
                raise asyncio.CancelledError from None
            events.extend(
                await coordinator.frontend.publish_output(
                    execution_request,
                    result,
                    on_progress=item.on_progress,
                    operation_id=scope.operation_id,
                )
            )
            events.append(
                await coordinator.frontend.emit_progress(
                    execution_origin,
                    {
                        "phase": "cell_complete",
                        "step": 1,
                        "status": result.status,
                        "text": f"Queued user cell completed with status {result.status}.",
                    },
                    on_progress=item.on_progress,
                    operation_id=scope.operation_id,
                    expected_state=State.EXECUTING,
                    author="user",
                )
            )
            if not coordinator.lifecycle.operation_is_current(scope.operation_id, State.EXECUTING):
                raise asyncio.CancelledError from None
            submission = Submission(
                action,
                result=result,
                execution=execution_request,
                message="\n".join(coordinator.visible_says(result)),
                say_outputs=coordinator.visible_say_outputs(result),
                events=tuple(events),
            )
            coordinator.complete_queue_item(
                item,
                QueueOutcome(queued_origin, "completed", submission=submission),
            )
            if result.status in ("uncertain", "cancelled"):
                coordinator.lifecycle.set_state_unless_stopping(State.FAILED)
                raise RuntimeError("Queued cell result is uncertain; agent turn stopped without replay")
        except asyncio.CancelledError:
            if dispatched:
                coordinator.lifecycle.set_state_unless_stopping(State.FAILED)
            coordinator.complete_queue_item(
                item,
                QueueOutcome(
                    queued_origin,
                    "interrupted",
                    error="Queued cell interrupted; side effects may have occurred",
                ),
            )
            raise
        except Exception as exc:
            if dispatched:
                coordinator.lifecycle.set_state_unless_stopping(State.FAILED)
            coordinator.complete_queue_item(
                item,
                QueueOutcome(
                    queued_origin,
                    "failed",
                    error=f"Queued cell failed; side effects may have occurred: {exc}",
                ),
            )
            raise
        finally:
            if coordinator.lifecycle.state is State.EXECUTING:
                coordinator.lifecycle.state = previous_state

    async def run_command(self, action: RoutedAction, command_config: ConfigSnapshot | None) -> Submission:
        """Dispatch under the current operation without releasing its ownership."""
        coordinator, scope = self.coordinator, self.scope
        coordinator.lifecycle.state = State.COMMAND
        coordinator.conversation.capture_history_sensitive_config(command_config)
        message = await coordinator.dispatch_command(action.source, command_config)
        if not coordinator.lifecycle.operation_is_current(scope.operation_id, State.COMMAND):
            raise asyncio.CancelledError from None
        if coordinator.config_store is not None:
            coordinator.config_revision = coordinator.config_store.snapshot.revision
            coordinator.conversation.capture_history_sensitive_config(coordinator.config_store.snapshot)
        if (
            not coordinator.conversation.journal_sensitive_config_ready
            and getattr(coordinator.journal, "persisted", None) is True
        ):
            message += (
                "\nWarning: sensitive configuration exceeds journal redaction limits; "
                "further persisted operations will stop safely."
            )
        return Submission(action, message=message)

    async def run_queued_command(self, item: _QueuedAction) -> None:
        coordinator = self.coordinator
        previous_state = coordinator.lifecycle.state
        try:
            submission = await self.run_command(item.action, item.config)
        except asyncio.CancelledError:
            # Commands may have side effects. Leave COMMAND set so the
            # outer submission fails closed; never replay this item.
            coordinator.complete_queue_item(
                item,
                QueueOutcome(
                    item.action.origin,
                    "interrupted",
                    error="Queued command interrupted; side effects may have occurred",
                ),
            )
            raise
        except Exception as exc:
            coordinator.complete_queue_item(
                item,
                QueueOutcome(
                    item.action.origin,
                    "failed",
                    error=f"Queued command failed; side effects may have occurred: {exc}",
                ),
            )
            raise
        else:
            coordinator.lifecycle.state = previous_state
            coordinator.complete_queue_item(
                item,
                QueueOutcome(
                    item.action.origin,
                    "completed",
                    submission=submission,
                ),
            )

    async def drain_boundary_queue(self) -> None:
        # Reserve English steering and run cells/commands in FIFO order
        # before the next provider call. Bound this drain so newly
        # arriving actions cannot indefinitely starve generation.
        coordinator, scope = self.coordinator, self.scope
        boundary_count = await coordinator.lifecycle.boundary_queue_size()
        for _ in range(boundary_count):
            item = await coordinator.lifecycle.take_boundary_action()
            if item is None:
                return
            if item.action.kind == "ask":
                scope.steering_awaiting_dispatch.append(item)
            elif item.action.kind == "command":
                await self.run_queued_command(item)
            else:
                await self.run_queued_direct(item)
