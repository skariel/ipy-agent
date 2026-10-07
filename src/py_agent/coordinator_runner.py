"""Submission runner; side-effect ownership remains in ExecutionLifecycle."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
import inspect
from itertools import count
from typing import TYPE_CHECKING, Literal, Protocol, cast
from uuid import uuid4

from .collapse_control import parse_collapse
from .configuration import ConfigSnapshot
from .contracts import (
    AgentDecision,
    ExecutionOutput,
    ExecutionRequest,
    ExecutionResult,
    InputHandler,
    InputReply,
    InputRequest,
    InputUnavailableError,
    ModelRequest,
    ModelResponse,
    Origin,
    OutputEvent,
    ProgressCallback,
    QueueOutcome,
    RoutedAction,
    SayOutput,
    Submission,
    UserAction,
)
from .coordinator_direct import DirectExecution
from .coordinator_model import ModelRequests
from .coordinator_support import (
    MAX_AGENT_RESPONSE_CHARS,
    State,
    _QueuedAction,
)
from .coordinator_support import (
    MAX_PENDING_ACTIONS as MAX_PENDING_ACTIONS,
)

if TYPE_CHECKING:
    from .coordinator_runtime import SessionRuntime


class _Router(Protocol):
    def route(self, action: UserAction) -> object: ...


class _Interpreter(Protocol):
    def interpret(self, response: ModelResponse) -> object: ...


class RequestRunner:
    """Run one serialized submission using explicit session components."""

    def __init__(self, coordinator: SessionRuntime) -> None:
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
        generation_id: str | None
        coordinator = self.coordinator
        if ((coordinator.lifecycle.state is not State.IDLE or coordinator.lifecycle._lock.locked() or coordinator.lifecycle._queue_worker is not None
             or coordinator.lifecycle._pending_actions) and _queued_action is None):
            raise RuntimeError("Session busy, queued work pending, or unavailable")
        async with coordinator.lifecycle._lock:
            if (coordinator.lifecycle.state is not State.IDLE
                    or ((_queued_action is None) and (coordinator.lifecycle._queue_worker is not None or coordinator.lifecycle._pending_actions))):
                raise RuntimeError("Session busy, queued work pending, or unavailable")
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
            config = _queued_action.config if _queued_action is not None else coordinator._current_config()
            coordinator.conversation._capture_history_sensitive_config(config)
            coordinator.conversation._observe_context_epoch(coordinator.conversation._read_context_epoch(), config=config)
            revision = config.revision if config is not None else coordinator.config_revision
            coordinator.config_revision = revision
            origin = (
                _queued_action.action.origin if _queued_action is not None
                else Origin(coordinator.session_id, uuid4().hex, frontend_id, revision)
            )

            def execution_input_handler(
                execution_origin: Origin, *, enabled: bool = allow_stdin,
                handler: InputHandler | None = input_handler,
                owner: str = frontend_id,
            ) -> InputHandler | None:
                if not enabled or handler is None:
                    return None

                async def dispatch_input(input_request: InputRequest) -> InputReply:
                    if (
                        not isinstance(input_request, InputRequest)
                        or input_request.origin != execution_origin
                        or input_request.owner_frontend_id != owner
                    ):
                        raise InputUnavailableError(
                            "Interactive input request does not belong to this execution frontend",
                        )
                    if (
                        coordinator.lifecycle.operation.execution_request is None
                        or coordinator.lifecycle.operation.execution_request.origin != execution_origin
                        or coordinator.lifecycle.state is not State.EXECUTING
                    ):
                        raise InputUnavailableError("Interactive input execution is no longer active")
                    coordinator.lifecycle.state = State.WAITING_FOR_INPUT
                    try:
                        response = handler(input_request)
                        if not inspect.isawaitable(response):
                            raise TypeError("Frontend input handler must be async")
                        reply = await response
                        if (
                            not isinstance(reply, InputReply)
                            or reply.origin != input_request.origin
                            or reply.sequence != input_request.sequence
                            or reply.owner_frontend_id != input_request.owner_frontend_id
                            or reply.password is not input_request.password
                        ):
                            raise InputUnavailableError(
                                "Frontend input reply did not match its originating request",
                            )
                        return reply
                    finally:
                        if coordinator.lifecycle.state is State.WAITING_FOR_INPUT:
                            coordinator.lifecycle.state = State.EXECUTING

                return dispatch_input

            from .host_stdlib import execution_llm_handler as create_handler

            def execution_output_handler(
                execution_origin: Origin, author: Literal['user', 'agent'],
                *, progress: ProgressCallback | None = on_progress,
            ) -> Callable[[ExecutionOutput], Awaitable[None]] | None:
                if progress is None:
                    return None

                async def deliver(output: ExecutionOutput) -> None:
                    if (output.kind != "stream"
                            or not coordinator.lifecycle._operation_is_current(operation_id, State.EXECUTING)):
                        raise asyncio.CancelledError from None
                    event = coordinator.frontend._new_output_event(
                        execution_origin, "stream",
                        {**dict(output.data), "author": author},
                        metadata={"provisional": True}, author=author,
                    )
                    # Frontend-only provisional preview: never add it to model
                    # context, journal, or observer history. The completed cell
                    # result remains the single authoritative output record.
                    delivered = progress(event)
                    if not inspect.isawaitable(delivered):
                        raise TypeError("Request progress callback must be async")
                    await delivered
                    if not coordinator.lifecycle._operation_is_current(operation_id, State.EXECUTING):
                        raise asyncio.CancelledError from None

                return deliver

            context_pending = False
            steering_awaiting_dispatch: list[_QueuedAction] = []
            steering_commit_started = False

            async def run_queued_direct(item: _QueuedAction) -> None:
                """Execute a user cell at a model boundary without ending the agent turn."""
                action = item.action
                queued_origin = action.origin
                execution_origin = Origin(
                    queued_origin.session_id, queued_origin.request_id,
                    queued_origin.frontend_id, queued_origin.config_revision,
                    None, uuid4().hex,
                )
                execution_request = ExecutionRequest(
                    execution_origin, action.source, "user",
                    action.language or "ipython",
                    allow_stdin=item.allow_stdin,
                    input_handler=execution_input_handler(
                        execution_origin, enabled=item.allow_stdin,
                        handler=item.input_handler, owner=queued_origin.frontend_id,
                    ),
                    output_handler=execution_output_handler(
                        execution_origin, "user", progress=item.on_progress,
                    ),
                    llm_handler=create_handler(coordinator, execution_origin),
                )
                previous_state = coordinator.lifecycle.state
                coordinator.lifecycle.state = State.EXECUTING
                events: list[OutputEvent] = []
                dispatched = False
                try:
                    events.append(await coordinator.frontend._emit_progress(
                        execution_origin,
                        {"phase": "execution_start", "author": "user",
                         "text": "Executing queued user cell."},
                        on_progress=item.on_progress, operation_id=operation_id,
                        expected_state=State.EXECUTING, author="user",
                    ))
                    dispatched = True
                    result = await coordinator.lifecycle._execute_dispatched(execution_request)
                    if not coordinator.lifecycle._operation_is_current(operation_id, State.EXECUTING):
                        raise asyncio.CancelledError from None
                    events.extend(await coordinator.frontend._publish_output(
                        execution_request, result, on_progress=item.on_progress,
                        operation_id=operation_id,
                    ))
                    events.append(await coordinator.frontend._emit_progress(
                        execution_origin,
                        {"phase": "cell_complete", "step": 1, "status": result.status,
                         "text": f"Queued user cell completed with status {result.status}."},
                        on_progress=item.on_progress, operation_id=operation_id,
                        expected_state=State.EXECUTING, author="user",
                    ))
                    if not coordinator.lifecycle._operation_is_current(operation_id, State.EXECUTING):
                        raise asyncio.CancelledError from None
                    submission = Submission(
                        action, result=result, execution=execution_request,
                        message="\n".join(coordinator._visible_says(result)),
                        say_outputs=coordinator._visible_say_outputs(result),
                        events=tuple(events),
                    )
                    coordinator._complete_queue_item(
                        item, QueueOutcome(queued_origin, "completed", submission=submission),
                    )
                    if result.status in ("uncertain", "cancelled"):
                        coordinator.lifecycle._set_state_unless_stopping(State.FAILED)
                        raise RuntimeError("Queued cell result is uncertain; agent turn stopped without replay")
                except asyncio.CancelledError:
                    if dispatched:
                        coordinator.lifecycle._set_state_unless_stopping(State.FAILED)
                    coordinator._complete_queue_item(item, QueueOutcome(
                        queued_origin, "interrupted",
                        error="Queued cell interrupted; side effects may have occurred",
                    ))
                    raise
                except Exception as exc:
                    if dispatched:
                        coordinator.lifecycle._set_state_unless_stopping(State.FAILED)
                    coordinator._complete_queue_item(item, QueueOutcome(
                        queued_origin, "failed",
                        error=f"Queued cell failed; side effects may have occurred: {exc}",
                    ))
                    raise
                finally:
                    if coordinator.lifecycle.state is State.EXECUTING:
                        coordinator.lifecycle.state = previous_state

            async def run_command(action: RoutedAction, command_config: ConfigSnapshot | None) -> Submission:
                """Dispatch under the current operation without releasing its ownership."""
                coordinator.lifecycle.state = State.COMMAND
                coordinator.conversation._capture_history_sensitive_config(command_config)
                message = await coordinator._dispatch_command(action.source, command_config)
                if not coordinator.lifecycle._operation_is_current(operation_id, State.COMMAND):
                    raise asyncio.CancelledError from None
                if coordinator.config_store is not None:
                    coordinator.config_revision = coordinator.config_store.snapshot.revision
                    coordinator.conversation._capture_history_sensitive_config(coordinator.config_store.snapshot)
                if (not coordinator.conversation._journal_sensitive_config_ready
                        and getattr(coordinator.journal, "persisted", None) is True):
                    message += (
                        "\nWarning: sensitive configuration exceeds journal redaction limits; "
                        "further persisted operations will stop safely."
                    )
                return Submission(action, message=message)

            async def run_queued_command(item: _QueuedAction) -> None:
                previous_state = coordinator.lifecycle.state
                try:
                    submission = await run_command(item.action, item.config)
                except asyncio.CancelledError:
                    # Commands may have side effects. Leave COMMAND set so the
                    # outer submission fails closed; never replay this item.
                    coordinator._complete_queue_item(item, QueueOutcome(
                        item.action.origin, "interrupted",
                        error="Queued command interrupted; side effects may have occurred",
                    ))
                    raise
                except Exception as exc:
                    coordinator._complete_queue_item(item, QueueOutcome(
                        item.action.origin, "failed",
                        error=f"Queued command failed; side effects may have occurred: {exc}",
                    ))
                    raise
                else:
                    coordinator.lifecycle.state = previous_state
                    coordinator._complete_queue_item(item, QueueOutcome(
                        item.action.origin, "completed", submission=submission,
                    ))

            async def drain_boundary_queue() -> None:
                # Reserve English steering and run cells/commands in FIFO order
                # before the next provider call. Bound this drain so newly
                # arriving actions cannot indefinitely starve generation.
                async with coordinator.lifecycle._queue_lock:
                    boundary_count = len(coordinator.lifecycle._pending_actions)
                for _ in range(boundary_count):
                    async with coordinator.lifecycle._queue_lock:
                        if not coordinator.lifecycle._pending_actions:
                            return
                        item = coordinator.lifecycle._pending_actions.popleft()
                    if item.action.kind == "ask":
                        steering_awaiting_dispatch.append(item)
                    elif item.action.kind == "command":
                        await run_queued_command(item)
                    else:
                        await run_queued_direct(item)

            try:
                routed = (
                    _queued_action.action if _queued_action is not None
                    else coordinator._validate_routed_action(cast(_Router, coordinator.router).route(UserAction(origin, text)), origin)
                )

                resume_checkpoint = None
                if routed.kind == "command" and routed.source.strip() == "resume":
                    resume_checkpoint = coordinator.recovery
                    if resume_checkpoint is None:
                        submission = await run_command(routed, config)
                        coordinator.lifecycle._set_state_unless_stopping(State.IDLE)
                        return submission
                    if resume_checkpoint.model != coordinator.model:
                        raise RuntimeError(
                            "Pending recovery belongs to a different model. Switch back or use /recovery discard."
                        )
                    routed = coordinator._validate_routed_action(
                        coordinator.router.route(UserAction(origin, resume_checkpoint.source)), origin,
                    )
                    coordinator.recovery = None
                elif routed.kind == "ask":
                    coordinator.recovery = None  # A new task supersedes a pending request.

                if routed.kind == "command":
                    submission = await run_command(routed, config)
                    coordinator.lifecycle._set_state_unless_stopping(State.IDLE)
                    return submission

                if routed.kind == "ask":
                    context_pending = (
                        callable(getattr(coordinator.context_service, "prepare_request", None))
                        and not (resume_checkpoint is not None and resume_checkpoint.committed)
                    )
                    if resume_checkpoint is not None:
                        if not resume_checkpoint.committed:
                            coordinator.conversation._prepare_context(routed.source, origin.request_id)
                        initial_context = resume_checkpoint.context
                        context_committed = resume_checkpoint.committed
                    else:
                        initial_context = coordinator.conversation._prepare_context(routed.source, origin.request_id)
                        context_committed = False
                    coordinator.conversation._observe_context_epoch(initial_context.epoch)
                    visible_messages: list[str] = []
                    visible_outputs: list[SayOutput] = []
                    published_events: list[OutputEvent] = []
                    executions: list[tuple[ExecutionRequest, ExecutionResult]] = []
                    last_result = None
                    last_execution = None
                    last_response = None
                    invalid_generations = 0
                    dispatched_steering: list[tuple[str, str]] = []

                    def preflight_exhausted(response: ModelResponse) -> Submission | None:
                        nonlocal invalid_generations
                        invalid_generations += 1
                        # A limitless successful-cell loop must not turn a
                        # broken format into unbounded paid provider retries.
                        if invalid_generations <= 2:
                            return None
                        coordinator.lifecycle._set_state_unless_stopping(State.IDLE)
                        visible_messages.append(
                            "Agent paused after 3 consecutive invalid model responses; "
                            "no rejected source was executed. Submit a new request to continue."
                        )
                        return Submission(
                            routed, result=last_result, message="\n".join(visible_messages),
                            execution=last_execution, response=response,
                            say_outputs=tuple(visible_outputs), executions=tuple(executions),
                            events=tuple(published_events),
                        )

                    # Follow the original agent loop by default: only an
                    # explicitly selected positive limit pauses a long task.
                    steps = count(1) if coordinator.max_agent_steps == 0 else range(1, coordinator.max_agent_steps + 1)
                    for step in steps:
                        if step > 1:
                            await drain_boundary_queue()
                            if coordinator.lifecycle.operation.identity != operation_id or coordinator.lifecycle.state in (
                                State.FAILED, State.STOPPING, State.CLOSED,
                            ):
                                raise asyncio.CancelledError from None
                        coordinator.lifecycle.state = State.GENERATING
                        generation_id = uuid4().hex
                        generation_origin = Origin(
                            origin.session_id, origin.request_id, origin.frontend_id,
                            origin.config_revision, generation_id,
                        )
                        published_events.append(await coordinator.frontend._emit_progress(
                            generation_origin,
                            {
                                "phase": "generation_start", "step": step,
                                "text": f"Agent: requesting a response (step {step}).",
                            },
                            on_progress=on_progress, operation_id=operation_id,
                            expected_state=State.GENERATING,
                        ))
                        # Leading queued steering was reserved at this safe
                        # boundary, before context assembly and transforms.
                        reset_needed = getattr(coordinator.context_service, "needs_reset", None)
                        reset_this_step = step > 1 and callable(reset_needed) and reset_needed()
                        if reset_this_step:
                            # Reset between model calls even during a long task.
                            # Reinsert the active user's request so eviction of
                            # dispatched history does not erase the task itself.
                            snapshot = coordinator.conversation._prepare_context(routed.source, origin.request_id)
                            context_committed = False
                            context_pending = callable(getattr(coordinator.context_service, "prepare_request", None))
                        else:
                            snapshot = initial_context if step == 1 else coordinator.conversation._latest_context()
                        coordinator.conversation._observe_context_epoch(snapshot.epoch)
                        if reset_this_step and dispatched_steering:
                            # Earlier queued user steering is still active task
                            # input. Restore its provenance after epoch eviction.
                            for steering_id, steering_text in dispatched_steering:
                                coordinator.conversation._append_steering_context(steering_text, steering_id)
                            if callable(getattr(coordinator.context_service, "snapshot", None)):
                                snapshot = coordinator.conversation._latest_context()
                            else:
                                snapshot = replace(
                                    snapshot,
                                    messages=(*snapshot.messages, *(("user", text) for _, text in dispatched_steering)),
                                    message_phases=(*snapshot.message_phases, *((None,) * len(dispatched_steering))),
                                )
                        prepare_generation = getattr(coordinator.context_service, "prepare_generation", None)
                        if callable(prepare_generation):
                            prepare_generation()
                            snapshot = coordinator.conversation._latest_context()
                        if resume_checkpoint is not None and step == 1:
                            snapshot = resume_checkpoint.context
                        forced_collapse = getattr(coordinator.context_service, "force_collapse", False) is True
                        if steering_awaiting_dispatch:
                            render_user = getattr(coordinator.context_service, "render_user", None)
                            additions = tuple(
                                ("user", render_user(item.action.source, item.action.origin.request_id)
                                 if callable(render_user) else item.action.source)
                                for item in steering_awaiting_dispatch
                            )
                            snapshot = replace(
                                snapshot,
                                messages=(*snapshot.messages, *additions),
                                message_phases=(
                                    *snapshot.message_phases, *((None,) * len(additions))
                                ),
                            )
                        cell_config = coordinator._current_config()
                        epoch_config = coordinator.conversation._epoch_config
                        if resume_checkpoint is not None and step == 1:
                            context = resume_checkpoint.context  # Never rerun transforms on saved request.
                        else:
                            context = await coordinator._run_context_transforms(
                                snapshot, config, cell_config, epoch_config,
                            )
                        model_request = ModelRequest(
                            generation_origin, context, coordinator.model, coordinator._model_options(config),
                        )
                        if resume_checkpoint is None or step != 1:
                            model_request = await coordinator.model_transform(
                                model_request, config, cell_config, epoch_config,
                            )
                        if not coordinator.lifecycle._operation_is_current(operation_id, State.GENERATING):
                            raise asyncio.CancelledError from None
                        # This is the final post-transform request actually dispatched to
                        # the provider. Persist it before any provider-side effect.
                        # Commit only after transforms/validation have succeeded. If
                        # cancelled earlier, no steering leaks into future context.
                        for steering_item in steering_awaiting_dispatch:
                            steering_commit_started = True
                            coordinator.conversation._append_steering_context(
                                steering_item.action.source, steering_item.action.origin.request_id,
                            )
                        if resume_checkpoint is not None and step == 1:
                            model_request = replace(
                                model_request, context=resume_checkpoint.context,
                                model=resume_checkpoint.model, options=dict(resume_checkpoint.options),
                            )
                        coordinator.lifecycle.operation.begin_model(model_request)
                        await coordinator.journal_policy._journal_record("record_model_request", model_request)
                        for steering_item in steering_awaiting_dispatch:
                            dispatched_steering.append((
                                steering_item.action.origin.request_id, steering_item.action.source,
                            ))
                            coordinator._complete_queue_item(
                                steering_item, QueueOutcome(steering_item.action.origin, "steered"),
                            )
                        steering_awaiting_dispatch.clear()
                        # Only transport/rate-limit failures explicitly classified
                        # as transient may retry. Never retry credentials, malformed
                        # responses, plugin validation or code execution. A retry
                        # sends the same already-journaled request; each failed
                        # attempt gets its own usage-unknown journal record.
                        model_request, response, overflow_recovered = await self.models.generate(
                            model_request, operation_id=operation_id, task_text=routed.source,
                            context_committed=context_committed,
                            published_events=published_events, generation_origin=generation_origin,
                            on_progress=on_progress,
                            executed_cells=len(executions) + (resume_checkpoint.executed_cells if resume_checkpoint else 0),
                        )
                        forced_collapse = forced_collapse or overflow_recovered

                        if not isinstance(response, ModelResponse):
                            await coordinator.journal_policy._record_provider_usage(model_request, None, outcome="failed")
                            raise TypeError("Provider must return a ModelResponse")
                        coordinator.activity = None
                        await coordinator.journal_policy._record_provider_usage(model_request, response, outcome="returned")
                        if not coordinator.lifecycle._operation_is_current(operation_id, State.GENERATING):
                            raise asyncio.CancelledError from None
                        if not isinstance(response.text, str):
                            raise TypeError("Provider response text must be text")
                        last_response = response
                        record_usage = getattr(coordinator.context_service, "record_response", None)
                        if callable(record_usage):
                            record_usage(response)
                        if len(response.text) > MAX_AGENT_RESPONSE_CHARS:
                            coordinator.conversation._commit_context(
                                origin.request_id, routed.source,
                                f"[generated response rejected: {len(response.text)} characters; limit 8000]",
                                {"preflight": {
                                    "executed": False,
                                    "error": f"Your response was {len(response.text)} characters (limit 8000). "
                                             "Try sending a smaller Python cell.",
                                }},
                                phase=response.phase, include_user=not context_committed,
                            )
                            context_committed = True
                            context_pending = False
                            exhausted = preflight_exhausted(response)
                            if exhausted is not None:
                                return exhausted
                            continue
                        # Context control is host-owned and never executed in the
                        # Python worker. Enforce the policy that accompanied this
                        # generation, not usage first reported by its response.
                        collapse_args = None
                        collapse_error = None
                        try:
                            collapse_args = parse_collapse(response.text, forced=forced_collapse)
                        except ValueError as exc:
                            collapse_error = str(exc)
                        if collapse_args is not None or collapse_error is not None:
                            await coordinator.journal_policy._journal_record(
                                "record_context_collapse", model_request, response.text,
                                outcome="requested",
                            )
                            if collapse_error is None:
                                collapse = getattr(coordinator.context_service, "collapse", None)
                                store_collapsed = getattr(coordinator.executor, "store_collapsed", None)
                                if not callable(collapse) or not callable(store_collapsed):
                                    collapse_error = "Collapse is unavailable for the selected context/executor services."
                                else:
                                    async def store_archive(
                                        text: str,
                                        store: Callable[[str], Awaitable[object]] = store_collapsed,
                                    ) -> object:
                                        try:
                                            index = await store(text)
                                        except BaseException:
                                            # Archive transport may terminate the
                                            # persistent worker, even on cancellation.
                                            # Never resume with a potentially lost namespace.
                                            coordinator.lifecycle._set_state_unless_stopping(State.FAILED)
                                            raise
                                        if not coordinator.lifecycle._operation_is_current(operation_id, State.GENERATING):
                                            raise asyncio.CancelledError from None
                                        return index

                                    try:
                                        receipt = await collapse(*cast(tuple[object, ...], collapse_args), store_archive)
                                    except (ValueError, TypeError) as exc:
                                        if coordinator.lifecycle.state is State.FAILED:
                                            raise
                                        collapse_error = str(exc)[:500]
                            if collapse_error is not None:
                                await coordinator.journal_policy._journal_record(
                                    "record_context_collapse", model_request, response.text,
                                    outcome="rejected", detail=collapse_error,
                                )
                                coordinator.conversation._commit_context(
                                    origin.request_id, routed.source,
                                    "[collapse cell rejected; no code executed]",
                                    {"preflight": {"executed": False, "error": collapse_error}},
                                    phase=response.phase, include_user=not context_committed,
                                )
                                context_committed = True
                                context_pending = False
                                exhausted = preflight_exhausted(response)
                                if exhausted is not None:
                                    return exhausted
                            else:
                                # Only the receipt remains at the call site. The
                                # original source is retained in the audit journal.
                                coordinator.conversation._commit_context(
                                    origin.request_id, routed.source, receipt, None,
                                    phase=response.phase, include_user=not context_committed,
                                )
                                context_committed = True
                                context_pending = False
                                invalid_generations = 0
                                await coordinator.journal_policy._journal_record(
                                    "record_context_collapse", model_request, response.text,
                                    outcome="succeeded", detail=receipt,
                                )
                            continue
                        decision = cast(_Interpreter, coordinator.interpreter).interpret(response)
                        if not isinstance(decision, AgentDecision):
                            raise TypeError("Interpreter must return an AgentDecision")
                        if decision.kind not in ("execute", "finish", "wait", "reject"):
                            raise ValueError(f"Interpreter returned unknown decision: {decision.kind!r}")
                        if not isinstance(decision.source, str) or not isinstance(decision.reason, str):
                            raise TypeError("Decision source and reason must be text")
                        if type(decision.retryable) is not bool or (decision.retryable and decision.kind != "reject"):
                            raise ValueError("Only a rejected response may request a format retry")
                        if decision.kind == "execute" and len(decision.source) > MAX_AGENT_RESPONSE_CHARS:
                            coordinator.conversation._commit_context(
                                origin.request_id, routed.source,
                                f"[generated cell rejected: {len(decision.source)} characters; limit 8000]",
                                {"preflight": {
                                    "executed": False,
                                    "error": f"Your Python cell was {len(decision.source)} characters (limit 8000). "
                                             "Try sending a smaller cell.",
                                }},
                                phase=response.phase, include_user=not context_committed,
                            )
                            context_committed = True
                            context_pending = False
                            exhausted = preflight_exhausted(response)
                            if exhausted is not None:
                                return exhausted
                            continue

                        if decision.kind == "reject" and decision.retryable:
                            if not coordinator.lifecycle._operation_is_current(operation_id, State.GENERATING):
                                raise asyncio.CancelledError from None
                            # Preserve only a short diagnostic, never the rejected
                            # Markdown. A format correction costs one of the same
                            # bounded agent steps and cannot dispatch code.
                            detail = decision.reason[:300] or "invalid cell"
                            if invalid_generations == 0:
                                correction = (
                                    "No code was executed. Invalid model response format: "
                                    + detail
                                    + ". There is no external tool-call API. Python function calls are available. "
                                      "Respond with exactly one "
                                      "complete Python/IPython cell as ordinary assistant message text: "
                                      "no tool call, JSON, prose outside the cell, or Markdown fences."
                                )
                            else:
                                # The full contract was already sent once; repeating it verbatim only
                                # pads the next request with the same wall of text.
                                correction = (
                                    "Still no valid cell. Invalid model response format: "
                                    + detail
                                    + ". Respond with exactly one complete Python/IPython cell and nothing "
                                      "else: no fences, prose, tool call, JSON, or function call."
                                )
                            coordinator.conversation._commit_context(
                                origin.request_id, routed.source,
                                "[model response rejected: invalid format; not executed]",
                                {"preflight": {"executed": False, "error": correction}},
                                phase=response.phase, include_user=not context_committed,
                            )
                            context_committed = True
                            context_pending = False
                            exhausted = preflight_exhausted(response)
                            if exhausted is not None:
                                return exhausted
                            published_events.append(await coordinator.frontend._emit_progress(
                                generation_origin,
                                {"phase": "format_retry", "step": step,
                                 "text": "Model response had invalid format; requesting a Python-only correction."},
                                on_progress=on_progress, operation_id=operation_id,
                                expected_state=State.GENERATING,
                            ))
                            continue

                        if decision.kind != "execute" or not decision.source.strip():
                            if context_pending:
                                coordinator.conversation._abandon_context(origin.request_id)
                                context_pending = False
                            coordinator.lifecycle._set_state_unless_stopping(State.IDLE)
                            if decision.kind == "finish":
                                reason = decision.reason or "Agent did not call say(final=True); the request is not complete."
                            elif decision.kind == "wait":
                                reason = decision.reason or "Agent is waiting; no final answer was committed."
                            elif decision.kind == "reject":
                                reason = decision.reason or "Provider response was rejected; no source was executed."
                            else:
                                reason = "Interpreter returned empty execution source; no source was executed."
                            if visible_messages:
                                visible_messages.append(reason)
                            else:
                                visible_messages = [reason]
                            return Submission(
                                routed, result=last_result, message="\n".join(visible_messages),
                                execution=last_execution, response=response,
                                say_outputs=tuple(visible_outputs), executions=tuple(executions),
                                events=tuple(published_events),
                            )

                        check_syntax = getattr(coordinator.interpreter, "check_syntax", None)
                        if callable(check_syntax):
                            syntax_error = check_syntax(decision.source)
                            if inspect.isawaitable(syntax_error) or (
                                syntax_error is not None and not isinstance(syntax_error, str)
                            ):
                                raise TypeError("Interpreter syntax check must return text or None synchronously")
                            if syntax_error is not None:
                                if not syntax_error:
                                    raise ValueError("Interpreter returned an empty syntax diagnostic")
                                if not coordinator.lifecycle._operation_is_current(operation_id, State.GENERATING):
                                    raise asyncio.CancelledError from None
                                # This is a model-visible correction, not an
                                # execution or user-visible cell failure. Never
                                # dispatch malformed source to the executor.
                                coordinator.conversation._commit_context(
                                    origin.request_id, routed.source, decision.source,
                                    {"preflight": {"executed": False, "syntax_error": syntax_error}},
                                    phase=response.phase, include_user=not context_committed,
                                )
                                context_committed = True
                                context_pending = False
                                exhausted = preflight_exhausted(response)
                                if exhausted is not None:
                                    return exhausted
                                continue

                        invalid_generations = 0
                        coordinator.lifecycle.state = State.EXECUTING
                        execution_origin = Origin(
                            origin.session_id, origin.request_id, origin.frontend_id,
                            origin.config_revision, generation_id, uuid4().hex,
                        )
                        execution_request = ExecutionRequest(
                            execution_origin, decision.source, "agent", "ipython",
                            allow_stdin=allow_stdin,
                            input_handler=execution_input_handler(execution_origin),
                            output_handler=execution_output_handler(execution_origin, "agent"),
                            llm_handler=create_handler(coordinator, execution_origin),
                        )
                        published_events.append(await coordinator.frontend._emit_progress(
                            execution_origin,
                            {
                                "phase": "execution_start", "step": step, "author": "agent",
                                "text": f"Agent: executing cell (step {step}).",
                            },
                            on_progress=on_progress, operation_id=operation_id,
                            expected_state=State.EXECUTING,
                        ))
                        result = await coordinator.lifecycle._execute_dispatched(execution_request)

                        if not coordinator.lifecycle._operation_is_current(operation_id, State.EXECUTING):
                            raise asyncio.CancelledError from None
                        published_events.extend(await coordinator.frontend._publish_output(
                            execution_request, result, on_progress=on_progress,
                            operation_id=operation_id,
                        ))
                        published_events.append(await coordinator.frontend._emit_progress(
                            execution_origin,
                            {
                                "phase": "cell_complete", "step": step,
                                "status": result.status,
                                "text": f"Agent cell {step} completed with status {result.status}.",
                            },
                            on_progress=on_progress, operation_id=operation_id,
                            expected_state=State.EXECUTING,
                        ))
                        if not coordinator.lifecycle._operation_is_current(operation_id, State.EXECUTING):
                            raise asyncio.CancelledError from None
                        visible_outputs.extend(coordinator._visible_say_outputs(result))
                        visible_messages.extend(coordinator._visible_says(result))
                        last_result, last_execution = result, execution_request
                        executions.append((execution_request, result))

                        observation = coordinator.observation_policy._packed_observation(execution_request, result)
                        coordinator.conversation._commit_context(
                            origin.request_id, routed.source, decision.source, observation,
                            phase=response.phase, include_user=not context_committed,
                        )
                        context_committed = True
                        context_pending = False
                        if result.status in ("success", "error"):
                            completed_cell = getattr(coordinator.context_service, "completed_cell", None)
                            if callable(completed_cell):
                                completed_cell()
                            await coordinator.conversation._archive_context_outputs()
                            if not coordinator.lifecycle._operation_is_current(operation_id, State.EXECUTING):
                                raise asyncio.CancelledError from None

                        if result.status in ("uncertain", "cancelled"):
                            coordinator.lifecycle._set_state_unless_stopping(State.FAILED)
                            if result.status == "uncertain":
                                visible_messages.append(
                                    "Execution result is uncertain; no source will be replayed. The session is paused."
                                )
                            return Submission(
                                routed, result=result, message="\n".join(visible_messages),
                                execution=execution_request, response=response,
                                say_outputs=tuple(visible_outputs), executions=tuple(executions),
                                events=tuple(published_events),
                            )
                        if result.final:
                            coordinator.lifecycle._set_state_unless_stopping(State.IDLE)
                            return Submission(
                                routed, result=result, message="\n".join(visible_messages),
                                execution=execution_request, response=response,
                                say_outputs=tuple(visible_outputs), executions=tuple(executions),
                                events=tuple(published_events),
                            )

                    coordinator.lifecycle._set_state_unless_stopping(State.GENERATING)
                    step_limit_message = (
                        f"Agent paused after {coordinator.max_agent_steps} agent steps without a successful "
                        "say(final=True); submit another request to continue."
                    )
                    published_events.append(await coordinator.frontend._emit_progress(
                        generation_origin,
                        {
                            "phase": "step_limit", "status": "paused",
                            "step_limit": coordinator.max_agent_steps, "text": step_limit_message,
                        },
                        on_progress=on_progress, operation_id=operation_id,
                        expected_state=State.GENERATING,
                    ))
                    coordinator.lifecycle._set_state_unless_stopping(State.IDLE)
                    visible_messages.append(step_limit_message)
                    return Submission(
                        routed, result=last_result, message="\n".join(visible_messages),
                        execution=last_execution, response=last_response,
                        say_outputs=tuple(visible_outputs), executions=tuple(executions),
                        events=tuple(published_events),
                    )

                return await self.direct.execute(
                    routed, origin, operation_id, allow_stdin=allow_stdin,
                    execution_input_handler=execution_input_handler,
                    execution_output_handler=execution_output_handler,
                    llm_handler_factory=lambda execution_origin: create_handler(coordinator, execution_origin),
                    on_progress=on_progress,
                )
            except asyncio.CancelledError:
                self._fail_submission(
                    origin, steering_awaiting_dispatch, context_pending=context_pending,
                    steering_commit_started=steering_commit_started, cancelled=True,
                )
                raise
            except Exception as exc:
                self._fail_submission(
                    origin, steering_awaiting_dispatch, context_pending=context_pending,
                    steering_commit_started=steering_commit_started, cancelled=False, error=exc,
                )
                raise
            finally:
                coordinator.activity = None
                coordinator.lifecycle.operation.finish(operation_id, task)
                if coordinator.lifecycle.state in (State.FAILED, State.STOPPING, State.CLOSED):
                    await coordinator.lifecycle._fail_pending_actions("interrupted", "Session is unavailable")
                asyncio.get_running_loop().call_soon(coordinator.lifecycle._start_queue_worker_if_idle)


    def _fail_submission(
        self, origin: Origin, steering: list[_QueuedAction], *,
        context_pending: bool, steering_commit_started: bool,
        cancelled: bool, error: Exception | None = None,
    ) -> None:
        """Settle reserved steering/context; never replay an admitted side effect."""
        coordinator = self.coordinator
        for item in steering:
            message = (
                "Steering was cancelled before provider dispatch" if cancelled
                else "Steering failed before provider dispatch"
                + ("; context may contain partial steering" if steering_commit_started else "")
                + ": " + str(error)
            )
            coordinator._complete_queue_item(
                item, QueueOutcome(
                    item.action.origin, "interrupted" if cancelled else "failed", error=message,
                ),
            )
        if context_pending:
            try:
                coordinator.conversation._abandon_context(origin.request_id)
            except Exception:
                pass
        if coordinator.lifecycle.state is State.GENERATING:
            coordinator.lifecycle._set_state_unless_stopping(State.IDLE)
        elif coordinator.lifecycle.state in (State.EXECUTING, State.COMMAND):
            # Only an acknowledged stopped execution permits reuse after cancel.
            acknowledged_stop = (
                cancelled and coordinator.lifecycle.state is State.EXECUTING
                and coordinator.lifecycle.operation.execution_outcome_status == "cancelled"
            )
            coordinator.lifecycle._set_state_unless_stopping(
                State.IDLE if acknowledged_stop else State.FAILED,
            )
