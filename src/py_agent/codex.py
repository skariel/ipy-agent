"""Python-native, no-tools adapter for ChatGPT's Codex subscription endpoint.

This is intentionally separate from litelm's API-key adapters. Credentials are
resolved on every request; native py tokens refresh, explicit Pi files remain read-only. The backend does
not accept max_output_tokens. Completed responses are not rejected for exceeding
an artificial local token budget. Complete-response validation remains; partial
stream text is never returned for execution.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any
import uuid

import httpx

from .provider import Completion, ProviderError, _ssl_failure_kind, _validate_messages

CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"
REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
_TERMINAL = {"response.completed", "response.done", "response.incomplete", "response.failed", "response.cancelled"}
_DELTA_EVENTS = {
    "response.output_text.delta",
    "response.output_text.done",
    "response.reasoning_summary_text.delta",
    "response.reasoning_summary_text.done",
    "response.reasoning_text.delta",
    "response.reasoning_text.done",
}
_METADATA_EVENTS = {"response.created", "response.in_progress", "response.queued"}
_PART_EVENTS = {
    "response.content_part.added",
    "response.content_part.done",
    "response.reasoning_summary_part.added",
    "response.reasoning_summary_part.done",
}


def read_codex_credentials(path):
    # Lazy import permits the independent adapter/auth modules to be tested and
    # wired separately. There is no fallback to environment or API-key auth.
    from .codex_auth import read_codex_credentials as read

    return read(path)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _parse_event(data: bytes) -> dict:
    try:
        result = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")),
        )
        if not isinstance(result, dict) or not isinstance(result.get("type"), str):
            raise ValueError("Missing event type")
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise ProviderError("Malformed Codex SSE event; no source accepted", kind="shape") from None


def _json_response_event(data: bytes, secrets=()) -> dict:
    """Accept only a native Responses object/envelope, never guessed source."""
    try:
        obj = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")),
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ProviderError("Malformed Codex JSON response; no source accepted", kind="response_format") from None
    if isinstance(obj, dict):
        if obj.get("type") in _TERMINAL and isinstance(obj.get("response"), dict):
            return obj
        if "status" in obj and isinstance(obj.get("output"), list):
            return {"type": "response.done", "response": obj}
        error = obj.get("error", obj.get("detail", obj.get("message")))
        if isinstance(error, dict):
            error = error.get("message", error.get("code"))
        if isinstance(error, str):
            detail = _redact(error, secrets)[:512]
            raise ProviderError(f"Codex returned a JSON error: {detail}; no source accepted", kind="response_error")
    raise ProviderError(
        "Codex returned unsupported JSON, not a Responses result; no source accepted", kind="response_format"
    )


async def _events(response, secrets=()):
    """Validate the complete payload, regardless of a proxy's MIME label.

    Native JSON Responses results go through the same terminal-status, refusal,
    output-shape and usage checks as SSE. HTML/other bodies never become source.
    """
    pending = bytearray()
    scan_from = 0
    data_lines: list[bytes] = []
    event_name = None
    done_marker = False
    wire_format = None
    media_type = response.headers.get("content-type", "missing")[:120]
    async for chunk in response.aiter_bytes(chunk_size=4096):
        pending.extend(chunk)
        if wire_format is None:
            start = bytes(pending).lstrip()
            if not start:
                continue
            wire_format = "json" if start[:1] in (b"{", b"[") else "sse"
        if wire_format == "json":
            continue
        while True:
            newline = pending.find(b"\n", scan_from)
            if newline < 0:
                scan_from = len(pending)
                break
            line = bytes(pending[:newline]).removesuffix(b"\r")
            del pending[: newline + 1]
            scan_from = 0
            if not line:
                if data_lines:
                    data = b"\n".join(data_lines)
                    data_lines = []
                    if data == b"[DONE]":
                        if done_marker:
                            raise ProviderError("Duplicate Codex SSE done marker", kind="shape")
                        done_marker = True
                        yield {"type": "_sse_done"}
                    else:
                        if done_marker:
                            raise ProviderError("Codex event after SSE done marker", kind="shape")
                        event = _parse_event(data)
                        if event_name not in (None, b"message", event["type"].encode("utf-8")):
                            raise ProviderError("Codex SSE event name/type mismatch", kind="shape")
                        yield event
                event_name = None
            elif line.startswith(b":"):
                continue
            else:
                field, _, value = line.partition(b":")
                value = value.removeprefix(b" ")
                if field == b"data":
                    data_lines.append(value)
                elif field == b"event":
                    event_name = value
                elif field not in (b"id", b"retry"):
                    raise ProviderError(
                        f"Unsupported Codex SSE field/non-SSE response (Content-Type: {media_type!r}); no source accepted",
                        kind="response_format",
                    )
    if wire_format == "json":
        yield _json_response_event(bytes(pending), secrets)
        return
    if pending or data_lines or event_name is not None:
        raise ProviderError(
            f"Truncated Codex SSE frame or non-SSE response (Content-Type: {media_type!r}); no source accepted",
            kind="response_format",
        )


def _usage(raw: Any) -> dict:
    if raw is None:
        return {"source": "unknown", "raw": None, "normalized": {}}
    if not isinstance(raw, dict):
        raise ProviderError("Unsupported Codex usage shape", kind="shape")
    normalized = {}
    for name, path in {
        "input_tokens": ("input_tokens",),
        "output_tokens": ("output_tokens",),
        "total_tokens": ("total_tokens",),
        "cache_read_tokens": ("input_tokens_details", "cached_tokens"),
        "cache_write_tokens": ("input_tokens_details", "cache_write_tokens"),
        "reasoning_tokens": ("output_tokens_details", "reasoning_tokens"),
    }.items():
        value = raw
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if value is not None:
            if type(value) is not int or value < 0:
                raise ProviderError("Invalid Codex usage counter", kind="shape")
            normalized[name] = value
    return {"source": "reported_by_codex", "raw": deepcopy(raw), "normalized": normalized}


def _item_error(item: Any, *, final=False) -> str | None:
    if not isinstance(item, dict):
        return "Unsupported Codex output item"
    if item.get("type") not in {"message", "reasoning"}:
        return "Unexpected Codex tool call or unsupported output item"
    if item.get("tool_calls") or item.get("function_call") or item.get("refusal"):
        return "Unexpected Codex tool call or refusal"
    if item["type"] == "reasoning":
        summary = item.get("summary", [])
        if not isinstance(summary, list) or any(
            not isinstance(p, dict) or p.get("type") != "summary_text" or not isinstance(p.get("text"), str)
            for p in summary
        ):
            return "Unsupported Codex reasoning summary"
        return None
    if item.get("role") != "assistant":
        return "Codex message is not from assistant"
    if final and item.get("status") != "completed":
        return "Codex output message did not complete"
    if item.get("phase") not in (None, "final_answer", "commentary"):
        return "Unsupported Codex message phase"
    content = item.get("content")
    if not isinstance(content, list):
        return "Unsupported Codex message content"
    if any(
        not isinstance(part, dict) or part.get("type") != "output_text" or not isinstance(part.get("text"), str)
        for part in content
    ):
        return "Codex refusal or unsupported content block"
    return None


def _completed_items(events):
    """Validate the sparse-terminal SSE variant without using any text deltas."""
    added, completed, ids = {}, {}, set()
    for event in events:
        index, item = event.get("output_index"), event.get("item")
        if type(index) is not int or index < 0 or not isinstance(item, dict):
            return [], "Codex completed-item events lack valid output indices"
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier:
            return [], "Codex completed-item event lacks an item ID"
        if event["type"] == "response.output_item.added":
            if index in added or identifier in ids:
                return [], "Duplicate Codex output item"
            added[index] = item
            ids.add(identifier)
        else:
            original = added.get(index)
            if original is None or index in completed:
                return [], "Codex output item completed without a unique start"
            if original.get("id") != identifier or original.get("type") != item.get("type"):
                return [], "Uncorrelated Codex completed output item"
            error = _item_error(item, final=True)
            if error:
                return [], error
            completed[index] = item
    if not completed or set(completed) != set(added) or sorted(completed) != list(range(len(completed))):
        return [], "Codex output-item sequence is incomplete"
    return [completed[index] for index in sorted(completed)], None


def _redact(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {_redact(key, secrets): _redact(item, secrets) for key, item in value.items()}
    return value


class CodexProvider:
    def __init__(
        self,
        model: str,
        auth_file: Path | None = None,
        *,
        session_id: str | None = None,
        effort: str = "medium",
        transport=None,
    ):
        model = self._validated_model(model)
        if session_id is not None and (not isinstance(session_id, str) or not session_id):
            raise ValueError("session_id must be a nonempty string")
        if effort not in REASONING_EFFORTS:
            raise ValueError("Unsupported Codex reasoning effort")
        self.model = model
        self.effort = effort
        self.auth_file = Path(auth_file) if auth_file is not None else None
        # A provider instance belongs to one py run. This opaque random value is
        # independent of prompts, paths, and credentials and survives every turn.
        self.session_id = session_id or uuid.uuid4().hex
        self._transport = transport  # deterministic tests; never an endpoint override

    @staticmethod
    def _validated_model(model: str) -> str:
        if not isinstance(model, str) or not re.fullmatch(
            r"openai-codex/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", model,
        ):
            raise ValueError("Select an explicit openai-codex/<model> model")
        return model

    def set_model(self, model: str) -> None:
        self.model = self._validated_model(model)

    def build_request(self, messages: list[dict], *, max_tokens: int | None = None) -> dict:
        messages = _validate_messages(messages, max_tokens)
        instructions = []
        inputs = []
        for message in messages:
            role, text = message["role"], message["content"]
            if role == "system":
                if inputs:
                    raise ValueError("Codex system instructions must precede conversation messages")
                instructions.append(text)
            elif role == "user":
                inputs.append({"role": "user", "content": [{"type": "input_text", "text": text}]})
            else:
                # Replay the executed cell's actual phase, not discarded later
                # messages. Observations remain user input, never assistant output.
                inputs.append({
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "phase": message.get("phase", "final_answer"),
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                })
        return {
            "model": self.model.split("/", 1)[1],
            "instructions": "\n\n".join(instructions),
            "input": inputs,
            "prompt_cache_key": self.session_id,
            "store": False,
            "stream": True,
            "reasoning": {"effort": self.effort},
            "text": {"verbosity": "low"},
        }

    def request_details(self, messages: list[dict], *, max_tokens: int | None = None) -> dict:
        """Exact JSON request plus nonsecret policy, for the supervisor journal."""
        return {
            "adapter": "codex_subscription_sse",
            "url": CODEX_URL,
            "body": self.build_request(messages, max_tokens=max_tokens),
            "headers": {"session-id": self.session_id, "x-client-request-id": self.session_id},
            "ignored_max_tokens": max_tokens,
            "output_limit_enforcement": "none",
            "auth": "explicit_read_only" if self.auth_file is not None else "py_oauth_with_pi_fallback",
            "remote_output_token_cap": False,
        }

    async def generate(self, messages: list[dict], *, max_tokens: int | None = None) -> Completion:
        body = self.build_request(messages, max_tokens=max_tokens)
        credentials = await asyncio.to_thread(read_codex_credentials, self.auth_file)
        headers = {
            "Authorization": f"Bearer {credentials.access}",
            "chatgpt-account-id": credentials.account_id,
            "OpenAI-Beta": "responses=experimental",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "session-id": self.session_id,
            "x-client-request-id": self.session_id,
            "originator": "py-agent",
            "User-Agent": "py-agent/0.1.0",
        }
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                follow_redirects=False,
                trust_env=True,
                timeout=httpx.Timeout(None, connect=20, pool=20),
            ) as client:
                async with client.stream("POST", CODEX_URL, json=body, headers=headers) as response:
                    if response.status_code != 200:
                        status = response.status_code
                        if status in {401, 403}:
                            raise ProviderError(
                                f"Codex authentication rejected (HTTP {status}); retry py login openai-codex (Pi files: pi /login openai-codex)",
                                kind="authentication",
                            )
                        kind = (
                            "rate_limit" if status == 429 else
                            "timeout" if status == 408 else
                            "overflow" if status == 413 else
                            "provider" if 500 <= status < 600 else
                            "request" if 400 <= status < 500 else "response_status"
                        )
                        raise ProviderError(f"Codex request failed (HTTP {status}); no source accepted", kind=kind)
                    # Validate actual SSE/JSON data, not just the MIME header:
                    # proxies may omit or relabel a valid streamed response.
                    secrets = (credentials.access, credentials.account_id)
                    result = await self._collect(response, max_tokens, secrets=secrets)
                    # Provider error/output bodies must not become a token echo.
                    safe_raw = _redact(result.raw, secrets)
                    if safe_raw != result.raw:
                        return Completion(
                            "",
                            "error",
                            _redact(result.usage, secrets),
                            None,
                            safe_raw,
                            "Codex response echoed credential material",
                        )
                    return result
        except asyncio.CancelledError:
            raise
        except ProviderError as exc:
            safe = _redact(str(exc), (credentials.access, credentials.account_id))
            raise ProviderError(safe, kind=exc.kind) from None
        except Exception as exc:
            # Raw SSL failures can escape httpx during stream reads. Classify
            # them before generic transport wrappers so certificate failures
            # never become retryable connection errors. Unknown local errors
            # remain nonretryable; never expose URL/header details.
            kind = _ssl_failure_kind(exc)
            if kind is None:
                kind = (
                    "timeout" if isinstance(exc, httpx.TimeoutException) else
                    "transport" if isinstance(exc, httpx.TransportError) else "internal"
                )
            raise ProviderError(
                f"Codex request failed ({type(exc).__name__}); no source accepted", kind=kind
            ) from None

    async def _collect(self, response, max_tokens, *, secrets=()):
        terminal = None
        terminal_type = None
        rejection = None
        event_count = 0
        for_audit = []
        item_events = []
        async for event in _events(response, secrets):
            event_count += 1
            kind = event["type"]
            if kind == "_sse_done":
                if terminal is None:
                    rejection = rejection or "Codex SSE ended before terminal response"
                continue
            for_audit.append(event)
            if terminal is not None:
                rejection = rejection or "Codex content or duplicate final after terminal response"
                continue
            if kind in _TERMINAL:
                terminal_type = kind
                terminal = event.get("response")
                if not isinstance(terminal, dict):
                    raise ProviderError("Codex terminal response is missing", kind="shape")
            elif kind in _METADATA_EVENTS:
                metadata = event.get("response")
                if not isinstance(metadata, dict):
                    rejection = rejection or "Unsupported Codex response metadata"
                else:
                    early_output = metadata.get("output", [])
                    if not isinstance(early_output, list):
                        rejection = rejection or "Unsupported Codex early output"
                    else:
                        for item in early_output:
                            rejection = rejection or _item_error(item)
                    if (
                        metadata.get("error")
                        or metadata.get("tools")
                        or metadata.get("status") in {"failed", "cancelled", "incomplete"}
                    ):
                        rejection = rejection or "Unexpected Codex failure or tools in response metadata"
            elif kind in {"response.output_item.added", "response.output_item.done"}:
                item_events.append(event)
                rejection = rejection or _item_error(event.get("item"), final=kind.endswith("done"))
            elif kind in _PART_EVENTS:
                part = event.get("part")
                expected = "summary_text" if "summary" in kind else "output_text"
                if not isinstance(part, dict) or part.get("type") != expected or not isinstance(part.get("text"), str):
                    rejection = rejection or "Codex refusal or unsupported content part"
            elif kind in _DELTA_EVENTS:
                field = "delta" if kind.endswith("delta") else "text"
                if not isinstance(event.get(field), str):
                    rejection = rejection or "Unsupported Codex text delta"
            else:
                # Report only the bounded protocol event *type*, never the SSE
                # payload (which may contain model text, credentials or data).
                # Unknown events still invalidate the entire generated cell.
                safe_kind = (
                    kind if re.fullmatch(r"response\.[A-Za-z0-9_.-]{1,80}", kind)
                    else "error" if kind == "error" else "unrecognized"
                )
                rejection = rejection or f"Codex rejected unsupported stream event type: {safe_kind}"
        if terminal is None:
            raise ProviderError("Codex stream ended without terminal response; no source accepted", kind="shape")
        usage = _usage(terminal.get("usage"))
        status = terminal.get("status")
        finish = (
            "stop" if terminal_type in {"response.completed", "response.done"} and status == "completed" else "error"
        )
        if (
            status == "incomplete"
            and isinstance(terminal.get("incomplete_details"), dict)
            and terminal["incomplete_details"].get("reason") == "max_output_tokens"
        ):
            finish = "length"
        if finish != "stop" or terminal.get("error") or terminal.get("incomplete_details"):
            rejection = rejection or "Codex response did not complete successfully"
        if terminal.get("tools") or terminal.get("tool_calls") or terminal.get("refusal"):
            rejection = rejection or "Unexpected Codex tools or refusal in terminal response"
        output = terminal.get("output")
        output_source = "terminal_response"
        if output == [] and item_events:
            # Some Codex SSE responses finish with output:[]; the complete
            # items were already delivered in output_item.done. This is NOT
            # reconstruction from deltas. Require a complete correlated ledger
            # plus the successful response-level terminal marker above.
            output, item_rejection = _completed_items(item_events)
            rejection = rejection or item_rejection
            output_source = "completed_item_events"
        messages, summaries = [], []
        if not isinstance(output, list):
            rejection = rejection or "Codex terminal output must be a list"
        else:
            for item in output:
                error = _item_error(item, final=True)
                if error:
                    rejection = rejection or error
                    continue
                if item["type"] == "message":
                    messages.append(item)
                else:
                    summaries.extend(part["text"] for part in item.get("summary", []))
        if sum(item.get("phase") != "commentary" for item in messages) > 1:
            rejection = rejection or "Multiple final Codex assistant messages"
        # One cell, then a REAL execution observation. Later messages in this
        # response were generated without that observation and cannot be acted
        # on. A native 'final_answer' label is NOT the agent's say(final=True).
        # This selection happens only after validating the complete response;
        # reasoning items, partial deltas and unsuccessful responses never run.
        selected = messages[0] if messages else None
        text = "".join(part["text"] for part in selected["content"]) if selected else ""
        if not text.strip():
            rejection = rejection or "Codex returned no executable text"
        raw = {
            "response": terminal,
            "events": for_audit,
            "event_count": event_count,
            "output_source": output_source,
            "cell_selection": "first_complete_assistant_message",
            "selected_phase": selected.get("phase") if selected else None,
            "discarded_followup_messages": max(0, len(messages) - 1),
            "ignored_max_tokens": max_tokens,
            "output_limit_enforcement": "none",
        }
        # Rejected content stays only in raw evidence, never in executable text.
        return Completion(
            text if rejection is None else "",
            finish,
            usage,
            "\n\n".join(summaries) or None,
            raw,
            rejection,
            phase=selected.get("phase") if selected else None,
        )
