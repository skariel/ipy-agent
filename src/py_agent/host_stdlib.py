"""Host-side implementation of the Python session standard library."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .contracts import ContextSnapshot, ModelRequest, ModelResponse
from .images import ImageAttachment
from .provider import ProviderError
from .retry_policy import MAX_MODEL_ATTEMPTS, model_retry
from .stdlib import MAX_RESULT_CHARS, validate_payload

if TYPE_CHECKING:
    from .contracts import Origin
    from .coordinator import Coordinator
    from .coordinator_runtime import SessionRuntime


def execution_llm_handler(
    coordinator: SessionRuntime | Coordinator, execution_origin: Origin,
) -> Callable[[Any], Awaitable[str]]:
    from .coordinator_runtime import SessionRuntime
    runtime = coordinator if isinstance(coordinator, SessionRuntime) else coordinator._runtime

    async def call(payload: Any) -> str:
        from .coordinator_support import State
        from .production_services import ProductionProviderAdapter
        request = runtime.lifecycle.operation.execution_request
        if (request is None or request.origin != execution_origin
                or runtime.lifecycle.state is not State.EXECUTING):
            raise RuntimeError("llm execution is no longer active")
        payload = validate_payload(payload)
        options = {"max_tokens": str(payload["max_tokens"])}
        effort = runtime.effective_effort
        if effort not in {"default", "unavailable"}:
            options["effort"] = effort
        context = ContextSnapshot(
            0, (("system", payload["system"]), ("user", payload["prompt"])),
            images=tuple((1, ImageAttachment.from_record(record)) for record in payload["images"]),
        )
        model_origin = replace(execution_origin, generation_id=uuid4().hex)
        model_request = ModelRequest(model_origin, context, runtime.model, options)
        provider = runtime.provider
        if isinstance(provider, ProductionProviderAdapter):
            # Fresh adapter wrapper omits context overflow recovery and never
            # brings the agent's system/history/boundaries into the subcall.
            provider = ProductionProviderAdapter(provider.adapter, provider_id=provider.provider_id,
                                                 max_tokens=provider.configured_max_tokens)
        from .runtime_status import Activity
        started = asyncio.get_running_loop().time()
        for attempt in range(MAX_MODEL_ATTEMPTS):
            runtime.activity = Activity("LLM subcall", attempt + 1, MAX_MODEL_ATTEMPTS, started)
            if runtime.lifecycle.operation.execution_request is not request:
                raise asyncio.CancelledError
            await runtime.journal_policy._journal_record("record_model_request", model_request)
            try:
                try:
                    response = await asyncio.wait_for(provider.generate(model_request), timeout=180)
                except TimeoutError:
                    raise ProviderError("llm() request timed out", kind="timeout") from None
            except asyncio.CancelledError:
                await runtime.journal_policy._record_provider_usage(model_request, None, outcome="cancelled")
                raise
            except Exception as exc:
                await runtime.journal_policy._record_provider_usage(model_request, None, outcome="failed")
                kind = getattr(exc, "kind", None)
                decision = model_retry(exc, attempt)
                attempts, delay = decision.attempts, decision.delay
                if decision.retry:
                    runtime.activity = Activity(
                        "LLM subcall retry", attempt + 2, attempts, started,
                        asyncio.get_running_loop().time() + delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise ProviderError("llm() provider request failed; no response accepted",
                                    kind=kind or "provider") from None
            if not isinstance(response, ModelResponse):
                await runtime.journal_policy._record_provider_usage(model_request, None, outcome="failed")
                raise TypeError("Invalid llm provider response")
            await runtime.journal_policy._record_provider_usage(model_request, response, outcome="returned")
            if response.finish_status != "complete" or response.rejection_reason or not response.text.strip():
                raise ProviderError("llm() returned an incomplete response; no text accepted", kind="provider")
            if len(response.text) > MAX_RESULT_CHARS:
                raise ValueError("llm() response exceeds 65536 characters")
            return response.text
        raise RuntimeError("llm retry budget exhausted")
    async def observed(payload: Any) -> str:
        previous = runtime.activity
        try:
            return await call(payload)
        finally:
            runtime.activity = previous
    return observed
