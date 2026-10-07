"""Submission admission and cleanup."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import inspect
from typing import TYPE_CHECKING, Protocol, cast
from uuid import uuid4

from .configuration import ConfigSnapshot
from .contracts import (
    ContextSnapshot,
    InputHandler,
    ModelRequest,
    ModelResponse,
    Origin,
    ProgressCallback,
    QueueOutcome,
    Submission,
    UserAction,
)
from .coordinator_agent import AgentTurn
from .coordinator_boundary import BoundaryQueue
from .coordinator_direct import DirectExecution
from .coordinator_io import RequestIO
from .coordinator_model import ModelRequests
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


class RequestRunner:
    """Run one serialized submission using explicit session components."""

    def __init__(self, coordinator: RequestRuntime) -> None:
        self.coordinator = coordinator
        self.models = ModelRequests(coordinator)
        self.direct = DirectExecution(coordinator)

    async def submit(
        self,
        frontend_id: str,
        text: str,
        *,
        allow_stdin: bool = False,
        input_handler: InputHandler | None = None,
        on_progress: ProgressCallback | None = None,
        _queued_action: _QueuedAction | None = None,
    ) -> Submission:
        coordinator = self.coordinator
        async with coordinator.lifecycle.submission_slot(queued=_queued_action is not None):
            if _queued_action is not None:
                allow_stdin = _queued_action.allow_stdin
                input_handler = _queued_action.input_handler
                on_progress = _queued_action.on_progress
            if not isinstance(frontend_id, str) or not isinstance(text, str):
                raise TypeError("Frontend ID and submission text must be text")
            if type(allow_stdin) is not bool:
                raise TypeError("allow_stdin must be a boolean")
            if input_handler is not None and not callable(input_handler):
                raise TypeError("input_handler must be callable or None")
            if on_progress is not None and not callable(on_progress):
                raise TypeError("on_progress must be callable or None")

            task = asyncio.current_task()
            operation_id = uuid4().hex
            coordinator.lifecycle.operation.begin(operation_id, task)
            config = _queued_action.config if _queued_action is not None else coordinator.current_config()
            coordinator.conversation.capture_history_sensitive_config(config)
            coordinator.conversation.observe_context_epoch(coordinator.conversation.read_context_epoch(), config=config)
            revision = config.revision if config is not None else coordinator.config_revision
            coordinator.config_revision = revision
            origin = (
                _queued_action.action.origin
                if _queued_action is not None
                else Origin(coordinator.session_id, uuid4().hex, frontend_id, revision)
            )

            scope = RequestScope(origin, operation_id, config, allow_stdin, input_handler, on_progress)
            io = RequestIO(coordinator, scope)
            boundary = BoundaryQueue(coordinator, scope, io)
            try:
                scope.routed = (
                    _queued_action.action
                    if _queued_action is not None
                    else coordinator.validate_routed_action(
                        cast(_Router, coordinator.router).route(UserAction(scope.origin, text)), scope.origin
                    )
                )

                scope.resume_checkpoint = None
                if scope.routed.kind == "command" and scope.routed.source.strip() == "resume":
                    scope.resume_checkpoint = coordinator.recovery
                    if scope.resume_checkpoint is None:
                        submission = await boundary.run_command(scope.routed, scope.config)
                        coordinator.lifecycle.set_state_unless_stopping(State.IDLE)
                        return submission
                    if scope.resume_checkpoint.model != coordinator.model:
                        raise RuntimeError(
                            "Pending recovery belongs to a different model. Switch back or use /recovery discard."
                        )
                    scope.routed = coordinator.validate_routed_action(
                        coordinator.router.route(UserAction(scope.origin, scope.resume_checkpoint.source)),
                        scope.origin,
                    )
                    coordinator.recovery = None
                elif scope.routed.kind == "ask":
                    coordinator.recovery = None  # A new task supersedes a pending request.

                if scope.routed.kind == "command":
                    submission = await boundary.run_command(scope.routed, scope.config)
                    coordinator.lifecycle.set_state_unless_stopping(State.IDLE)
                    return submission
                routed = scope.routed

                if scope.routed.kind == "ask":
                    return await AgentTurn(self, scope, routed, boundary).run()

                return await self.direct.execute(
                    scope.routed,
                    scope.origin,
                    scope.operation_id,
                    allow_stdin=scope.allow_stdin,
                    execution_input_handler=io.input,
                    execution_output_handler=io.output,
                    llm_handler_factory=io.llm,
                    on_progress=scope.on_progress,
                )
            except asyncio.CancelledError:
                self._fail_submission(
                    scope.origin,
                    scope.steering_awaiting_dispatch,
                    context_pending=scope.context_pending,
                    steering_commit_started=scope.steering_commit_started,
                    cancelled=True,
                )
                raise
            except Exception as exc:
                self._fail_submission(
                    scope.origin,
                    scope.steering_awaiting_dispatch,
                    context_pending=scope.context_pending,
                    steering_commit_started=scope.steering_commit_started,
                    cancelled=False,
                    error=exc,
                )
                raise
            finally:
                coordinator.activity = None
                coordinator.lifecycle.operation.finish(scope.operation_id, task)
                if coordinator.lifecycle.state in (State.FAILED, State.STOPPING, State.CLOSED):
                    await coordinator.lifecycle.fail_pending_actions("interrupted", "Session is unavailable")
                asyncio.get_running_loop().call_soon(coordinator.lifecycle.start_queue_worker_if_idle)

    def _fail_submission(
        self,
        origin: Origin,
        steering: list[_QueuedAction],
        *,
        context_pending: bool,
        steering_commit_started: bool,
        cancelled: bool,
        error: Exception | None = None,
    ) -> None:
        """Settle reserved steering/context; never replay an admitted side effect."""
        coordinator = self.coordinator
        for item in steering:
            message = (
                "Steering was cancelled before provider dispatch"
                if cancelled
                else "Steering failed before provider dispatch"
                + ("; context may contain partial steering" if steering_commit_started else "")
                + ": "
                + str(error)
            )
            coordinator.complete_queue_item(
                item,
                QueueOutcome(
                    item.action.origin,
                    "interrupted" if cancelled else "failed",
                    error=message,
                ),
            )
        if context_pending:
            try:
                coordinator.conversation.abandon_context(origin.request_id)
            except Exception:
                pass
        if coordinator.lifecycle.state is State.GENERATING:
            coordinator.lifecycle.set_state_unless_stopping(State.IDLE)
        elif coordinator.lifecycle.state in (State.EXECUTING, State.COMMAND):
            # Only an acknowledged stopped execution permits reuse after cancel.
            acknowledged_stop = (
                cancelled
                and coordinator.lifecycle.state is State.EXECUTING
                and coordinator.lifecycle.operation.execution_outcome_status == "cancelled"
            )
            coordinator.lifecycle.set_state_unless_stopping(
                State.IDLE if acknowledged_stop else State.FAILED,
            )

    def restore_steering(
        self,
        snapshot: ContextSnapshot,
        steering: list[tuple[str, str]],
    ) -> ContextSnapshot:
        """Restore still-active queued input after history epoch eviction."""
        coordinator = self.coordinator
        for request_id, source in steering:
            coordinator.conversation.append_steering_context(source, request_id)
        if callable(getattr(coordinator.context_service, "snapshot", None)):
            return coordinator.conversation.latest_context()
        return replace(
            snapshot,
            messages=(*snapshot.messages, *(("user", source) for _, source in steering)),
            message_phases=(*snapshot.message_phases, *((None,) * len(steering))),
        )

    async def prepare_model_request(
        self,
        snapshot: ContextSnapshot,
        origin: Origin,
        config: ConfigSnapshot | None,
        *,
        steering: list[_QueuedAction],
        saved_context: ContextSnapshot | None,
    ) -> tuple[ModelRequest, bool]:
        """Assemble and transform a request; never commit steering or dispatch it.

        Saved requests bypass both transforms. The caller checks operation
        identity after awaiting this preparation, then commits/journals before
        provider dispatch.
        """
        coordinator = self.coordinator
        prepare_generation = getattr(coordinator.context_service, "prepare_generation", None)
        if callable(prepare_generation):
            prepare_generation()
            snapshot = coordinator.conversation.latest_context()
        if saved_context is not None:
            snapshot = saved_context
        forced_collapse = getattr(coordinator.context_service, "force_collapse", False) is True
        if steering:
            render_user = getattr(coordinator.context_service, "render_user", None)
            additions = tuple(
                (
                    "user",
                    render_user(item.action.source, item.action.origin.request_id)
                    if callable(render_user)
                    else item.action.source,
                )
                for item in steering
            )
            snapshot = replace(
                snapshot,
                messages=(*snapshot.messages, *additions),
                message_phases=(*snapshot.message_phases, *((None,) * len(additions))),
            )
        cell_config = coordinator.current_config()
        epoch_config = coordinator.conversation.epoch_config
        context = saved_context
        if context is None:
            context = await coordinator.run_context_transforms(
                snapshot,
                config,
                cell_config,
                epoch_config,
            )
        request = ModelRequest(
            origin,
            context,
            coordinator.model,
            coordinator.model_options(config),
        )
        if saved_context is None:
            request = await coordinator.model_transform(
                request,
                config,
                cell_config,
                epoch_config,
            )
        return request, forced_collapse

    @staticmethod
    def format_correction(reason: str, *, repeated: bool) -> str:
        """Bound rejected-response diagnostics without retaining rejected source."""
        detail = reason[:300] or "invalid cell"
        if repeated:
            return (
                "Still no valid cell. Invalid model response format: "
                + detail
                + ". Respond with exactly one complete Python/IPython cell and nothing "
                "else: no fences, prose, tool call, JSON, or function call."
            )
        return (
            "No code was executed. Invalid model response format: "
            + detail
            + ". There is no external tool-call API. Python function calls are available. "
            "Respond with exactly one complete Python/IPython cell as ordinary assistant message text: "
            "no tool call, JSON, prose outside the cell, or Markdown fences."
        )

    def check_cell_syntax(self, source: str) -> str | None:
        """Validate custom syntax-check contract, without dispatch or context writes."""
        check = getattr(self.coordinator.interpreter, "check_syntax", None)
        if not callable(check):
            return None
        error = check(source)
        if inspect.isawaitable(error) or (error is not None and not isinstance(error, str)):
            raise TypeError("Interpreter syntax check must return text or None synchronously")
        if error == "":
            raise ValueError("Interpreter returned an empty syntax diagnostic")
        return error

    # Compatibility for callers that exercised this formatting helper directly.
    _format_correction = format_correction
