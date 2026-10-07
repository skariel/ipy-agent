"""SessionRuntime lifecycle component; orchestration remains in SessionRuntime."""
from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING, Literal, Protocol, cast
from uuid import uuid4

from .async_commit import settle
from .contracts import (
    MAX_FRONTEND_ID_CHARS,
    MAX_QUEUED_ACTION_CHARS,
    ExecutionRequest,
    ExecutionResult,
    ExecutorCapabilityError,
    InputHandler,
    ModelResponse,
    Origin,
    ProgressCallback,
    QueueFullError,
    QueueOutcome,
    QueueTicket,
    UserAction,
)
from .coordinator_operation import Operation
from .coordinator_support import (
    MAX_PENDING_ACTIONS,
    State,
    _QueuedAction,
)

if TYPE_CHECKING:
    from .coordinator_runtime import SessionRuntime


class _LifecycleExecutor(Protocol):
    async def execute(self, request: ExecutionRequest) -> ExecutionResult: ...
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def interrupt(self) -> None: ...


class _LifecycleRouter(Protocol):
    def route(self, action: UserAction) -> object: ...


class ExecutionLifecycle:
    """Owns lifecycle state and behavior for one coordinator."""

    def __init__(self, coordinator: SessionRuntime) -> None:
        self.coordinator = coordinator
        self._journal_started = False
        self._journal_closed = False
        self._journal_failed = False
        self.state = State.NEW
        self._lock = asyncio.Lock()
        self._queue_lock = asyncio.Lock()
        self._pending_actions: deque[_QueuedAction] = deque()
        self._queue_worker: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._executor_close_attempted = False
        self.operation = Operation()
        self._execution_record_lock = asyncio.Lock()
        self._generation: asyncio.Task[ModelResponse] | None = None

    def _set_state_unless_stopping(self, state: State) -> None:
        if self.state not in (State.STOPPING, State.CLOSED):
            self.state = state

    def _operation_is_current(self, operation_id: str, state: State) -> bool:
        return self.operation.current(operation_id) and self.state is state

    async def _record_execution_result(self, request: ExecutionRequest, result: ExecutionResult) -> None:
        await settle(self._commit_execution(request, result=result))

    async def _record_uncertain_execution(self, request: ExecutionRequest, reason: str) -> None:
        await settle(self._commit_execution(request, reason=reason))

    async def _commit_execution(
        self, request: ExecutionRequest, *,
        result: ExecutionResult | None = None, reason: str | None = None,
    ) -> None:
        async with self._execution_record_lock:
            if self.operation.execution_request is request and self.operation.execution_result_recorded:
                return
            if result is not None:
                await self.coordinator.journal_policy.commit(lambda journal: journal.record_execution_result(request, result))
            else:
                self.coordinator.execution_outcome = (
                    "Execution is uncertain; side effects may have occurred. Session paused. "
                    "Inspect external state before restart; never replay automatically."
                )
                self.coordinator.recovery = None
                await self.coordinator.journal_policy.commit(lambda journal: journal.record_uncertain_execution(request, reason or "Execution outcome unavailable"))
            if self.operation.execution_request is request:
                self.operation.execution_result_recorded = True

    async def _execute_dispatched(self, request: ExecutionRequest) -> ExecutionResult:
        """Commit source before dispatch and result before publishing its output."""
        self.coordinator.recovery = None
        await self.coordinator.journal_policy.commit(lambda journal: journal.record_execution_source(request))
        self.operation.begin_execution(request)
        try:
            try:
                result = await cast(_LifecycleExecutor, self.coordinator.executor).execute(request)
            except asyncio.CancelledError:
                await self.coordinator.lifecycle._record_uncertain_execution(
                    request, "Executor call was cancelled; side effects may have occurred",
                )
                raise
            except Exception as exc:
                await self.coordinator.lifecycle._record_uncertain_execution(request, f"Executor raised {type(exc).__name__}")
                raise
        finally:
            self.operation.execution_active = False
        try:
            self.coordinator._validate_execution_result(request, result)
            if result.status == "uncertain":
                self.coordinator.execution_outcome = (
                    "Execution is uncertain; side effects may have occurred. Session paused. "
                    "Inspect external state before restart; never replay automatically."
                )
                self.coordinator.recovery = None
            elif result.status == "cancelled":
                self.coordinator.execution_outcome = (
                    "Execution cancelled; side effects may have occurred. No Python source is replayed."
                )
                self.coordinator.recovery = None
            else:
                self.coordinator.execution_outcome = (
                    f"Last Python cell completed with status {result.status}; "
                    "its side effects remain. Model recovery never replays it."
                )
        except Exception as exc:
            await self.coordinator.lifecycle._record_uncertain_execution(request, f"Executor returned an invalid result: {type(exc).__name__}")
            raise
        await self.coordinator.lifecycle._record_execution_result(request, result)
        self.operation.execution_outcome_status = result.status
        return result

    async def _close_executor(self) -> None:
        if self._executor_close_attempted:
            return
        self._executor_close_attempted = True
        await cast(_LifecycleExecutor, self.coordinator.executor).close()

    async def _close_journal(self, state: str) -> None:
        if self._journal_closed:
            return
        failure = None
        try:
            if self._journal_started and not self._journal_failed:
                await self.coordinator.journal_policy.commit(lambda journal: journal.end(self.coordinator.session_id, self.coordinator.config_revision, state))
        except BaseException as exc:
            failure = exc
        try:
            from .journal_worker import SQLiteJournalWorker
            if isinstance(self.coordinator.journal, SQLiteJournalWorker):
                await self.coordinator.journal.aclose()
            else:
                self.coordinator.journal.close()
        except BaseException as exc:
            if failure is None:
                failure = exc
            elif hasattr(failure, "add_note"):
                failure.add_note(f"Journal close also failed: {type(exc).__name__}")
        finally:
            self._journal_closed = True
        if failure is not None:
            raise failure

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self.state is not State.NEW:
                raise RuntimeError("SessionRuntime cannot start unless it is new")
            try:
                config = self.coordinator._current_config()
                if config is not None:
                    self.coordinator.config_revision = config.revision
                self._journal_started = True
                await self.coordinator.journal_policy.commit(lambda journal: journal.start(self.coordinator.session_id, self.coordinator.config_revision, self.coordinator.provider_id, self.coordinator.model))
                if (callable(getattr(self.coordinator.context_service, "collapse", None))
                        and not callable(getattr(self.coordinator.executor, "store_collapsed", None))):
                    raise ExecutorCapabilityError(
                        "store_collapsed archival required by the selected context service"
                    )
                await cast(_LifecycleExecutor, self.coordinator.executor).start()
            except BaseException:
                self.state = State.FAILED
                try:
                    await self.coordinator.lifecycle._close_executor()
                except BaseException:
                    # Preserve startup failure; close() remains safe to call.
                    pass
                try:
                    await self.coordinator.lifecycle._close_journal(State.FAILED.value)
                except BaseException:
                    # Preserve startup failure; the journal has still been closed.
                    pass
                raise
            self.state = State.IDLE

    @property
    def pending_action_count(self) -> int:
        """Number of accepted actions not yet dispatched or applied as steering."""
        return len(self._pending_actions)

    @property
    def queue_active(self) -> bool:
        """Whether a queue item is pending or the serial queue worker is draining."""
        return bool(self._pending_actions) or self._queue_worker is not None

    async def enqueue(
        self,
        frontend_id: str,
        text: str,
        *,
        allow_stdin: bool = False,
        input_handler: InputHandler | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> QueueTicket:
        """Accept bounded FIFO work without cancelling the active provider or cell.

        English asks become steering at a safe cell boundary. Direct @/!/%
        cells and slash commands run at that boundary before the next model
        request, without interrupting execution. Queue items are processed in
        FIFO order; commands do not wait for the whole agent turn to finish.
        """
        if self.state in (State.NEW, State.STOPPING, State.FAILED, State.CLOSED):
            raise RuntimeError("Session unavailable for queued actions")
        if (not isinstance(frontend_id, str) or not frontend_id
                or len(frontend_id) > MAX_FRONTEND_ID_CHARS or "\x00" in frontend_id):
            raise ValueError("Frontend ID must be nonempty bounded text without NUL")
        if not isinstance(text, str):
            raise TypeError("Queued submission text must be text")
        if len(text) > MAX_QUEUED_ACTION_CHARS:
            raise ValueError("Queued submission exceeds the text size limit")
        if type(allow_stdin) is not bool:
            raise TypeError("allow_stdin must be a boolean")
        if input_handler is not None and not callable(input_handler):
            raise TypeError("input_handler must be callable or None")
        if on_progress is not None and not callable(on_progress):
            raise TypeError("on_progress must be callable or None")

        config = self.coordinator._current_config()
        revision = config.revision if config is not None else self.coordinator.config_revision
        origin = Origin(self.coordinator.session_id, uuid4().hex, frontend_id, revision)
        routed = self.coordinator._validate_routed_action(cast(_LifecycleRouter, self.coordinator.router).route(UserAction(origin, text)), origin)
        loop = asyncio.get_running_loop()
        completion: asyncio.Future[QueueOutcome] = loop.create_future()
        async with self._queue_lock:
            if self.state in (State.STOPPING, State.FAILED, State.CLOSED):
                raise RuntimeError("Session unavailable for queued actions")
            if len(self._pending_actions) >= MAX_PENDING_ACTIONS:
                raise QueueFullError(
                    f"Pending action queue is full ({MAX_PENDING_ACTIONS}); no action was accepted"
                )
            item = _QueuedAction(
                routed, text, config, allow_stdin, input_handler, on_progress, completion,
            )
            self._pending_actions.append(item)
            ticket = QueueTicket(
                routed.origin, routed.kind, len(self._pending_actions), asyncio.shield(completion),
            )
        self.coordinator.lifecycle._start_queue_worker_if_idle()
        return ticket

    def _start_queue_worker_if_idle(self) -> None:
        if (self._queue_worker is None and self._pending_actions
                and self.state is State.IDLE and not self._lock.locked()):
            worker = asyncio.create_task(
                self.coordinator.lifecycle._drain_queue(), name="py-agent-queued-actions",
            )
            self._queue_worker = worker
            # A task cancelled before its coroutine starts never enters the
            # drainer's finally block. Always release the worker slot.
            worker.add_done_callback(self.coordinator.lifecycle._queue_worker_finished)

    def _queue_worker_finished(self, worker: asyncio.Task[None]) -> None:
        if self._queue_worker is worker:
            self._queue_worker = None
        self.coordinator.lifecycle._start_queue_worker_if_idle()

    async def _fail_pending_actions(self, status: Literal["steered", "completed", "failed", "interrupted", "closed"], error: str) -> None:
        async with self._queue_lock:
            pending = tuple(self._pending_actions)
            self._pending_actions.clear()
        for item in pending:
            self.coordinator._complete_queue_item(item, QueueOutcome(item.action.origin, status, error=error))

    async def _drain_queue(self) -> None:
        current = asyncio.current_task()
        try:
            while True:
                async with self._queue_lock:
                    if not self._pending_actions:
                        return
                    if self.state is not State.IDLE or self._lock.locked():
                        return
                    item = self._pending_actions.popleft()
                try:
                    submission = await self.coordinator.runner.submit(
                        item.action.origin.frontend_id, item.text, _queued_action=item,
                    )
                except asyncio.CancelledError:
                    self.coordinator._complete_queue_item(
                        item, QueueOutcome(item.action.origin, "interrupted", error="Queued action was cancelled"),
                    )
                    await self.coordinator.lifecycle._fail_pending_actions(
                        "interrupted", "Queue processing was cancelled; pending actions were not dispatched",
                    )
                    return
                except Exception as exc:
                    self.coordinator._complete_queue_item(
                        item, QueueOutcome(item.action.origin, "failed", error=str(exc)),
                    )
                    if self.state in (State.FAILED, State.STOPPING, State.CLOSED):
                        await self.coordinator.lifecycle._fail_pending_actions("interrupted", "Session is unavailable")
                        return
                else:
                    self.coordinator._complete_queue_item(
                        item, QueueOutcome(item.action.origin, "completed", submission=submission),
                    )
        finally:
            if self._queue_worker is current:
                self._queue_worker = None
            self.coordinator.lifecycle._start_queue_worker_if_idle()

    async def interrupt(self) -> None:
        if self.state in (State.STOPPING, State.CLOSED, State.NEW, State.FAILED):
            return
        if self.state is State.IDLE:
            await self.coordinator.lifecycle._fail_pending_actions("interrupted", "Queued action cancelled by explicit interrupt")
            worker = self._queue_worker
            if worker is not None and worker is not asyncio.current_task():
                worker.cancel()
            return
        if self.state is State.GENERATING:
            # Invalidate before requesting cancellation. Even a provider that
            # suppresses cancellation cannot cause its late response to execute.
            self.operation.invalidate()
            await self.coordinator.lifecycle._fail_pending_actions("interrupted", "Queued action cancelled by explicit interrupt")
            generation = self._generation
            if generation is not None and not generation.done():
                generation.cancel()
            elif self.operation.task is not None and self.operation.task is not asyncio.current_task():
                # Async transforms run before a provider task exists.
                self.operation.task.cancel()
            return
        if self.state is State.COMMAND:
            # Commands may have external side effects too; cancel once and do
            # not advertise the session as safe to replay.
            self.operation.invalidate()
            self.state = State.FAILED
            await self.coordinator.lifecycle._fail_pending_actions("interrupted", "Queued action cancelled by explicit interrupt")
            active = self.operation.task
            if active is not None and active is not asyncio.current_task():
                active.cancel()
            return
        if self.state in (State.EXECUTING, State.WAITING_FOR_INPUT):
            # Invalidate before requesting cancellation so a late executor result
            # cannot be dispatched even if the executor ignores the interrupt.
            self.operation.invalidate()
            await self.coordinator.lifecycle._fail_pending_actions("interrupted", "Queued action cancelled by explicit interrupt")
            if self.operation.execution_active:
                # Stop the cell and let the cancelled operation choose its own
                # transition: a "cancelled" result means the executor kept a
                # usable namespace, anything else fails the session closed.
                await cast(_LifecycleExecutor, self.coordinator.executor).interrupt()
            else:
                # The cell result is already recorded, but output delivery or
                # observer acknowledgement was cancelled; keep the fail-closed
                # transition instead of promising an unchanged session.
                self.state = State.FAILED
                active = self.operation.task
                if active is not None and active is not asyncio.current_task():
                    active.cancel()

    async def close(self) -> None:
        task = asyncio.current_task()
        if task is self.operation.task:
            raise RuntimeError("Cannot close a coordinator from an active submission")
        if self._close_task is None:
            self._close_task = asyncio.create_task(self.coordinator.lifecycle._close_impl(), name="py-agent-coordinator-close")
        await asyncio.shield(self._close_task)

    async def _close_impl(self) -> None:
        async with self._lifecycle_lock:
            if self.state is State.CLOSED:
                return
            was_executing = self.operation.execution_active
            self.state = State.STOPPING
            await self.coordinator.lifecycle._fail_pending_actions("closed", "SessionRuntime closed before queued action dispatch")
            worker = self._queue_worker
            if worker is not None and worker is not asyncio.current_task():
                worker.cancel()
            self.operation.invalidate()
            generation = self._generation
            if generation is not None and not generation.done():
                generation.cancel()
            active = self.operation.task
            failure = None
            if active is not None and active is not asyncio.current_task():
                active.cancel()
                # Give cancellation a bounded chance to unwind. A provider may
                # suppress cancellation; its invalidated result cannot dispatch.
                done, _ = await asyncio.wait({active}, timeout=self.coordinator._shutdown_timeout)
                if active in done:
                    await asyncio.gather(active, return_exceptions=True)
                else:
                    if was_executing:
                        try:
                            await cast(_LifecycleExecutor, self.coordinator.executor).interrupt()
                        except BaseException:
                            pass
                    done, _ = await asyncio.wait({active}, timeout=min(self.coordinator._shutdown_timeout, 1.0))
                    if active in done:
                        await asyncio.gather(active, return_exceptions=True)
                    elif not self._journal_failed:
                        # The operation is now explicitly marked as uncertain or
                        # cancelled before its journal is closed. Late completions
                        # cannot overwrite that terminal evidence or be replayed.
                        try:
                            if (self.operation.execution_request is not None
                                    and not self.operation.execution_result_recorded):
                                await self.coordinator.lifecycle._record_uncertain_execution(
                                    self.operation.execution_request,
                                    "SessionRuntime shutdown timed out; execution outcome may be uncertain",
                                )
                            if (self.operation.model_request is not None
                                    and not self.operation.provider_usage_recorded):
                                await self.coordinator.journal_policy._record_provider_usage(
                                    self.operation.model_request, None, outcome="cancelled",
                                )
                        except BaseException as exc:
                            failure = exc
            if worker is not None and worker is not active and worker is not asyncio.current_task():
                done, _ = await asyncio.wait({worker}, timeout=self.coordinator._shutdown_timeout)
                if worker in done:
                    await asyncio.gather(worker, return_exceptions=True)
            try:
                await self.coordinator.lifecycle._close_executor()
            except BaseException as exc:
                if failure is None:
                    failure = exc
            try:
                await self.coordinator.lifecycle._close_journal(State.STOPPING.value)
            except BaseException as exc:
                if failure is None:
                    failure = exc
                elif hasattr(failure, "add_note"):
                    failure.add_note(f"Journal shutdown also failed: {type(exc).__name__}")
            finally:
                self.state = State.CLOSED
            if failure is not None:
                raise failure

