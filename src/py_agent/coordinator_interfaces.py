"""Narrow structural dependencies for request phases; no facade dependency."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .configuration import ConfigSnapshot, ConfigStore
from .contracts import (
    AgentDecision,
    ContextSnapshot,
    ExecutionResult,
    ModelRequest,
    ModelResponse,
    Origin,
    ProviderService,
    QueueOutcome,
    RoutedAction,
    Router,
    SayOutput,
)
from .coordinator_conversation import ConversationState
from .coordinator_frontend import FrontendRouting
from .coordinator_journal import JournalPolicy
from .coordinator_lifecycle import ExecutionLifecycle
from .coordinator_observations import ModelObservations
from .coordinator_support import _QueuedAction
from .runtime_status import Activity, Recovery
from .session_journal import JournalService


class ModelRuntime(Protocol):
    """Capabilities needed by transport retry/overflow handling only."""

    @property
    def provider(self) -> ProviderService: ...
    @property
    def context_service(self) -> object: ...
    @property
    def frontend(self) -> FrontendRouting: ...
    @property
    def journal_policy(self) -> JournalPolicy: ...
    @property
    def lifecycle(self) -> ExecutionLifecycle: ...
    @property
    def retry_waiter(self) -> Callable[[float, str], Awaitable[None]]: ...

    activity: Activity | None
    recovery: Recovery | None


class DirectRuntime(Protocol):
    """Capabilities needed to dispatch/publish one direct user cell."""

    @property
    def frontend(self) -> FrontendRouting: ...
    @property
    def lifecycle(self) -> ExecutionLifecycle: ...
    def visible_says(self, result: ExecutionResult) -> tuple[str, ...]: ...
    def visible_say_outputs(self, result: ExecutionResult) -> tuple[SayOutput, ...]: ...


class Interpreter(Protocol):
    def interpret(self, response: ModelResponse) -> AgentDecision: ...


class RequestRuntime(ModelRuntime, DirectRuntime, Protocol):
    """Session capabilities used by admission and request phases.

    No facade dependency and no access to component-owned locks or queues.
    Optional context extensions are discovered by ConversationState/the phases.
    """

    @property
    def conversation(self) -> ConversationState: ...
    @property
    def observation_policy(self) -> ModelObservations: ...
    @property
    def router(self) -> Router: ...
    @property
    def interpreter(self) -> Interpreter: ...
    @property
    def executor(self) -> object: ...
    @property
    def journal(self) -> JournalService: ...
    @property
    def config_store(self) -> ConfigStore | None: ...
    @property
    def max_agent_steps(self) -> int: ...
    @property
    def session_id(self) -> str: ...
    @property
    def model(self) -> str: ...

    config_revision: int
    execution_outcome: str
    model_transform: Callable[
        [ModelRequest, ConfigSnapshot | None, ConfigSnapshot | None, ConfigSnapshot | None],
        Awaitable[ModelRequest],
    ]

    def current_config(self) -> ConfigSnapshot | None: ...
    def model_options(self, config: ConfigSnapshot | None) -> dict[str, str]: ...
    async def run_context_transforms(
        self,
        snapshot: ContextSnapshot,
        request_config: ConfigSnapshot | None,
        cell_config: ConfigSnapshot | None,
        epoch_config: ConfigSnapshot | None,
    ) -> ContextSnapshot: ...
    def validate_routed_action(self, routed: object, origin: Origin) -> RoutedAction: ...
    def complete_queue_item(self, item: _QueuedAction, outcome: QueueOutcome) -> None: ...
    async def dispatch_command(self, source: str, config: ConfigSnapshot | None) -> str: ...
    def execution_llm_handler(self, origin: Origin) -> Callable[[Any], Awaitable[str]]: ...
