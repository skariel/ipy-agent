"""Request-bound input, output and nested-model adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import inspect
from typing import TYPE_CHECKING, Any, Literal, Protocol

from .contracts import (
    ExecutionOutput,
    InputHandler,
    InputReply,
    InputRequest,
    InputUnavailableError,
    ModelResponse,
    Origin,
    ProgressCallback,
    UserAction,
)
from .coordinator_request import RequestScope
from .coordinator_support import (
    MAX_PENDING_ACTIONS as MAX_PENDING_ACTIONS,
)
from .coordinator_support import (
    State,
)

if TYPE_CHECKING:
    from .coordinator_interfaces import RequestRuntime


class _Router(Protocol):
    def route(self, action: UserAction) -> object: ...


class _Interpreter(Protocol):
    def interpret(self, response: ModelResponse) -> object: ...


class RequestIO:
    """Frontend input and output adapters bound to one operation."""

    def __init__(self, coordinator: RequestRuntime, scope: RequestScope) -> None:
        self.coordinator = coordinator
        self.scope = scope

    def input(self, origin: Origin) -> InputHandler | None:
        return self.execution_input_handler(
            origin,
            enabled=self.scope.allow_stdin,
            handler=self.scope.input_handler,
            owner=self.scope.origin.frontend_id,
        )

    def output(
        self,
        origin: Origin,
        author: Literal["user", "agent"],
    ) -> Callable[[ExecutionOutput], Awaitable[None]] | None:
        return self.execution_output_handler(origin, author, progress=self.scope.on_progress)

    def llm(self, origin: Origin) -> Callable[[Any], Awaitable[str]]:
        return self.coordinator.execution_llm_handler(origin)

    def execution_input_handler(
        self,
        execution_origin: Origin,
        *,
        enabled: bool,
        handler: InputHandler | None,
        owner: str,
    ) -> InputHandler | None:
        coordinator = self.coordinator
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

    def execution_output_handler(
        self,
        execution_origin: Origin,
        author: Literal["user", "agent"],
        *,
        progress: ProgressCallback | None,
    ) -> Callable[[ExecutionOutput], Awaitable[None]] | None:
        coordinator = self.coordinator
        if progress is None:
            return None

        async def deliver(output: ExecutionOutput) -> None:
            if output.kind != "stream" or not coordinator.lifecycle.operation_is_current(
                self.scope.operation_id, State.EXECUTING
            ):
                raise asyncio.CancelledError from None
            event = coordinator.frontend.new_output_event(
                execution_origin,
                "stream",
                {**dict(output.data), "author": author},
                metadata={"provisional": True},
                author=author,
            )
            # Frontend-only provisional preview: never add it to model
            # context, journal, or observer history. The completed cell
            # result remains the single authoritative output record.
            delivered = progress(event)
            if not inspect.isawaitable(delivered):
                raise TypeError("Request progress callback must be async")
            await delivered
            if not coordinator.lifecycle.operation_is_current(self.scope.operation_id, State.EXECUTING):
                raise asyncio.CancelledError from None

        return deliver
