"""An externally contributed decorator for the public Executor protocol."""

from __future__ import annotations

import asyncio

from py_agent.contracts import Executor, ExecutorCapabilities, ExecutionRequest, ExecutionResult
from py_agent.plugins import Contributions, ExecutorWrapperContribution, PluginManifest, hookimpl

PLUGIN_ID = "example-executor-wrapper"


class ExecutionAuditWrapper:
    """Forward lifecycle and each execution once, preserving delegate results.

    Cancellation and delegate errors propagate unchanged; the wrapper never
    retries side-effecting execution.
    """

    def __init__(self, delegate: Executor):
        required = ("start", "execute", "interrupt", "close")
        if any(not callable(getattr(delegate, name, None)) for name in required):
            raise TypeError("delegate does not implement the public Executor methods")
        capabilities = getattr(delegate, "capabilities", None)
        if not isinstance(capabilities, ExecutorCapabilities):
            raise TypeError("delegate must declare ExecutorCapabilities")
        self.delegate = delegate
        self.capabilities = capabilities
        self.execution_count = 0

    async def start(self) -> None:
        await self.delegate.start()

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.execution_count += 1
        try:
            return await self.delegate.execute(request)
        except asyncio.CancelledError:
            raise

    async def interrupt(self) -> None:
        await self.delegate.interrupt()

    async def close(self) -> None:
        await self.delegate.close()


class ExecutorWrapperPlugin:
    @hookimpl
    def py_agent_register(self) -> Contributions:
        return Contributions(
            manifest=PluginManifest(PLUGIN_ID),
            executor_wrappers=(ExecutorWrapperContribution("audit", ExecutionAuditWrapper),),
        )


plugin = ExecutorWrapperPlugin()
