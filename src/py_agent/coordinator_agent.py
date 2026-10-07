"""Agent generation, preflight, execution and finalization phases."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from itertools import count
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from .collapse_control import parse_collapse
from .contracts import (
    AgentDecision,
    ContextSnapshot,
    ExecutionRequest,
    ExecutionResult,
    ModelRequest,
    ModelResponse,
    Origin,
    OutputEvent,
    QueueOutcome,
    RoutedAction,
    SayOutput,
    Submission,
)
from .coordinator_boundary import BoundaryQueue
from .coordinator_request import RequestScope
from .coordinator_support import (
    MAX_AGENT_RESPONSE_CHARS,
    State,
)
from .coordinator_support import (
    MAX_PENDING_ACTIONS as MAX_PENDING_ACTIONS,
)

if TYPE_CHECKING:
    pass


if TYPE_CHECKING:
    from .coordinator_runner import RequestRunner


@dataclass
class AgentState:
    initial_context: ContextSnapshot
    context_committed: bool
    visible_messages: list[str] = field(default_factory=list)
    visible_outputs: list[SayOutput] = field(default_factory=list)
    published_events: list[OutputEvent] = field(default_factory=list)
    executions: list[tuple[ExecutionRequest, ExecutionResult]] = field(default_factory=list)
    last_result: ExecutionResult | None = None
    last_execution: ExecutionRequest | None = None
    last_response: ModelResponse | None = None
    invalid_generations: int = 0
    dispatched_steering: list[tuple[str, str]] = field(default_factory=list)


class PreflightFlow(Enum):
    CONTINUE = auto()
    NOT_COLLAPSE = auto()


class AgentTurn:
    """One request's model/cell loop; outer runner retains admission and cleanup."""

    def __init__(
        self, runner: RequestRunner, scope: RequestScope, routed: RoutedAction, boundary: BoundaryQueue
    ) -> None:
        self.runner = runner
        self.coordinator = runner.coordinator
        self.scope = scope
        self.routed = routed
        self.boundary = boundary
        self.io = boundary.io
        coordinator = self.coordinator
        scope.context_pending = callable(getattr(coordinator.context_service, "prepare_request", None)) and not (
            scope.resume_checkpoint is not None and scope.resume_checkpoint.committed
        )
        if scope.resume_checkpoint is not None:
            if not scope.resume_checkpoint.committed:
                coordinator.conversation.prepare_context(routed.source, scope.origin.request_id)
            initial_context = scope.resume_checkpoint.context
            context_committed = scope.resume_checkpoint.committed
        else:
            initial_context = coordinator.conversation.prepare_context(routed.source, scope.origin.request_id)
            context_committed = False
        coordinator.conversation.observe_context_epoch(initial_context.epoch)
        self.state = AgentState(initial_context, context_committed)

    async def run(self) -> Submission:
        coordinator = self.coordinator
        steps = count(1) if coordinator.max_agent_steps == 0 else range(1, coordinator.max_agent_steps + 1)
        for step in steps:
            model_request, response, forced_collapse, generation_origin = await self._generate(step)
            decision = await self._preflight(model_request, response, forced_collapse, generation_origin, step)
            if isinstance(decision, Submission):
                return decision
            if decision is None:
                continue
            submission = await self._execute(decision, response, generation_origin, step)
            if submission is not None:
                return submission
        return await self._step_limit(generation_origin)

    async def _generate(self, step: int) -> tuple[ModelRequest, ModelResponse, bool, Origin]:
        coordinator, scope, state = self.coordinator, self.scope, self.state
        if step > 1:
            await self.boundary.drain_boundary_queue()
            if coordinator.lifecycle.operation.identity != scope.operation_id or coordinator.lifecycle.state in (
                State.FAILED,
                State.STOPPING,
                State.CLOSED,
            ):
                raise asyncio.CancelledError from None
        coordinator.lifecycle.state = State.GENERATING
        generation_id = uuid4().hex
        generation_origin = Origin(
            scope.origin.session_id,
            scope.origin.request_id,
            scope.origin.frontend_id,
            scope.origin.config_revision,
            generation_id,
        )
        state.published_events.append(
            await coordinator.frontend.emit_progress(
                generation_origin,
                {
                    "phase": "generation_start",
                    "step": step,
                    "text": f"Agent: requesting a response (step {step}).",
                },
                on_progress=scope.on_progress,
                operation_id=scope.operation_id,
                expected_state=State.GENERATING,
            )
        )
        # Leading queued steering was reserved at this safe
        # boundary, before context assembly and transforms.
        reset_needed = getattr(coordinator.context_service, "needs_reset", None)
        reset_this_step = step > 1 and callable(reset_needed) and reset_needed()
        if reset_this_step:
            # Reset between model calls even during a long task.
            # Reinsert the active user's request so eviction of
            # dispatched history does not erase the task itself.
            snapshot = coordinator.conversation.prepare_context(self.routed.source, scope.origin.request_id)
            state.context_committed = False
            scope.context_pending = callable(getattr(coordinator.context_service, "prepare_request", None))
        else:
            snapshot = state.initial_context if step == 1 else coordinator.conversation.latest_context()
        coordinator.conversation.observe_context_epoch(snapshot.epoch)
        if reset_this_step and state.dispatched_steering:
            snapshot = self.runner.restore_steering(snapshot, state.dispatched_steering)
        model_request, forced_collapse = await self.runner.prepare_model_request(
            snapshot,
            generation_origin,
            scope.config,
            steering=scope.steering_awaiting_dispatch,
            saved_context=scope.resume_checkpoint.context
            if scope.resume_checkpoint is not None and step == 1
            else None,
        )
        if not coordinator.lifecycle.operation_is_current(scope.operation_id, State.GENERATING):
            raise asyncio.CancelledError from None
        # This is the final post-transform request actually dispatched to
        # the provider. Persist it before any provider-side effect.
        # Commit only after transforms/validation have succeeded. If
        # cancelled earlier, no steering leaks into future context.
        for steering_item in scope.steering_awaiting_dispatch:
            scope.steering_commit_started = True
            coordinator.conversation.append_steering_context(
                steering_item.action.source,
                steering_item.action.origin.request_id,
            )
        if scope.resume_checkpoint is not None and step == 1:
            model_request = replace(
                model_request,
                context=scope.resume_checkpoint.context,
                model=scope.resume_checkpoint.model,
                options=dict(scope.resume_checkpoint.options),
            )
        coordinator.lifecycle.operation.begin_model(model_request)
        await coordinator.journal_policy.commit(lambda journal: journal.record_model_request(model_request))
        for steering_item in scope.steering_awaiting_dispatch:
            state.dispatched_steering.append((
                steering_item.action.origin.request_id,
                steering_item.action.source,
            ))
            coordinator.complete_queue_item(
                steering_item,
                QueueOutcome(steering_item.action.origin, "steered"),
            )
        scope.steering_awaiting_dispatch.clear()
        # Only transport/rate-limit failures explicitly classified
        # as transient may retry. Never retry credentials, malformed
        # responses, plugin validation or code execution. A retry
        # sends the same already-journaled request; each failed
        # attempt gets its own usage-unknown journal record.
        model_request, response, overflow_recovered = await self.runner.models.generate(
            model_request,
            operation_id=scope.operation_id,
            task_text=self.routed.source,
            context_committed=state.context_committed,
            published_events=state.published_events,
            generation_origin=generation_origin,
            on_progress=scope.on_progress,
            executed_cells=len(state.executions)
            + (scope.resume_checkpoint.executed_cells if scope.resume_checkpoint else 0),
        )
        forced_collapse = forced_collapse or overflow_recovered

        if not isinstance(response, ModelResponse):
            await coordinator.journal_policy.record_provider_usage(model_request, None, outcome="failed")
            raise TypeError("Provider must return a ModelResponse")
        coordinator.activity = None
        await coordinator.journal_policy.record_provider_usage(model_request, response, outcome="returned")
        if not coordinator.lifecycle.operation_is_current(scope.operation_id, State.GENERATING):
            raise asyncio.CancelledError from None
        if not isinstance(response.text, str):
            raise TypeError("Provider response text must be text")
        state.last_response = response
        record_usage = getattr(coordinator.context_service, "record_response", None)
        if callable(record_usage):
            record_usage(response)
        return model_request, response, forced_collapse, generation_origin

    def _preflight_exhausted(self, response: ModelResponse, reason: str) -> Submission | None:
        coordinator, state = self.coordinator, self.state
        state.invalid_generations += 1
        # A limitless successful-cell loop must not turn a
        # broken format into unbounded paid provider retries.
        if state.invalid_generations <= 2:
            return None
        coordinator.lifecycle.set_state_unless_stopping(State.IDLE)
        state.visible_messages.append(
            "Agent paused after 3 consecutive invalid model responses; "
            "no rejected source was executed. "
            f"Last rejection: {reason[:300]}. "
            "Submit a new request asking for a smaller, valid Python/IPython cell to continue."
        )
        return Submission(
            self.routed,
            result=state.last_result,
            message="\n".join(state.visible_messages),
            execution=state.last_execution,
            response=response,
            say_outputs=tuple(state.visible_outputs),
            executions=tuple(state.executions),
            events=tuple(state.published_events),
        )

    async def _collapse(
        self, model_request: ModelRequest, response: ModelResponse, forced_collapse: bool
    ) -> Submission | PreflightFlow:
        coordinator, scope, state = self.coordinator, self.scope, self.state
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
            await coordinator.journal_policy.commit(
                lambda journal: journal.record_context_collapse(model_request, response.text, outcome="requested")
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
                            coordinator.lifecycle.set_state_unless_stopping(State.FAILED)
                            raise
                        if not coordinator.lifecycle.operation_is_current(scope.operation_id, State.GENERATING):
                            raise asyncio.CancelledError from None
                        return index

                    try:
                        receipt = await collapse(*cast(tuple[object, ...], collapse_args), store_archive)
                    except (ValueError, TypeError) as exc:
                        if coordinator.lifecycle.state is State.FAILED:
                            raise
                        collapse_error = str(exc)[:500]
            if collapse_error is not None:
                await coordinator.journal_policy.commit(
                    lambda journal: journal.record_context_collapse(
                        model_request, response.text, outcome="rejected", detail=collapse_error
                    )
                )
                coordinator.conversation.commit_context(
                    scope.origin.request_id,
                    self.routed.source,
                    "[collapse cell rejected; no code executed]",
                    {"preflight": {"executed": False, "error": collapse_error}},
                    phase=response.phase,
                    include_user=not state.context_committed,
                )
                state.context_committed = True
                scope.context_pending = False
                exhausted = self._preflight_exhausted(response, collapse_error)
                if exhausted is not None:
                    return exhausted
            else:
                # Only the receipt remains at the call site. The
                # original source is retained in the audit journal.
                coordinator.conversation.commit_context(
                    scope.origin.request_id,
                    self.routed.source,
                    receipt,
                    None,
                    phase=response.phase,
                    include_user=not state.context_committed,
                )
                state.context_committed = True
                scope.context_pending = False
                state.invalid_generations = 0
                await coordinator.journal_policy.commit(
                    lambda journal: journal.record_context_collapse(
                        model_request, response.text, outcome="succeeded", detail=receipt
                    )
                )
            return PreflightFlow.CONTINUE
        return PreflightFlow.NOT_COLLAPSE

    async def _preflight(
        self,
        model_request: ModelRequest,
        response: ModelResponse,
        forced_collapse: bool,
        generation_origin: Origin,
        step: int,
    ) -> AgentDecision | Submission | None:
        coordinator, scope, state = self.coordinator, self.scope, self.state
        if len(response.text) > MAX_AGENT_RESPONSE_CHARS:
            coordinator.conversation.commit_context(
                scope.origin.request_id,
                self.routed.source,
                f"[generated response rejected: {len(response.text)} characters; limit 8000]",
                {
                    "preflight": {
                        "executed": False,
                        "error": f"Your response was {len(response.text)} characters (limit 8000). "
                        "Try sending a smaller Python cell.",
                    }
                },
                phase=response.phase,
                include_user=not state.context_committed,
            )
            state.context_committed = True
            scope.context_pending = False
            exhausted = self._preflight_exhausted(response, f"Response exceeds 8000 characters ({len(response.text)} characters)")
            if exhausted is not None:
                return exhausted
            return None
        collapse = await self._collapse(model_request, response, forced_collapse)
        if isinstance(collapse, Submission):
            return collapse
        if collapse is PreflightFlow.CONTINUE:
            return None
        decision = coordinator.interpreter.interpret(response)
        if not isinstance(decision, AgentDecision):
            raise TypeError("Interpreter must return an AgentDecision")
        if decision.kind not in ("execute", "finish", "wait", "reject"):
            raise ValueError(f"Interpreter returned unknown decision: {decision.kind!r}")
        if not isinstance(decision.source, str) or not isinstance(decision.reason, str):
            raise TypeError("Decision source and reason must be text")
        if type(decision.retryable) is not bool or (decision.retryable and decision.kind != "reject"):
            raise ValueError("Only a rejected response may request a format retry")
        if decision.kind == "execute" and len(decision.source) > MAX_AGENT_RESPONSE_CHARS:
            coordinator.conversation.commit_context(
                scope.origin.request_id,
                self.routed.source,
                f"[generated cell rejected: {len(decision.source)} characters; limit 8000]",
                {
                    "preflight": {
                        "executed": False,
                        "error": f"Your Python cell was {len(decision.source)} characters (limit 8000). "
                        "Try sending a smaller cell.",
                    }
                },
                phase=response.phase,
                include_user=not state.context_committed,
            )
            state.context_committed = True
            scope.context_pending = False
            exhausted = self._preflight_exhausted(response, f"Cell exceeds 8000 characters ({len(decision.source)} characters)")
            if exhausted is not None:
                return exhausted
            return None

        if decision.kind == "reject" and decision.retryable:
            if not coordinator.lifecycle.operation_is_current(scope.operation_id, State.GENERATING):
                raise asyncio.CancelledError from None
            correction = self.runner.format_correction(
                decision.reason,
                repeated=state.invalid_generations > 0,
            )
            coordinator.conversation.commit_context(
                scope.origin.request_id,
                self.routed.source,
                "[model response rejected: invalid format; not executed]",
                {"preflight": {"executed": False, "error": correction}},
                phase=response.phase,
                include_user=not state.context_committed,
            )
            state.context_committed = True
            scope.context_pending = False
            exhausted = self._preflight_exhausted(response, decision.reason)
            if exhausted is not None:
                return exhausted
            state.published_events.append(
                await coordinator.frontend.emit_progress(
                    generation_origin,
                    {
                        "phase": "format_retry",
                        "step": step,
                        "text": "Model response had invalid format; requesting a Python-only correction.",
                    },
                    on_progress=scope.on_progress,
                    operation_id=scope.operation_id,
                    expected_state=State.GENERATING,
                )
            )
            return None

        if decision.kind != "execute" or not decision.source.strip():
            if scope.context_pending:
                coordinator.conversation.abandon_context(scope.origin.request_id)
                scope.context_pending = False
            coordinator.lifecycle.set_state_unless_stopping(State.IDLE)
            if decision.kind == "finish":
                reason = decision.reason or "Agent did not call say(final=True); the request is not complete."
            elif decision.kind == "wait":
                reason = decision.reason or "Agent is waiting; no final answer was committed."
            elif decision.kind == "reject":
                reason = decision.reason or "Provider response was rejected; no source was executed."
            else:
                reason = "Interpreter returned empty execution source; no source was executed."
            if state.visible_messages:
                state.visible_messages.append(reason)
            else:
                state.visible_messages = [reason]
            return Submission(
                self.routed,
                result=state.last_result,
                message="\n".join(state.visible_messages),
                execution=state.last_execution,
                response=response,
                say_outputs=tuple(state.visible_outputs),
                executions=tuple(state.executions),
                events=tuple(state.published_events),
            )

        syntax_error = self.runner.check_cell_syntax(decision.source)
        if syntax_error is not None:
            if not coordinator.lifecycle.operation_is_current(scope.operation_id, State.GENERATING):
                raise asyncio.CancelledError from None
            # This is a model-visible correction, not an
            # execution or user-visible cell failure. Never
            # dispatch malformed source to the executor.
            coordinator.conversation.commit_context(
                scope.origin.request_id,
                self.routed.source,
                decision.source,
                {"preflight": {"executed": False, "syntax_error": syntax_error}},
                phase=response.phase,
                include_user=not state.context_committed,
            )
            state.context_committed = True
            scope.context_pending = False
            exhausted = self._preflight_exhausted(response, syntax_error)
            if exhausted is not None:
                return exhausted
            return None

        return decision

    async def _execute(
        self, decision: AgentDecision, response: ModelResponse, generation_origin: Origin, step: int
    ) -> Submission | None:
        coordinator, scope, state = self.coordinator, self.scope, self.state
        state.invalid_generations = 0
        coordinator.lifecycle.state = State.EXECUTING
        execution_origin = Origin(
            scope.origin.session_id,
            scope.origin.request_id,
            scope.origin.frontend_id,
            scope.origin.config_revision,
            generation_origin.generation_id,
            uuid4().hex,
        )
        execution_request = ExecutionRequest(
            execution_origin,
            decision.source,
            "agent",
            "ipython",
            allow_stdin=scope.allow_stdin,
            input_handler=self.io.input(execution_origin),
            output_handler=self.io.output(execution_origin, "agent"),
            llm_handler=self.io.llm(execution_origin),
        )
        state.published_events.append(
            await coordinator.frontend.emit_progress(
                execution_origin,
                {
                    "phase": "execution_start",
                    "step": step,
                    "author": "agent",
                    "text": f"Agent: executing cell (step {step}).",
                },
                on_progress=scope.on_progress,
                operation_id=scope.operation_id,
                expected_state=State.EXECUTING,
            )
        )
        result = await coordinator.lifecycle.execute_dispatched(execution_request)

        return await self._finalize_execution(execution_request, result, response, step)

    async def _finalize_execution(
        self,
        execution_request: ExecutionRequest,
        result: ExecutionResult,
        response: ModelResponse,
        step: int,
    ) -> Submission | None:
        coordinator, scope, state = self.coordinator, self.scope, self.state
        if not coordinator.lifecycle.operation_is_current(scope.operation_id, State.EXECUTING):
            raise asyncio.CancelledError from None
        state.published_events.extend(
            await coordinator.frontend.publish_output(
                execution_request,
                result,
                on_progress=scope.on_progress,
                operation_id=scope.operation_id,
            )
        )
        state.published_events.append(
            await coordinator.frontend.emit_progress(
                execution_request.origin,
                {
                    "phase": "cell_complete",
                    "step": step,
                    "status": result.status,
                    "text": f"Agent cell {step} completed with status {result.status}.",
                },
                on_progress=scope.on_progress,
                operation_id=scope.operation_id,
                expected_state=State.EXECUTING,
            )
        )
        if not coordinator.lifecycle.operation_is_current(scope.operation_id, State.EXECUTING):
            raise asyncio.CancelledError from None
        state.visible_outputs.extend(coordinator.visible_say_outputs(result))
        state.visible_messages.extend(coordinator.visible_says(result))
        state.last_result, state.last_execution = result, execution_request
        state.executions.append((execution_request, result))

        observation = coordinator.observation_policy.packed_observation(execution_request, result)
        coordinator.conversation.commit_context(
            scope.origin.request_id,
            self.routed.source,
            execution_request.source,
            observation,
            phase=response.phase,
            include_user=not state.context_committed,
        )
        state.context_committed = True
        scope.context_pending = False
        if result.status in ("success", "error"):
            completed_cell = getattr(coordinator.context_service, "completed_cell", None)
            if callable(completed_cell):
                completed_cell()
            await coordinator.conversation.archive_context_outputs()
            if not coordinator.lifecycle.operation_is_current(scope.operation_id, State.EXECUTING):
                raise asyncio.CancelledError from None

        if result.status in ("uncertain", "cancelled"):
            coordinator.lifecycle.set_state_unless_stopping(State.FAILED)
            if result.status == "uncertain":
                state.visible_messages.append(
                    "Execution result is uncertain; no source will be replayed. The session is paused."
                )
            return Submission(
                self.routed,
                result=result,
                message="\n".join(state.visible_messages),
                execution=execution_request,
                response=response,
                say_outputs=tuple(state.visible_outputs),
                executions=tuple(state.executions),
                events=tuple(state.published_events),
            )
        if result.final:
            coordinator.lifecycle.set_state_unless_stopping(State.IDLE)
            return Submission(
                self.routed,
                result=result,
                message="\n".join(state.visible_messages),
                execution=execution_request,
                response=response,
                say_outputs=tuple(state.visible_outputs),
                executions=tuple(state.executions),
                events=tuple(state.published_events),
            )
        return None

    async def _step_limit(self, generation_origin: Origin) -> Submission:
        coordinator, scope, state = self.coordinator, self.scope, self.state
        coordinator.lifecycle.set_state_unless_stopping(State.GENERATING)
        step_limit_message = (
            f"Agent paused after {coordinator.max_agent_steps} agent steps without a successful "
            "say(final=True); submit another request to continue."
        )
        state.published_events.append(
            await coordinator.frontend.emit_progress(
                generation_origin,
                {
                    "phase": "step_limit",
                    "status": "paused",
                    "step_limit": coordinator.max_agent_steps,
                    "text": step_limit_message,
                },
                on_progress=scope.on_progress,
                operation_id=scope.operation_id,
                expected_state=State.GENERATING,
            )
        )
        coordinator.lifecycle.set_state_unless_stopping(State.IDLE)
        state.visible_messages.append(step_limit_message)
        return Submission(
            self.routed,
            result=state.last_result,
            message="\n".join(state.visible_messages),
            execution=state.last_execution,
            response=state.last_response,
            say_outputs=tuple(state.visible_outputs),
            executions=tuple(state.executions),
            events=tuple(state.published_events),
        )
