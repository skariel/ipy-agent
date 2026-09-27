"""Minimal built-in services used to exercise the public plugin boundary."""
from __future__ import annotations

import ast
import re
import tokenize

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
            # Only command identifiers use the slash-command route. Pasted
            # absolute paths/logs must remain user input, including their full
            # multiline body, rather than disappearing as "Unknown command".
            name = command.split(None, 1)[0]
            if re.fullmatch(r"[a-z][a-z0-9_-]*", name):
                return RoutedAction(action.origin, "command", command)
        return RoutedAction(action.origin, "ask", text)


class FakeProvider:
    """Deterministic one-step fixture: no credentials or outbound requests."""

    model = "fake/deterministic"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        # The typed context distinguishes actual user requests from execution
        # observations; never infer provenance from text prefixes.
        text = next((message for role, message in reversed(request.context.messages)
                     if role == "user"), "")
        return ModelResponse(text=f"say({text!r}, final=True)")


_SINGLE_CODE_FENCE = re.compile(
    r"\A[ \t]*```(?:python|py|ipython)?[ \t]*\r?\n"
    r"(?P<source>.*?)\r?\n```[ \t]*\Z",
    re.DOTALL | re.IGNORECASE,
)
_FENCE_LINE = re.compile(r"(?m)^[ \t]*```")


class BasicInterpreter:
    def check_syntax(self, source: str) -> str | None:
        """Compile the IPython-transformed cell without running it.

        This is only a syntax check: magics, imports, functions, names and
        filesystem effects can still fail when the cell actually runs.
        """
        from IPython.core.inputtransformer2 import TransformerManager

        try:
            transformed = TransformerManager().transform_cell(source)
            compile(transformed, "<agent-cell>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        except SyntaxError as exc:
            return f"{type(exc).__name__}: {exc.msg} (line {exc.lineno or 1})"
        except tokenize.TokenError as exc:
            return f"TokenError: {str(exc)[:300]}"
        return None

    def interpret(self, response: ModelResponse) -> AgentDecision:
        if not isinstance(response, ModelResponse):
            return AgentDecision("reject", reason="Invalid provider response")
        if response.finish_status != "complete" or response.rejection_reason is not None:
            return AgentDecision("reject", reason=response.rejection_reason or "Incomplete provider response")
        if not isinstance(response.text, str) or not response.text.strip():
            return AgentDecision("reject", reason="Empty provider response")
        source = response.text.strip()
        fenced = _SINGLE_CODE_FENCE.fullmatch(source)
        if fenced is not None:
            source = fenced.group("source")
        elif _FENCE_LINE.search(source) and self.check_syntax(source) is not None:
            # A fence line inside an otherwise valid cell (for example inside a
            # string literal) is not mixed Markdown and must still execute.
            return AgentDecision(
                "reject", reason="Provider returned mixed or malformed Markdown; no cell was executed",
                retryable=True,
            )
        if not source.strip():
            return AgentDecision("reject", reason="Provider returned an empty code cell")
        return AgentDecision("execute", source=source)


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
