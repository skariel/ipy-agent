"""Buffered, no-tools litelm boundary. This module never executes model text.

Usage is reported by litelm, not necessarily the provider's untouched wire usage.
In particular litelm translates Anthropic cache fields and may omit stream input
usage. Missing counters remain unknown; we never infer cost or sum cache tokens.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
import inspect
import json
from pathlib import Path
import re
from typing import Any, Protocol


@dataclass(frozen=True)
class Completion:
    text: str
    finish_reason: str = "stop"
    usage: dict = field(default_factory=dict)
    reasoning: str | None = None
    raw: dict = field(default_factory=dict)
    rejection_reason: str | None = None
    phase: str | None = None  # native assistant phase, where the provider supplies one

    @property
    def successful(self) -> bool:
        return (
            self.rejection_reason is None
            and self.finish_reason == "stop"
            and isinstance(self.text, str)
            and bool(self.text.strip())
        )


class Provider(Protocol):
    model: str

    async def generate(self, messages: list[dict], *, max_tokens: int | None = None) -> Completion: ...


class ProviderError(RuntimeError):
    """No executable completion exists. Incurred usage may be unknown."""

    def __init__(self, message: str, *, kind: str = "provider", raw: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.raw = raw or {}
        self.usage_unknown = True


def _json_object(value: Any) -> dict:
    original = value
    if hasattr(value, "model_dump"):
        try:
            value = value.model_dump(mode="json")
        except TypeError:
            value = value.model_dump()
    if isinstance(value, dict):
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
            return json.loads(encoded)
        except (TypeError, ValueError):
            # Some compatibility wrappers implement model_dump() by walking
            # Pydantic internals (including sets) but expose valid JSON through
            # json(). Prefer that provider-owned serialization as a fallback.
            pass
    serializer = getattr(original, "json", None)
    if callable(serializer):
        try:
            encoded = serializer()
            if isinstance(encoded, bytes):
                encoded = encoded.decode("utf-8")
            decoded = json.loads(encoded)
            if isinstance(decoded, dict):
                return decoded
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            pass
    if not isinstance(value, dict):
        raise ProviderError("Unsupported provider response object", kind="shape")
    raise ProviderError("Non-JSON provider response", kind="shape")


def normalize_usage(raw: dict | None) -> dict:
    """Keep translated raw metadata beside counters, with no cache arithmetic."""
    if raw is None:
        return {"source": "unknown", "raw": None, "normalized": {}}
    if not isinstance(raw, dict):
        raise ProviderError("Unsupported usage shape", kind="shape")
    counters = {}
    paths = {
        "input_tokens": ("prompt_tokens",),
        "output_tokens": ("completion_tokens",),
        "total_tokens": ("total_tokens",),
        "cache_read_tokens": ("prompt_tokens_details", "cached_tokens"),
        "cache_creation_tokens": ("prompt_tokens_details", "cache_creation_tokens"),
        "cache_write_tokens": ("prompt_tokens_details", "cache_write_tokens"),
        "reasoning_tokens": ("completion_tokens_details", "reasoning_tokens"),
    }
    for name, path in paths.items():
        value: Any = raw
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            counters[name] = value
    return {"source": "reported_by_litelm", "raw": deepcopy(raw), "normalized": counters}


def _validate_messages(messages: list[dict], max_tokens: int | None) -> list[dict]:
    if max_tokens is not None and (type(max_tokens) is not int or max_tokens <= 0):
        raise ValueError("max_tokens must be a positive integer or None")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a nonempty explicit context")
    for message in messages:
        if (
            not isinstance(message, dict)
            or not {"role", "content"} <= set(message)
            or set(message) - {"role", "content", "phase"}
            or message["role"] not in {"system", "user", "assistant"}
            or not isinstance(message["content"], str)
            or (
                "phase" in message
                and (message["role"] != "assistant" or message["phase"] not in {"commentary", "final_answer"})
            )
        ):
            raise ValueError("Only explicit text system/user/assistant messages are supported")
    return deepcopy(messages)


def _payload_error(message: dict, *, streaming: bool = False) -> str | None:
    if message.get("role") not in ({None, "assistant"} if streaming else {"assistant"}):
        return "Unexpected response role"
    for field_name in ("tool_calls", "function_call", "refusal", "images", "audio"):
        if message.get(field_name):
            return f"Unsupported response field: {field_name}"
    extra = message.get("provider_specific_fields")
    if extra not in (None, {}):
        return "Unsupported provider-specific message fields"
    if message.get("content") is not None and not isinstance(message["content"], str):
        return "Content must be plain text"
    if message.get("reasoning_content") is not None and not isinstance(message["reasoning_content"], str):
        return "Unsupported reasoning shape"
    blocks = message.get("thinking_blocks")
    if blocks is not None and (
        not isinstance(blocks, list)
        or any(
            not isinstance(block, dict) or block.get("type") not in {"thinking", "redacted_thinking"}
            for block in blocks
        )
    ):
        return "Unsupported thinking blocks"
    return None


def _completion(raw: dict) -> Completion:
    usage = normalize_usage(raw.get("usage"))
    choices = raw.get("choices")
    rejection = None
    text, reason, reasoning = "", "unknown", None
    if raw.get("error"):
        rejection = "Provider returned an error"
    elif not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        rejection = "Expected exactly one completion choice"
    else:
        choice = choices[0]
        message = choice.get("message")
        reason = choice.get("finish_reason") or "unknown"
        if choice.get("index", 0) != 0:
            rejection = "Unexpected choice index"
        elif not isinstance(message, dict):
            rejection = "Missing assistant message"
        else:
            rejection = _payload_error(message)
            if isinstance(message.get("content"), str):
                text = message["content"]
            reasoning = message.get("reasoning_content")
    if not isinstance(reason, str):
        reason, rejection = "unknown", "Unsupported finish reason"
    if not isinstance(reasoning, str):
        reasoning = None
    return Completion(text, reason, usage, reasoning, raw, rejection)


def _litelm_failure_kind(exc: Exception) -> str:
    """Retry only failures with a recognizable transient provider category."""
    status = getattr(exc, "status_code", None)
    if type(status) is not int:
        status = getattr(exc, "status", None)
    if type(status) is int:
        if status in (401, 403):
            return "authentication"
        if status == 429:
            return "rate_limit"
        if status == 408:
            return "timeout"
        if status == 413:
            return "overflow"
        if 500 <= status < 600:
            return "provider"
        if 400 <= status < 500:
            return "request"
    name = type(exc).__name__
    if name == "ContextWindowExceededError":
        return "overflow"
    if name in {"AuthenticationError", "PermissionDeniedError", "InvalidRequestError", "BadRequestError"}:
        return "authentication" if name in {"AuthenticationError", "PermissionDeniedError"} else "request"
    if name in {"RateLimitError", "RateLimitException"}:
        return "rate_limit"
    if name in {"TimeoutError", "APITimeoutError", "ReadTimeout", "ConnectTimeout"}:
        return "timeout"
    if name in {"APIConnectionError", "ConnectionError", "TransportError"}:
        return "transport"
    if name in {"ServiceUnavailableError", "InternalServerError"}:
        return "provider"
    return "internal"


_MODEL_ID = re.compile(r"[^\x00-\x20\x7f]{1,512}\Z")

# The shared effort vocabulary is Codex's preset list. DeepSeek accepts only
# low/high/max and disables thinking through a separate toggle, so translate at
# the provider boundary instead of teaching the coordinator two vocabularies.
_DEEPSEEK_EFFORT = {
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "max",
}


def _litelm_effort_options(model: str, effort: str) -> dict[str, object]:
    """Translate one shared effort preset into provider request options."""
    if model.split("/", 1)[0] != "deepseek":
        return {"reasoning_effort": effort}
    if effort == "none":
        # DeepSeek documents no "none" effort value; thinking mode is toggled
        # through the OpenAI-compatible extra body instead.
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {"reasoning_effort": _DEEPSEEK_EFFORT.get(effort, effort)}


class LitelmProvider:
    """API-key client; no pi subscription authentication or server-side history.

    Cancellation propagates as CancelledError. Consumers must journal unknown
    billing for interrupted calls and independently reject stale generations.
    """

    def __init__(
        self,
        model: str,
        api_base: str | None = None,
        stream: bool = False,
        auth_file: Path | None = None,
    ):
        self.model = self._validated_model(model)
        self.api_base, self.stream = api_base, stream
        self.auth_file = Path(auth_file) if auth_file is not None else None
        self.effort: str | None = None

    @staticmethod
    def _validated_model(model: str) -> str:
        if not isinstance(model, str) or _MODEL_ID.fullmatch(model) is None:
            raise ValueError("An explicit bounded provider/model ID without whitespace is required")
        return model

    def set_model(self, model: str) -> None:
        self.model = self._validated_model(model)

    async def generate(self, messages: list[dict], *, max_tokens: int | None = None) -> Completion:
        context = [{"role": m["role"], "content": m["content"]} for m in _validate_messages(messages, max_tokens)]
        try:
            import litelm
        except ImportError as exc:
            raise ProviderError("Install the locked litelm dependencies", kind="configuration") from exc
        kwargs = {"model": self.model, "messages": context, "stream": self.stream, "num_retries": 0}
        provider_id = self.model.split("/", 1)[0] if "/" in self.model else "openai"
        from .codex_auth import read_provider_api_key

        credential = read_provider_api_key(provider_id, self.auth_file)
        if credential is not None:
            kwargs["api_key"] = credential.key
        if self.effort is not None:
            kwargs.update(_litelm_effort_options(self.model, self.effort))
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if self.api_base is not None:
            kwargs["api_base"] = self.api_base
        if self.stream and self.model.split("/", 1)[0] not in {"anthropic", "bedrock", "cloudflare", "mistral"}:
            # OpenAI-compatible servers use this option. litelm's custom
            # Anthropic path forwards unknown kwargs to the native SDK.
            kwargs["stream_options"] = {"include_usage": True}
        try:
            response = await litelm.acompletion(**kwargs)
            if self.stream:
                return await self._collect(response)
            return _completion(_json_object(response))
        except (asyncio.CancelledError, ProviderError):
            raise
        except Exception as exc:
            # Exception strings can contain credential-bearing URLs or headers.
            # Record the category, never deliberately persist their raw content.
            name = type(exc).__name__
            kind = _litelm_failure_kind(exc)
            raise ProviderError(f"litelm request failed ({name}); no source accepted", kind=kind) from exc

    async def _collect(self, stream: Any) -> Completion:
        if not hasattr(stream, "__aiter__"):
            raise ProviderError("Expected asynchronous stream", kind="shape")
        chunks, texts, reasons, thinking = [], [], [], []
        usage_samples = []
        usage = None
        rejection = None
        finished = False
        try:
            async for chunk in stream:
                raw = _json_object(chunk)
                chunks.append(raw)
                if raw.get("error"):
                    rejection = rejection or "Provider returned a streaming error"
                if raw.get("usage") is not None:
                    normalize_usage(raw["usage"])
                    usage_samples.append(raw["usage"])
                    usage = raw["usage"]
                choices = raw.get("choices")
                if choices == [] and raw.get("usage") is not None:
                    continue
                if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                    rejection = rejection or "Expected one stream choice or usage-only chunk"
                    continue
                choice = choices[0]
                delta = choice.get("delta")
                if choice.get("index", 0) != 0 or not isinstance(delta, dict):
                    rejection = rejection or "Invalid stream choice"
                    continue
                rejection = rejection or _payload_error(delta, streaming=True)
                content, reasoning = delta.get("content"), delta.get("reasoning_content")
                if finished and (content or reasoning):
                    rejection = rejection or "Content after stream finish"
                if isinstance(content, str):
                    texts.append(content)
                if isinstance(reasoning, str):
                    thinking.append(reasoning)
                reason = choice.get("finish_reason")
                if reason is not None:
                    if finished:
                        rejection = rejection or "Multiple stream finish markers"
                    reasons.append(reason)
                    finished = True
        finally:
            close = getattr(stream, "aclose", None) or getattr(stream, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result
        reason = reasons[0] if len(reasons) == 1 and isinstance(reasons[0], str) else "unknown"
        if not finished:
            rejection = rejection or "Stream ended without finish marker"
        reported = normalize_usage(usage)
        reported["samples"] = usage_samples
        if self.model.startswith("anthropic/") and len(usage_samples) > 1:
            # Inspected litelm translates message_start as input/0/input and
            # message_delta as 0/output/output. Neither 'total' is a true total.
            # Preserve every raw sample, use the first input and final output,
            # and do not pretend the translated last input=0 was real usage.
            first = normalize_usage(usage_samples[0])["normalized"]
            reported["normalized"].pop("total_tokens", None)
            reported["normalized"].pop("input_tokens", None)
            if "input_tokens" in first:
                reported["normalized"]["input_tokens"] = first["input_tokens"]
        return Completion("".join(texts), reason, reported, "".join(thinking) or None, {"chunks": chunks}, rejection)


class FakeProvider:
    """Deterministic calls, including cancellation races; no paid requests.

    Each started call consumes one response even if cancelled. Set ``gate`` to an
    asyncio.Event to hold returns, and await ``started`` to synchronize steering.
    ``requests`` contains independent exact input copies.
    """

    def __init__(
        self, responses, *, model: str = "fake/deterministic", delay: float = 0, gate: asyncio.Event | None = None
    ):
        self.model, self.delay, self.gate = model, delay, gate
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.started = asyncio.Event()
        self.cancelled = 0

    async def generate(self, messages: list[dict], *, max_tokens: int | None = None) -> Completion:
        context = _validate_messages(messages, max_tokens)
        self.requests.append({"messages": context, "max_tokens": max_tokens, "model": self.model})
        self.started.set()
        if not self.responses:
            raise ProviderError("Fake provider exhausted", kind="exhausted")
        response = self.responses.pop(0)
        try:
            if self.gate is not None:
                await self.gate.wait()
            await asyncio.sleep(self.delay)
            if isinstance(response, BaseException):
                raise response
            if isinstance(response, str):
                return Completion(response)
            if not isinstance(response, Completion):
                raise ProviderError("Unsupported fake response", kind="shape")
            return deepcopy(response)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
