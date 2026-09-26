"""External command and async context/model transformation examples."""

from __future__ import annotations

from collections.abc import Mapping

from py_agent.configuration import ConfigField, Scalar
from py_agent.contracts import ContextSnapshot, ModelRequest
from py_agent.plugins import (
    CommandContribution,
    Contributions,
    PluginManifest,
    TransformContribution,
    hookimpl,
)

PLUGIN_ID = "example-command-context"


class GreetingCommand:
    """Small command handler callable through the coordinator registry."""

    def __init__(self, greeting: str = "hello"):
        if not isinstance(greeting, str):
            raise TypeError("greeting must be text")
        self.greeting = greeting

    def execute(self, arguments: str = "") -> str:
        if not isinstance(arguments, str):
            raise TypeError("command arguments must be text")
        name = arguments.strip()
        if not name:
            return self.greeting
        return f"{self.greeting}, {name}"


class PrefixContextTransform:
    """Async context stage returning a fresh public ContextSnapshot."""

    def __init__(self, prefix: str):
        if not isinstance(prefix, str) or not prefix.strip():
            raise ValueError("prefix must be nonempty text")
        self.prefix = prefix

    async def transform(self, snapshot: ContextSnapshot) -> ContextSnapshot:
        if not isinstance(snapshot, ContextSnapshot):
            raise TypeError("snapshot must be a ContextSnapshot")
        return ContextSnapshot(
            snapshot.epoch,
            (("system", self.prefix), *snapshot.messages),
            (None, *snapshot.message_phases),
            snapshot.transform_trace,
        )


class RequestTagTransform:
    """Async model-request stage adding a namespaced, non-secret option."""

    def __init__(self, tag: str):
        if not isinstance(tag, str):
            raise TypeError("tag must be text")
        self.tag = tag

    async def transform(self, request: ModelRequest) -> ModelRequest:
        if not isinstance(request, ModelRequest):
            raise TypeError("request must be a ModelRequest")
        return ModelRequest(
            request.origin,
            request.context,
            request.model,
            {**request.options, "example_request_tag": self.tag},
            request.transform_trace,
        )


def _greeting_factory(config: Mapping[str, Scalar]) -> GreetingCommand:
    return GreetingCommand(config["greeting"])


def _context_factory(config: Mapping[str, Scalar]) -> PrefixContextTransform:
    return PrefixContextTransform(config["context_prefix"])


def _request_factory(config: Mapping[str, Scalar]) -> RequestTagTransform:
    return RequestTagTransform(config["request_tag"])


class CommandContextPlugin:
    @hookimpl
    def py_agent_register(self) -> Contributions:
        return Contributions(
            manifest=PluginManifest(PLUGIN_ID),
            commands=(CommandContribution(
                "greet", _greeting_factory,
                summary="Greet a name", usage="/greet [name]",
            ),),
            transforms=(
                TransformContribution("context", "prefix", _context_factory),
                TransformContribution("model-request", "tag", _request_factory),
            ),
            config_fields=(
                ConfigField(
                    f"{PLUGIN_ID}.greeting",
                    PLUGIN_ID,
                    str,
                    "hello",
                    documentation="Greeting used by /greet.",
                ),
                ConfigField(
                    f"{PLUGIN_ID}.context_prefix",
                    PLUGIN_ID,
                    str,
                    "External example context:",
                    documentation="System prefix added by the context pipeline stage.",
                ),
                ConfigField(
                    f"{PLUGIN_ID}.request_tag",
                    PLUGIN_ID,
                    str,
                    "external-example",
                    documentation="Tag added to transformed model requests.",
                ),
            ),
        )


plugin = CommandContextPlugin()
