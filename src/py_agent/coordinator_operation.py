"""Single owner of a submission's identity and side-effect evidence."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .contracts import ExecutionRequest, ModelRequest


@dataclass
class Operation:
    task: asyncio.Task[Any] | None = None
    identity: str | None = None
    model_request: ModelRequest | None = None
    provider_usage_recorded: bool = False
    execution_request: ExecutionRequest | None = None
    execution_result_recorded: bool = False
    execution_active: bool = False
    execution_outcome_status: str | None = None

    def begin(self, identity: str, task: asyncio.Task[Any] | None) -> None:
        if self.identity is not None:
            raise RuntimeError("An operation already owns this session")
        self.task = task
        self.identity = identity
        self.model_request = None
        self.provider_usage_recorded = False
        self.execution_request = None
        self.execution_result_recorded = False
        self.execution_active = False
        self.execution_outcome_status = None

    def invalidate(self) -> None:
        """Reject late output without erasing evidence needed by shutdown."""
        self.identity = None

    def finish(self, identity: str, task: asyncio.Task[Any] | None) -> None:
        if self.task is task:
            self.task = None
        if self.identity == identity:
            self.identity = None

    def current(self, identity: str) -> bool:
        return self.identity == identity

    def begin_model(self, request: ModelRequest) -> None:
        self.model_request = request
        self.provider_usage_recorded = False

    def begin_execution(self, request: ExecutionRequest) -> None:
        self.execution_request = request
        self.execution_result_recorded = False
        self.execution_outcome_status = None
        self.execution_active = True
