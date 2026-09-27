"""Frontend-independent serialized coordinator with explicit service selection."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, replace
from enum import Enum
import inspect
from itertools import count
import json
import math
from pathlib import Path
import re
import shlex
from types import MappingProxyType
from uuid import uuid4

from .collapse_control import parse_collapse
from .configuration import ApplyAt, ConfigSnapshot, ConfigStore
from .context_export import default_export_path, write_context_html
from .contracts import (
    MAX_FRONTEND_ID_CHARS,
    MAX_QUEUED_ACTION_CHARS,
    AgentDecision,
    CompletenessResult,
    CompletionResult,
    ContextSnapshot,
    ExecutionOutput,
    ExecutionRequest,
    ExecutionResult,
    ExecutorCapabilities,
    ExecutorCapabilityError,
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
    QueueFullError,
    QueueOutcome,
    QueueTicket,
    RoutedAction,
    SayOutput,
    Submission,
    UserAction,
)
from .plugins import PluginError, PluginRuntime
from .session_journal import (
    MAX_EVENT_BYTES,
    MAX_HISTORY_PAGE_CHARS,
    MAX_HISTORY_SEARCH_BYTES,
    MAX_HISTORY_SEARCH_QUERY_CHARS,
    MAX_HISTORY_SEARCH_SCAN,
    JournalError,
    JournalService,
    NoPersistenceJournal,
)

MAX_PENDING_ACTIONS = 32
HISTORY_DEFAULT_LIMIT = 10
HISTORY_MAX_ITEMS = 20
HISTORY_MAX_PAGE_CHARS = 4_000
HISTORY_MAX_COMMAND_CHARS = 1_024
HISTORY_MAX_OFFSET = MAX_EVENT_BYTES
HISTORY_MAX_SENSITIVE_VALUES = 256
HISTORY_MAX_SENSITIVE_CHARS = 65_536
HISTORY_MAX_SENSITIVE_VALUE_CHARS = 256
MAX_AGENT_RESPONSE_CHARS = 8_000
MODEL_OBSERVATION_MAX_EVENTS = 256
MODEL_OBSERVATION_MAX_DISPLAY_CHARS = 8_000
MODEL_OBSERVATION_MAX_MIME_TYPES = 16
_OBSERVATION_MIME_TYPE = re.compile(r"[A-Za-z0-9!#$&^_.+-]{1,64}/[A-Za-z0-9!#$&^_.+-]{1,64}\Z")
_TERMINAL_ESCAPE = re.compile(
    r"(?:"
    r"\x1b\].*?(?:\x07|\x1b\\|\x9c)|\x9d.*?(?:\x07|\x1b\\|\x9c)|"
    r"\x1b[P^_X].*?(?:\x1b\\|\x9c)|[\x90\x98\x9e\x9f].*?\x9c|"
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x9b[0-?]*[ -/]*[@-~]|\x1b[@-_]"
    r")",
    re.DOTALL,
)
_TERMINAL_INCOMPLETE_ESCAPE = re.compile(
    r"(?:\x1b\].*|\x9d.*|\x1b[P^_X].*|[\x90\x98\x9e\x9f].*|"
    r"\x1b\[[0-?]*[ -/]*|\x9b[0-?]*[ -/]*)\Z",
    re.DOTALL,
)
_HISTORY_USAGE = (
    "Usage: /history [recent [COUNT]] | /history search [--limit COUNT] "
    "[--kind KIND] QUERY | /history page EVENT_ID [OFFSET [CHARS]]"
)
_CONTEXT_USAGE = "Usage: /context save [PATH]"
_MODEL_USAGE = "Usage: /model [MODEL_ID]"
_EFFORT_PRESETS = ("none", "minimal", "low", "medium", "high", "xhigh")
_EFFORT_USAGE = "Usage: /effort [none|minimal|low|medium|high|xhigh]"
_COORDINATOR_COMMANDS = frozenset({"history", "context", "model", "effort"})


def _strip_observation_terminal_controls(text: str) -> str:
    safe = _TERMINAL_ESCAPE.sub("", text)
    safe = _TERMINAL_INCOMPLETE_ESCAPE.sub("", safe)
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", safe)


def _bounded_plain_fallback(
    text: str, limit: int = MODEL_OBSERVATION_MAX_DISPLAY_CHARS,
) -> str:
    """Keep bounded plain text, stripping terminal control sequences."""
    truncated = len(text) > limit
    safe = _strip_observation_terminal_controls(text[:limit])
    if truncated:
        marker = "\n[plain-text fallback truncated]"
        if limit <= len(marker):
            return marker[:limit]
        safe = safe[:limit - len(marker)] + marker
    return safe


def _observation_mime_types(data: object) -> tuple[list[str], bool]:
    if not hasattr(data, "keys"):
        return [], False
    mime_types = []
    for name in data:
        if not isinstance(name, str) or _OBSERVATION_MIME_TYPE.fullmatch(name) is None:
            continue
        if len(mime_types) >= MODEL_OBSERVATION_MAX_MIME_TYPES:
            return mime_types, True
        mime_types.append(name)
    return mime_types, False


class State(str, Enum):
    NEW = "new"
    IDLE = "idle"
    GENERATING = "generating"
    EXECUTING = "executing"
    WAITING_FOR_INPUT = "waiting-for-input"
    COMMAND = "command"
    STOPPING = "stopping"
    FAILED = "failed"
    CLOSED = "closed"


@dataclass
class _QueuedAction:
    action: RoutedAction
    text: str
    config: ConfigSnapshot | None
    allow_stdin: bool
    input_handler: InputHandler | None
    on_progress: ProgressCallback | None
    completion: asyncio.Future[QueueOutcome]


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
        self._context: list[tuple[str, str]] = []
        self._context_epoch = 0
        self._context_overlay: list[tuple[int, str]] = []
        self._context_overlay_epoch: int | None = None
        self._restart_config = creation_config
        self._epoch_config = self._restart_config
        self._epoch_config_epoch = self._read_context_epoch()
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
        self._event_sequence = 0
        self.best_effort_observer_failures = 0
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
        self._history_sensitive_values: set[str] = set()
        self._history_sensitive_chars = 0
        self._history_content_hidden = False
        self._journal_sensitive_config_ready = True
        self._capture_history_sensitive_config(creation_config)
        self._journal_started = False
        self._journal_closed = False
        self._journal_failed = False
        self._shutdown_timeout = shutdown_timeout
        self.max_agent_steps = max_agent_steps
        self.state = State.NEW

        self._lock = asyncio.Lock()
        self._queue_lock = asyncio.Lock()
        self._pending_actions: deque[_QueuedAction] = deque()
        self._queue_worker: asyncio.Task | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._close_task: asyncio.Task | None = None
        self._executor_close_attempted = False
        self._active_task: asyncio.Task | None = None
        self._active_operation_id: str | None = None
        self._active_model_request: ModelRequest | None = None
        self._active_provider_usage_recorded = False
        self._cache_totals = {"input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
        self._cache_complete = {name: True for name in self._cache_totals}
        self._cache_reports = 0
        self._active_execution_request: ExecutionRequest | None = None
        self._active_execution_result_recorded = False
        self._execution_active = False
        # Status of the executor result that belongs to the current dispatch,
        # cleared before each dispatch. A cancellation that arrives after a
        # "cancelled" result may keep a session whose namespace survived.
        self._execution_outcome_status: str | None = None
        self._generation: asyncio.Task | None = None
        # Minimal context fallback for embedders that do not select a context
        # policy. Production CLI sessions select ProductionContextAdapter.
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
        if self.state not in (State.STOPPING, State.CLOSED):
            self.state = state

    def _commit_fallback_context(self, user_text: str, assistant_text: str,
                                 observation: str | None = None, *, include_user: bool = True) -> None:
        if include_user:
            self._context.append(("user", user_text))
        self._context.append(("assistant", assistant_text))
        if observation is not None:
            self._context.append(("observation", observation[:8000]))
        self._context_epoch += 1

    def _operation_is_current(self, operation_id: str, state: State) -> bool:
        return self._active_operation_id == operation_id and self.state is state

    def _current_config(self) -> ConfigSnapshot | None:
        if self.config_store is None:
            return None
        snapshot = self.config_store.snapshot
        if not isinstance(snapshot, ConfigSnapshot):
            raise TypeError("Configuration store returned an invalid snapshot")
        return snapshot

    def _capture_history_sensitive_config(self, snapshot: ConfigSnapshot | None) -> None:
        """Retain bounded sensitive values and install write-time journal redaction."""
        if snapshot is None or self.config_store is None:
            return
        for name, field in self.config_store.registry.fields.items():
            if not field.sensitive:
                continue
            entry = snapshot.entries.get(name)
            if entry is None or entry.value is None or entry.value == "":
                continue
            value = entry.value
            if not isinstance(value, str):
                self._history_content_hidden = True
                if getattr(self.journal, "persisted", None) is True:
                    self._journal_sensitive_config_ready = False
                continue
            if len(value) < 4 or len(value) > HISTORY_MAX_SENSITIVE_VALUE_CHARS:
                self._history_content_hidden = True
            if value in self._history_sensitive_values:
                continue
            if (len(self._history_sensitive_values) >= HISTORY_MAX_SENSITIVE_VALUES
                    or self._history_sensitive_chars + len(value) > HISTORY_MAX_SENSITIVE_CHARS):
                self._history_content_hidden = True
                self._journal_sensitive_config_ready = False
                continue
            self._history_sensitive_values.add(value)
            self._history_sensitive_chars += len(value)
        if getattr(self.journal, "persisted", None) is True:
            try:
                self.journal.set_sensitive_values(tuple(sorted(self._history_sensitive_values)))
            except Exception:
                self._journal_sensitive_config_ready = False

    def _history_patterns(self) -> tuple[str, ...]:
        patterns = set()
        for value in self._history_sensitive_values:
            patterns.add(value)
            escaped = json.dumps(value, ensure_ascii=False)[1:-1]
            if escaped:
                patterns.add(escaped)
        return tuple(sorted(patterns, key=len, reverse=True))

    def _redact_history_text(self, text: str, *, preserve_offsets: bool = False) -> str:
        if self._history_content_hidden:
            return "[history content hidden to protect sensitive configuration]"
        if self._history_sensitive_values and not preserve_offsets:
            # recent/search API excerpts may cut through a sensitive value. The
            # paged read path uses overlap-aware masking; short excerpts are
            # omitted whenever config-derived secret values are in scope.
            return "[history excerpt hidden to protect sensitive configuration]"
        patterns = self._history_patterns()
        if not patterns:
            return text
        matcher = re.compile("|".join(re.escape(pattern) for pattern in patterns), re.IGNORECASE)
        return matcher.sub(
            lambda match: ("█" * len(match.group(0))) if preserve_offsets else "[REDACTED]",
            text,
        )

    def _read_history_page(self, event_id: str, offset: int, limit: int) -> dict[str, object]:
        page = self.journal.read(self.session_id, event_id, offset=offset, limit=limit)
        content = page.get("content")
        if not isinstance(content, str):
            raise JournalError("Journal returned an invalid history page")
        if self._history_content_hidden:
            page["content"] = self._redact_history_text(content)
            return page
        patterns = self._history_patterns()
        if not patterns or not content:
            page["content"] = content
            return page
        overlap = max(map(len, patterns)) - 1
        start = max(0, offset - overlap)
        page_chars = len(content)
        extended_limit = min(
            MAX_HISTORY_PAGE_CHARS,
            (offset - start) + page_chars + overlap,
        )
        extended = self.journal.read(
            self.session_id, event_id, offset=start, limit=extended_limit,
        )
        surrounding = extended.get("content")
        if not isinstance(surrounding, str):
            raise JournalError("Journal returned an invalid history page")
        masked = self._redact_history_text(surrounding, preserve_offsets=True)
        page_start = offset - start
        page["content"] = masked[page_start:page_start + page_chars]
        return page

    def _read_context_epoch(self) -> int | None:
        """Read the context epoch when the selected context service exposes it."""
        if self.context_service is None:
            return self._context_epoch
        epoch = getattr(self.context_service, "epoch", None)
        if type(epoch) is int and epoch >= 0:
            return epoch
        snapshot = getattr(self.context_service, "snapshot", None)
        if callable(snapshot):
            try:
                value = snapshot()
            except Exception:
                return None
            if isinstance(value, ContextSnapshot):
                return value.epoch
        return None

    def _observe_context_epoch(
        self, epoch: int | None, *, config: ConfigSnapshot | None = None,
    ) -> None:
        """Latch EPOCH settings only after the context reports a newer epoch."""
        if type(epoch) is not int or epoch < 0:
            return
        if self._epoch_config_epoch is None:
            # The initial epoch could not be inspected; treat the first observed
            # value as a baseline rather than an unverified epoch transition.
            self._epoch_config_epoch = epoch
            if self._context_overlay_epoch is not None and self._context_overlay_epoch != epoch:
                self._context_overlay.clear()
                self._context_overlay_epoch = epoch
        elif epoch > self._epoch_config_epoch:
            self._epoch_config = self._current_config() if config is None else config
            self._epoch_config_epoch = epoch
            self._context_overlay.clear()
            self._context_overlay_epoch = epoch

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
        """Apply the configured input router without dispatching an execution."""
        config = self._current_config()
        revision = config.revision if config is not None else self.config_revision
        origin = Origin(self.session_id, uuid4().hex, "inspection", revision)
        if not code:
            return RoutedAction(origin, "ask", "")
        try:
            routed = self.router.route(UserAction(origin, code))
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
        """Complete direct input in the selected executor's persistent namespace."""
        self._check_query_cursor(code, cursor_pos)
        async with self._lock:
            if (self.state is not State.IDLE or self._queue_worker is not None
                    or self._pending_actions):
                raise RuntimeError("Session busy or unavailable")
            task = asyncio.current_task()
            self._active_task = task
            try:
                routed = self._route_query(code)
                if routed.kind != "execute":
                    return CompletionResult((), cursor_pos, cursor_pos)
                offset = self._query_prefix_offset(code, routed)
                if offset and cursor_pos == 0:
                    return CompletionResult((), 0, 0)
                capabilities = self.executor.capabilities
                method = getattr(self.executor, "complete", None)
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
                if self._active_task is task:
                    self._active_task = None

    async def inspect(self, code: str, cursor_pos: int, detail_level: int = 0) -> InspectionResult:
        """Inspect a direct-input name in the selected worker without evaluating code."""
        self._check_query_cursor(code, cursor_pos)
        if type(detail_level) is not int or detail_level not in (0, 1):
            raise ValueError("Inspection detail level must be 0 or 1")
        async with self._lock:
            if (self.state is not State.IDLE or self._queue_worker is not None
                    or self._pending_actions):
                raise RuntimeError("Session busy or unavailable")
            task = asyncio.current_task()
            self._active_task = task
            try:
                routed = self._route_query(code)
                if routed.kind != "execute":
                    return InspectionResult(False)
                offset = self._query_prefix_offset(code, routed)
                if offset and cursor_pos == 0:
                    return InspectionResult(False)
                capabilities = self.executor.capabilities
                method = getattr(self.executor, "inspect", None)
                if not capabilities.inspection or not callable(method):
                    raise ExecutorCapabilityError("inspection")
                result = await method(routed.source, cursor_pos - offset, detail_level)
                if not isinstance(result, InspectionResult):
                    raise TypeError("Executor inspection must return InspectionResult")
                return result
            finally:
                if self._active_task is task:
                    self._active_task = None

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
        """Check routed input completeness without executing it or using the worker."""
        if not isinstance(code, str):
            return CompletenessResult("invalid")
        async with self._lock:
            if (self.state is not State.IDLE or self._queue_worker is not None
                    or self._pending_actions):
                raise RuntimeError("Session busy or unavailable")
            task = asyncio.current_task()
            self._active_task = task
            try:
                routed = self._route_query(code)
                if routed.kind != "execute":
                    return CompletenessResult("complete")
                return self._parse_completeness(routed.source)
            finally:
                if self._active_task is task:
                    self._active_task = None

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
        if not self._operation_is_current(operation_id, expected_state):
            raise asyncio.CancelledError
        for registration in self._observer_registrations:
            observer = self.output_observers[registration.qualified_name]
            try:
                delivered = observer.observe(event)
                if not inspect.isawaitable(delivered):
                    raise TypeError(f"Observer {registration.qualified_name} must be async")
                await delivered
            except asyncio.CancelledError:
                raise
            except Exception:
                if registration.critical:
                    raise
                self.best_effort_observer_failures += 1
            if not self._operation_is_current(operation_id, expected_state):
                raise asyncio.CancelledError
        if on_progress is not None:
            delivered = on_progress(event)
            if not inspect.isawaitable(delivered):
                raise TypeError("Request progress callback must be async")
            await delivered
            if not self._operation_is_current(operation_id, expected_state):
                raise asyncio.CancelledError

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
        event = self._new_output_event(
            origin, "progress", data, author=author,
        )
        await self._dispatch_output_event(
            event, on_progress=on_progress, operation_id=operation_id,
            expected_state=expected_state,
        )
        return event

    async def _publish_output(
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
            kind: str,
            data: dict[str, object],
            *,
            display_id: str | None = None,
            metadata: dict[str, object] | None = None,
        ) -> None:
            event = self._new_output_event(
                origin, kind, data, display_id=display_id, metadata=metadata,
                author=request.author,
            )
            events.append(event)
            await self._dispatch_output_event(
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

        for output in result.say_outputs:
            if output.final and result.status != "success":
                continue  # Staged finals are not user-visible until successful completion.
            await append(
                "display", {"text/plain": self._say_text(output.content)},
                metadata={"py_agent_source": "say", "final": output.final},
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
        if self.context_service is not None:
            prepare = getattr(self.context_service, "prepare_request", None)
            if callable(prepare):
                snapshot = prepare(text, request_id)
                if not isinstance(snapshot, ContextSnapshot):
                    raise TypeError("Context service must return a ContextSnapshot")
                overlay = tuple(
                    ("user", message) for epoch, message in self._context_overlay
                    if epoch == snapshot.epoch
                ) if self._context_overlay_epoch == snapshot.epoch else ()
                if overlay:
                    insertion = (
                        len(snapshot.messages) - 1
                        if snapshot.messages and snapshot.messages[-1] == ("user", text)
                        else len(snapshot.messages)
                    )
                    messages = list(snapshot.messages)
                    phases = list(snapshot.message_phases)
                    messages[insertion:insertion] = overlay
                    phases[insertion:insertion] = [None] * len(overlay)
                    snapshot = replace(
                        snapshot, messages=tuple(messages), message_phases=tuple(phases),
                    )
                return snapshot
            # The v1 ContextService protocol exposes reset policy and usage
            # accounting but not mutation methods. Keep a minimal compatible
            # append-only snapshot until a service supplies the richer adapter.
            needs_reset = getattr(self.context_service, "needs_reset", None)
            if callable(needs_reset) and needs_reset():
                self._context.clear()
                self._context_epoch += 1
        return ContextSnapshot(self._context_epoch, (*self._context, ("user", text)))

    def _latest_context(self) -> ContextSnapshot:
        snapshot = getattr(self.context_service, "snapshot", None) if self.context_service is not None else None
        if callable(snapshot):
            value = snapshot()
            if not isinstance(value, ContextSnapshot):
                raise TypeError("Context service snapshot() must return ContextSnapshot")
            if self._context_overlay and self._context_overlay_epoch == value.epoch:
                additions = tuple(
                    ("user", text) for epoch, text in self._context_overlay if epoch == value.epoch
                )
                value = replace(
                    value,
                    messages=(*value.messages, *additions),
                    message_phases=(*value.message_phases, *((None,) * len(additions))),
                )
            return value
        return ContextSnapshot(self._context_epoch, tuple(self._context))

    def _append_steering_context(self, text: str, request_id: str) -> None:
        """Append steering after the completed cell's observation, never mid-request."""
        if self.context_service is None:
            self._context.append(("user", text))
            return
        add = getattr(self.context_service, "add", None)
        if callable(add):
            result = add("user", text, refs=(request_id,))
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                raise TypeError("Context service add() must be synchronous")
            if callable(getattr(self.context_service, "snapshot", None)):
                return
            self._context.append(("user", text))
            return
        if not callable(getattr(self.context_service, "snapshot", None)):
            self._context.append(("user", text))
            return
        epoch = self._read_context_epoch()
        if epoch is None:
            epoch = self._context_epoch
        if self._context_overlay_epoch != epoch:
            self._context_overlay.clear()
            self._context_overlay_epoch = epoch
        self._context_overlay.append((epoch, text))

    def _packed_observation(self, request: ExecutionRequest, result: ExecutionResult):
        if result.origin != request.origin:
            raise RuntimeError("Cannot pack output from a different execution origin")
        events = []
        omitted_events = 0
        event_index = 0
        display_chars_remaining = MODEL_OBSERVATION_MAX_DISPLAY_CHARS
        origin = request.origin
        common = {
            "session_id": origin.session_id,
            "request_id": origin.request_id,
            "frontend_id": origin.frontend_id,
            "config_revision": origin.config_revision,
            "generation_id": origin.generation_id,
            "execution_id": origin.execution_id,
            "author": request.author,
        }

        def append_event(data: dict[str, object]) -> None:
            nonlocal event_index, omitted_events
            if len(events) < MODEL_OBSERVATION_MAX_EVENTS:
                events.append({"event_index": event_index, **data, **common})
            else:
                omitted_events += 1
            event_index += 1

        if result.output_events:
            seen_streams = set()
            for output in result.output_events:
                if output.kind == "stream":
                    name, text = output.data.get("name"), output.data.get("text")
                    if name in ("stdout", "stderr") and isinstance(text, str) and text:
                        seen_streams.add(name)
                        append_event({
                            "stream": name,
                            "text": _strip_observation_terminal_controls(text),
                        })
                elif output.kind in ("display", "execute_result", "update"):
                    mime_types, mime_types_truncated = _observation_mime_types(output.data)
                    fallback = output.data.get("text/plain")
                    if isinstance(fallback, str) and display_chars_remaining:
                        display = _bounded_plain_fallback(fallback, display_chars_remaining)
                        display_chars_remaining -= len(display)
                    else:
                        display = ""
                    if not display:
                        summary = ", ".join(mime_types) or "no safe MIME types"
                        if mime_types_truncated:
                            summary += ", additional MIME types omitted"
                        display = f"[rich output omitted; available MIME types: {summary}]"
                    append_event({
                        "display": display,
                        "output_kind": output.kind,
                        "mime_types": mime_types,
                        "mime_types_truncated": mime_types_truncated,
                    })
                elif output.kind == "clear":
                    clear_event = {"clear": True, "output_kind": "clear"}
                    wait = output.data.get("wait")
                    if type(wait) is bool:
                        clear_event["wait"] = wait
                    append_event(clear_event)
            for stream, text in (("stdout", result.stdout), ("stderr", result.stderr)):
                if text and stream not in seen_streams:
                    append_event({
                        "stream": stream,
                        "text": _strip_observation_terminal_controls(text),
                    })
        else:
            for stream, text in (("stdout", result.stdout), ("stderr", result.stderr)):
                if text:
                    append_event({
                        "stream": stream,
                        "text": _strip_observation_terminal_controls(text),
                    })

        for output in result.say_outputs:
            content = output.content
            if isinstance(content, str):
                content = _strip_observation_terminal_controls(content)
            append_event({"say": content, "final": output.final})
        if result.status != "success":
            append_event({
                "error": _strip_observation_terminal_controls(
                    result.error or f"Execution {result.status}"
                ),
                "status": result.status,
            })
        if omitted_events:
            events.append({
                "event_index": event_index,
                "omitted_events": omitted_events,
                **common,
            })

        if self.observations is None:
            observed = [
                event.get("text", event.get("display", ""))
                for event in events
                if "stream" in event or "display" in event
            ]
            observed = [text for text in observed if isinstance(text, str) and text]
            if result.output_events:
                output_text = "\n".join(observed)
            else:
                output_text = "".join(
                    event["text"] for event in events if "stream" in event
                )
            says = "\n".join(
                _strip_observation_terminal_controls(self._say_text(output.content))
                for output in result.say_outputs
            )
            if says:
                output_text += ("\n" if output_text else "") + says
            if omitted_events:
                output_text += (
                    ("\n" if output_text else "")
                    + f"[{omitted_events} execution output events omitted]"
                )
            error = _strip_observation_terminal_controls(result.error or "no error details")
            feedback = output_text + (
                f"\nExecution {result.status}: {error}"
                if result.status != "success" else ""
            )
            if len(feedback) > 8_000:
                return f"Output too long ({len(feedback)} chars); omitted."
            return feedback
        pack = getattr(self.observations, "pack", None)
        if not callable(pack):
            raise TypeError("Selected observation service must expose pack")
        packed = pack(events)
        if not hasattr(packed, "items"):
            raise TypeError("Observation service must return a mapping")
        if (result.output_reference is not None and hasattr(packed, "get")
                and packed.get("_output_already_omitted") is True
                and isinstance(packed.get("output"), str)):
            packed = {**packed, "output": packed["output"] + (
                f" Stream text is saved as outputs[{result.output_reference}]."
            )}
        serialized = json.dumps(packed, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(serialized) > 8_000:
            fallback = {
                "error": f"Observation too long ({len(serialized)} chars); omitted.",
                "status": "output_too_large", "executed": True,
            }
            if result.output_reference is not None:
                fallback["error"] += (
                    f" Stream text is saved as outputs[{result.output_reference}]."
                )
                fallback["_stored_output_index"] = result.output_reference
            return fallback
        if result.output_reference is not None:
            return {**packed, "_stored_output_index": result.output_reference}
        return packed

    async def _archive_context_outputs(self) -> None:
        archive = getattr(self.context_service, "archive_execution_outputs", None)
        store = getattr(self.executor, "store_outputs", None)
        if callable(archive) and callable(store):
            # An executor lacking this explicit control capability leaves old
            # observations intact rather than claiming nonexistent references.
            await archive(store)

    def _abandon_context(self, request_id: str) -> None:
        if self.context_service is None:
            return
        abandon = getattr(self.context_service, "abandon_request", None)
        if callable(abandon):
            abandon(request_id)

    def _commit_context(self, request_id: str, user_text: str, assistant_text: str,
                        observation=None, phase: str | None = None, *, include_user: bool = True) -> None:
        if self.context_service is None:
            text = None
            if observation is not None:
                if isinstance(observation, str):
                    text = observation
                else:
                    text = json.dumps(observation, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            self._commit_fallback_context(user_text, assistant_text, text, include_user=include_user)
            return
        commit = getattr(self.context_service, "commit_response", None)
        if callable(commit):
            commit(request_id, assistant_text, observation=observation, phase=phase)
            if callable(getattr(self.context_service, "snapshot", None)):
                return
        text = observation if isinstance(observation, str) else (
            json.dumps(observation, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            if observation is not None else None
        )
        self._commit_fallback_context(user_text, assistant_text, text, include_user=include_user)

    def _journal_record(self, method: str, *args, **kwargs) -> None:
        if not self._journal_sensitive_config_ready:
            self._journal_failed = True
            self._set_state_unless_stopping(State.FAILED)
            raise JournalError(
                "Sensitive configuration exceeds journal redaction limits; operation stopped without replay"
            )
        if self._journal_failed:
            raise JournalError("Durable journal failed; the session is fail-closed and will not replay work")
        try:
            result = getattr(self.journal, method)(*args, **kwargs)
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                raise TypeError("Coordinator journal methods must commit synchronously")
        except Exception as exc:
            self._journal_failed = True
            self._set_state_unless_stopping(State.FAILED)
            raise JournalError(
                "Durable journal failed; the operation stopped without replay"
            ) from exc

    @property
    def cache_summary(self) -> tuple[str, str, str]:
        """Session-weighted cache rate and complete reported read/write totals."""
        rate = "?"
        if (self._cache_reports and self._cache_complete["input_tokens"]
                and self._cache_complete["cache_read_tokens"]
                and self._cache_totals["input_tokens"] > 0):
            total = self._cache_totals["input_tokens"]
            rate = str((self._cache_totals["cache_read_tokens"] * 100 + total // 2) // total)
        read = str(self._cache_totals["cache_read_tokens"]) if (
            self._cache_reports and self._cache_complete["cache_read_tokens"]
        ) else "?"
        write = str(self._cache_totals["cache_write_tokens"]) if (
            self._cache_reports and self._cache_complete["cache_write_tokens"]
        ) else "?"
        return rate, read, write

    def _record_provider_usage(
        self, request: ModelRequest, response: ModelResponse | None, *, outcome: str,
    ) -> None:
        if self._active_model_request is request and self._active_provider_usage_recorded:
            return
        self._journal_record("record_provider_usage", request, response, outcome=outcome)
        if response is not None:
            reported = response.usage.get("normalized", response.usage)
            counters = reported if hasattr(reported, "get") else {}
            self._cache_reports += 1
            for name in self._cache_totals:
                value = counters.get(name)
                if name == "cache_write_tokens" and (type(value) is not int or value < 0):
                    value = counters.get("cache_creation_tokens")
                if type(value) is int and value >= 0:
                    self._cache_totals[name] += value
                else:
                    self._cache_complete[name] = False
        if self._active_model_request is request:
            self._active_provider_usage_recorded = True

    def _record_execution_result(self, request: ExecutionRequest, result: ExecutionResult) -> None:
        if self._active_execution_request is request and self._active_execution_result_recorded:
            return
        self._journal_record("record_execution_result", request, result)
        if self._active_execution_request is request:
            self._active_execution_result_recorded = True

    def _record_uncertain_execution(self, request: ExecutionRequest, reason: str) -> None:
        if self._active_execution_request is request and self._active_execution_result_recorded:
            return
        self._journal_record("record_uncertain_execution", request, reason)
        if self._active_execution_request is request:
            self._active_execution_result_recorded = True

    async def _execute_dispatched(self, request: ExecutionRequest) -> ExecutionResult:
        """Commit source before dispatch and result before publishing its output."""
        self._journal_record("record_execution_source", request)
        self._active_execution_request = request
        self._active_execution_result_recorded = False
        self._execution_outcome_status = None
        self._execution_active = True
        try:
            try:
                result = await self.executor.execute(request)
            except asyncio.CancelledError:
                self._record_uncertain_execution(
                    request, "Executor call was cancelled; side effects may have occurred",
                )
                raise
            except Exception as exc:
                self._record_uncertain_execution(request, f"Executor raised {type(exc).__name__}")
                raise
        finally:
            self._execution_active = False
        try:
            self._validate_execution_result(request, result)
        except Exception as exc:
            self._record_uncertain_execution(request, f"Executor returned an invalid result: {type(exc).__name__}")
            raise
        self._record_execution_result(request, result)
        self._execution_outcome_status = result.status
        return result

    async def _close_executor(self) -> None:
        if self._executor_close_attempted:
            return
        self._executor_close_attempted = True
        await self.executor.close()

    def _close_journal(self, state: str) -> None:
        if self._journal_closed:
            return
        failure = None
        try:
            if self._journal_started and not self._journal_failed:
                self._journal_record("end", self.session_id, self.config_revision, state)
        except BaseException as exc:
            failure = exc
        try:
            self.journal.close()
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
                raise RuntimeError("Coordinator cannot start unless it is new")
            try:
                config = self._current_config()
                if config is not None:
                    self.config_revision = config.revision
                self._journal_record(
                    "start", self.session_id, self.config_revision, self.provider_id, self.model,
                )
                self._journal_started = True
                if (callable(getattr(self.context_service, "collapse", None))
                        and not callable(getattr(self.executor, "store_collapsed", None))):
                    raise ExecutorCapabilityError(
                        "store_collapsed archival required by the selected context service"
                    )
                await self.executor.start()
            except BaseException:
                self.state = State.FAILED
                try:
                    await self._close_executor()
                except BaseException:
                    # Preserve startup failure; close() remains safe to call.
                    pass
                try:
                    self._close_journal(State.FAILED.value)
                except BaseException:
                    # Preserve startup failure; the journal has still been closed.
                    pass
                raise
            self.state = State.IDLE

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

        Leading English asks become steering at a safe cell boundary. Direct
        @/!/% cells run at that boundary before the next model request, without
        interrupting execution. Slash commands wait for the turn to finish and
        block later queue items; no item overtakes an earlier one.
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

        config = self._current_config()
        revision = config.revision if config is not None else self.config_revision
        origin = Origin(self.session_id, uuid4().hex, frontend_id, revision)
        routed = self._validate_routed_action(self.router.route(UserAction(origin, text)), origin)
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
        self._start_queue_worker_if_idle()
        return ticket

    def _start_queue_worker_if_idle(self) -> None:
        if (self._queue_worker is None and self._pending_actions
                and self.state is State.IDLE and not self._lock.locked()):
            worker = asyncio.create_task(
                self._drain_queue(), name="py-agent-queued-actions",
            )
            self._queue_worker = worker
            # A task cancelled before its coroutine starts never enters the
            # drainer's finally block. Always release the worker slot.
            worker.add_done_callback(self._queue_worker_finished)

    def _queue_worker_finished(self, worker: asyncio.Task) -> None:
        if self._queue_worker is worker:
            self._queue_worker = None
        self._start_queue_worker_if_idle()

    @staticmethod
    def _complete_queue_item(item: _QueuedAction, outcome: QueueOutcome) -> None:
        if not item.completion.done():
            item.completion.set_result(outcome)

    async def _fail_pending_actions(self, status: str, error: str) -> None:
        async with self._queue_lock:
            pending = tuple(self._pending_actions)
            self._pending_actions.clear()
        for item in pending:
            self._complete_queue_item(item, QueueOutcome(item.action.origin, status, error=error))

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
                    submission = await self.submit(
                        item.action.origin.frontend_id, item.text, _queued_action=item,
                    )
                except asyncio.CancelledError:
                    self._complete_queue_item(
                        item, QueueOutcome(item.action.origin, "interrupted", error="Queued action was cancelled"),
                    )
                    await self._fail_pending_actions(
                        "interrupted", "Queue processing was cancelled; pending actions were not dispatched",
                    )
                    return
                except Exception as exc:
                    self._complete_queue_item(
                        item, QueueOutcome(item.action.origin, "failed", error=str(exc)),
                    )
                    if self.state in (State.FAILED, State.STOPPING, State.CLOSED):
                        await self._fail_pending_actions("interrupted", "Session is unavailable")
                        return
                else:
                    self._complete_queue_item(
                        item, QueueOutcome(item.action.origin, "completed", submission=submission),
                    )
        finally:
            if self._queue_worker is current:
                self._queue_worker = None
            self._start_queue_worker_if_idle()

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
        if ((self.state is not State.IDLE or self._lock.locked() or self._queue_worker is not None
             or self._pending_actions) and _queued_action is None):
            raise RuntimeError("Session busy, queued work pending, or unavailable")
        async with self._lock:
            if (self.state is not State.IDLE
                    or ((_queued_action is None) and (self._queue_worker is not None or self._pending_actions))):
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
            self._active_task = task
            self._active_operation_id = operation_id
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
                        self._active_execution_request is None
                        or self._active_execution_request.origin != execution_origin
                        or self.state is not State.EXECUTING
                    ):
                        raise InputUnavailableError("Interactive input execution is no longer active")
                    self.state = State.WAITING_FOR_INPUT
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
                        if self.state is State.WAITING_FOR_INPUT:
                            self.state = State.EXECUTING

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
                previous_state = self.state
                self.state = State.EXECUTING
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
                    if self.state is State.EXECUTING:
                        self.state = previous_state

            async def drain_boundary_queue() -> None:
                # Reserve leading English steering in FIFO order and execute
                # direct cells before the next provider call. A slash command
                # at the head remains deferred and blocks everything behind it.
                async with self._queue_lock:
                    boundary_count = len(self._pending_actions)
                for _ in range(boundary_count):
                    async with self._queue_lock:
                        if not self._pending_actions or self._pending_actions[0].action.kind == "command":
                            return
                        item = self._pending_actions.popleft()
                    if item.action.kind == "ask":
                        steering_awaiting_dispatch.append(item)
                    else:
                        await run_queued_direct(item)

            try:
                routed = (
                    _queued_action.action if _queued_action is not None
                    else self._validate_routed_action(self.router.route(UserAction(origin, text)), origin)
                )

                if routed.kind == "command":
                    self.state = State.COMMAND
                    message = await self._dispatch_command(routed.source, config)
                    if not self._operation_is_current(operation_id, State.COMMAND):
                        raise asyncio.CancelledError
                    if self.config_store is not None:
                        self.config_revision = self.config_store.snapshot.revision
                        self._capture_history_sensitive_config(self.config_store.snapshot)
                    if (not self._journal_sensitive_config_ready
                            and getattr(self.journal, "persisted", None) is True):
                        message += (
                            "\nWarning: sensitive configuration exceeds journal redaction limits; "
                            "further persisted operations will stop safely."
                        )
                    self._set_state_unless_stopping(State.IDLE)
                    return Submission(routed, message=message)

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
                            if self._active_operation_id != operation_id or self.state in (
                                State.FAILED, State.STOPPING, State.CLOSED,
                            ):
                                raise asyncio.CancelledError
                        self.state = State.GENERATING
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
                        epoch_config = self._epoch_config
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
                        self._active_model_request = model_request
                        self._active_provider_usage_recorded = False
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
                            self._generation = generation
                            try:
                                response = await generation
                            except asyncio.CancelledError:
                                self._record_provider_usage(model_request, None, outcome="cancelled")
                                raise
                            except Exception as exc:
                                kind = getattr(exc, "kind", None)
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
                                if self._generation is generation:
                                    self._generation = None
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
                                        if self.state is State.FAILED:
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
                            correction = (
                                "No code was executed. Invalid model response format: "
                                + (decision.reason[:300] or "invalid cell")
                                + ". No tools or function calls are available. Respond with exactly one "
                                  "complete Python/IPython cell as ordinary assistant message text: "
                                  "no tool call, JSON, prose outside the cell, or Markdown fences."
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
                        self.state = State.EXECUTING
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
                if self._active_operation_id != operation_id or self.state is not State.IDLE:
                    raise asyncio.CancelledError
                self.state = State.EXECUTING
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
                if self.state is State.GENERATING:
                    self._set_state_unless_stopping(State.IDLE)
                elif self.state in (State.EXECUTING, State.COMMAND):
                    # Execution/commands may already have side effects; never replay them.
                    # A user interrupt leaves the session usable only when the
                    # executor reported that the interrupted cell itself stopped.
                    if (self.state is State.EXECUTING
                            and self._execution_outcome_status == "cancelled"):
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
                if self.state is State.GENERATING:
                    self._set_state_unless_stopping(State.IDLE)
                elif self.state in (State.EXECUTING, State.COMMAND):
                    self._set_state_unless_stopping(State.FAILED)
                raise
            finally:
                if self._active_task is task:
                    self._active_task = None
                if self._active_operation_id == operation_id:
                    self._active_operation_id = None
                if self.state in (State.FAILED, State.STOPPING, State.CLOSED):
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
                        registration.plugin_id, config, cell_config, self._epoch_config, cell_config,
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
        """Capture current context plus the exact most recent provider request."""
        snapshot = self._latest_context()

        def snapshot_data(value: ContextSnapshot) -> dict[str, object]:
            return {
                "epoch": value.epoch,
                "messages": [
                    {"role": role, "content": content, "phase": phase}
                    for (role, content), phase in zip(
                        value.messages, value.message_phases, strict=True,
                    )
                ],
                "transform_trace": list(value.transform_trace),
            }

        request = self._active_model_request
        request_data = None
        if request is not None:
            request_data = {
                "origin": vars(request.origin),
                "model": request.model,
                "options": dict(request.options),
                "transform_trace": list(request.transform_trace),
                "context": snapshot_data(request.context),
            }

        context = getattr(self.context_service, "context", None)
        raw_groups = []
        for group in getattr(context, "groups", ()):
            raw_groups.append({
                "messages": [dict(message) for message in getattr(group, "messages", ())],
                "refs": list(getattr(group, "refs", ())),
                "execution_output_indexes": list(
                    getattr(group, "execution_output_indexes", ()),
                ),
            })
        archives = []
        for index, raw in sorted(getattr(context, "collapsed", {}).items()):
            try:
                content = json.loads(raw)
            except (TypeError, ValueError):
                content = raw
            archives.append({"index": index, "content": content})

        capabilities = getattr(self.executor, "capabilities", None)
        capability_data = vars(capabilities) if isinstance(capabilities, ExecutorCapabilities) else None
        core_commands = getattr(self.command_registry, "commands", {})
        return {
            "export": {
                "format": "py-context-html-v1",
                "session_id": self.session_id,
                "provider_id": self.provider_id,
                "model": self.model,
                "config_revision": self.config_revision,
                "effort_override": self._effort_override,
                "warning": "Contains sensitive, unredacted session context.",
            },
            "current_context": snapshot_data(snapshot),
            "last_model_request": request_data,
            "runtime": {
                "services": {
                    "router": type(self.router).__name__,
                    "provider": type(self.provider).__name__,
                    "interpreter": type(self.interpreter).__name__,
                    "executor": type(self.executor).__name__,
                    "context": type(self.context_service).__name__
                    if self.context_service is not None else None,
                    "observations": type(self.observations).__name__
                    if self.observations is not None else None,
                },
                "executor_capabilities": capability_data,
                "commands": sorted(
                    set(core_commands) | set(self.external_commands) | _COORDINATOR_COMMANDS
                ),
                "context_transforms": [
                    item.qualified_name for item in self._plugin_runtime.transforms["context"]
                ],
                "model_request_transforms": [
                    item.qualified_name
                    for item in self._plugin_runtime.transforms["model-request"]
                ],
                "model_tools": {
                    "registered": [],
                    "note": (
                        "This agent does not send structured tool definitions to the model; "
                        "Python/IPython execution is specified by the system prompt, and its "
                        "observations are included in the messages above."
                    ),
                },
            },
            "raw_context": {
                "groups": raw_groups,
                "reported_input_tokens": getattr(context, "reported_input_tokens", None),
                "window_tokens": getattr(context, "window_tokens", None),
            },
            "collapsed_archives": archives,
        }

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
        if not isinstance(arguments, str) or len(arguments) > 4096:
            return _CONTEXT_USAGE
        try:
            tokens = shlex.split(arguments, posix=True)
        except ValueError:
            return _CONTEXT_USAGE
        if tokens and tokens[0] != "save":
            return _CONTEXT_USAGE
        if len(tokens) > 2:
            return _CONTEXT_USAGE
        path = default_export_path(self.session_id) if len(tokens) < 2 else Path(tokens[1])
        try:
            saved = write_context_html(path, self._context_export_payload())
        except FileExistsError:
            return f"Context export refused to overwrite existing file: {path}"
        except OSError as exc:
            return f"Context export failed: {exc}"
        return f"Saved private context explorer to {saved} (file mode 0600)."

    def _history_command(self, arguments: str) -> str:
        if getattr(self.journal, "persisted", None) is False:
            return (
                "History persistence is disabled: this session selected the explicit "
                "no-persistence journal. No history is stored or replayed."
            )
        if getattr(self.journal, "persisted", None) is not True:
            return "History viewing is unavailable for the selected journal service."
        if any(not callable(getattr(self.journal, name, None)) for name in ("recent", "search", "read")):
            return "History viewing is unavailable for the selected journal service."
        if not isinstance(arguments, str) or len(arguments) > HISTORY_MAX_COMMAND_CHARS:
            return _HISTORY_USAGE
        try:
            tokens = shlex.split(arguments, posix=True)
        except ValueError:
            return _HISTORY_USAGE
        operation = tokens[0] if tokens else "recent"
        if operation == "recent":
            values = tokens[1:]
            count = self._history_count(values, default=HISTORY_DEFAULT_LIMIT)
            if count is None:
                return _HISTORY_USAGE
            return self._history_recent(count)
        if operation.isdecimal() and len(tokens) == 1:
            count = self._history_count(tokens, default=HISTORY_DEFAULT_LIMIT)
            return self._history_recent(count) if count is not None else _HISTORY_USAGE
        if operation == "search":
            return self._history_search(tokens[1:])
        if operation in {"page", "read"}:
            return self._history_page(tokens[1:])
        return _HISTORY_USAGE

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
        try:
            entries = self.journal.recent(self.session_id, limit=count)
        except Exception:
            return "History is unavailable because the journal could not be read."
        if not entries:
            return "No history entries are recorded for this session. History is read-only; nothing is replayed."
        lines = ["Recent journal events (newest first; read-only, never replayed):"]
        for entry in entries[:count]:
            if not isinstance(entry, dict):
                continue
            event_id = entry.get("id")
            kind = entry.get("kind")
            request_id = entry.get("request_id")
            excerpt = entry.get("excerpt")
            if not isinstance(event_id, str) or not isinstance(kind, str) or not isinstance(excerpt, str):
                continue
            safe_excerpt = self._redact_history_text(excerpt[:240])
            safe_id = event_id[:128]
            safe_kind = kind[:100]
            request = f" request={request_id[:128]}" if isinstance(request_id, str) else ""
            lines.append(f"{safe_id} {safe_kind}{request}: {safe_excerpt}")
        if len(lines) == 1:
            return "No readable history entries are available. History is read-only; nothing is replayed."
        return "\n".join(lines)

    def _history_search(self, tokens: list[str]) -> str:
        count = HISTORY_DEFAULT_LIMIT
        kind = None
        query_tokens = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token == "--limit" and not query_tokens and index + 1 < len(tokens):
                parsed = self._history_count([tokens[index + 1]], default=HISTORY_DEFAULT_LIMIT)
                if parsed is None:
                    return _HISTORY_USAGE
                count = parsed
                index += 2
                continue
            if token == "--kind" and not query_tokens and index + 1 < len(tokens):
                kind = tokens[index + 1]
                if not kind or len(kind) > 100 or "\x00" in kind:
                    return _HISTORY_USAGE
                index += 2
                continue
            if token.startswith("--") and not query_tokens:
                return _HISTORY_USAGE
            query_tokens.append(token)
            index += 1
        query = " ".join(query_tokens)
        if not query or len(query) > MAX_HISTORY_SEARCH_QUERY_CHARS:
            return _HISTORY_USAGE
        try:
            entries = self.journal.search(
                self.session_id, query, kind=kind, limit=count,
                scan_limit=MAX_HISTORY_SEARCH_SCAN,
            )
        except Exception:
            return "History search is unavailable; check the query and journal."
        lines = [
            f"History search (at most {MAX_HISTORY_SEARCH_SCAN} events / "
            f"{MAX_HISTORY_SEARCH_BYTES} bytes checked; read-only, never replayed):"
        ]
        for entry in entries[:count]:
            if not isinstance(entry, dict):
                continue
            event_id = entry.get("id")
            event_kind = entry.get("kind")
            excerpt = entry.get("excerpt")
            if not isinstance(event_id, str) or not isinstance(event_kind, str) or not isinstance(excerpt, str):
                continue
            request_id = entry.get("request_id")
            request = f" request={request_id[:128]}" if isinstance(request_id, str) else ""
            offset = entry.get("offset")
            location = (
                f" around={offset}"
                if type(offset) is int and 0 <= offset <= HISTORY_MAX_OFFSET else ""
            )
            safe_excerpt = self._redact_history_text(excerpt[:400])
            lines.append(
                f"{event_id[:128]} {event_kind[:100]}{request}{location}: {safe_excerpt}"
            )
        if len(lines) == 1:
            lines.append("No matches.")
        return "\n".join(lines)

    def _history_page(self, tokens: list[str]) -> str:
        if not 1 <= len(tokens) <= 3 or re.fullmatch(r"e[0-9]{12}", tokens[0]) is None:
            return _HISTORY_USAGE
        offset = self._history_integer(
            tokens[1], maximum=HISTORY_MAX_OFFSET,
        ) if len(tokens) >= 2 else 0
        limit = self._history_integer(
            tokens[2], maximum=HISTORY_MAX_PAGE_CHARS,
        ) if len(tokens) >= 3 else min(2_000, HISTORY_MAX_PAGE_CHARS)
        if offset is None or limit is None or limit < 1:
            return _HISTORY_USAGE
        try:
            page = self._read_history_page(tokens[0], offset, limit)
        except KeyError:
            return "No history event matches that ID in this session."
        except Exception:
            return "History page is unavailable because the journal could not be read."
        content = page.get("content")
        next_offset = page.get("next_offset")
        total_chars = page.get("total_chars")
        if (not isinstance(content, str) or type(next_offset) is not int
                or type(total_chars) is not int or next_offset < offset
                or next_offset > HISTORY_MAX_OFFSET or total_chars < next_offset
                or total_chars > HISTORY_MAX_OFFSET):
            return "History page is unavailable because the journal returned invalid data."
        lines = [
            f"Journal event {tokens[0]} characters {offset}-{next_offset} of {total_chars} "
            "(read-only; history is never replayed):",
            content,
        ]
        if page.get("truncated") is True:
            lines.append(f"Next page: /history page {tokens[0]} {next_offset} {limit}")
        return "\n".join(lines)

    async def interrupt(self) -> None:
        if self.state in (State.STOPPING, State.CLOSED, State.NEW, State.FAILED):
            return
        if self.state is State.IDLE:
            await self._fail_pending_actions("interrupted", "Queued action cancelled by explicit interrupt")
            worker = self._queue_worker
            if worker is not None and worker is not asyncio.current_task():
                worker.cancel()
            return
        if self.state is State.GENERATING:
            # Invalidate before requesting cancellation. Even a provider that
            # suppresses cancellation cannot cause its late response to execute.
            self._active_operation_id = None
            await self._fail_pending_actions("interrupted", "Queued action cancelled by explicit interrupt")
            generation = self._generation
            if generation is not None and not generation.done():
                generation.cancel()
            elif self._active_task is not None and self._active_task is not asyncio.current_task():
                # Async transforms run before a provider task exists.
                self._active_task.cancel()
            return
        if self.state is State.COMMAND:
            # Commands may have external side effects too; cancel once and do
            # not advertise the session as safe to replay.
            self._active_operation_id = None
            self.state = State.FAILED
            await self._fail_pending_actions("interrupted", "Queued action cancelled by explicit interrupt")
            active = self._active_task
            if active is not None and active is not asyncio.current_task():
                active.cancel()
            return
        if self.state in (State.EXECUTING, State.WAITING_FOR_INPUT):
            # Invalidate before requesting cancellation so a late executor result
            # cannot be dispatched even if the executor ignores the interrupt.
            self._active_operation_id = None
            await self._fail_pending_actions("interrupted", "Queued action cancelled by explicit interrupt")
            if self._execution_active:
                # Stop the cell and let the cancelled operation choose its own
                # transition: a "cancelled" result means the executor kept a
                # usable namespace, anything else fails the session closed.
                await self.executor.interrupt()
            else:
                # The cell result is already recorded, but output delivery or
                # observer acknowledgement was cancelled; keep the fail-closed
                # transition instead of promising an unchanged session.
                self.state = State.FAILED
                active = self._active_task
                if active is not None and active is not asyncio.current_task():
                    active.cancel()

    async def close(self) -> None:
        task = asyncio.current_task()
        if task is self._active_task:
            raise RuntimeError("Cannot close a coordinator from an active submission")
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_impl(), name="py-agent-coordinator-close")
        await asyncio.shield(self._close_task)

    async def _close_impl(self) -> None:
        async with self._lifecycle_lock:
            if self.state is State.CLOSED:
                return
            was_executing = self._execution_active
            self.state = State.STOPPING
            await self._fail_pending_actions("closed", "Coordinator closed before queued action dispatch")
            worker = self._queue_worker
            if worker is not None and worker is not asyncio.current_task():
                worker.cancel()
            self._active_operation_id = None
            generation = self._generation
            if generation is not None and not generation.done():
                generation.cancel()
            active = self._active_task
            failure = None
            if active is not None and active is not asyncio.current_task():
                active.cancel()
                # Give cancellation a bounded chance to unwind. A provider may
                # suppress cancellation; its invalidated result cannot dispatch.
                done, _ = await asyncio.wait({active}, timeout=self._shutdown_timeout)
                if active in done:
                    await asyncio.gather(active, return_exceptions=True)
                else:
                    if was_executing:
                        try:
                            await self.executor.interrupt()
                        except BaseException:
                            pass
                    done, _ = await asyncio.wait({active}, timeout=min(self._shutdown_timeout, 1.0))
                    if active in done:
                        await asyncio.gather(active, return_exceptions=True)
                    elif not self._journal_failed:
                        # The operation is now explicitly marked as uncertain or
                        # cancelled before its journal is closed. Late completions
                        # cannot overwrite that terminal evidence or be replayed.
                        try:
                            if (self._active_execution_request is not None
                                    and not self._active_execution_result_recorded):
                                self._record_uncertain_execution(
                                    self._active_execution_request,
                                    "Coordinator shutdown timed out; execution outcome may be uncertain",
                                )
                            if (self._active_model_request is not None
                                    and not self._active_provider_usage_recorded):
                                self._record_provider_usage(
                                    self._active_model_request, None, outcome="cancelled",
                                )
                        except BaseException as exc:
                            failure = exc
            if worker is not None and worker is not active and worker is not asyncio.current_task():
                done, _ = await asyncio.wait({worker}, timeout=self._shutdown_timeout)
                if worker in done:
                    await asyncio.gather(worker, return_exceptions=True)
            try:
                await self._close_executor()
            except BaseException as exc:
                if failure is None:
                    failure = exc
            try:
                self._close_journal(State.STOPPING.value)
            except BaseException as exc:
                if failure is None:
                    failure = exc
                elif hasattr(failure, "add_note"):
                    failure.add_note(f"Journal shutdown also failed: {type(exc).__name__}")
            finally:
                self.state = State.CLOSED
            if failure is not None:
                raise failure
