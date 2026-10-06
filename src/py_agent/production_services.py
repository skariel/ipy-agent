"""Public-service adapters for the established production provider/context code.

The wire protocols, authentication, credential storage and response validation
remain owned by :mod:`provider` and :mod:`codex`. This module only translates the
public coordinator contracts to and from those existing adapters.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from .context import (
    COLLAPSE_FORCED,
    COLLAPSE_REMINDER,
    CONTRACT,
    Context,
    Group,
    compact,
)
from .contracts import (
    ContextService,
    ContextSnapshot,
    ModelRequest,
    ModelResponse,
    ObservationService,
)
from .limits import Limits
from .observations import pack_observations
from .plugins import Contributions, PluginManifest, Service, hookimpl


class ProductionContextAdapter:
    """Expose existing context policy through a typed service surface.

    Context owns history and lossless collapse transactions. This adapter owns
    generation-boundary policy (markers, reminders, and forced mode), and renders
    immutable snapshots for providers. Production history is never auto-evicted.
    """

    def __init__(self, context: Context | None = None, *, limits: Limits | None = None):
        if context is not None and limits is not None:
            raise ValueError("Pass a Context or Limits, not both")
        self.context = context if context is not None else Context(limits or Limits())
        if context is None:
            self.context.contract = CONTRACT
        self._contract = self.context.contract
        self._responses = 0
        self._reminded_at = 0
        self._reminder_pending = False
        self._completed_cells = 0
        self._marker_pending = False
        self._force_collapse = False
        self._overflow_recovery = False
        for group in self.context.groups:
            if group.messages and group.messages[0].get("role") == "user":
                self._identify_user(group)

    def _identify_user(self, group: Group) -> None:
        message = group.messages[0]
        if "boundary_id" not in message:
            request_id = group.refs[0] if group.refs else None
            message.update(
                boundary_id=self.context.user_boundary_id(request_id), boundary_kind="user",
            )

    @property
    def force_collapse(self) -> bool:
        """Latched at prepare_generation, not changed by the current response."""
        return self._force_collapse

    def render_user(self, text: str, request_id: str) -> str:
        """Render queued steering with the ID it will retain upon dispatch."""
        return self.context.render_boundary({
            "content": text, "boundary_id": self.context.user_boundary_id(request_id),
            "boundary_kind": "user",
        })

    def completed_cell(self) -> None:
        """Called after a completed execution and its result have been committed."""
        self._completed_cells += 1
        if self._completed_cells % 10 == 0:
            self._marker_pending = True

    def prepare_generation(self) -> None:
        """Apply policy only between completed cell/result groups."""
        usage = self.context.reported_input_tokens
        entering_force = (
            not self._force_collapse and usage is not None
            and usage * 100 > self.context.window_tokens * 90
        )
        if self._marker_pending or entering_force:
            self.context.add_marker()
            self._marker_pending = False
        if entering_force:
            self._force_collapse = True
        self._reminder_pending = self._responses // 50 > self._reminded_at // 50
        if self._reminder_pending:
            self._reminded_at = self._responses

    def recover_overflow(self) -> ContextSnapshot:
        """Offer a small collapse-only request without evicting live history.

        The recovery summary is an index, not an invented summary of unseen
        history. The agent must consult the resulting archive before proceeding.
        """
        if self._overflow_recovery:
            raise ValueError("Context overflow persisted during collapse recovery")
        boundaries = [
            message for group in self.context.groups for message in group.messages
            if message.get("boundary_id")
        ]
        if not boundaries:
            raise ValueError("Context overflow has no history to archive")
        start = boundaries[0]["boundary_id"]
        marker = self.context.add_marker()
        end = marker.messages[0]["boundary_id"]
        self._force_collapse = True
        self._overflow_recovery = True
        summary = (
            "Context overflow recovery: history was archived, not summarized. "
            "Before continuing, inspect the originals in the collapsed archive "
            "referenced above to recover the latest user request, constraints, "
            "and execution state. Do not guess missing details or replay side effects."
        )
        source = f"collapse({start!r}, {end!r}, {summary!r})"
        self._overflow_snapshot = ContextSnapshot(self.context.epoch, (
            ("system", "Context overflow recovery. Output exactly the following "
             "standalone Python cell, with no other code or text:\n" + source),
        ))
        return self._overflow_snapshot

    async def collapse(
        self, start_id: str, end_id: str, summary: str,
        store: Callable[[str], Awaitable[int]],
    ) -> str:
        receipt = await self.context.collapse(start_id, end_id, summary, store)
        self._overflow_recovery = False
        self._force_collapse = False
        self._reminder_pending = False
        return receipt

    @property
    def epoch(self) -> int:
        return self.context.epoch

    @property
    def reported_input_tokens(self) -> int | None:
        return self.context.reported_input_tokens

    def provider_messages(self, snapshot: ContextSnapshot) -> tuple[dict, ...]:
        if not isinstance(snapshot, ContextSnapshot):
            raise TypeError("Context adapter requires a ContextSnapshot")

        system_messages = []
        conversation = []
        from .images import content_with_images
        by_message = {}
        for index, image in snapshot.images:
            by_message.setdefault(index, []).append(image)
        for index, ((role, content), phase) in enumerate(zip(snapshot.messages, snapshot.message_phases, strict=True)):
            if index in by_message:
                content = content_with_images(content, by_message[index])
            if role == "system":
                system_messages.append(content)
            elif role in {"user", "assistant"}:
                message = {"role": role, "content": content}
                if role == "assistant" and phase is not None:
                    message["phase"] = phase
                conversation.append(message)
            elif role == "observation":
                # Preserve the distinct observation record internally; this
                # provider accepts only system/user/assistant roles, so deliver
                # its text as a separate user-role message without a wrapper.
                conversation.append({"role": "user", "content": content})
            else:  # ContextSnapshot validates this too; keep the service boundary defensive.
                raise ValueError(f"Unsupported context role: {role!r}")

        return tuple(
            [{"role": "system", "content": text} for text in system_messages] + conversation
        )

    def record_response(self, response: ModelResponse) -> None:
        if not isinstance(response, ModelResponse):
            raise TypeError("Context usage accounting requires a ModelResponse")
        # Context accounting deliberately ignores missing/invalid input counters,
        # retaining the last reported measurement for the current epoch.
        self.context.record_usage(dict(response.usage))
        self._responses += 1

    def needs_reset(self) -> bool:
        # Model-directed collapse replaces whole-history eviction in production.
        return False

    def add(self, role: str, content: str, refs: Sequence[str] = ()) -> Group:
        group = self.context.add(role, content, refs)
        if role == "user":
            self._identify_user(group)
        return group

    def observation(self, content: object, refs: Sequence[str] = (), group: Group | None = None) -> Group:
        return self.context.observation(content, refs, group)

    @staticmethod
    def _snapshot_images(messages):
        from .images import MAX_CONTEXT_IMAGE_BYTES, MAX_CONTEXT_IMAGES, ImageAttachment
        # Preserve original alignment. Keep only the newest bounded image history;
        # older text explicitly records omission, rather than claiming visibility.
        selected = []
        for index, message in enumerate(messages):
            for record in message.get("images", ()):
                selected.append((index, ImageAttachment.from_record(record)))
        retained = []
        byte_count = 0
        for item in reversed(selected):
            if len(retained) >= MAX_CONTEXT_IMAGES or byte_count + len(item[1].data) > MAX_CONTEXT_IMAGE_BYTES:
                break
            retained.append(item)
            byte_count += len(item[1].data)
        retained.reverse()
        keep = len(retained)
        omitted = selected[:-keep] if keep else selected
        for index in {item[0] for item in omitted}:
            messages[index]["content"] += "\n[Older image attachment omitted by context image limit.]"
        return tuple(retained)

    def snapshot(self) -> ContextSnapshot:
        if self._overflow_recovery:
            return self._overflow_snapshot
        messages = self.context.messages()
        if self._force_collapse:
            messages.append({"role": "system", "content": COLLAPSE_FORCED})
        elif self._reminder_pending:
            messages.append({"role": "system", "content": COLLAPSE_REMINDER})
        images = self._snapshot_images(messages)
        return ContextSnapshot(
            self.context.epoch,
            tuple((message["role"], self.context.render_boundary(message)) for message in messages),
            tuple(message.get("phase") for message in messages),
            images=images,
        )

    def retention(
        self,
        pending: set[str],
        *,
        memories_count: int = 0,
        namespace_summary: object = None,
    ) -> tuple[list[Group], list[Group]]:
        return self.context.retention(
            pending,
            memories_count=memories_count,
            namespace_summary=namespace_summary,
        )

    def commit_epoch(
        self,
        retained: list[Group],
        *,
        memories_count: int = 0,
        namespace_summary: object = None,
    ) -> None:
        self.context.commit(
            retained,
            memories_count=memories_count,
            namespace_summary=namespace_summary,
        )
        self.context.contract = self._contract
        self._force_collapse = False
        self._marker_pending = False
        self._reminder_pending = False

    def prepare_request(self, user_text: str, request_id: str) -> ContextSnapshot:
        """Append the pending user message without evicting dispatched history."""
        if not isinstance(user_text, str) or not isinstance(request_id, str) or not request_id:
            raise TypeError("A context request requires text and a request identity")
        self.add("user", user_text, refs=(request_id,))
        return self.snapshot()

    def abandon_request(self, request_id: str) -> None:
        """Remove an undispatched user message after cancellation or rejection."""
        if not isinstance(request_id, str) or not request_id:
            raise TypeError("A context request requires an identity")
        self.context.groups[:] = [
            group for group in self.context.groups
            if not (
                request_id in group.refs
                and len(group.messages) == 1
                and group.messages[0].get("role") == "user"
                and group.messages[0].get("boundary_kind") != "summary"
            )
        ]

    def commit_response(
        self,
        request_id: str,
        assistant_text: str,
        *,
        observation: Mapping[str, object] | None = None,
        phase: str | None = None,
    ) -> None:
        """Append generated source and evidence (execution or rejected syntax check)."""
        if not isinstance(request_id, str) or not request_id or not isinstance(assistant_text, str):
            raise TypeError("A context response requires a request identity and text")
        if phase not in (None, "commentary", "final_answer"):
            raise ValueError("Unsupported assistant phase")
        group = self.add("assistant", assistant_text, refs=(request_id,))
        if phase is not None:
            group.messages[0]["phase"] = phase
        if observation is not None:
            if not isinstance(observation, Mapping):
                raise TypeError("Packed observations must be a mapping")
            payload = dict(observation)
            stored_index = payload.pop("_stored_output_index", None)
            already_omitted = payload.pop("_output_already_omitted", False)
            reads = payload.pop("_output_reads", [])
            images = payload.pop("_images", [])
            from .images import MAX_IMAGES, ImageAttachment
            if not isinstance(images, (list, tuple)) or len(images) > MAX_IMAGES:
                raise ValueError("Invalid observation image limit")
            images = [ImageAttachment.from_record(image).record() for image in images]
            if (not isinstance(reads, list) or len(reads) > 8
                    or any(not isinstance(read, dict) or set(read) != {"text", "reference"}
                           or not isinstance(read["text"], str)
                           or not isinstance(read["reference"], str)
                           or len(read["reference"]) > 512 for read in reads)
                    or sum(len(read["text"]) for read in reads) > 8000):
                raise ValueError("Invalid bounded output reads")
            if stored_index is not None and (type(stored_index) is not int or stored_index < 1):
                raise ValueError("Stored output reference must be a positive integer")
            if type(already_omitted) is not bool:
                raise TypeError("Omitted-output marker must be boolean")
            executed = True
            if set(payload) == {"output"} and isinstance(payload["output"], str):
                content = payload["output"]
            elif (set(payload) == {"preflight"}
                  and isinstance(payload["preflight"], Mapping)):
                preflight = payload["preflight"]
                diagnostic = preflight.get("syntax_error", preflight.get("error"))
                if not isinstance(diagnostic, str):
                    raise TypeError("Preflight diagnostic must be text")
                content = "Cell not executed: " + diagnostic
                executed = False
                if len(content) > 8_000:
                    content = "Cell not executed: diagnostic too long; send a smaller cell."
            elif payload.get("status") == "output_too_large" and isinstance(payload.get("error"), str):
                content = payload["error"]
                already_omitted = True
            else:
                content = compact(payload)
            if content:
                if executed and stored_index is None and not already_omitted and not images:
                    group.execution_output_indexes.append(len(group.messages))
                group.messages.append({"role": "observation", "content": content,
                                       **({"images": images} if images else {})})
            for read in reads:
                group.execution_output_indexes.append(len(group.messages))
                group.messages.append({
                    "role": "observation", "content": read["text"],
                    "output_read_reference": read["reference"],
                })

    async def archive_execution_outputs(
        self, store: Callable[[tuple[str, ...]], Awaitable[tuple[int, ...]]],
    ) -> int:
        """Archive a batch in the worker, then replace context only after its ack."""
        candidates = self.context.outputs_to_archive()[:10]
        if not candidates:
            return 0
        texts = tuple(content for message, content in candidates
                      if "output_read_reference" not in message)
        if any(len(text) > 16_000 for text in texts):
            return 0  # Cannot store losslessly; leave originals in context.
        indexes = await store(texts) if texts else ()
        if (not isinstance(indexes, tuple) or len(indexes) != len(texts)
                or any(type(index) is not int or index < 1 for index in indexes)
                or len(set(indexes)) != len(indexes)):
            raise ValueError("Executor did not confirm distinct output references")
        stored = iter(indexes)
        self.context.compact_execution_outputs(tuple(
            (message, original, None if "output_read_reference" in message else next(stored))
            for message, original in candidates
        ))
        return len(candidates)


class ProductionProviderAdapter:
    """Adapt an existing ``Completion`` provider to ``ProviderService``.

    ``adapter`` is normally ``LitelmProvider`` or ``CodexProvider``. The
    provider keeps all transport/authentication responsibility; this class
    supplies model-ready context and preserves the completion's usage and
    attribution metadata without fabricating counters.
    """

    def __init__(
        self,
        adapter: Any,
        *,
        provider_id: str | None = None,
        context: ContextService | None = None,
        max_tokens: int | None = None,
    ):
        if not callable(getattr(adapter, "generate", None)):
            raise TypeError("Production provider adapter must expose async generate")
        if max_tokens is not None and (type(max_tokens) is not int or max_tokens <= 0):
            raise ValueError("max_tokens must be a positive integer or None")
        if provider_id is not None and (not isinstance(provider_id, str) or not provider_id.strip()):
            raise ValueError("provider_id must be nonempty text or None")
        self.adapter = adapter
        self.context = context if context is not None else ProductionContextAdapter()
        self.provider_id = provider_id
        self.configured_max_tokens = max_tokens
        model = getattr(adapter, "model", None)
        self.model = model if isinstance(model, str) else "unknown"

    def set_model(self, model: str) -> None:
        from .codex import CodexProvider
        from .model_catalog import adapter_for_model
        from .provider import LitelmProvider

        if isinstance(self.adapter, (LitelmProvider, CodexProvider)):
            selected_id = adapter_for_model(model)
            current_id = "codex" if isinstance(self.adapter, CodexProvider) else "litelm"
            if selected_id != current_id:
                if self.configured_max_tokens or getattr(self.adapter, "api_base", None) or getattr(self.adapter, "stream", False):
                    raise ValueError("Clear adapter-specific endpoint/stream/token settings before switching transports")
                auth_file = self.adapter.auth_file
                candidate = (
                    CodexProvider(model, auth_file=auth_file)
                    if selected_id == "codex"
                    else LitelmProvider(model, auth_file=auth_file)
                )
                self.adapter = candidate
                self.provider_id = selected_id
            else:
                self.adapter.set_model(model)
        else:
            setter = getattr(self.adapter, "set_model", None)
            if not callable(setter):
                raise ValueError("Selected provider does not support live model changes")
            setter(model)
        self.model = self.adapter.model

    def _max_tokens(self, request: ModelRequest) -> int | None:
        option = request.options.get("max_tokens")
        if option is None:
            return self.configured_max_tokens
        if not isinstance(option, str) or not option.isascii() or not option.isdecimal():
            raise ValueError("ModelRequest max_tokens option must be a positive decimal integer")
        value = int(option)
        if value <= 0:
            raise ValueError("ModelRequest max_tokens option must be positive")
        return value

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if not isinstance(request, ModelRequest):
            raise TypeError("Production provider requires a ModelRequest")
        messages = self.context.provider_messages(request.context)
        effort = request.options.get("effort")
        supports_effort = hasattr(self.adapter, "effort")
        previous_effort = getattr(self.adapter, "effort", None)
        change_effort = isinstance(effort, str) and supports_effort
        if change_effort:
            # Coordinator submissions are serialized. Apply request-scoped
            # Codex effort without changing the adapter's configured baseline.
            self.adapter.effort = effort
        try:
            completion = await self.adapter.generate(
                [dict(message) for message in messages],
                max_tokens=self._max_tokens(request),
            )
        finally:
            if change_effort:
                self.adapter.effort = previous_effort
        text = getattr(completion, "text", None)
        if not isinstance(text, str):
            raise TypeError("Provider completion text must be text")
        finish_reason = getattr(completion, "finish_reason", None)
        rejection_reason = getattr(completion, "rejection_reason", None)
        successful = getattr(completion, "successful", None)
        if type(successful) is not bool:
            successful = (
                finish_reason == "stop"
                and isinstance(text, str)
                and bool(text.strip())
                and rejection_reason is None
            )

        usage = getattr(completion, "usage", {})
        if not isinstance(usage, Mapping):
            raise TypeError("Provider completion usage must be a mapping")
        reasoning = getattr(completion, "reasoning", None)
        phase = getattr(completion, "phase", None)
        raw = getattr(completion, "raw", None)
        if successful:
            finish_status = "complete"
            accepted_text = text
        else:
            # Never let text from a rejected, partial, or empty provider result
            # appear executable to an interpreter that only checks text.
            finish_status = finish_reason if isinstance(finish_reason, str) and finish_reason != "stop" else "rejected"
            accepted_text = ""
            if rejection_reason is None:
                rejection_reason = finish_status

        provider_id = self.provider_id or request.model
        actual_model = getattr(self.adapter, "model", None)
        return ModelResponse(
            text=accepted_text,
            reasoning=reasoning if isinstance(reasoning, str) else "",
            finish_status=finish_status,
            usage=usage,
            provider_id=provider_id,
            model=actual_model if isinstance(actual_model, str) else request.model,
            rejection_reason=rejection_reason if isinstance(rejection_reason, str) else None,
            phase=phase if isinstance(phase, str) else None,
            adapter_metadata={"raw": raw} if raw is not None else {},
        )


class ProductionObservationAdapter:
    """Project execution evidence to minimal, bounded, readable model text."""

    def pack(self, events: list[dict]) -> Mapping[str, object]:
        if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
            raise TypeError("events must be a list of dictionaries")
        lines: list[str] = []
        content_chars = 0
        for event in pack_observations(events)["events"]:
            stream = event.get("stream")
            if stream in ("stdout", "stderr") and isinstance(event.get("text"), str):
                text = event["text"]
                if text:
                    content_chars += len(text)
                    lines.append(f"{stream}:\n{text}")
            elif "display" in event and isinstance(event["display"], str):
                content_chars += len(event["display"])
                lines.append("Out:\n" + event["display"])
            elif "error" in event:
                text = str(event["error"])
                content_chars += len(text)
                lines.append("Execution error: " + text)
            elif type(event.get("omitted_events")) is int and event["omitted_events"] > 0:
                text = f"[{event['omitted_events']} output events omitted]"
                content_chars += len(text)
                lines.append(text)
        output = "\n".join(lines)
        # Match the worker's raw-content budget, not the rendered length:
        # labels/separators must not discard an otherwise visible 8,000 chars.
        # Keep a separate envelope bound for callers supplying arbitrarily many
        # empty events; 16,000 also accommodates the coordinator's 256 events.
        if content_chars > 8_000 or len(output) > 16_000:
            output = f"Output too long ({len(output)} chars); omitted."
            return {"output": output, "_output_already_omitted": True}
        return {"output": output}

    def model_content(self, events: list[dict]) -> str:
        return self.pack(events)["output"]


def litelm_provider_factory(
    model: str,
    *,
    api_base: str | None = None,
    stream: bool = False,
    auth_file=None,
) -> Callable[[], Any]:
    """Return a lazy factory for the existing API-key provider adapter."""

    def create():
        from .provider import LitelmProvider

        return LitelmProvider(
            model, api_base=api_base, stream=stream, auth_file=auth_file,
        )

    return create


def codex_provider_factory(
    model: str,
    *,
    auth_file=None,
    session_id: str | None = None,
    effort: str = "medium",
    transport=None,
) -> Callable[[], Any]:
    """Return a lazy factory for the existing pi-authenticated Codex adapter.

    Authentication and refresh policy remain entirely inside ``CodexProvider``
    and ``codex_auth``. This factory never reads credentials itself.
    """

    def create():
        from .codex import CodexProvider

        kwargs = {"session_id": session_id, "effort": effort, "transport": transport}
        if auth_file is not None:
            kwargs["auth_file"] = auth_file
        return CodexProvider(model, **kwargs)

    return create


class ProductionServicesPlugin:
    """Register production adapters without selecting or authenticating them.

    Provider factories are opt-in so callers can resolve models and credentials
    using the repository's existing configuration/auth paths. Factory closures
    must return a fresh provider instance for each session.
    """

    def __init__(
        self,
        *,
        litelm_factory: Callable[[], Any] | None = None,
        codex_factory: Callable[[], Any] | None = None,
        context_factory: Callable[[], ContextService] = ProductionContextAdapter,
        observations_factory: Callable[[], ObservationService] = ProductionObservationAdapter,
    ):
        for name, factory in (
            ("litelm_factory", litelm_factory),
            ("codex_factory", codex_factory),
            ("context_factory", context_factory),
            ("observations_factory", observations_factory),
        ):
            if factory is not None and not callable(factory):
                raise TypeError(f"{name} must be callable")
        self.litelm_factory = litelm_factory
        self.codex_factory = codex_factory
        self.context_factory = context_factory
        self.observations_factory = observations_factory

    @hookimpl
    def py_agent_register(self) -> Contributions:
        services = [
            Service("context", "production", self.context_factory),
            Service("observation", "bounded", self.observations_factory),
        ]
        for provider_id, adapter_factory in (
            ("litelm", self.litelm_factory),
            ("codex", self.codex_factory),
        ):
            if adapter_factory is None:
                continue

            def create_provider(factory=adapter_factory, selected_id=provider_id):
                return ProductionProviderAdapter(
                    factory(),
                    provider_id=selected_id,
                    context=self.context_factory(),
                )

            services.append(Service("provider", provider_id, create_provider))
        return Contributions(PluginManifest("production"), tuple(services))
