"""Transport retries and overflow recovery for a single model request."""
from __future__ import annotations

import asyncio
from dataclasses import replace

from .contracts import ModelRequest, ModelResponse, Origin, OutputEvent, ProgressCallback
from .coordinator_interfaces import ModelRuntime
from .coordinator_support import State


class ModelRequests:
    def __init__(self, coordinator: ModelRuntime) -> None:
        self.coordinator = coordinator

    async def generate(
        self, request: ModelRequest, *, operation_id: str, task_text: str,
        context_committed: bool, executed_cells: int,
        published_events: list[OutputEvent], generation_origin: Origin,
        on_progress: ProgressCallback | None,
    ) -> tuple[ModelRequest, ModelResponse, bool]:
        coordinator = self.coordinator
        model_request = request
        forced_collapse = False
        from .runtime_status import Activity
        activity_started = asyncio.get_running_loop().time()
        from .retry_policy import MAX_MODEL_ATTEMPTS, model_retry
        for attempt in range(MAX_MODEL_ATTEMPTS):
            coordinator.activity = Activity("Model request", attempt + 1, MAX_MODEL_ATTEMPTS, activity_started)
            generation = asyncio.create_task(
                coordinator.provider.generate(model_request),
                name=f"py-agent-generation-{model_request.origin.generation_id}-attempt-{attempt + 1}",
            )
            coordinator.lifecycle._generation = generation
            try:
                response = await generation
            except asyncio.CancelledError:
                await coordinator.journal_policy._record_provider_usage(model_request, None, outcome="cancelled")
                raise
            except Exception as exc:
                kind = getattr(exc, "kind", None)
                recover = getattr(coordinator.context_service, "recover_overflow", None)
                if kind == "overflow" and attempt == 0 and callable(recover):
                    await coordinator.journal_policy._record_provider_usage(model_request, None, outcome="failed")
                    if not coordinator.lifecycle._operation_is_current(operation_id, State.GENERATING):
                        raise asyncio.CancelledError from None
                    recovery_context = recover()
                    forced_collapse = True
                    model_request = replace(model_request, context=recovery_context)
                    coordinator.lifecycle.operation.begin_model(model_request)
                    await coordinator.journal_policy._journal_record("record_model_request", model_request)
                    continue
                decision = model_retry(exc, attempt)
                max_attempts, delay = decision.attempts, decision.delay
                if decision.retry:
                    await coordinator.journal_policy._journal_record(
                        "record_provider_usage", model_request, None, outcome="retry_failed",
                    )
                    try:
                        published_events.append(await coordinator.frontend._emit_progress(
                            generation_origin,
                            {"phase": "provider_retry", "attempt": attempt + 2,
                             "text": f"Provider request failed ({kind}); retrying ({attempt + 2}/{max_attempts}) in {delay:g}s."},
                            on_progress=on_progress, operation_id=operation_id,
                            expected_state=State.GENERATING,
                        ))
                        await coordinator.retry_waiter(delay, operation_id)
                    except asyncio.CancelledError:
                        await coordinator.journal_policy._record_provider_usage(model_request, None, outcome="cancelled")
                        raise
                    except Exception:
                        await coordinator.journal_policy._record_provider_usage(model_request, None, outcome="failed")
                        raise
                    if not coordinator.lifecycle._operation_is_current(operation_id, State.GENERATING):
                        await coordinator.journal_policy._record_provider_usage(model_request, None, outcome="cancelled")
                        raise asyncio.CancelledError from None
                    continue
                await coordinator.journal_policy._record_provider_usage(model_request, None, outcome="failed")
                if kind == "transport":
                    from .runtime_status import Recovery
                    coordinator.recovery = Recovery(
                        task_text, model_request.context, context_committed,
                        executed_cells,
                        model_request.model, tuple(model_request.options.items()),
                    )
                    from .provider import ProviderError
                    raise ProviderError(
                        f"{exc} (automatic retries exhausted after {max_attempts} attempts)",
                        kind="transport",
                    ) from None
                raise
            finally:
                if coordinator.lifecycle._generation is generation:
                    coordinator.lifecycle._generation = None
            break

        return model_request, response, forced_collapse
