"""SessionRuntime observations policy."""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
from typing import TYPE_CHECKING, Any, cast

from .contracts import ExecutionRequest, ExecutionResult
from .coordinator_support import (
    MODEL_OBSERVATION_MAX_DISPLAY_CHARS,
    MODEL_OBSERVATION_MAX_EVENTS,
    _bounded_plain_fallback,
    _observation_mime_types,
    _strip_observation_terminal_controls,
)
from .output_reads import output_read_reference

if TYPE_CHECKING:
    from .coordinator_runtime import SessionRuntime


@dataclass
class ModelObservations:
    """SessionRuntime observations policy; no execution or provider replay."""

    coordinator: SessionRuntime

    def packed_observation(self, request: ExecutionRequest, result: ExecutionResult) -> str | dict[str, Any]:
        from .images import observation_images
        records, notices = observation_images(result.output_events)
        packed = self._packed_text_observation(request, result)
        if not notices:
            return packed
        packed = {"output": packed} if isinstance(packed, str) else dict(packed)
        # Metadata remains text; bytes travel separately through context/provider
        # boundaries, never pasted as base64 into model-visible text.
        text = "\n".join(notices)
        if "events" in packed:
            events = [dict(event) for event in packed["events"]]
            display_events = [event for event in events if event.get("output_kind") in {"display", "execute_result", "update"}]
            if display_events:
                # Preserve event identities/order rather than inventing a rich
                # event without execution provenance.
                display_events[-1]["display"] = str(display_events[-1].get("display", ""))[:6000] + "\n" + text
            packed["events"] = events
        else:
            packed["output"] = str(packed.get("output", "")) + "\n" + text
        if records:
            packed["_images"] = list(records)
        return packed

    def _packed_text_observation(self, request: ExecutionRequest, result: ExecutionResult) -> str | dict[str, Any]:
        # Archive reads have their own bounded envelope. Never let unrelated
        # stdout (including an oversized stream) hide or re-archive an excerpt.
        reads: list[dict[str, str]] = []
        ordinary = []
        read_chars = 0
        for output in result.output_events:
            ref = output_read_reference(output.metadata)
            text = output.data.get("text/plain")
            if (output.kind == "display" and ref is not None
                    and isinstance(text, str) and len(reads) < 8
                    and read_chars + len(text) <= 8000):
                read_chars += len(text)
                reads.append({
                    "text": _strip_observation_terminal_controls(text),
                    "reference": (
                        f"outputs[{ref['index']}] chars {ref['start']}:{ref['end']} "
                        f"of {ref['total']}; retrieve with "
                        f"read_output({ref['index']}, start={ref['start']}, "
                        f"limit={max(1, ref['end'] - ref['start'])})"
                    ),
                })
            else:
                ordinary.append(output)
        packed = self._packed_regular_observation(
            request, replace(result, output_events=tuple(ordinary)),
        )
        if not reads:
            return packed
        if isinstance(packed, str):
            return "\n".join([packed, *(read["text"] for read in reads)])
        return {**packed, "_output_reads": reads}

    def _packed_regular_observation(self, request: ExecutionRequest, result: ExecutionResult) -> str | dict[str, Any]:
        if result.origin != request.origin:
            raise RuntimeError("Cannot pack output from a different execution origin")
        events: list[dict[str, object]] = []
        omitted_events = 0
        event_index = 0
        display_chars_remaining = MODEL_OBSERVATION_MAX_DISPLAY_CHARS
        origin = request.origin
        common = {
            "session_id": origin.session_id,
            "request_id": origin.request_id,
            "frontend_id": origin.frontend_id,
            "config_revision": origin.config_revision,
            "generation_id": origin.generation_id,
            "execution_id": origin.execution_id,
            "author": request.author,
        }

        def append_event(data: dict[str, object]) -> None:
            nonlocal event_index, omitted_events
            if len(events) < MODEL_OBSERVATION_MAX_EVENTS:
                events.append({"event_index": event_index, **data, **common})
            else:
                omitted_events += 1
            event_index += 1

        if result.output_events:
            seen_streams = set()
            for output in result.output_events:
                if output.kind == "stream":
                    name, text = output.data.get("name"), output.data.get("text")
                    if name in ("stdout", "stderr") and isinstance(text, str) and text:
                        seen_streams.add(name)
                        append_event({
                            "stream": name,
                            "text": _strip_observation_terminal_controls(text),
                        })
                elif output.kind in ("display", "execute_result", "update"):
                    mime_types, mime_types_truncated = _observation_mime_types(output.data)
                    fallback = output.data.get("text/plain")
                    if isinstance(fallback, str) and display_chars_remaining:
                        display = _bounded_plain_fallback(fallback, display_chars_remaining)
                        display_chars_remaining -= len(display)
                    else:
                        display = ""
                    if not display:
                        summary = ", ".join(mime_types) or "no safe MIME types"
                        if mime_types_truncated:
                            summary += ", additional MIME types omitted"
                        display = f"[rich output omitted; available MIME types: {summary}]"
                    append_event({
                        "display": display,
                        "output_kind": output.kind,
                        "mime_types": mime_types,
                        "mime_types_truncated": mime_types_truncated,
                    })
                elif output.kind == "clear":
                    clear_event = {"clear": True, "output_kind": "clear"}
                    wait = output.data.get("wait")
                    if type(wait) is bool:
                        clear_event["wait"] = wait
                    append_event(clear_event)
            for stream, text in (("stdout", result.stdout), ("stderr", result.stderr)):
                if text and stream not in seen_streams:
                    append_event({
                        "stream": stream,
                        "text": _strip_observation_terminal_controls(text),
                    })
        else:
            for stream, text in (("stdout", result.stdout), ("stderr", result.stderr)):
                if text:
                    append_event({
                        "stream": stream,
                        "text": _strip_observation_terminal_controls(text),
                    })

        for say_output in result.say_outputs:
            content = say_output.content
            if isinstance(content, str):
                content = _strip_observation_terminal_controls(content)
            append_event({"say": content, "final": say_output.final})
        if result.status != "success":
            append_event({
                "error": _strip_observation_terminal_controls(
                    result.error or f"Execution {result.status}"
                ),
                "status": result.status,
            })
        if omitted_events:
            events.append({
                "event_index": event_index,
                "omitted_events": omitted_events,
                **common,
            })

        if self.coordinator.observations is None:
            observed = [
                event.get("text", event.get("display", ""))
                for event in events
                if "stream" in event or "display" in event
            ]
            observed_text = [text for text in observed if isinstance(text, str) and text]
            if result.output_events:
                output_text = "\n".join(observed_text)
            else:
                output_text = "".join(
                    cast(str, event["text"]) for event in events if "stream" in event
                )
            says = "\n".join(
                _strip_observation_terminal_controls(self.coordinator._say_text(output.content))
                for output in result.say_outputs
            )
            if says:
                output_text += ("\n" if output_text else "") + says
            if omitted_events:
                output_text += (
                    ("\n" if output_text else "")
                    + f"[{omitted_events} execution output events omitted]"
                )
            error = _strip_observation_terminal_controls(result.error or "no error details")
            feedback = output_text + (
                f"\nExecution {result.status}: {error}"
                if result.status != "success" else ""
            )
            if len(feedback) > 8_000:
                return f"Output too long ({len(feedback)} chars); omitted."
            return feedback
        pack = getattr(self.coordinator.observations, "pack", None)
        if not callable(pack):
            raise TypeError("Selected observation service must expose pack")
        packed: dict[str, Any] = pack(events)
        if not hasattr(packed, "items"):
            raise TypeError("Observation service must return a mapping")
        if (result.output_reference is not None and hasattr(packed, "get")
                and packed.get("_output_already_omitted") is True
                and isinstance(packed.get("output"), str)):
            packed = {**packed, "output": packed["output"] + (
                f" Stream text is saved as outputs[{result.output_reference}]."
            )}
        serialized = json.dumps(packed, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        # Plain observation text is the actual model content, not its JSON
        # encoding. Labels and JSON escapes must not consume its raw-text budget.
        plain_output = set(packed) <= {"output", "_output_already_omitted"} and isinstance(
            packed.get("output"), str,
        )
        oversized = len(packed["output"]) > 16_000 if plain_output else len(serialized) > 8_000
        if oversized:
            oversized_fallback: dict[str, object] = {
                "error": f"Observation too long ({len(serialized)} chars); omitted.",
                "status": "output_too_large", "executed": True,
            }
            if result.output_reference is not None:
                oversized_fallback["error"] = cast(str, oversized_fallback["error"]) + (
                    f" Stream text is saved as outputs[{result.output_reference}]."
                )
                oversized_fallback["_stored_output_index"] = result.output_reference
            return oversized_fallback
        if result.output_reference is not None:
            return {**packed, "_stored_output_index": result.output_reference}
        return packed


    # Compatibility for observation consumers that exercised the old helper.
    _packed_observation = packed_observation
