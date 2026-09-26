"""Bounded output-observer example using the public coordinator contract."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from py_agent.configuration import ConfigField, Scalar
from py_agent.contracts import OutputEvent
from py_agent.plugins import Contributions, ObserverContribution, PluginManifest, hookimpl

PLUGIN_ID = "example-output-observer"


class BoundedOutputObserver:
    """A bounded FIFO: awaited producers wait for a slow consumer."""

    def __init__(self, maxsize: int = 1):
        if type(maxsize) is not int or maxsize < 1:
            raise ValueError("maxsize must be a positive integer")
        self._events: asyncio.Queue[OutputEvent] = asyncio.Queue(maxsize=maxsize)

    async def observe(self, event: OutputEvent) -> None:
        if not isinstance(event, OutputEvent):
            raise TypeError("observer input must be an OutputEvent")
        await self._events.put(event)

    async def receive(self) -> OutputEvent:
        return await self._events.get()

    def acknowledge(self) -> None:
        self._events.task_done()

    async def join(self) -> None:
        await self._events.join()


def _observer_factory(config: Mapping[str, Scalar]) -> BoundedOutputObserver:
    return BoundedOutputObserver(config["queue_size"])


class OutputObserverPlugin:
    @hookimpl
    def py_agent_register(self) -> Contributions:
        return Contributions(
            manifest=PluginManifest(PLUGIN_ID),
            observers=(ObserverContribution("bounded", _observer_factory, critical=False),),
            config_fields=(
                ConfigField(
                    f"{PLUGIN_ID}.queue_size",
                    PLUGIN_ID,
                    int,
                    1,
                    documentation="Capacity of the example observer's bounded FIFO.",
                    minimum=1,
                ),
            ),
        )


plugin = OutputObserverPlugin()
