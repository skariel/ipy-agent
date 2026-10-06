"""Host-side implementation of the Python session standard library."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

from .contracts import ContextSnapshot, ModelRequest, ModelResponse
from .images import ImageAttachment
from .provider import ProviderError
from .stdlib import MAX_RESULT_CHARS, validate_payload


def execution_llm_handler(coordinator, execution_origin):
    async def call(payload):
        from .coordinator_support import State
        from .production_services import ProductionProviderAdapter
        request = coordinator._lifecycle._active_execution_request
        if (request is None or request.origin != execution_origin
                or coordinator.state is not State.EXECUTING):
            raise RuntimeError("llm execution is no longer active")
        payload = validate_payload(payload)
        options = {"max_tokens": str(payload["max_tokens"])}
        effort = coordinator.effective_effort
        if effort not in {"default", "unavailable"}:
            options["effort"] = effort
        context = ContextSnapshot(
            0, (("system", payload["system"]), ("user", payload["prompt"])),
            images=tuple((1, ImageAttachment.from_record(record)) for record in payload["images"]),
        )
        model_origin = replace(execution_origin, generation_id=uuid4().hex)
        model_request = ModelRequest(model_origin, context, coordinator.model, options)
        provider = coordinator.provider
        if isinstance(provider, ProductionProviderAdapter):
            # Fresh adapter wrapper omits context overflow recovery and never
            # brings the agent's system/history/boundaries into the subcall.
            provider = ProductionProviderAdapter(provider.adapter, provider_id=provider.provider_id,
                                                 max_tokens=provider.configured_max_tokens)
        from .runtime_status import Activity
        started = asyncio.get_running_loop().time()
        for attempt in range(5):
            coordinator.activity = Activity("LLM subcall", attempt + 1, 5, started)
            if coordinator._lifecycle._active_execution_request is not request:
                raise asyncio.CancelledError
            coordinator._journal_record("record_model_request", model_request)
            try:
                response = await asyncio.wait_for(provider.generate(model_request), timeout=180)
            except asyncio.CancelledError:
                coordinator._record_provider_usage(model_request, None, outcome="cancelled")
                raise
            except Exception as exc:
                coordinator._record_provider_usage(model_request, None, outcome="failed")
                kind = getattr(exc, "kind", None)
                attempts = 5 if kind == "transport" else 3
                if kind in {"transport", "rate_limit", "server"} and attempt + 1 < attempts:
                    delay = min(8, 2 ** attempt) if kind == "transport" else .5 * (attempt + 1)
                    coordinator.activity = Activity(
                        "LLM subcall retry", attempt + 2, attempts, started,
                        asyncio.get_running_loop().time() + delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise ProviderError("llm() provider request failed; no response accepted",
                                    kind=kind or "provider") from None
            if not isinstance(response, ModelResponse):
                coordinator._record_provider_usage(model_request, None, outcome="failed")
                raise TypeError("Invalid llm provider response")
            coordinator._record_provider_usage(model_request, response, outcome="returned")
            if response.finish_status != "complete" or response.rejection_reason or not response.text.strip():
                raise ProviderError("llm() returned an incomplete response; no text accepted", kind="provider")
            if len(response.text) > MAX_RESULT_CHARS:
                raise ValueError("llm() response exceeds 65536 characters")
            return response.text
        raise RuntimeError("llm retry budget exhausted")
    async def observed(payload):
        previous = coordinator.activity
        try:
            return await call(payload)
        finally:
            coordinator.activity = previous
    return observed
