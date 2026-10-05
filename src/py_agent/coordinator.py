"""Serialized orchestration over explicit coordinator state owners."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import inspect
from itertools import count
import json
import math
import shlex
from types import MappingProxyType
from uuid import uuid4

from .collapse_control import parse_collapse
from .configuration import ApplyAt, ConfigSnapshot, ConfigStore
from .contracts import (
    MAX_QUEUED_ACTION_CHARS,
    AgentDecision,
    CompletenessResult,
    CompletionResult,
    ContextSnapshot,
    ExecutionOutput,
    ExecutionRequest,
    ExecutionResult,
    ExecutorCapabilities,
    InputHandler,
    InputReply,
    InputRequest,
    InputUnavailableError,
    InspectionResult,
    ModelRequest,
    ModelResponse,
    Origin,
    OutputEvent,
    ProgressCallback,
    QueueOutcome,
    QueueTicket,
    RoutedAction,
    SayOutput,
    Submission,
    UserAction,
)
from .coordinator_conversation import ConversationState
from .coordinator_frontend import FrontendRouting
from .coordinator_journal import JournalPolicy
from .coordinator_lifecycle import ExecutionLifecycle
from .coordinator_observations import ModelObservations
from .coordinator_support import (
    _COORDINATOR_COMMANDS,
    _EFFORT_PRESETS,
    _EFFORT_USAGE,
    _MODEL_USAGE,
    HISTORY_MAX_ITEMS,
    MAX_AGENT_RESPONSE_CHARS,
    State,
    _QueuedAction,
)
from .coordinator_support import (
    MAX_PENDING_ACTIONS as MAX_PENDING_ACTIONS,
)
from .plugins import PluginError, PluginRuntime
from .session_journal import JournalService, NoPersistenceJournal


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
    ):
        self._conversation = ConversationState(self)
        self._frontend = FrontendRouting(self)
        self._observations = ModelObservations(self)
        self._journal = JournalPolicy(self)
        self._lifecycle = ExecutionLifecycle(self)
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

        self.router = self._sync_factory(runtime.select("router", router).factory)
        self.provider = self._sync_factory(runtime.select("provider", provider).factory)
        self.interpreter = self._sync_factory(runtime.select("interpreter", interpreter).factory)
        self.executor = self._sync_factory(runtime.select("executor", executor).factory)
        for wrapper_name in selected_wrappers:
            wrapper = runtime.executor_wrappers.get(wrapper_name)
            if wrapper is None:
                raise PluginError(f"No enabled executor wrapper named {wrapper_name}")
            self.executor = self._sync_factory(wrapper.factory, self.executor)
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
        self.model = model or getattr(self.provider, "model", provider)
        self._effort_override: str | None = None
        self.session_id = uuid4().hex
        self.config_revision = config_revision
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
    def _sync_factory(factory, *args):
        value = factory(*args)
        if inspect.isawaitable(value):
            close = getattr(value, "close", None)
            if callable(close):
                close()
            raise TypeError("Plugin factories must be synchronous")
        return value

    @staticmethod
    def _require_methods(service, *names: str) -> None:
        missing = [name for name in names if not callable(getattr(service, name, None))]
        if missing:
            raise TypeError(f"Selected service is missing callable methods: {', '.join(missing)}")

    def _set_state_unless_stopping(self, state: State) -> None:
        return self._lifecycle._set_state_unless_stopping(state)

    def _commit_fallback_context(self, user_text: str, assistant_text: str,
                                 observation: str | None = None, *, include_user: bool = True) -> None:
        return self._conversation._commit_fallback_context(user_text, assistant_text, observation, include_user=include_user)

    def _operation_is_current(self, operation_id: str, state: State) -> bool:
        return self._lifecycle._operation_is_current(operation_id, state)

    def _current_config(self) -> ConfigSnapshot | None:
        if self.config_store is None:
            return None
        snapshot = self.config_store.snapshot
        if not isinstance(snapshot, ConfigSnapshot):
            raise TypeError("Configuration store returned an invalid snapshot")
        return snapshot

    def _capture_history_sensitive_config(self, snapshot: ConfigSnapshot | None) -> None:
        return self._conversation._capture_history_sensitive_config(snapshot)

    def _history_patterns(self) -> tuple[str, ...]:
        return self._conversation._history_patterns()

    def _redact_history_text(self, text: str, *, preserve_offsets: bool = False) -> str:
        return self._conversation._redact_history_text(text, preserve_offsets=preserve_offsets)

    def _read_history_page(self, event_id: str, offset: int, limit: int) -> dict[str, object]:
        return self._conversation._read_history_page(event_id, offset, limit)

    def _read_context_epoch(self) -> int | None:
        return self._conversation._read_context_epoch()

    def _observe_context_epoch(
        self, epoch: int | None, *, config: ConfigSnapshot | None = None,
    ) -> None:
        return self._conversation._observe_context_epoch(epoch, config=config)

    def _plugin_config(
        self,
        plugin_id: str,
        request_snapshot: ConfigSnapshot | None,
        cell_snapshot: ConfigSnapshot | None,
        epoch_snapshot: ConfigSnapshot | None,
        immediate_snapshot: ConfigSnapshot | None,
    ) -> MappingProxyType:
        """Resolve one factory namespace using each field's declared boundary.

        ``request_snapshot`` is fixed for a submission. ``cell_snapshot`` is
        captured once per command/model cell, ``epoch_snapshot`` advances only
        when the context epoch does, and immediate settings are sampled just
        before each factory call. Observer factories pass the creation snapshot
        for every boundary because observer instances are long-lived.
        """
        snapshots = {
            ApplyAt.IMMEDIATE: immediate_snapshot,
            ApplyAt.REQUEST: request_snapshot,
            ApplyAt.CELL: cell_snapshot,
            ApplyAt.EPOCH: epoch_snapshot,
            ApplyAt.RESTART: self._restart_config,
        }
        values = {}
        for name, field in self._plugin_runtime.config.fields.items():
            if field.owner != plugin_id:
                continue
            snapshot = snapshots[field.apply_at]
            entry = snapshot.entries.get(name) if snapshot is not None else None
            values[name.removeprefix(plugin_id + ".")] = field.default if entry is None else entry.value
        return MappingProxyType(values)

    async def _run_context_transforms(
        self,
        snapshot: ContextSnapshot,
        request_config: ConfigSnapshot | None,
        cell_config: ConfigSnapshot | None,
        epoch_config: ConfigSnapshot | None,
    ) -> ContextSnapshot:
        current = snapshot
        trace = list(snapshot.transform_trace)
        for registration in self._plugin_runtime.transforms["context"]:
            immediate_config = self._current_config()
            stage = self._sync_factory(
                registration.create, self._plugin_config(
                    registration.plugin_id, request_config, cell_config, epoch_config, immediate_config,
                ),
            )
            transform = getattr(stage, "transform", None)
            if not callable(transform):
                raise TypeError(f"Context transform {registration.qualified_name} must expose transform(snapshot)")
            result = transform(current)
            if not inspect.isawaitable(result):
                raise TypeError(f"Context transform {registration.qualified_name} must be async")
            candidate = await result
            if not isinstance(candidate, ContextSnapshot):
                raise TypeError(f"Context transform {registration.qualified_name} must return ContextSnapshot")
            if candidate.epoch != current.epoch:
                raise ValueError(f"Context transform {registration.qualified_name} cannot change the context epoch")
            trace.append(registration.qualified_name)
            current = replace(candidate, transform_trace=tuple(trace))
        return current

    async def _run_model_transforms(
        self,
        request: ModelRequest,
        request_config: ConfigSnapshot | None,
        cell_config: ConfigSnapshot | None,
        epoch_config: ConfigSnapshot | None,
    ) -> ModelRequest:
        current = request
        trace = list(request.transform_trace)
        for registration in self._plugin_runtime.transforms["model-request"]:
            immediate_config = self._current_config()
            stage = self._sync_factory(
                registration.create, self._plugin_config(
                    registration.plugin_id, request_config, cell_config, epoch_config, immediate_config,
                ),
            )
            transform = getattr(stage, "transform", None)
            if not callable(transform):
                raise TypeError(f"Model transform {registration.qualified_name} must expose transform(request)")
            result = transform(current)
            if not inspect.isawaitable(result):
                raise TypeError(f"Model transform {registration.qualified_name} must be async")
            candidate = await result
            if not isinstance(candidate, ModelRequest):
                raise TypeError(f"Model transform {registration.qualified_name} must return ModelRequest")
            if candidate.origin != current.origin:
                raise ValueError(f"Model transform {registration.qualified_name} cannot change request origin")
            if candidate.context.epoch != current.context.epoch:
                raise ValueError(f"Model transform {registration.qualified_name} cannot change the context epoch")
            context = replace(candidate.context, transform_trace=current.context.transform_trace)
            trace.append(registration.qualified_name)
            current = replace(candidate, context=context, transform_trace=tuple(trace))
        return current

    @staticmethod
    def _validate_execution_result(request: ExecutionRequest, result: ExecutionResult) -> None:
        if not isinstance(result, ExecutionResult):
            raise TypeError("Executor must return an ExecutionResult")
        if result.origin != request.origin:
            raise RuntimeError("Executor returned a result for a different origin")
        if result.status not in ("success", "error", "cancelled", "uncertain"):
            raise RuntimeError(f"Executor returned unknown status: {result.status!r}")
        if not isinstance(result.stdout, str) or not isinstance(result.stderr, str):
            raise TypeError("Executor output streams must be text")
        if result.error is not None and not isinstance(result.error, str):
            raise TypeError("Executor error must be text or None")
        if type(result.final) is not bool:
            raise TypeError("Execution final marker must be a boolean")

    def _route_query(self, code: str) -> RoutedAction:
        return self._frontend._route_query(code)

    @staticmethod
    def _query_prefix_offset(code: str, routed: RoutedAction) -> int:
        # The default direct-Python route removes exactly one leading @.
        # Other routers may assign @ differently, so do not invent an offset.
        return 1 if code.startswith("@") and routed.kind == "execute" and routed.source == code[1:] else 0

    @staticmethod
    def _check_query_cursor(code: str, cursor_pos: int) -> None:
        if not isinstance(code, str):
            raise TypeError("Query source must be text")
        if type(cursor_pos) is not int or not 0 <= cursor_pos <= len(code):
            raise ValueError("Query cursor position is outside the source")

    async def complete(self, code: str, cursor_pos: int) -> CompletionResult:
        return await self._frontend.complete(code, cursor_pos)

    async def inspect(self, code: str, cursor_pos: int, detail_level: int = 0) -> InspectionResult:
        return await self._frontend.inspect(code, cursor_pos, detail_level)

    @staticmethod
    def _parse_completeness(source: str) -> CompletenessResult:
        try:
            from IPython.core.inputtransformer2 import TransformerManager

            status, indent = TransformerManager().check_complete(source)
            spaces = " " * min(indent, 80) if type(indent) is int and indent > 0 else ""
            return CompletenessResult(status, spaces)
        except ImportError:
            try:
                compiled = compile(source, "<jupyter-is-complete>", "exec")
                del compiled
            except (SyntaxError, OverflowError, ValueError):
                try:
                    import codeop
                    compiled = codeop.compile_command(source, symbol="exec")
                except (SyntaxError, OverflowError, ValueError):
                    return CompletenessResult("invalid")
                return CompletenessResult("incomplete" if compiled is None else "invalid")
            return CompletenessResult("complete")
        except (SyntaxError, OverflowError, ValueError):
            return CompletenessResult("invalid")

    async def is_complete(self, code: str) -> CompletenessResult:
        return await self._frontend.is_complete(code)

    @staticmethod
    def _say_text(content: object) -> str:
        if isinstance(content, str):
            return content
        return json.dumps(content, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    @staticmethod
    def _visible_say_outputs(result: ExecutionResult) -> tuple[SayOutput, ...]:
        return tuple(
            output for output in result.say_outputs
            if not output.final or result.status == "success"
        )

    @classmethod
    def _visible_says(cls, result: ExecutionResult) -> tuple[str, ...]:
        return tuple(cls._say_text(output.content) for output in cls._visible_say_outputs(result))

    def _new_output_event(
        self,
        origin: Origin,
        kind: str,
        data: dict[str, object],
        *,
        display_id: str | None = None,
        metadata: dict[str, object] | None = None,
        author: str | None = None,
    ) -> OutputEvent:
        return self._frontend._new_output_event(origin, kind, data, display_id=display_id, metadata=metadata, author=author)

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
        author: str = "agent",
    ) -> OutputEvent:
        return await self._frontend._emit_progress(origin, data, on_progress=on_progress, operation_id=operation_id, expected_state=expected_state, author=author)

    async def _publish_output(
        self,
        request: ExecutionRequest,
        result: ExecutionResult,
        *,
        on_progress: ProgressCallback | None,
        operation_id: str,
    ) -> tuple[OutputEvent, ...]:
        return await self._frontend._publish_output(request, result, on_progress=on_progress, operation_id=operation_id)

    def _model_options(self, snapshot: ConfigSnapshot | None) -> dict[str, str]:
        options: dict[str, str] = {}
        if snapshot is not None:
            max_tokens = snapshot.entries.get("model.max_tokens")
            if (self.provider_id == "litelm" and max_tokens is not None
                    and type(max_tokens.value) is int and max_tokens.value > 0):
                options["max_tokens"] = str(max_tokens.value)
        if self._effort_override is not None and self.provider_id in {"litelm", "codex"}:
            options["effort"] = self._effort_override
        elif snapshot is not None and self.provider_id == "codex":
            effort = snapshot.entries.get("model.effort")
            if effort is not None and isinstance(effort.value, str):
                options["effort"] = effort.value
        return options

    def _prepare_context(self, text: str, request_id: str) -> ContextSnapshot:
        return self._conversation._prepare_context(text, request_id)

    def _latest_context(self) -> ContextSnapshot:
        return self._conversation._latest_context()

    def _append_steering_context(self, text: str, request_id: str) -> None:
        return self._conversation._append_steering_context(text, request_id)

    def _packed_observation(self, request: ExecutionRequest, result: ExecutionResult):
        # Archive reads have their own bounded envelope. Never let unrelated
        # stdout (including an oversized stream) hide or re-archive an excerpt.
        return self._observations._packed_observation(request, result)

    def _packed_regular_observation(self, request: ExecutionRequest, result: ExecutionResult):
        return self._observations._packed_regular_observation(request, result)

    async def _archive_context_outputs(self) -> None:
        return await self._conversation._archive_context_outputs()

    def _abandon_context(self, request_id: str) -> None:
        return self._conversation._abandon_context(request_id)

    def _commit_context(self, request_id: str, user_text: str, assistant_text: str,
                        observation=None, phase: str | None = None, *, include_user: bool = True) -> None:
        return self._conversation._commit_context(request_id, user_text, assistant_text, observation, phase, include_user=include_user)

    def _journal_record(self, method: str, *args, **kwargs) -> None:
        return self._journal._journal_record(method, *args, **kwargs)

    @property
    def cache_summary(self) -> tuple[str, str, str]:
        return self._journal.cache_summary

    def _record_provider_usage(
        self, request: ModelRequest, response: ModelResponse | None, *, outcome: str,
    ) -> None:
        return self._journal._record_provider_usage(request, response, outcome=outcome)

    def _record_execution_result(self, request: ExecutionRequest, result: ExecutionResult) -> None:
        return self._lifecycle._record_execution_result(request, result)

    def _record_uncertain_execution(self, request: ExecutionRequest, reason: str) -> None:
        return self._lifecycle._record_uncertain_execution(request, reason)

    async def _execute_dispatched(self, request: ExecutionRequest) -> ExecutionResult:
        return await self._lifecycle._execute_dispatched(request)

    async def _close_executor(self) -> None:
        return await self._lifecycle._close_executor()

    def _close_journal(self, state: str) -> None:
        return self._lifecycle._close_journal(state)

    async def start(self) -> None:
        return await self._lifecycle.start()

    @staticmethod
    def _validate_routed_action(routed: object, origin: Origin) -> RoutedAction:
        if not isinstance(routed, RoutedAction):
            raise TypeError("Router must return a RoutedAction")
        if routed.origin != origin:
            raise ValueError("Router changed request origin")
        if routed.kind not in ("ask", "execute", "command"):
            raise ValueError(f"Router returned unknown action kind: {routed.kind!r}")
        if not isinstance(routed.source, str) or not routed.source.strip():
            raise ValueError("Router returned an empty or invalid source")
        if len(routed.source) > MAX_QUEUED_ACTION_CHARS:
            raise ValueError("Routed action exceeds the queued-action size limit")
        if routed.language is not None and not isinstance(routed.language, str):
            raise TypeError("Routed action language must be text or None")
        return routed

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
        return self._lifecycle._start_queue_worker_if_idle()

    def _queue_worker_finished(self, worker: asyncio.Task) -> None:
        return self._lifecycle._queue_worker_finished(worker)

    @staticmethod
    def _complete_queue_item(item: _QueuedAction, outcome: QueueOutcome) -> None:
        if not item.completion.done():
            item.completion.set_result(outcome)

    async def _fail_pending_actions(self, status: str, error: str) -> None:
        return await self._lifecycle._fail_pending_actions(status, error)

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
        if ((self._lifecycle.state is not State.IDLE or self._lifecycle._lock.locked() or self._lifecycle._queue_worker is not None
             or self._lifecycle._pending_actions) and _queued_action is None):
            raise RuntimeError("Session busy, queued work pending, or unavailable")
        async with self._lifecycle._lock:
            if (self._lifecycle.state is not State.IDLE
                    or ((_queued_action is None) and (self._lifecycle._queue_worker is not None or self._lifecycle._pending_actions))):
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
            self._lifecycle._active_task = task
            self._lifecycle._active_operation_id = operation_id
            config = _queued_action.config if _queued_action is not None else self._current_config()
            self._capture_history_sensitive_config(config)
            self._observe_context_epoch(self._read_context_epoch(), config=config)
            revision = config.revision if config is not None else self.config_revision
            self.config_revision = revision
            origin = (
                _queued_action.action.origin if _queued_action is not None
                else Origin(self.session_id, uuid4().hex, frontend_id, revision)
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
                        self._lifecycle._active_execution_request is None
                        or self._lifecycle._active_execution_request.origin != execution_origin
                        or self._lifecycle.state is not State.EXECUTING
                    ):
                        raise InputUnavailableError("Interactive input execution is no longer active")
                    self._lifecycle.state = State.WAITING_FOR_INPUT
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
                        if self._lifecycle.state is State.WAITING_FOR_INPUT:
                            self._lifecycle.state = State.EXECUTING

                return dispatch_input

            def execution_output_handler(
                execution_origin: Origin, author: str,
                *, progress: ProgressCallback | None = on_progress,
            ):
                if progress is None:
                    return None

                async def deliver(output: ExecutionOutput) -> None:
                    if (output.kind != "stream"
                            or not self._operation_is_current(operation_id, State.EXECUTING)):
                        raise asyncio.CancelledError
                    event = self._new_output_event(
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
                    if not self._operation_is_current(operation_id, State.EXECUTING):
                        raise asyncio.CancelledError

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
                )
                previous_state = self._lifecycle.state
                self._lifecycle.state = State.EXECUTING
                events: list[OutputEvent] = []
                dispatched = False
                try:
                    events.append(await self._emit_progress(
                        execution_origin,
                        {"phase": "execution_start", "author": "user",
                         "text": "Executing queued user cell."},
                        on_progress=item.on_progress, operation_id=operation_id,
                        expected_state=State.EXECUTING, author="user",
                    ))
                    dispatched = True
                    result = await self._execute_dispatched(execution_request)
                    if not self._operation_is_current(operation_id, State.EXECUTING):
                        raise asyncio.CancelledError
                    events.extend(await self._publish_output(
                        execution_request, result, on_progress=item.on_progress,
                        operation_id=operation_id,
                    ))
                    events.append(await self._emit_progress(
                        execution_origin,
                        {"phase": "cell_complete", "step": 1, "status": result.status,
                         "text": f"Queued user cell completed with status {result.status}."},
                        on_progress=item.on_progress, operation_id=operation_id,
                        expected_state=State.EXECUTING, author="user",
                    ))
                    if not self._operation_is_current(operation_id, State.EXECUTING):
                        raise asyncio.CancelledError
                    submission = Submission(
                        action, result=result, execution=execution_request,
                        message="\n".join(self._visible_says(result)),
                        say_outputs=self._visible_say_outputs(result),
                        events=tuple(events),
                    )
                    self._complete_queue_item(
                        item, QueueOutcome(queued_origin, "completed", submission=submission),
                    )
                    if result.status in ("uncertain", "cancelled"):
                        self._set_state_unless_stopping(State.FAILED)
                        raise RuntimeError("Queued cell result is uncertain; agent turn stopped without replay")
                except asyncio.CancelledError:
                    if dispatched:
                        self._set_state_unless_stopping(State.FAILED)
                    self._complete_queue_item(item, QueueOutcome(
                        queued_origin, "interrupted",
                        error="Queued cell interrupted; side effects may have occurred",
                    ))
                    raise
                except Exception as exc:
                    if dispatched:
                        self._set_state_unless_stopping(State.FAILED)
                    self._complete_queue_item(item, QueueOutcome(
                        queued_origin, "failed",
                        error=f"Queued cell failed; side effects may have occurred: {exc}",
                    ))
                    raise
                finally:
                    if self._lifecycle.state is State.EXECUTING:
                        self._lifecycle.state = previous_state

            async def run_command(action: RoutedAction, command_config: ConfigSnapshot | None) -> Submission:
                """Dispatch under the current operation without releasing its ownership."""
                self._lifecycle.state = State.COMMAND
                self._capture_history_sensitive_config(command_config)
                message = await self._dispatch_command(action.source, command_config)
                if not self._operation_is_current(operation_id, State.COMMAND):
                    raise asyncio.CancelledError
                if self.config_store is not None:
                    self.config_revision = self.config_store.snapshot.revision
                    self._capture_history_sensitive_config(self.config_store.snapshot)
                if (not self._conversation._journal_sensitive_config_ready
                        and getattr(self.journal, "persisted", None) is True):
                    message += (
                        "\nWarning: sensitive configuration exceeds journal redaction limits; "
                        "further persisted operations will stop safely."
                    )
                return Submission(action, message=message)

            async def run_queued_command(item: _QueuedAction) -> None:
                previous_state = self._lifecycle.state
                try:
                    submission = await run_command(item.action, item.config)
                except asyncio.CancelledError:
                    # Commands may have side effects. Leave COMMAND set so the
                    # outer submission fails closed; never replay this item.
                    self._complete_queue_item(item, QueueOutcome(
                        item.action.origin, "interrupted",
                        error="Queued command interrupted; side effects may have occurred",
                    ))
                    raise
                except Exception as exc:
                    self._complete_queue_item(item, QueueOutcome(
                        item.action.origin, "failed",
                        error=f"Queued command failed; side effects may have occurred: {exc}",
                    ))
                    raise
                else:
                    self._lifecycle.state = previous_state
                    self._complete_queue_item(item, QueueOutcome(
                        item.action.origin, "completed", submission=submission,
                    ))

            async def drain_boundary_queue() -> None:
                # Reserve English steering and run cells/commands in FIFO order
                # before the next provider call. Bound this drain so newly
                # arriving actions cannot indefinitely starve generation.
                async with self._lifecycle._queue_lock:
                    boundary_count = len(self._lifecycle._pending_actions)
                for _ in range(boundary_count):
                    async with self._lifecycle._queue_lock:
                        if not self._lifecycle._pending_actions:
                            return
                        item = self._lifecycle._pending_actions.popleft()
                    if item.action.kind == "ask":
                        steering_awaiting_dispatch.append(item)
                    elif item.action.kind == "command":
                        await run_queued_command(item)
                    else:
                        await run_queued_direct(item)

            try:
                routed = (
                    _queued_action.action if _queued_action is not None
                    else self._validate_routed_action(self.router.route(UserAction(origin, text)), origin)
                )

                if routed.kind == "command":
                    submission = await run_command(routed, config)
                    self._set_state_unless_stopping(State.IDLE)
                    return submission

                if routed.kind == "ask":
                    context_pending = callable(getattr(self.context_service, "prepare_request", None))
                    initial_context = self._prepare_context(routed.source, origin.request_id)
                    self._observe_context_epoch(initial_context.epoch)
                    context_committed = False
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
                        self._set_state_unless_stopping(State.IDLE)
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
                    steps = count(1) if self.max_agent_steps == 0 else range(1, self.max_agent_steps + 1)
                    for step in steps:
                        if step > 1:
                            await drain_boundary_queue()
                            if self._lifecycle._active_operation_id != operation_id or self._lifecycle.state in (
                                State.FAILED, State.STOPPING, State.CLOSED,
                            ):
                                raise asyncio.CancelledError
                        self._lifecycle.state = State.GENERATING
                        generation_id = uuid4().hex
                        generation_origin = Origin(
                            origin.session_id, origin.request_id, origin.frontend_id,
                            origin.config_revision, generation_id,
                        )
                        published_events.append(await self._emit_progress(
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
                        reset_needed = getattr(self.context_service, "needs_reset", None)
                        reset_this_step = step > 1 and callable(reset_needed) and reset_needed()
                        if reset_this_step:
                            # Reset between model calls even during a long task.
                            # Reinsert the active user's request so eviction of
                            # dispatched history does not erase the task itself.
                            snapshot = self._prepare_context(routed.source, origin.request_id)
                            context_committed = False
                            context_pending = callable(getattr(self.context_service, "prepare_request", None))
                        else:
                            snapshot = initial_context if step == 1 else self._latest_context()
                        self._observe_context_epoch(snapshot.epoch)
                        if reset_this_step and dispatched_steering:
                            # Earlier queued user steering is still active task
                            # input. Restore its provenance after epoch eviction.
                            for steering_id, steering_text in dispatched_steering:
                                self._append_steering_context(steering_text, steering_id)
                            if callable(getattr(self.context_service, "snapshot", None)):
                                snapshot = self._latest_context()
                            else:
                                snapshot = replace(
                                    snapshot,
                                    messages=(*snapshot.messages, *(("user", text) for _, text in dispatched_steering)),
                                    message_phases=(*snapshot.message_phases, *((None,) * len(dispatched_steering))),
                                )
                        prepare_generation = getattr(self.context_service, "prepare_generation", None)
                        if callable(prepare_generation):
                            prepare_generation()
                            snapshot = self._latest_context()
                        forced_collapse = getattr(self.context_service, "force_collapse", False) is True
                        if steering_awaiting_dispatch:
                            render_user = getattr(self.context_service, "render_user", None)
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
                        cell_config = self._current_config()
                        epoch_config = self._conversation._epoch_config
                        context = await self._run_context_transforms(
                            snapshot, config, cell_config, epoch_config,
                        )
                        model_request = ModelRequest(
                            generation_origin, context, self.model, self._model_options(config),
                        )
                        model_request = await self._run_model_transforms(
                            model_request, config, cell_config, epoch_config,
                        )
                        if not self._operation_is_current(operation_id, State.GENERATING):
                            raise asyncio.CancelledError
                        # This is the final post-transform request actually dispatched to
                        # the provider. Persist it before any provider-side effect.
                        # Commit only after transforms/validation have succeeded. If
                        # cancelled earlier, no steering leaks into future context.
                        for steering_item in steering_awaiting_dispatch:
                            steering_commit_started = True
                            self._append_steering_context(
                                steering_item.action.source, steering_item.action.origin.request_id,
                            )
                        self._lifecycle._active_model_request = model_request
                        self._lifecycle._active_provider_usage_recorded = False
                        self._journal_record("record_model_request", model_request)
                        for steering_item in steering_awaiting_dispatch:
                            dispatched_steering.append((
                                steering_item.action.origin.request_id, steering_item.action.source,
                            ))
                            self._complete_queue_item(
                                steering_item, QueueOutcome(steering_item.action.origin, "steered"),
                            )
                        steering_awaiting_dispatch.clear()
                        # Only transport/rate-limit failures explicitly classified
                        # as transient may retry. Never retry credentials, malformed
                        # responses, plugin validation or code execution. A retry
                        # sends the same already-journaled request; each failed
                        # attempt gets its own usage-unknown journal record.
                        for attempt in range(3):
                            generation = asyncio.create_task(
                                self.provider.generate(model_request),
                                name=f"py-agent-generation-{generation_id}-attempt-{attempt + 1}",
                            )
                            self._lifecycle._generation = generation
                            try:
                                response = await generation
                            except asyncio.CancelledError:
                                self._record_provider_usage(model_request, None, outcome="cancelled")
                                raise
                            except Exception as exc:
                                kind = getattr(exc, "kind", None)
                                recover = getattr(self.context_service, "recover_overflow", None)
                                if kind == "overflow" and attempt == 0 and callable(recover):
                                    self._record_provider_usage(model_request, None, outcome="failed")
                                    if not self._operation_is_current(operation_id, State.GENERATING):
                                        raise asyncio.CancelledError
                                    recovery_context = recover()
                                    forced_collapse = True
                                    model_request = replace(model_request, context=recovery_context)
                                    self._lifecycle._active_model_request = model_request
                                    self._lifecycle._active_provider_usage_recorded = False
                                    self._journal_record("record_model_request", model_request)
                                    continue
                                transient = isinstance(kind, str) and kind in {
                                    "provider", "rate_limit", "transport", "timeout",
                                }
                                if transient and attempt < 2:
                                    self._journal_record(
                                        "record_provider_usage", model_request, None, outcome="retry_failed",
                                    )
                                    try:
                                        published_events.append(await self._emit_progress(
                                            generation_origin,
                                            {"phase": "provider_retry", "attempt": attempt + 2,
                                             "text": f"Provider request failed ({kind}); retrying ({attempt + 2}/3)."},
                                            on_progress=on_progress, operation_id=operation_id,
                                            expected_state=State.GENERATING,
                                        ))
                                        await asyncio.sleep(0.5 * (attempt + 1))
                                    except asyncio.CancelledError:
                                        self._record_provider_usage(model_request, None, outcome="cancelled")
                                        raise
                                    except Exception:
                                        self._record_provider_usage(model_request, None, outcome="failed")
                                        raise
                                    if not self._operation_is_current(operation_id, State.GENERATING):
                                        self._record_provider_usage(model_request, None, outcome="cancelled")
                                        raise asyncio.CancelledError
                                    continue
                                self._record_provider_usage(model_request, None, outcome="failed")
                                raise
                            finally:
                                if self._lifecycle._generation is generation:
                                    self._lifecycle._generation = None
                            break

                        if not isinstance(response, ModelResponse):
                            self._record_provider_usage(model_request, None, outcome="failed")
                            raise TypeError("Provider must return a ModelResponse")
                        self._record_provider_usage(model_request, response, outcome="returned")
                        if not self._operation_is_current(operation_id, State.GENERATING):
                            raise asyncio.CancelledError
                        if not isinstance(response.text, str):
                            raise TypeError("Provider response text must be text")
                        last_response = response
                        record_usage = getattr(self.context_service, "record_response", None)
                        if callable(record_usage):
                            record_usage(response)
                        if len(response.text) > MAX_AGENT_RESPONSE_CHARS:
                            self._commit_context(
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
                            self._journal_record(
                                "record_context_collapse", model_request, response.text,
                                outcome="requested",
                            )
                            if collapse_error is None:
                                collapse = getattr(self.context_service, "collapse", None)
                                store_collapsed = getattr(self.executor, "store_collapsed", None)
                                if not callable(collapse) or not callable(store_collapsed):
                                    collapse_error = "Collapse is unavailable for the selected context/executor services."
                                else:
                                    async def store_archive(text, store=store_collapsed):
                                        try:
                                            index = await store(text)
                                        except BaseException:
                                            # Archive transport may terminate the
                                            # persistent worker, even on cancellation.
                                            # Never resume with a potentially lost namespace.
                                            self._set_state_unless_stopping(State.FAILED)
                                            raise
                                        if not self._operation_is_current(operation_id, State.GENERATING):
                                            raise asyncio.CancelledError
                                        return index

                                    try:
                                        receipt = await collapse(*collapse_args, store_archive)
                                    except (ValueError, TypeError) as exc:
                                        if self._lifecycle.state is State.FAILED:
                                            raise
                                        collapse_error = str(exc)[:500]
                            if collapse_error is not None:
                                self._journal_record(
                                    "record_context_collapse", model_request, response.text,
                                    outcome="rejected", detail=collapse_error,
                                )
                                self._commit_context(
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
                                self._commit_context(
                                    origin.request_id, routed.source, receipt, None,
                                    phase=response.phase, include_user=not context_committed,
                                )
                                context_committed = True
                                context_pending = False
                                invalid_generations = 0
                                self._journal_record(
                                    "record_context_collapse", model_request, response.text,
                                    outcome="succeeded", detail=receipt,
                                )
                            continue
                        decision = self.interpreter.interpret(response)
                        if not isinstance(decision, AgentDecision):
                            raise TypeError("Interpreter must return an AgentDecision")
                        if decision.kind not in ("execute", "finish", "wait", "reject"):
                            raise ValueError(f"Interpreter returned unknown decision: {decision.kind!r}")
                        if not isinstance(decision.source, str) or not isinstance(decision.reason, str):
                            raise TypeError("Decision source and reason must be text")
                        if type(decision.retryable) is not bool or (decision.retryable and decision.kind != "reject"):
                            raise ValueError("Only a rejected response may request a format retry")
                        if decision.kind == "execute" and len(decision.source) > MAX_AGENT_RESPONSE_CHARS:
                            self._commit_context(
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
                            if not self._operation_is_current(operation_id, State.GENERATING):
                                raise asyncio.CancelledError
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
                            self._commit_context(
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
                            published_events.append(await self._emit_progress(
                                generation_origin,
                                {"phase": "format_retry", "step": step,
                                 "text": "Model response had invalid format; requesting a Python-only correction."},
                                on_progress=on_progress, operation_id=operation_id,
                                expected_state=State.GENERATING,
                            ))
                            continue

                        if decision.kind != "execute" or not decision.source.strip():
                            if context_pending:
                                self._abandon_context(origin.request_id)
                                context_pending = False
                            self._set_state_unless_stopping(State.IDLE)
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

                        check_syntax = getattr(self.interpreter, "check_syntax", None)
                        if callable(check_syntax):
                            syntax_error = check_syntax(decision.source)
                            if inspect.isawaitable(syntax_error) or (
                                syntax_error is not None and not isinstance(syntax_error, str)
                            ):
                                raise TypeError("Interpreter syntax check must return text or None synchronously")
                            if syntax_error is not None:
                                if not syntax_error:
                                    raise ValueError("Interpreter returned an empty syntax diagnostic")
                                if not self._operation_is_current(operation_id, State.GENERATING):
                                    raise asyncio.CancelledError
                                # This is a model-visible correction, not an
                                # execution or user-visible cell failure. Never
                                # dispatch malformed source to the executor.
                                self._commit_context(
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
                        self._lifecycle.state = State.EXECUTING
                        execution_origin = Origin(
                            origin.session_id, origin.request_id, origin.frontend_id,
                            origin.config_revision, generation_id, uuid4().hex,
                        )
                        execution_request = ExecutionRequest(
                            execution_origin, decision.source, "agent", "ipython",
                            allow_stdin=allow_stdin,
                            input_handler=execution_input_handler(execution_origin),
                            output_handler=execution_output_handler(execution_origin, "agent"),
                        )
                        published_events.append(await self._emit_progress(
                            execution_origin,
                            {
                                "phase": "execution_start", "step": step, "author": "agent",
                                "text": f"Agent: executing cell (step {step}).",
                            },
                            on_progress=on_progress, operation_id=operation_id,
                            expected_state=State.EXECUTING,
                        ))
                        result = await self._execute_dispatched(execution_request)

                        if not self._operation_is_current(operation_id, State.EXECUTING):
                            raise asyncio.CancelledError
                        published_events.extend(await self._publish_output(
                            execution_request, result, on_progress=on_progress,
                            operation_id=operation_id,
                        ))
                        published_events.append(await self._emit_progress(
                            execution_origin,
                            {
                                "phase": "cell_complete", "step": step,
                                "status": result.status,
                                "text": f"Agent cell {step} completed with status {result.status}.",
                            },
                            on_progress=on_progress, operation_id=operation_id,
                            expected_state=State.EXECUTING,
                        ))
                        if not self._operation_is_current(operation_id, State.EXECUTING):
                            raise asyncio.CancelledError
                        visible_outputs.extend(self._visible_say_outputs(result))
                        visible_messages.extend(self._visible_says(result))
                        last_result, last_execution = result, execution_request
                        executions.append((execution_request, result))

                        observation = self._packed_observation(execution_request, result)
                        self._commit_context(
                            origin.request_id, routed.source, decision.source, observation,
                            phase=response.phase, include_user=not context_committed,
                        )
                        context_committed = True
                        context_pending = False
                        if result.status in ("success", "error"):
                            completed_cell = getattr(self.context_service, "completed_cell", None)
                            if callable(completed_cell):
                                completed_cell()
                            await self._archive_context_outputs()
                            if not self._operation_is_current(operation_id, State.EXECUTING):
                                raise asyncio.CancelledError

                        if result.status in ("uncertain", "cancelled"):
                            self._set_state_unless_stopping(State.FAILED)
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
                            self._set_state_unless_stopping(State.IDLE)
                            return Submission(
                                routed, result=result, message="\n".join(visible_messages),
                                execution=execution_request, response=response,
                                say_outputs=tuple(visible_outputs), executions=tuple(executions),
                                events=tuple(published_events),
                            )

                    self._set_state_unless_stopping(State.GENERATING)
                    step_limit_message = (
                        f"Agent paused after {self.max_agent_steps} agent steps without a successful "
                        "say(final=True); submit another request to continue."
                    )
                    published_events.append(await self._emit_progress(
                        generation_origin,
                        {
                            "phase": "step_limit", "status": "paused",
                            "step_limit": self.max_agent_steps, "text": step_limit_message,
                        },
                        on_progress=on_progress, operation_id=operation_id,
                        expected_state=State.GENERATING,
                    ))
                    self._set_state_unless_stopping(State.IDLE)
                    visible_messages.append(step_limit_message)
                    return Submission(
                        routed, result=last_result, message="\n".join(visible_messages),
                        execution=last_execution, response=last_response,
                        say_outputs=tuple(visible_outputs), executions=tuple(executions),
                        events=tuple(published_events),
                    )

                # Direct execution bypasses provider and agent policy exactly once.
                generation_id = None
                author = "user"
                if self._lifecycle._active_operation_id != operation_id or self._lifecycle.state is not State.IDLE:
                    raise asyncio.CancelledError
                self._lifecycle.state = State.EXECUTING
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
                )
                published_events = [await self._emit_progress(
                    execution_origin,
                    {"phase": "execution_start", "author": author,
                     "text": "Executing user cell."},
                    on_progress=on_progress, operation_id=operation_id,
                    expected_state=State.EXECUTING, author=author,
                )]
                result = await self._execute_dispatched(execution_request)
                if not self._operation_is_current(operation_id, State.EXECUTING):
                    raise asyncio.CancelledError
                published_events.extend(await self._publish_output(
                    execution_request, result, on_progress=on_progress,
                    operation_id=operation_id,
                ))
                published_events.append(await self._emit_progress(
                    execution_origin,
                    {
                        "phase": "cell_complete", "step": 1, "status": result.status,
                        "text": f"User cell completed with status {result.status}.",
                    },
                    on_progress=on_progress, operation_id=operation_id,
                    expected_state=State.EXECUTING, author=author,
                ))
                if not self._operation_is_current(operation_id, State.EXECUTING):
                    raise asyncio.CancelledError
                self._set_state_unless_stopping(
                    State.FAILED if result.status in ("uncertain", "cancelled") else State.IDLE,
                )
                return Submission(
                    routed, result=result, message="\n".join(self._visible_says(result)),
                    execution=execution_request,
                    say_outputs=self._visible_say_outputs(result),
                    events=published_events,
                )
            except asyncio.CancelledError:
                for steering_item in steering_awaiting_dispatch:
                    self._complete_queue_item(
                        steering_item,
                        QueueOutcome(
                            steering_item.action.origin, "interrupted",
                            error="Steering was cancelled before provider dispatch",
                        ),
                    )
                if context_pending:
                    try:
                        self._abandon_context(origin.request_id)
                    except Exception:
                        pass
                if self._lifecycle.state is State.GENERATING:
                    self._set_state_unless_stopping(State.IDLE)
                elif self._lifecycle.state in (State.EXECUTING, State.COMMAND):
                    # Execution/commands may already have side effects; never replay them.
                    # A user interrupt leaves the session usable only when the
                    # executor reported that the interrupted cell itself stopped.
                    if (self._lifecycle.state is State.EXECUTING
                            and self._lifecycle._execution_outcome_status == "cancelled"):
                        self._set_state_unless_stopping(State.IDLE)
                    else:
                        self._set_state_unless_stopping(State.FAILED)
                raise
            except Exception as exc:
                for steering_item in steering_awaiting_dispatch:
                    self._complete_queue_item(
                        steering_item,
                        QueueOutcome(
                            steering_item.action.origin, "failed",
                            error=(
                                "Steering failed before provider dispatch"
                                + ("; context may contain partial steering" if steering_commit_started else "")
                                + ": " + str(exc)
                            ),
                        ),
                    )
                if context_pending:
                    try:
                        self._abandon_context(origin.request_id)
                    except Exception:
                        pass
                if self._lifecycle.state is State.GENERATING:
                    self._set_state_unless_stopping(State.IDLE)
                elif self._lifecycle.state in (State.EXECUTING, State.COMMAND):
                    self._set_state_unless_stopping(State.FAILED)
                raise
            finally:
                if self._lifecycle._active_task is task:
                    self._lifecycle._active_task = None
                if self._lifecycle._active_operation_id == operation_id:
                    self._lifecycle._active_operation_id = None
                if self._lifecycle.state in (State.FAILED, State.STOPPING, State.CLOSED):
                    await self._fail_pending_actions("interrupted", "Session is unavailable")
                asyncio.get_running_loop().call_soon(self._start_queue_worker_if_idle)

    async def _dispatch_command(self, source: str, config: ConfigSnapshot | None) -> str:
        parts = source.split(None, 1)
        name = parts[0] if parts else ""
        arguments = parts[1].strip() if len(parts) > 1 else ""
        core_commands = getattr(self.command_registry, "commands", {})
        try:
            if name == "history":
                self._capture_history_sensitive_config(self._current_config())
                return self._history_command(arguments)
            if name == "context":
                return self._context_command(arguments)
            if name == "model":
                return self._model_command(arguments)
            if name == "effort":
                return self._effort_command(arguments, config)
            if name in core_commands:
                response = self.command_registry.dispatch(name, arguments)
            elif name in self.external_commands:
                registration = self.external_commands[name]
                self._observe_context_epoch(self._read_context_epoch())
                cell_config = self._current_config()
                command = self._sync_factory(
                    registration.create, self._plugin_config(
                        registration.plugin_id, config, cell_config, self._conversation._epoch_config, cell_config,
                    ),
                )
                execute = getattr(command, "execute", None)
                if not callable(execute):
                    return "Command service returned an invalid handler."
                response = execute(arguments)
                if inspect.isawaitable(response):
                    response = await response
            elif self.command_registry is None and not self.external_commands:
                return "Command service is not configured"
            else:
                return "Unknown command"
        except asyncio.CancelledError:
            raise
        except Exception:
            return "Command failed; no configuration change was committed."
        if isinstance(response, str):
            return response
        ok, message = getattr(response, "ok", None), getattr(response, "text", None)
        if type(ok) is not bool or not isinstance(message, str):
            return "Command service returned an invalid response."
        return message

    def _context_export_payload(self) -> dict[str, object]:
        return self._conversation._context_export_payload()

    def _model_command(self, arguments: str) -> str:
        if not isinstance(arguments, str) or len(arguments) > 512:
            return _MODEL_USAGE
        try:
            tokens = shlex.split(arguments, posix=True)
        except ValueError:
            return _MODEL_USAGE
        if not tokens:
            return f"Model: {self.model} (provider: {self.provider_id})"
        if len(tokens) != 1:
            return _MODEL_USAGE
        setter = getattr(self.provider, "set_model", None)
        if not callable(setter):
            return "Live model changes are unavailable for the selected provider."
        previous = self.model
        try:
            setter(tokens[0])
            selected = getattr(self.provider, "model", None)
            if not isinstance(selected, str) or not selected:
                raise TypeError("provider returned invalid model state")
        except (TypeError, ValueError) as exc:
            return f"Model was not changed: {exc}"
        self.model = selected
        return f"Model changed for this session: {previous} -> {selected}"

    def _configured_effort(self, config: ConfigSnapshot | None) -> str | None:
        if self.provider_id != "codex" or config is None:
            return None
        entry = config.entries.get("model.effort")
        return entry.value if entry is not None and isinstance(entry.value, str) else None

    def _effort_command(self, arguments: str, config: ConfigSnapshot | None) -> str:
        if not isinstance(arguments, str) or len(arguments) > 32:
            return _EFFORT_USAGE
        value = arguments.strip().lower()
        if not value:
            effective = self._effort_override or self._configured_effort(config) or "provider default"
            return f"Effort: {effective}. Presets: {', '.join(_EFFORT_PRESETS)}"
        if value not in _EFFORT_PRESETS or any(character.isspace() for character in value):
            return _EFFORT_USAGE
        if self.provider_id not in {"litelm", "codex"}:
            return "Effort presets are unavailable for the selected provider."
        self._effort_override = value
        note = (
            " DeepSeek maps minimal/low to low, medium/high to high, xhigh to max, "
            "and none disables thinking."
            if self.provider_id == "litelm" and self.model.startswith("deepseek/")
            else ""
        )
        return f"Effort changed for this session: {value}.{note}"

    def _context_command(self, arguments: str) -> str:
        return self._conversation._context_command(arguments)

    def _history_command(self, arguments: str) -> str:
        return self._conversation._history_command(arguments)

    @staticmethod
    def _history_count(values: list[str], *, default: int) -> int | None:
        if not values:
            return default
        if len(values) != 1 or not values[0].isdecimal() or len(values[0]) > 3:
            return None
        count = int(values[0])
        return count if 1 <= count <= HISTORY_MAX_ITEMS else None

    @staticmethod
    def _history_integer(value: str, *, maximum: int) -> int | None:
        if not value.isdecimal() or len(value) > len(str(maximum)):
            return None
        number = int(value)
        return number if number <= maximum else None

    def _history_recent(self, count: int) -> str:
        return self._conversation._history_recent(count)

    def _history_search(self, tokens: list[str]) -> str:
        return self._conversation._history_search(tokens)

    def _history_page(self, tokens: list[str]) -> str:
        return self._conversation._history_page(tokens)

    async def interrupt(self) -> None:
        return await self._lifecycle.interrupt()

    async def close(self) -> None:
        return await self._lifecycle.close()

    async def _close_impl(self) -> None:
        return await self._lifecycle._close_impl()

    @property
    def _context(self):
        """Compatibility view; mutable state is owned by conversation."""
        return self._conversation._context

    @_context.setter
    def _context(self, value):
        self._conversation._context = value

    @property
    def _context_epoch(self):
        """Compatibility view; mutable state is owned by conversation."""
        return self._conversation._context_epoch

    @_context_epoch.setter
    def _context_epoch(self, value):
        self._conversation._context_epoch = value

    @property
    def _context_overlay(self):
        """Compatibility view; mutable state is owned by conversation."""
        return self._conversation._context_overlay

    @_context_overlay.setter
    def _context_overlay(self, value):
        self._conversation._context_overlay = value

    @property
    def _context_overlay_epoch(self):
        """Compatibility view; mutable state is owned by conversation."""
        return self._conversation._context_overlay_epoch

    @_context_overlay_epoch.setter
    def _context_overlay_epoch(self, value):
        self._conversation._context_overlay_epoch = value

    @property
    def _history_sensitive_values(self):
        """Compatibility view; mutable state is owned by conversation."""
        return self._conversation._history_sensitive_values

    @_history_sensitive_values.setter
    def _history_sensitive_values(self, value):
        self._conversation._history_sensitive_values = value

    @property
    def _history_sensitive_chars(self):
        """Compatibility view; mutable state is owned by conversation."""
        return self._conversation._history_sensitive_chars

    @_history_sensitive_chars.setter
    def _history_sensitive_chars(self, value):
        self._conversation._history_sensitive_chars = value

    @property
    def _history_content_hidden(self):
        """Compatibility view; mutable state is owned by conversation."""
        return self._conversation._history_content_hidden

    @_history_content_hidden.setter
    def _history_content_hidden(self, value):
        self._conversation._history_content_hidden = value

    @property
    def _journal_sensitive_config_ready(self):
        """Compatibility view; mutable state is owned by conversation."""
        return self._conversation._journal_sensitive_config_ready

    @_journal_sensitive_config_ready.setter
    def _journal_sensitive_config_ready(self, value):
        self._conversation._journal_sensitive_config_ready = value

    @property
    def _epoch_config(self):
        """Compatibility view; mutable state is owned by conversation."""
        return self._conversation._epoch_config

    @_epoch_config.setter
    def _epoch_config(self, value):
        self._conversation._epoch_config = value

    @property
    def _epoch_config_epoch(self):
        """Compatibility view; mutable state is owned by conversation."""
        return self._conversation._epoch_config_epoch

    @_epoch_config_epoch.setter
    def _epoch_config_epoch(self, value):
        self._conversation._epoch_config_epoch = value

    @property
    def _event_sequence(self):
        """Compatibility view; mutable state is owned by frontend."""
        return self._frontend._event_sequence

    @_event_sequence.setter
    def _event_sequence(self, value):
        self._frontend._event_sequence = value

    @property
    def best_effort_observer_failures(self):
        """Compatibility view; mutable state is owned by frontend."""
        return self._frontend.best_effort_observer_failures

    @best_effort_observer_failures.setter
    def best_effort_observer_failures(self, value):
        self._frontend.best_effort_observer_failures = value

    @property
    def state(self):
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle.state

    @state.setter
    def state(self, value):
        self._lifecycle.state = value

    @property
    def _lock(self):
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._lock

    @_lock.setter
    def _lock(self, value):
        self._lifecycle._lock = value

    @property
    def _queue_lock(self):
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._queue_lock

    @_queue_lock.setter
    def _queue_lock(self, value):
        self._lifecycle._queue_lock = value

    @property
    def _pending_actions(self):
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._pending_actions

    @_pending_actions.setter
    def _pending_actions(self, value):
        self._lifecycle._pending_actions = value

    @property
    def _queue_worker(self):
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._queue_worker

    @_queue_worker.setter
    def _queue_worker(self, value):
        self._lifecycle._queue_worker = value

    @property
    def _lifecycle_lock(self):
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._lifecycle_lock

    @_lifecycle_lock.setter
    def _lifecycle_lock(self, value):
        self._lifecycle._lifecycle_lock = value

    @property
    def _close_task(self):
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._close_task

    @_close_task.setter
    def _close_task(self, value):
        self._lifecycle._close_task = value

    @property
    def _executor_close_attempted(self):
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._executor_close_attempted

    @_executor_close_attempted.setter
    def _executor_close_attempted(self, value):
        self._lifecycle._executor_close_attempted = value

    @property
    def _active_task(self):
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._active_task

    @_active_task.setter
    def _active_task(self, value):
        self._lifecycle._active_task = value

    @property
    def _active_operation_id(self):
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._active_operation_id

    @_active_operation_id.setter
    def _active_operation_id(self, value):
        self._lifecycle._active_operation_id = value

    @property
    def _active_model_request(self):
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._active_model_request

    @_active_model_request.setter
    def _active_model_request(self, value):
        self._lifecycle._active_model_request = value

    @property
    def _active_provider_usage_recorded(self):
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._active_provider_usage_recorded

    @_active_provider_usage_recorded.setter
    def _active_provider_usage_recorded(self, value):
        self._lifecycle._active_provider_usage_recorded = value

    @property
    def _active_execution_request(self):
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._active_execution_request

    @_active_execution_request.setter
    def _active_execution_request(self, value):
        self._lifecycle._active_execution_request = value

    @property
    def _active_execution_result_recorded(self):
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._active_execution_result_recorded

    @_active_execution_result_recorded.setter
    def _active_execution_result_recorded(self, value):
        self._lifecycle._active_execution_result_recorded = value

    @property
    def _execution_active(self):
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._execution_active

    @_execution_active.setter
    def _execution_active(self, value):
        self._lifecycle._execution_active = value

    @property
    def _execution_outcome_status(self):
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._execution_outcome_status

    @_execution_outcome_status.setter
    def _execution_outcome_status(self, value):
        self._lifecycle._execution_outcome_status = value

    @property
    def _generation(self):
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._generation

    @_generation.setter
    def _generation(self, value):
        self._lifecycle._generation = value

    @property
    def _journal_started(self):
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._journal_started

    @_journal_started.setter
    def _journal_started(self, value):
        self._lifecycle._journal_started = value

    @property
    def _journal_closed(self):
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._journal_closed

    @_journal_closed.setter
    def _journal_closed(self, value):
        self._lifecycle._journal_closed = value

    @property
    def _journal_failed(self):
        """Compatibility view; mutable state is owned by lifecycle."""
        return self._lifecycle._journal_failed

    @_journal_failed.setter
    def _journal_failed(self, value):
        self._lifecycle._journal_failed = value

    @property
    def _cache_totals(self):
        """Compatibility view of journal accounting state."""
        return self._journal._cache_totals

    @_cache_totals.setter
    def _cache_totals(self, value):
        self._journal._cache_totals = value

    @property
    def _cache_complete(self):
        """Compatibility view of journal accounting state."""
        return self._journal._cache_complete

    @_cache_complete.setter
    def _cache_complete(self, value):
        self._journal._cache_complete = value

    @property
    def _cache_reports(self):
        """Compatibility view of journal accounting state."""
        return self._journal._cache_reports

    @_cache_reports.setter
    def _cache_reports(self, value):
        self._journal._cache_reports = value
