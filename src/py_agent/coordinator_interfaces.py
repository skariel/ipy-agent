"""Narrow structural dependencies for request phases; no facade dependency."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from .contracts import ExecutionResult, ProviderService, SayOutput
from .coordinator_frontend import FrontendRouting
from .coordinator_journal import JournalPolicy
from .coordinator_lifecycle import ExecutionLifecycle
from .runtime_status import Activity, Recovery


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
    def _visible_says(self, result: ExecutionResult) -> tuple[str, ...]: ...
    def _visible_say_outputs(self, result: ExecutionResult) -> tuple[SayOutput, ...]: ...
