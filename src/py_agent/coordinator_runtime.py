"""Typed session dependencies and request policies; independent of the facade."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
import inspect
import json
import shlex
from types import CodeType, MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, cast

from .configuration import ApplyAt, ConfigSnapshot, ConfigStore, Scalar
from .contracts import (
    MAX_QUEUED_ACTION_CHARS,
    CompletenessResult,
    ContextSnapshot,
    ExecutionRequest,
    ExecutionResult,
    Executor,
    ModelRequest,
    Origin,
    ProviderService,
    QueueOutcome,
    RoutedAction,
    Router,
    SayOutput,
)
from .contracts import (
    Submission as Submission,
)
from .coordinator_support import (
    _EFFORT_PRESETS,
    _EFFORT_USAGE,
    _MODEL_USAGE,
    HISTORY_MAX_ITEMS,
    _QueuedAction,
)
from .coordinator_support import (
    MAX_PENDING_ACTIONS as MAX_PENDING_ACTIONS,
)
from .coordinator_support import (
    State as State,
)
from .plugins import PluginRuntime, RegisteredCommand, RegisteredObserver
from .runtime_status import Activity, Recovery
from .session_journal import JournalService

_Service = TypeVar('_Service')


class _CommandRegistry(Protocol):
    def dispatch(self, name: str, arguments: str) -> object: ...


if TYPE_CHECKING:
    from .coordinator_conversation import ConversationState
    from .coordinator_frontend import FrontendRouting
    from .coordinator_journal import JournalPolicy
    from .coordinator_lifecycle import ExecutionLifecycle
    from .coordinator_observations import ModelObservations
    from .coordinator_runner import RequestRunner

class SessionRuntime:
    """Explicit shared dependencies, policies and component links for one session."""
    _effort_override: str | None
    _observer_registrations: tuple[RegisteredObserver, ...]
    _plugin_runtime: PluginRuntime
    _restart_config: ConfigSnapshot | None
    _shutdown_timeout: float
    activity: Activity | None
    command_registry: object
    config_revision: int
    config_store: ConfigStore | None
    context_service: object
    execution_outcome: str
    executor: Executor
    external_commands: Mapping[str, RegisteredCommand]
    interpreter: object
    journal: JournalService
    max_agent_steps: int
    model: str
    observations: object
    output_observers: MappingProxyType[str, object]
    provider: ProviderService
    provider_id: str
    recovery: Recovery | None
    router: Router
    session_id: str
    conversation: ConversationState
    frontend: FrontendRouting
    journal_policy: JournalPolicy
    lifecycle: ExecutionLifecycle
    observation_policy: ModelObservations
    runner: RequestRunner

    def __init__(self) -> None:
        # Components are wired by the composition root before any request starts.
        self.model_transform: Callable[[ModelRequest, ConfigSnapshot | None, ConfigSnapshot | None, ConfigSnapshot | None], Awaitable[ModelRequest]] = self._run_model_transforms
        self.retry_waiter: Callable[[float, str], Awaitable[None]] = self._wait_provider_retry

    @staticmethod
    def _sync_factory(factory: Callable[..., _Service], *args: object) -> _Service:
        value = factory(*args)
        if inspect.isawaitable(value):
            close = getattr(value, 'close', None)
            if callable(close):
                close()
            raise TypeError('Plugin factories must be synchronous')
        return value

    def _current_config(self) -> ConfigSnapshot | None:
        if self.config_store is None:
            return None
        snapshot = self.config_store.snapshot
        if not isinstance(snapshot, ConfigSnapshot):
            raise TypeError('Configuration store returned an invalid snapshot')
        return snapshot

    def _plugin_config(self, plugin_id: str, request_snapshot: ConfigSnapshot | None, cell_snapshot: ConfigSnapshot | None, epoch_snapshot: ConfigSnapshot | None, immediate_snapshot: ConfigSnapshot | None) -> Mapping[str, Scalar]:
        """Resolve one factory namespace using each field's declared boundary.

            ``request_snapshot`` is fixed for a submission. ``cell_snapshot`` is
            captured once per command/model cell, ``epoch_snapshot`` advances only
            when the context epoch does, and immediate settings are sampled just
            before each factory call. Observer factories pass the creation snapshot
            for every boundary because observer instances are long-lived.
            """
        snapshots = {ApplyAt.IMMEDIATE: immediate_snapshot, ApplyAt.REQUEST: request_snapshot, ApplyAt.CELL: cell_snapshot, ApplyAt.EPOCH: epoch_snapshot, ApplyAt.RESTART: self._restart_config}
        values = {}
        for name, field in self._plugin_runtime.config.fields.items():
            if field.owner != plugin_id:
                continue
            snapshot = snapshots[field.apply_at]
            entry = snapshot.entries.get(name) if snapshot is not None else None
            values[name.removeprefix(plugin_id + '.')] = field.default if entry is None else entry.value
        return MappingProxyType(values)

    async def _run_context_transforms(self, snapshot: ContextSnapshot, request_config: ConfigSnapshot | None, cell_config: ConfigSnapshot | None, epoch_config: ConfigSnapshot | None) -> ContextSnapshot:
        current = snapshot
        trace = list(snapshot.transform_trace)
        for registration in self._plugin_runtime.transforms['context']:
            immediate_config = self._current_config()
            stage = self._sync_factory(registration.create, self._plugin_config(registration.plugin_id, request_config, cell_config, epoch_config, immediate_config))
            transform = getattr(stage, 'transform', None)
            if not callable(transform):
                raise TypeError(f'Context transform {registration.qualified_name} must expose transform(snapshot)')
            result = transform(current)
            if not inspect.isawaitable(result):
                raise TypeError(f'Context transform {registration.qualified_name} must be async')
            candidate = await result
            if not isinstance(candidate, ContextSnapshot):
                raise TypeError(f'Context transform {registration.qualified_name} must return ContextSnapshot')
            if candidate.epoch != current.epoch:
                raise ValueError(f'Context transform {registration.qualified_name} cannot change the context epoch')
            trace.append(registration.qualified_name)
            current = replace(candidate, transform_trace=tuple(trace))
        return current

    async def _run_model_transforms(self, request: ModelRequest, request_config: ConfigSnapshot | None, cell_config: ConfigSnapshot | None, epoch_config: ConfigSnapshot | None) -> ModelRequest:
        current = request
        trace = list(request.transform_trace)
        for registration in self._plugin_runtime.transforms['model-request']:
            immediate_config = self._current_config()
            stage = self._sync_factory(registration.create, self._plugin_config(registration.plugin_id, request_config, cell_config, epoch_config, immediate_config))
            transform = getattr(stage, 'transform', None)
            if not callable(transform):
                raise TypeError(f'Model transform {registration.qualified_name} must expose transform(request)')
            result = transform(current)
            if not inspect.isawaitable(result):
                raise TypeError(f'Model transform {registration.qualified_name} must be async')
            candidate = await result
            if not isinstance(candidate, ModelRequest):
                raise TypeError(f'Model transform {registration.qualified_name} must return ModelRequest')
            if candidate.origin != current.origin:
                raise ValueError(f'Model transform {registration.qualified_name} cannot change request origin')
            if candidate.context.epoch != current.context.epoch:
                raise ValueError(f'Model transform {registration.qualified_name} cannot change the context epoch')
            context = replace(candidate.context, transform_trace=current.context.transform_trace)
            trace.append(registration.qualified_name)
            current = replace(candidate, context=context, transform_trace=tuple(trace))
        return current

    @staticmethod
    def _validate_execution_result(request: ExecutionRequest, result: ExecutionResult) -> None:
        if not isinstance(result, ExecutionResult):
            raise TypeError('Executor must return an ExecutionResult')
        if result.origin != request.origin:
            raise RuntimeError('Executor returned a result for a different origin')
        if result.status not in ('success', 'error', 'cancelled', 'uncertain'):
            raise RuntimeError(f'Executor returned unknown status: {result.status!r}')
        if not isinstance(result.stdout, str) or not isinstance(result.stderr, str):
            raise TypeError('Executor output streams must be text')
        if result.error is not None and (not isinstance(result.error, str)):
            raise TypeError('Executor error must be text or None')
        if type(result.final) is not bool:
            raise TypeError('Execution final marker must be a boolean')

    @staticmethod
    def _query_prefix_offset(code: str, routed: RoutedAction) -> int:
        return 1 if code.startswith('@') and routed.kind == 'execute' and (routed.source == code[1:]) else 0

    @staticmethod
    def _check_query_cursor(code: str, cursor_pos: int) -> None:
        if not isinstance(code, str):
            raise TypeError('Query source must be text')
        if type(cursor_pos) is not int or not 0 <= cursor_pos <= len(code):
            raise ValueError('Query cursor position is outside the source')

    @staticmethod
    def _parse_completeness(source: str) -> CompletenessResult:
        try:
            from IPython.core.inputtransformer2 import TransformerManager
            status, indent = cast(Callable[[], Any], TransformerManager)().check_complete(source)
            spaces = ' ' * min(indent, 80) if type(indent) is int and indent > 0 else ''
            return CompletenessResult(status, spaces)
        except ImportError:
            try:
                compiled: CodeType | None = compile(source, '<jupyter-is-complete>', 'exec')
                del compiled
            except (SyntaxError, OverflowError, ValueError):
                try:
                    import codeop
                    compiled = codeop.compile_command(source, symbol='exec')
                except (SyntaxError, OverflowError, ValueError):
                    return CompletenessResult('invalid')
                return CompletenessResult('incomplete' if compiled is None else 'invalid')
            return CompletenessResult('complete')
        except (SyntaxError, OverflowError, ValueError):
            return CompletenessResult('invalid')

    @staticmethod
    def _say_text(content: object) -> str:
        if isinstance(content, str):
            return content
        return json.dumps(content, ensure_ascii=False, allow_nan=False, separators=(',', ':'))

    @staticmethod
    def _visible_say_outputs(result: ExecutionResult) -> tuple[SayOutput, ...]:
        return tuple(output for output in result.say_outputs if not output.final or result.status == 'success')

    @classmethod
    def _visible_says(cls, result: ExecutionResult) -> tuple[str, ...]:
        return tuple(cls._say_text(output.content) for output in cls._visible_say_outputs(result))

    def _model_options(self, snapshot: ConfigSnapshot | None) -> dict[str, str]:
        options: dict[str, str] = {}
        if snapshot is not None:
            max_tokens = snapshot.entries.get('model.max_tokens')
            if self.provider_id == 'litelm' and max_tokens is not None and (type(max_tokens.value) is int) and (max_tokens.value > 0):
                options['max_tokens'] = str(max_tokens.value)
        if self._effort_override is not None and self.provider_id in {'litelm', 'codex'}:
            options['effort'] = self._effort_override
        elif snapshot is not None and self.provider_id in {'codex', 'litelm'}:
            effort = snapshot.entries.get('model.effort')
            if effort is not None and isinstance(effort.value, str) and (self.provider_id == 'codex' or effort.source != 'default'):
                options['effort'] = effort.value
        return options

    @staticmethod
    def _validate_routed_action(routed: object, origin: Origin) -> RoutedAction:
        if not isinstance(routed, RoutedAction):
            raise TypeError('Router must return a RoutedAction')
        if routed.origin != origin:
            raise ValueError('Router changed request origin')
        if routed.kind not in ('ask', 'execute', 'command'):
            raise ValueError(f'Router returned unknown action kind: {routed.kind!r}')
        if not isinstance(routed.source, str) or not routed.source.strip():
            raise ValueError('Router returned an empty or invalid source')
        if len(routed.source) > MAX_QUEUED_ACTION_CHARS:
            raise ValueError('Routed action exceeds the queued-action size limit')
        if routed.language is not None and (not isinstance(routed.language, str)):
            raise TypeError('Routed action language must be text or None')
        return routed

    @staticmethod
    def _complete_queue_item(item: _QueuedAction, outcome: QueueOutcome) -> None:
        if not item.completion.done():
            item.completion.set_result(outcome)

    async def _dispatch_command(self, source: str, config: ConfigSnapshot | None) -> str:
        parts = source.split(None, 1)
        name = parts[0] if parts else ''
        arguments = parts[1].strip() if len(parts) > 1 else ''
        core_commands = getattr(self.command_registry, 'commands', {})
        try:
            if name == 'resume':
                return 'No recoverable model request is pending. Python cells are never replayed.'
            if name == 'recovery':
                if arguments == 'discard':
                    self.recovery = None
                    return 'Pending recovery discarded. Completed execution is unchanged.'
                if arguments:
                    return 'Usage: /recovery [discard]'
                return self.recovery.text() if self.recovery else self.execution_outcome
            if name == 'history':
                self.conversation._capture_history_sensitive_config(self._current_config())
                return await self.conversation._history_command(arguments)
            if name == 'context':
                return self.conversation._context_command(arguments)
            if name == 'model':
                return self._model_command(arguments)
            if name in {'effort', 'think'}:
                return self._effort_command(arguments, config)
            if name in core_commands:
                response = cast(_CommandRegistry, self.command_registry).dispatch(name, arguments)
            elif name in self.external_commands:
                registration = self.external_commands[name]
                self.conversation._observe_context_epoch(self.conversation._read_context_epoch())
                cell_config = self._current_config()
                command = self._sync_factory(registration.create, self._plugin_config(registration.plugin_id, config, cell_config, self.conversation._epoch_config, cell_config))
                execute = getattr(command, 'execute', None)
                if not callable(execute):
                    return 'Command service returned an invalid handler.'
                response = execute(arguments)
                if inspect.isawaitable(response):
                    response = await response
            elif self.command_registry is None and (not self.external_commands):
                return 'Command service is not configured'
            else:
                return 'Unknown command'
        except asyncio.CancelledError:
            raise
        except Exception:
            return 'Command failed; no configuration change was committed.'
        if isinstance(response, str):
            return response
        ok, message = (getattr(response, 'ok', None), getattr(response, 'text', None))
        if type(ok) is not bool or not isinstance(message, str):
            return 'Command service returned an invalid response.'
        return message

    def _model_command(self, arguments: str) -> str:
        if not isinstance(arguments, str) or len(arguments) > 512:
            return _MODEL_USAGE
        try:
            tokens = shlex.split(arguments, posix=True)
        except ValueError:
            return _MODEL_USAGE
        if not tokens:
            from .model_catalog import available_models
            from .provider import ProviderError
            current = f'Model: {self.model} (provider: {self.provider_id})'
            if self.provider_id not in {'litelm', 'codex'}:
                return current + '\nLive model changes are unavailable for the selected provider.'
            if not hasattr(self.provider, 'adapter'):
                return current
            try:
                models = available_models(getattr(self.provider.adapter, 'auth_file', None))
            except ProviderError as exc:
                return current + f'\nCannot list available models: {exc}'
            return current + '\nAvailable models (configured credentials; account access may vary):\n' + ('\n'.join(models) if models else 'None. Use /login or py login PROVIDER, then retry.')
        if len(tokens) != 1:
            return _MODEL_USAGE
        setter = getattr(self.provider, 'set_model', None)
        if not callable(setter):
            return 'Live model changes are unavailable for the selected provider.'
        previous = self.model
        try:
            setter(tokens[0])
            selected = getattr(self.provider, 'model', None)
            if not isinstance(selected, str) or not selected:
                raise TypeError('provider returned invalid model state')
        except (TypeError, ValueError) as exc:
            return f'Model was not changed: {exc}'
        self.model = selected
        if self.provider_id in {'litelm', 'codex'}:
            self.provider_id = getattr(self.provider, 'provider_id', None) or self.provider_id
        return f'Model changed for this session: {previous} -> {selected}'

    def _configured_effort(self, config: ConfigSnapshot | None) -> str | None:
        if self.provider_id not in {'codex', 'litelm'} or config is None:
            return None
        entry = config.entries.get('model.effort')
        if self.provider_id == 'litelm' and entry is not None and (entry.source == 'default'):
            return None
        return entry.value if entry is not None and isinstance(entry.value, str) else None

    async def _wait_provider_retry(self, delay: float, operation_id: str) -> None:
        """Bounded backoff, responsive to interrupts even after generation ended."""
        deadline = asyncio.get_running_loop().time() + delay
        while True:
            if not self.lifecycle._operation_is_current(operation_id, State.GENERATING):
                raise asyncio.CancelledError from None
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.1, remaining))

    @property
    def effective_effort(self) -> str:
        """Current session effort, using the same precedence as model requests."""
        if self.provider_id not in {'codex', 'litelm'}:
            return 'unavailable'
        snapshot = self.config_store.snapshot if self.config_store is not None else None
        effort = self._effort_override or self._configured_effort(snapshot)
        if effort is not None:
            return effort
        adapter = getattr(self.provider, 'adapter', None)
        fallback = getattr(adapter, 'effort', None) if self.provider_id == 'codex' else None
        return fallback if isinstance(fallback, str) and fallback else 'default'

    def _effort_command(self, arguments: str, config: ConfigSnapshot | None) -> str:
        if not isinstance(arguments, str) or len(arguments) > 32:
            return _EFFORT_USAGE
        value = arguments.strip().lower()
        if not value:
            effective = self._effort_override or self._configured_effort(config) or 'provider default'
            return f"Effort: {effective}. Presets: {', '.join(_EFFORT_PRESETS)}"
        if value not in _EFFORT_PRESETS or any(character.isspace() for character in value):
            return _EFFORT_USAGE
        if self.provider_id not in {'litelm', 'codex'}:
            return 'Effort presets are unavailable for the selected provider.'
        self._effort_override = value
        note = ' DeepSeek maps minimal/low to low, medium/high to high, xhigh to max, and none disables thinking.' if self.provider_id == 'litelm' and self.model.startswith('deepseek/') else ''
        return f'Effort changed for this session: {value}.{note}'

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
