"""Serialized orchestration over explicit coordinator state owners."""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
import math
from types import MappingProxyType
from typing import Any, Literal, Protocol, TypeVar, cast
from uuid import uuid4

from .configuration import ConfigSnapshot, ConfigStore, Scalar
from .contracts import (
    CompletenessResult,
    CompletionResult,
    ContextSnapshot,
    ExecutionRequest,
    ExecutionResult,
    Executor,
    ExecutorCapabilities,
    InputHandler,
    InspectionResult,
    ModelRequest,
    ModelResponse,
    Origin,
    OutputEvent,
    ProgressCallback,
    ProviderService,
    QueueOutcome,
    QueueTicket,
    RoutedAction,
    Router,
    SayOutput,
)
from .contracts import (
    Submission as Submission,
)
from .coordinator_conversation import ConversationState
from .coordinator_frontend import FrontendRouting
from .coordinator_journal import JournalPolicy
from .coordinator_lifecycle import ExecutionLifecycle
from .coordinator_observations import ModelObservations
from .coordinator_runner import RequestRunner
from .coordinator_runtime import SessionRuntime
from .coordinator_support import (
    _COORDINATOR_COMMANDS,
    _QueuedAction,
)
from .coordinator_support import (
    MAX_PENDING_ACTIONS as MAX_PENDING_ACTIONS,
)
from .coordinator_support import (
    State as State,
)
from .plugins import PluginError, PluginRuntime, RegisteredCommand, RegisteredObserver
from .runtime_status import Activity, Recovery
from .session_journal import JournalService, NoPersistenceJournal

_Service = TypeVar('_Service')


class _CommandRegistry(Protocol):
    def dispatch(self, name: str, arguments: str) -> object: ...


class Coordinator:
    def __init__(
        self,
        runtime: PluginRuntime,
        *,
        router: str,
        provider: str,
        interpreter: str,
        executor: str,
        context: object | None = None,
        observations: object | None = None,
        command_registry: object | None = None,
        config_store: ConfigStore | None = None,
        journal: JournalService | None = None,
        model: str | None = None,
        executor_wrappers: tuple[str, ...] = (),
        config_revision: int = 0,
        shutdown_timeout: float = 5.0,
        max_agent_steps: int = 0,
    ) -> None:
        self._runtime = SessionRuntime()
        self._conversation = ConversationState(self._runtime)
        self._runtime.conversation = self._conversation
        self._frontend = FrontendRouting(self._runtime)
        self._runtime.frontend = self._frontend
        self._observations = ModelObservations(self._runtime)
        self._runtime.observation_policy = self._observations
        self._journal = JournalPolicy(self._runtime)
        self._runtime.journal_policy = self._journal
        self._lifecycle = ExecutionLifecycle(self._runtime)
        self._runtime.lifecycle = self._lifecycle
        self._runner = RequestRunner(self._runtime)
        self._runtime.runner = self._runner
        if type(max_agent_steps) is not int or max_agent_steps < 0:
            raise ValueError("max_agent_steps must be a nonnegative integer (0 means unlimited)")
        if isinstance(shutdown_timeout, bool) or not isinstance(shutdown_timeout, (int, float)):
            raise ValueError("shutdown_timeout must be a finite positive number")
        try:
            shutdown_timeout = float(shutdown_timeout)
        except OverflowError:
            raise ValueError("shutdown_timeout must be a finite positive number") from None
        if not math.isfinite(shutdown_timeout) or shutdown_timeout <= 0:
            raise ValueError("shutdown_timeout must be a finite positive number")
        if config_store is not None and not isinstance(config_store, ConfigStore):
            raise TypeError("config_store must be a ConfigStore or None")
        if type(config_revision) is not int or config_revision < 0:
            raise ValueError("config_revision must be a nonnegative integer")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ValueError("model must be nonempty text or None")

        if not isinstance(runtime, PluginRuntime):
            raise TypeError("runtime must be a PluginRuntime")
        creation_config = config_store.snapshot if config_store is not None else None
        if creation_config is not None and not isinstance(creation_config, ConfigSnapshot):
            raise TypeError("Configuration store returned an invalid snapshot")
        if isinstance(executor_wrappers, (str, bytes)):
            raise TypeError("executor_wrappers must be a sequence of qualified wrapper IDs")
        try:
            selected_wrappers = tuple(executor_wrappers)
        except TypeError:
            raise TypeError("executor_wrappers must be a sequence of qualified wrapper IDs") from None
        if (any(not isinstance(name, str) or not name.strip() for name in selected_wrappers)
                or len(selected_wrappers) != len(set(selected_wrappers))):
            raise ValueError("Executor wrapper IDs must be unique nonempty text")
        unknown_wrappers = set(selected_wrappers) - runtime.executor_wrappers.keys()
        if unknown_wrappers:
            raise PluginError(f"No enabled executor wrappers named {sorted(unknown_wrappers)}")
        registered_names = getattr(command_registry, "commands", {})
        if isinstance(registered_names, dict) or hasattr(registered_names, "keys"):
            configured_names = set(registered_names)
            builtin_conflicts = configured_names & _COORDINATOR_COMMANDS
            if builtin_conflicts:
                raise PluginError(
                    "Configured command collides with coordinator built-in command: "
                    + ", ".join(sorted(builtin_conflicts))
                )
            conflicts = set(runtime.commands) & (configured_names | _COORDINATOR_COMMANDS)
            if conflicts:
                raise PluginError(f"External commands collide with configured or built-in commands: {sorted(conflicts)}")

        self.router = cast(Router, self._sync_factory(runtime.select("router", router).factory))
        self.provider = cast(ProviderService, self._sync_factory(runtime.select("provider", provider).factory))
        self.interpreter = self._sync_factory(runtime.select("interpreter", interpreter).factory)
        self.executor = cast(Executor, self._sync_factory(runtime.select("executor", executor).factory))
        for wrapper_name in selected_wrappers:
            wrapper = runtime.executor_wrappers.get(wrapper_name)
            if wrapper is None:
                raise PluginError(f"No enabled executor wrapper named {wrapper_name}")
            self.executor = cast(Executor, self._sync_factory(wrapper.factory, self.executor))
        self.context_service = context
        self.observations = observations
        self.command_registry = command_registry
        self.external_commands = runtime.commands
        self.config_store = config_store
        self._plugin_runtime = runtime
        # Runtime factories are created at different application boundaries.
        # Keep restart and epoch snapshots separate from each request's desired
        # snapshot; none of these changes replaces selected long-lived services.
        self._restart_config = creation_config
        self._conversation._epoch_config = self._restart_config
        self._conversation._epoch_config_epoch = self._read_context_epoch()
        self._observer_registrations = runtime.observers
        output_observers = {}
        observer_config = self._restart_config
        for registration in self._observer_registrations:
            observer = self._sync_factory(
                registration.create, self._plugin_config(
                    registration.plugin_id, observer_config, observer_config, observer_config,
                    observer_config,
                ),
            )
            if not callable(getattr(observer, "observe", None)):
                raise TypeError(f"Observer {registration.qualified_name} must expose observe(event)")
            output_observers[registration.qualified_name] = observer
        self.output_observers = MappingProxyType(output_observers)
        self.provider_id = provider
        self.model = cast(str, model or getattr(self.provider, "model", provider))
        self._effort_override: str | None = None
        self.session_id = uuid4().hex
        self.config_revision = config_revision
        self.activity: Activity | None = None
        self.recovery: Recovery | None = None
        self.execution_outcome = "No execution yet."

        self.journal = NoPersistenceJournal() if journal is None else journal
        self._require_methods(
            self.journal, "start", "record_model_request", "record_provider_usage",
            "record_execution_source", "record_execution_result", "record_uncertain_execution",
            "record_context_collapse",
            "set_sensitive_values", "recent", "search", "read", "end", "close",
        )
        if type(getattr(self.journal, "persisted", None)) is not bool:
            raise TypeError("Selected journal must declare whether it persists records")
        self._capture_history_sensitive_config(creation_config)
        self._shutdown_timeout = shutdown_timeout
        self.max_agent_steps = max_agent_steps

        self._require_methods(self.router, "route")
        self._require_methods(self.provider, "generate")
        self._require_methods(self.interpreter, "interpret")
        self._require_methods(self.executor, "start", "execute", "interrupt", "close")
        if not isinstance(getattr(self.executor, "capabilities", None), ExecutorCapabilities):
            raise TypeError("Selected executor must declare ExecutorCapabilities")

    @staticmethod
    def _sync_factory(factory: Callable[..., _Service], *args: object) -> _Service:
        return SessionRuntime._sync_factory(factory, *args)

    @staticmethod
    def _require_methods(service: object, *names: str) -> None:
        missing = [name for name in names if not callable(getattr(service, name, None))]
        if missing:
            raise TypeError(f"Selected service is missing callable methods: {', '.join(missing)}")

    def _set_state_unless_stopping(self, state: State) -> None:
        return self._lifecycle.set_state_unless_stopping(state)

    def _commit_fallback_context(self, user_text: str, assistant_text: str,
                                 observation: str | None = None, *, include_user: bool = True) -> None:
        return self._conversation._commit_fallback_context(user_text, assistant_text, observation, include_user=include_user)

    def _operation_is_current(self, operation_id: str, state: State) -> bool:
        return self._lifecycle.operation_is_current(operation_id, state)

    def _current_config(self) -> ConfigSnapshot | None:
        return self._runtime.current_config()

    def _capture_history_sensitive_config(self, snapshot: ConfigSnapshot | None) -> None:
        return self._conversation.capture_history_sensitive_config(snapshot)

    def _history_patterns(self) -> tuple[str, ...]:
        return self._conversation._history_patterns()

    def _redact_history_text(self, text: str, *, preserve_offsets: bool = False) -> str:
        return self._conversation._redact_history_text(text, preserve_offsets=preserve_offsets)

    async def _read_history_page(self, event_id: str, offset: int, limit: int) -> dict[str, object]:
        return await self._conversation._read_history_page(event_id, offset, limit)

    def _read_context_epoch(self) -> int | None:
        return self._conversation.read_context_epoch()

    def _observe_context_epoch(
        self, epoch: int | None, *, config: ConfigSnapshot | None = None,
    ) -> None:
        return self._conversation.observe_context_epoch(epoch, config=config)

    def _plugin_config(
        self,
        plugin_id: str,
        request_snapshot: ConfigSnapshot | None,
        cell_snapshot: ConfigSnapshot | None,
        epoch_snapshot: ConfigSnapshot | None,
        immediate_snapshot: ConfigSnapshot | None,
    ) -> Mapping[str, Scalar]:
        return self._runtime._plugin_config(plugin_id, request_snapshot, cell_snapshot, epoch_snapshot, immediate_snapshot)

    async def _run_context_transforms(
        self,
        snapshot: ContextSnapshot,
        request_config: ConfigSnapshot | None,
        cell_config: ConfigSnapshot | None,
        epoch_config: ConfigSnapshot | None,
    ) -> ContextSnapshot:
        return await self._runtime.run_context_transforms(snapshot, request_config, cell_config, epoch_config)

    @property
    def _run_model_transforms(self) -> Callable[[ModelRequest, ConfigSnapshot | None, ConfigSnapshot | None, ConfigSnapshot | None], Awaitable[ModelRequest]]:
        return self._runtime.model_transform

    @_run_model_transforms.setter
    def _run_model_transforms(self, value: Callable[[ModelRequest, ConfigSnapshot | None, ConfigSnapshot | None, ConfigSnapshot | None], Awaitable[ModelRequest]]) -> None:
        self._runtime.model_transform = value

    @staticmethod
    def _validate_execution_result(request: ExecutionRequest, result: ExecutionResult) -> None:
        return SessionRuntime._validate_execution_result(request, result)

    def _route_query(self, code: str) -> RoutedAction:
        return self._frontend._route_query(code)

    @staticmethod
    def _query_prefix_offset(code: str, routed: RoutedAction) -> int:
        # The default direct-Python route removes exactly one leading @.
        # Other routers may assign @ differently, so do not invent an offset.
        return SessionRuntime._query_prefix_offset(code, routed)

    @staticmethod
    def _check_query_cursor(code: str, cursor_pos: int) -> None:
        return SessionRuntime._check_query_cursor(code, cursor_pos)

    async def complete(self, code: str, cursor_pos: int) -> CompletionResult:
        return await self._frontend.complete(code, cursor_pos)

    async def inspect(self, code: str, cursor_pos: int, detail_level: int = 0) -> InspectionResult:
        return await self._frontend.inspect(code, cursor_pos, detail_level)

    @staticmethod
    def _parse_completeness(source: str) -> CompletenessResult:
        return SessionRuntime._parse_completeness(source)

    async def is_complete(self, code: str) -> CompletenessResult:
        return await self._frontend.is_complete(code)

    @staticmethod
    def _say_text(content: object) -> str:
        return SessionRuntime._say_text(content)

    @staticmethod
    def _visible_say_outputs(result: ExecutionResult) -> tuple[SayOutput, ...]:
        return SessionRuntime.visible_say_outputs(result)

    @classmethod
    def _visible_says(cls, result: ExecutionResult) -> tuple[str, ...]:
        return SessionRuntime.visible_says(result)

    def _new_output_event(
        self,
        origin: Origin,
        kind: Literal["stream", "display", "execute_result", "update", "clear", "error", "progress"],
        data: dict[str, object],
        *,
        display_id: str | None = None,
        metadata: dict[str, object] | None = None,
        author: Literal['user', 'agent'] | None = None,
    ) -> OutputEvent:
        return self._frontend.new_output_event(origin, kind, data, display_id=display_id, metadata=metadata, author=author)

    async def _dispatch_output_event(
        self,
        event: OutputEvent,
        *,
        on_progress: ProgressCallback | None,
        operation_id: str,
        expected_state: State,
    ) -> None:
        return await self._frontend._dispatch_output_event(event, on_progress=on_progress, operation_id=operation_id, expected_state=expected_state)

    async def _emit_progress(
        self,
        origin: Origin,
        data: dict[str, object],
        *,
        on_progress: ProgressCallback | None,
        operation_id: str,
        expected_state: State,
        author: Literal["user", "agent"] = "agent",
    ) -> OutputEvent:
        return await self._frontend.emit_progress(origin, data, on_progress=on_progress, operation_id=operation_id, expected_state=expected_state, author=author)

    async def _publish_output(
        self,
        request: ExecutionRequest,
        result: ExecutionResult,
        *,
        on_progress: ProgressCallback | None,
        operation_id: str,
    ) -> tuple[OutputEvent, ...]:
        return await self._frontend.publish_output(request, result, on_progress=on_progress, operation_id=operation_id)

    def _model_options(self, snapshot: ConfigSnapshot | None) -> dict[str, str]:
        return self._runtime.model_options(snapshot)

    def _prepare_context(self, text: str, request_id: str) -> ContextSnapshot:
        return self._conversation.prepare_context(text, request_id)

    def _latest_context(self) -> ContextSnapshot:
        return self._conversation.latest_context()

    def _append_steering_context(self, text: str, request_id: str) -> None:
        return self._conversation.append_steering_context(text, request_id)

    def _packed_observation(self, request: ExecutionRequest, result: ExecutionResult) -> object:
        # Archive reads have their own bounded envelope. Never let unrelated
        # stdout (including an oversized stream) hide or re-archive an excerpt.
        return self._observations.packed_observation(request, result)

    def _packed_regular_observation(self, request: ExecutionRequest, result: ExecutionResult) -> object:
        return self._observations._packed_regular_observation(request, result)

    async def _archive_context_outputs(self) -> None:
        return await self._conversation.archive_context_outputs()

    def _abandon_context(self, request_id: str) -> None:
        return self._conversation.abandon_context(request_id)

    def _commit_context(self, request_id: str, user_text: str, assistant_text: str,
                        observation: object | None = None, phase: str | None = None, *, include_user: bool = True) -> None:
        return self._conversation.commit_context(request_id, user_text, assistant_text, observation, phase, include_user=include_user)

    async def _journal_record(self, method: str, *args: object, **kwargs: object) -> None:
        return await self._journal._journal_record(method, *args, **kwargs)

    @property
    def cache_summary(self) -> tuple[str, str, str]:
        return self._journal.cache_summary

    async def _record_provider_usage(
        self, request: ModelRequest, response: ModelResponse | None, *, outcome: str,
    ) -> None:
        return await self._journal.record_provider_usage(request, response, outcome=outcome)

    async def _record_execution_result(self, request: ExecutionRequest, result: ExecutionResult) -> None:
        return await self._lifecycle._record_execution_result(request, result)

    async def _record_uncertain_execution(self, request: ExecutionRequest, reason: str) -> None:
        return await self._lifecycle._record_uncertain_execution(request, reason)

    async def _execute_dispatched(self, request: ExecutionRequest) -> ExecutionResult:
        return await self._lifecycle.execute_dispatched(request)

    async def _close_executor(self) -> None:
        return await self._lifecycle._close_executor()

    async def _close_journal(self, state: str) -> None:
        return await self._lifecycle._close_journal(state)

    async def start(self) -> None:
        return await self._lifecycle.start()

    @staticmethod
    def _validate_routed_action(routed: object, origin: Origin) -> RoutedAction:
        return SessionRuntime.validate_routed_action(routed, origin)

    @property
    def pending_action_count(self) -> int:
        return self._lifecycle.pending_action_count

    @property
    def queue_active(self) -> bool:
        return self._lifecycle.queue_active

    async def enqueue(
        self,
        frontend_id: str,
        text: str,
        *,
        allow_stdin: bool = False,
        input_handler: InputHandler | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> QueueTicket:
        return await self._lifecycle.enqueue(frontend_id, text, allow_stdin=allow_stdin, input_handler=input_handler, on_progress=on_progress)

    def _start_queue_worker_if_idle(self) -> None:
        return self._lifecycle.start_queue_worker_if_idle()

    def _queue_worker_finished(self, worker: asyncio.Task[None]) -> None:
        return self._lifecycle._queue_worker_finished(worker)

    @staticmethod
    def _complete_queue_item(item: _QueuedAction, outcome: QueueOutcome) -> None:
        return SessionRuntime.complete_queue_item(item, outcome)

    async def _fail_pending_actions(self, status: Literal["steered", "completed", "failed", "interrupted", "closed"], error: str) -> None:
        return await self._lifecycle.fail_pending_actions(status, error)

    async def _drain_queue(self) -> None:
        return await self._lifecycle._drain_queue()

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
        return await self._runner.submit(
            frontend_id, text, allow_stdin=allow_stdin, input_handler=input_handler,
            on_progress=on_progress, _queued_action=_queued_action,
        )

    async def _dispatch_command(self, source: str, config: ConfigSnapshot | None) -> str:
        return await self._runtime.dispatch_command(source, config)

    def _context_export_payload(self) -> dict[str, object]:
        return self._conversation._context_export_payload()

    def _model_command(self, arguments: str) -> str:
        return self._runtime._model_command(arguments)

    def _configured_effort(self, config: ConfigSnapshot | None) -> str | None:
        return self._runtime._configured_effort(config)

    @property
    def _wait_provider_retry(self) -> Callable[[float, str], Awaitable[None]]:
        return self._runtime.retry_waiter

    @_wait_provider_retry.setter
    def _wait_provider_retry(self, value: Callable[[float, str], Awaitable[None]]) -> None:
        self._runtime.retry_waiter = value

    @property
    def effective_effort(self) -> str:
        return self._runtime.effective_effort

    def _effort_command(self, arguments: str, config: ConfigSnapshot | None) -> str:
        return self._runtime._effort_command(arguments, config)

    def _context_command(self, arguments: str) -> str:
        return self._conversation._context_command(arguments)

    async def _history_command(self, arguments: str) -> str:
        return await self._conversation._history_command(arguments)

    @staticmethod
    def _history_count(values: list[str], *, default: int) -> int | None:
        return SessionRuntime._history_count(values, default=default)

    @staticmethod
    def _history_integer(value: str, *, maximum: int) -> int | None:
        return SessionRuntime._history_integer(value, maximum=maximum)

    async def _history_recent(self, count: int) -> str:
        return await self._conversation._history_recent(count)

    async def _history_search(self, tokens: list[str]) -> str:
        return await self._conversation._history_search(tokens)

    async def _history_page(self, tokens: list[str]) -> str:
        return await self._conversation._history_page(tokens)

    async def interrupt(self) -> None:
        return await self._lifecycle.interrupt()

    async def close(self) -> None:
        return await self._lifecycle.close()

    async def _close_impl(self) -> None:
        return await self._lifecycle._close_impl()

    @property
    def _context(self) -> list[tuple[str, str]]:
        """Compatibility view; mutable state is owned by conversation."""
        return self._conversation._context

    @_context.setter
    def _context(self, value: list[tuple[str, str]]) -> None:
        self._conversation._context = value

    @property
    def _context_epoch(self) -> int:
        """Compatibility view; mutable state is owned by conversation."""
        return self._conversation._context_epoch

    @_context_epoch.setter
    def _context_epoch(self, value: int) -> None:
        self._conversation._context_epoch = value

    @property
    def _context_overlay(self) -> list[tuple[int, str]]:
        """Compatibility view; mutable state is owned by conversation."""
        return self._conversation._context_overlay

    @_context_overlay.setter
    def _context_overlay(self, value: list[tuple[int, str]]) -> None:
        self._conversation._context_overlay = value

    @property
    def _context_overlay_epoch(self) -> int | None:
        """Compatibility view; mutable state is owned by conversation."""
        return self._conversation._context_overlay_epoch

    @_context_overlay_epoch.setter
    def _context_overlay_epoch(self, value: int | None) -> None:
        self._conversation._context_overlay_epoch = value

    @property
    def _history_sensitive_values(self) -> set[str]:
        """Compatibility view; mutable state is owned by conversation."""
        return self._conversation._history_sensitive_values

    @_history_sensitive_values.setter
    def _history_sensitive_values(self, value: set[str]) -> None:
        self._conversation._history_sensitive_values = value

    @property
    def _history_sensitive_chars(self) -> int:
        """Compatibility view; mutable state is owned by conversation."""
        return self._conversation._history_sensitive_chars

    @_history_sensitive_chars.setter
    def _history_sensitive_chars(self, value: int) -> None:
        self._conversation._history_sensitive_chars = value

    @property
    def _history_content_hidden(self) -> bool:
        """Compatibility view; mutable state is owned by conversation."""
        return self._conversation._history_content_hidden

    @_history_content_hidden.setter
    def _history_content_hidden(self, value: bool) -> None:
        self._conversation._history_content_hidden = value

    @property
    def _journal_sensitive_config_ready(self) -> bool:
        """Compatibility view; mutable state is owned by conversation."""
        return self._conversation.journal_sensitive_config_ready

    @_journal_sensitive_config_ready.setter
    def _journal_sensitive_config_ready(self, value: bool) -> None:
        self._conversation.journal_sensitive_config_ready = value

    @property
    def _epoch_config(self) -> ConfigSnapshot | None:
        """Compatibility view; mutable state is owned by conversation."""
        return self._conversation._epoch_config

    @_epoch_config.setter
    def _epoch_config(self, value: ConfigSnapshot | None) -> None:
        self._conversation._epoch_config = value

    @property
    def _epoch_config_epoch(self) -> int | None:
        """Compatibility view; mutable state is owned by conversation."""
        return self._conversation._epoch_config_epoch

    @_epoch_config_epoch.setter
    def _epoch_config_epoch(self, value: int | None) -> None:
        self._conversation._epoch_config_epoch = value

    @property
    def _event_sequence(self) -> int:
        """Compatibility view; mutable state is owned by frontend."""
        return self._frontend._event_sequence

    @_event_sequence.setter
    def _event_sequence(self, value: int) -> None:
        self._frontend._event_sequence = value

    @property
    def best_effort_observer_failures(self) -> int:
        """Compatibility view; mutable state is owned by frontend."""
        return self._frontend.best_effort_observer_failures

    @best_effort_observer_failures.setter
    def best_effort_observer_failures(self, value: int) -> None:
        self._frontend.best_effort_observer_failures = value

    @property
    def state(self) -> State:
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle.state

    @state.setter
    def state(self, value: State) -> None:
        self._lifecycle.state = value

    @property
    def _lock(self) -> asyncio.Lock:
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._lock

    @_lock.setter
    def _lock(self, value: asyncio.Lock) -> None:
        self._lifecycle._lock = value

    @property
    def _queue_lock(self) -> asyncio.Lock:
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._queue_lock

    @_queue_lock.setter
    def _queue_lock(self, value: asyncio.Lock) -> None:
        self._lifecycle._queue_lock = value

    @property
    def _pending_actions(self) -> deque[_QueuedAction]:
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._pending_actions

    @_pending_actions.setter
    def _pending_actions(self, value: deque[_QueuedAction]) -> None:
        self._lifecycle._pending_actions = value

    @property
    def _queue_worker(self) -> asyncio.Task[None] | None:
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._queue_worker

    @_queue_worker.setter
    def _queue_worker(self, value: asyncio.Task[None] | None) -> None:
        self._lifecycle._queue_worker = value

    @property
    def _lifecycle_lock(self) -> asyncio.Lock:
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._lifecycle_lock

    @_lifecycle_lock.setter
    def _lifecycle_lock(self, value: asyncio.Lock) -> None:
        self._lifecycle._lifecycle_lock = value

    @property
    def _close_task(self) -> asyncio.Task[None] | None:
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._close_task

    @_close_task.setter
    def _close_task(self, value: asyncio.Task[None] | None) -> None:
        self._lifecycle._close_task = value

    @property
    def _executor_close_attempted(self) -> bool:
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._executor_close_attempted

    @_executor_close_attempted.setter
    def _executor_close_attempted(self, value: bool) -> None:
        self._lifecycle._executor_close_attempted = value

    @property
    def _active_task(self) -> asyncio.Task[Any] | None:
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle.operation.task

    @_active_task.setter
    def _active_task(self, value: asyncio.Task[Any] | None) -> None:
        self._lifecycle.operation.task = value

    @property
    def _active_operation_id(self) -> str | None:
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle.operation.identity

    @_active_operation_id.setter
    def _active_operation_id(self, value: str | None) -> None:
        self._lifecycle.operation.identity = value

    @property
    def _active_model_request(self) -> ModelRequest | None:
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle.operation.model_request

    @_active_model_request.setter
    def _active_model_request(self, value: ModelRequest | None) -> None:
        self._lifecycle.operation.model_request = value

    @property
    def _active_provider_usage_recorded(self) -> bool:
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle.operation.provider_usage_recorded

    @_active_provider_usage_recorded.setter
    def _active_provider_usage_recorded(self, value: bool) -> None:
        self._lifecycle.operation.provider_usage_recorded = value

    @property
    def _active_execution_request(self) -> ExecutionRequest | None:
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle.operation.execution_request

    @_active_execution_request.setter
    def _active_execution_request(self, value: ExecutionRequest | None) -> None:
        self._lifecycle.operation.execution_request = value

    @property
    def _active_execution_result_recorded(self) -> bool:
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle.operation.execution_result_recorded

    @_active_execution_result_recorded.setter
    def _active_execution_result_recorded(self, value: bool) -> None:
        self._lifecycle.operation.execution_result_recorded = value

    @property
    def _execution_active(self) -> bool:
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle.operation.execution_active

    @_execution_active.setter
    def _execution_active(self, value: bool) -> None:
        self._lifecycle.operation.execution_active = value

    @property
    def _execution_outcome_status(self) -> str | None:
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle.operation.execution_outcome_status

    @_execution_outcome_status.setter
    def _execution_outcome_status(self, value: str | None) -> None:
        self._lifecycle.operation.execution_outcome_status = value

    @property
    def _generation(self) -> asyncio.Task[ModelResponse] | None:
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._generation

    @_generation.setter
    def _generation(self, value: asyncio.Task[ModelResponse] | None) -> None:
        self._lifecycle._generation = value

    @property
    def _journal_started(self) -> bool:
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._journal_started

    @_journal_started.setter
    def _journal_started(self, value: bool) -> None:
        self._lifecycle._journal_started = value

    @property
    def _journal_closed(self) -> bool:
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._journal_closed

    @_journal_closed.setter
    def _journal_closed(self, value: bool) -> None:
        self._lifecycle._journal_closed = value

    @property
    def _journal_failed(self) -> bool:
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._journal_failed

    @_journal_failed.setter
    def _journal_failed(self, value: bool) -> None:
        self._lifecycle._journal_failed = value

    @property
    def _cache_totals(self) -> dict[str, int]:
        """Compatibility view of journal accounting state."""
        return self._journal._cache_totals

    @_cache_totals.setter
    def _cache_totals(self, value: dict[str, int]) -> None:
        self._journal._cache_totals = value

    @property
    def _cache_complete(self) -> dict[str, bool]:
        """Compatibility view of journal accounting state."""
        return self._journal._cache_complete

    @_cache_complete.setter
    def _cache_complete(self, value: dict[str, bool]) -> None:
        self._journal._cache_complete = value

    @property
    def _cache_reports(self) -> int:
        """Compatibility view of journal accounting state."""
        return self._journal._cache_reports

    @_cache_reports.setter
    def _cache_reports(self, value: int) -> None:
        self._journal._cache_reports = value

    @property
    def _effort_override(self) -> str | None:
        return self._runtime._effort_override

    @_effort_override.setter
    def _effort_override(self, value: str | None) -> None:
        self._runtime._effort_override = value

    @property
    def _observer_registrations(self) -> tuple[RegisteredObserver, ...]:
        return self._runtime._observer_registrations

    @_observer_registrations.setter
    def _observer_registrations(self, value: tuple[RegisteredObserver, ...]) -> None:
        self._runtime._observer_registrations = value

    @property
    def _plugin_runtime(self) -> PluginRuntime:
        return self._runtime._plugin_runtime

    @_plugin_runtime.setter
    def _plugin_runtime(self, value: PluginRuntime) -> None:
        self._runtime._plugin_runtime = value

    @property
    def _restart_config(self) -> ConfigSnapshot | None:
        return self._runtime._restart_config

    @_restart_config.setter
    def _restart_config(self, value: ConfigSnapshot | None) -> None:
        self._runtime._restart_config = value

    @property
    def _shutdown_timeout(self) -> float:
        return self._runtime._shutdown_timeout

    @_shutdown_timeout.setter
    def _shutdown_timeout(self, value: float) -> None:
        self._runtime._shutdown_timeout = value

    @property
    def activity(self) -> Activity | None:
        return self._runtime.activity

    @activity.setter
    def activity(self, value: Activity | None) -> None:
        self._runtime.activity = value

    @property
    def command_registry(self) -> object:
        return self._runtime.command_registry

    @command_registry.setter
    def command_registry(self, value: object) -> None:
        self._runtime.command_registry = value

    @property
    def config_revision(self) -> int:
        return self._runtime.config_revision

    @config_revision.setter
    def config_revision(self, value: int) -> None:
        self._runtime.config_revision = value

    @property
    def config_store(self) -> ConfigStore | None:
        return self._runtime.config_store

    @config_store.setter
    def config_store(self, value: ConfigStore | None) -> None:
        self._runtime.config_store = value

    @property
    def context_service(self) -> object:
        return self._runtime.context_service

    @context_service.setter
    def context_service(self, value: object) -> None:
        self._runtime.context_service = value

    @property
    def execution_outcome(self) -> str:
        return self._runtime.execution_outcome

    @execution_outcome.setter
    def execution_outcome(self, value: str) -> None:
        self._runtime.execution_outcome = value

    @property
    def executor(self) -> Executor:
        return self._runtime.executor

    @executor.setter
    def executor(self, value: Executor) -> None:
        self._runtime.executor = value

    @property
    def external_commands(self) -> Mapping[str, RegisteredCommand]:
        return self._runtime.external_commands

    @external_commands.setter
    def external_commands(self, value: Mapping[str, RegisteredCommand]) -> None:
        self._runtime.external_commands = value

    @property
    def interpreter(self) -> object:
        return self._runtime.interpreter

    @interpreter.setter
    def interpreter(self, value: object) -> None:
        self._runtime.interpreter = value

    @property
    def journal(self) -> JournalService:
        return self._runtime.journal

    @journal.setter
    def journal(self, value: JournalService) -> None:
        self._runtime.journal = value

    @property
    def max_agent_steps(self) -> int:
        return self._runtime.max_agent_steps

    @max_agent_steps.setter
    def max_agent_steps(self, value: int) -> None:
        self._runtime.max_agent_steps = value

    @property
    def model(self) -> str:
        return self._runtime.model

    @model.setter
    def model(self, value: str) -> None:
        self._runtime.model = value

    @property
    def observations(self) -> object:
        return self._runtime.observations

    @observations.setter
    def observations(self, value: object) -> None:
        self._runtime.observations = value

    @property
    def output_observers(self) -> MappingProxyType[str, object]:
        return self._runtime.output_observers

    @output_observers.setter
    def output_observers(self, value: MappingProxyType[str, object]) -> None:
        self._runtime.output_observers = value

    @property
    def provider(self) -> ProviderService:
        return self._runtime.provider

    @provider.setter
    def provider(self, value: ProviderService) -> None:
        self._runtime.provider = value

    @property
    def provider_id(self) -> str:
        return self._runtime.provider_id

    @provider_id.setter
    def provider_id(self, value: str) -> None:
        self._runtime.provider_id = value

    @property
    def recovery(self) -> Recovery | None:
        return self._runtime.recovery

    @recovery.setter
    def recovery(self, value: Recovery | None) -> None:
        self._runtime.recovery = value

    @property
    def router(self) -> Router:
        return self._runtime.router

    @router.setter
    def router(self, value: Router) -> None:
        self._runtime.router = value

    @property
    def session_id(self) -> str:
        return self._runtime.session_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        self._runtime.session_id = value
