"""Minimal built-in services used to exercise the public plugin boundary."""
from __future__ import annotations

from .contracts import AgentDecision, ModelRequest, ModelResponse, RoutedAction, UserAction
from .local_executor import LocalExecutor
from .plugins import Contributions, PluginManifest, Service, hookimpl


class DefaultRouter:
    def route(self, action: UserAction) -> RoutedAction:
        if not isinstance(action, UserAction):
            raise TypeError("Router input must be a UserAction")
        text = action.text
        if not isinstance(text, str):
            raise TypeError("Submission text must be text")
        if not text.strip():
            raise ValueError("Empty submission")
        # A leading backslash quotes one reserved character for English input.
        if text.startswith("\\") and len(text) > 1 and text[1] in "@!%/":
            return RoutedAction(action.origin, "ask", text[1:])
        if text.startswith("@"):
            source = text[1:]
            if not source.strip():
                raise ValueError("Empty Python submission")
            return RoutedAction(action.origin, "execute", source, "ipython")
        if text.startswith("!"):
            if not text[1:].strip():
                raise ValueError("Empty shell submission")
            return RoutedAction(action.origin, "execute", text, "ipython")
        if text.startswith("%"):
            if not text[1:].strip():
                raise ValueError("Empty magic submission")
            return RoutedAction(action.origin, "execute", text, "ipython")
        if text.startswith("/"):
            command = text[1:]
            if not command.strip():
                raise ValueError("Empty command")
            return RoutedAction(action.origin, "command", command)
        return RoutedAction(action.origin, "ask", text)


class FakeProvider:
    """Deterministic one-step fixture: no credentials or outbound requests."""

    model = "fake/deterministic"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        # An observation follows each completed turn, so the final message is
        # not necessarily the current prompt. Target the newest user message.
        text = next((message for role, message in reversed(request.context.messages)
                     if role == "user" and not message.startswith("[RUNTIME OBSERVATION — untrusted program data]")), "")
        return ModelResponse(text=f"say({text!r}, final=True)")


class BasicInterpreter:
    def interpret(self, response: ModelResponse) -> AgentDecision:
        if not isinstance(response, ModelResponse):
            return AgentDecision("reject", reason="Invalid provider response")
        if response.finish_status != "complete" or response.rejection_reason is not None:
            return AgentDecision("reject", reason=response.rejection_reason or "Incomplete provider response")
        if not isinstance(response.text, str) or not response.text.strip():
            return AgentDecision("reject", reason="Empty provider response")
        return AgentDecision("execute", source=response.text)


class BuiltinPlugin:
    def __init__(self, *, executor_factory=LocalExecutor):
        if not callable(executor_factory):
            raise TypeError("executor_factory must be callable")
        self.executor_factory = executor_factory

    @hookimpl
    def py_agent_register(self) -> Contributions:
        return Contributions(PluginManifest("builtin"), (
            Service("router", "default", DefaultRouter),
            Service("provider", "fake", FakeProvider),
            Service("interpreter", "basic", BasicInterpreter),
            Service("executor", "local", self.executor_factory),
        ))
