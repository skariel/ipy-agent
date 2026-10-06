"""Coordinator conversation component; orchestration remains in Coordinator."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import inspect
import json
from pathlib import Path
import re
import shlex
from typing import TYPE_CHECKING

from .configuration import ConfigSnapshot
from .context_export import default_export_path, write_context_html
from .contracts import ContextSnapshot, ExecutorCapabilities
from .coordinator_support import (
    _CONTEXT_USAGE,
    _COORDINATOR_COMMANDS,
    _HISTORY_USAGE,
    HISTORY_DEFAULT_LIMIT,
    HISTORY_MAX_COMMAND_CHARS,
    HISTORY_MAX_OFFSET,
    HISTORY_MAX_PAGE_CHARS,
    HISTORY_MAX_SENSITIVE_CHARS,
    HISTORY_MAX_SENSITIVE_VALUE_CHARS,
    HISTORY_MAX_SENSITIVE_VALUES,
)
from .session_journal import (
    MAX_HISTORY_PAGE_CHARS,
    MAX_HISTORY_SEARCH_BYTES,
    MAX_HISTORY_SEARCH_QUERY_CHARS,
    MAX_HISTORY_SEARCH_SCAN,
    JournalError,
)

if TYPE_CHECKING:
    from .coordinator import Coordinator


class ConversationState:
    """Owns conversation state and behavior for one coordinator."""

    def __init__(self, coordinator: Coordinator) -> None:
        self.coordinator = coordinator
        self._epoch_config = None
        self._epoch_config_epoch = None
        self._context: list[tuple[str, str]] = []
        self._context_epoch = 0
        self._context_images = []
        self._context_overlay: list[tuple[int, str]] = []
        self._context_overlay_epoch: int | None = None
        self._history_sensitive_values: set[str] = set()
        self._history_sensitive_chars = 0
        self._history_content_hidden = False
        self._journal_sensitive_config_ready = True

    def _commit_fallback_context(self, user_text: str, assistant_text: str,
                                 observation: str | None = None, *, include_user: bool = True) -> None:
        if include_user:
            self._context.append(("user", user_text))
        self._context.append(("assistant", assistant_text))
        if observation is not None:
            self._context.append(("observation", observation[:8000]))
        self._context_epoch += 1

    def _capture_history_sensitive_config(self, snapshot: ConfigSnapshot | None) -> None:
        """Retain bounded sensitive values and install write-time journal redaction."""
        if snapshot is None or self.coordinator.config_store is None:
            return
        for name, field in self.coordinator.config_store.registry.fields.items():
            if not field.sensitive:
                continue
            entry = snapshot.entries.get(name)
            if entry is None or entry.value is None or entry.value == "":
                continue
            value = entry.value
            if not isinstance(value, str):
                self._history_content_hidden = True
                if getattr(self.coordinator.journal, "persisted", None) is True:
                    self._journal_sensitive_config_ready = False
                continue
            if len(value) < 4 or len(value) > HISTORY_MAX_SENSITIVE_VALUE_CHARS:
                self._history_content_hidden = True
            if value in self._history_sensitive_values:
                continue
            if (len(self._history_sensitive_values) >= HISTORY_MAX_SENSITIVE_VALUES
                    or self._history_sensitive_chars + len(value) > HISTORY_MAX_SENSITIVE_CHARS):
                self._history_content_hidden = True
                self._journal_sensitive_config_ready = False
                continue
            self._history_sensitive_values.add(value)
            self._history_sensitive_chars += len(value)
        if getattr(self.coordinator.journal, "persisted", None) is True:
            try:
                self.coordinator.journal.set_sensitive_values(tuple(sorted(self._history_sensitive_values)))
            except Exception:
                self._journal_sensitive_config_ready = False

    def _history_patterns(self) -> tuple[str, ...]:
        patterns = set()
        for value in self._history_sensitive_values:
            patterns.add(value)
            escaped = json.dumps(value, ensure_ascii=False)[1:-1]
            if escaped:
                patterns.add(escaped)
        return tuple(sorted(patterns, key=len, reverse=True))

    def _redact_history_text(self, text: str, *, preserve_offsets: bool = False) -> str:
        if self._history_content_hidden:
            return "[history content hidden to protect sensitive configuration]"
        if self._history_sensitive_values and not preserve_offsets:
            # recent/search API excerpts may cut through a sensitive value. The
            # paged read path uses overlap-aware masking; short excerpts are
            # omitted whenever config-derived secret values are in scope.
            return "[history excerpt hidden to protect sensitive configuration]"
        patterns = self.coordinator._history_patterns()
        if not patterns:
            return text
        matcher = re.compile("|".join(re.escape(pattern) for pattern in patterns), re.IGNORECASE)
        return matcher.sub(
            lambda match: ("█" * len(match.group(0))) if preserve_offsets else "[REDACTED]",
            text,
        )

    def _read_history_page(self, event_id: str, offset: int, limit: int) -> dict[str, object]:
        page = self.coordinator.journal.read(self.coordinator.session_id, event_id, offset=offset, limit=limit)
        content = page.get("content")
        if not isinstance(content, str):
            raise JournalError("Journal returned an invalid history page")
        if self._history_content_hidden:
            page["content"] = self.coordinator._redact_history_text(content)
            return page
        patterns = self.coordinator._history_patterns()
        if not patterns or not content:
            page["content"] = content
            return page
        overlap = max(map(len, patterns)) - 1
        start = max(0, offset - overlap)
        page_chars = len(content)
        extended_limit = min(
            MAX_HISTORY_PAGE_CHARS,
            (offset - start) + page_chars + overlap,
        )
        extended = self.coordinator.journal.read(
            self.coordinator.session_id, event_id, offset=start, limit=extended_limit,
        )
        surrounding = extended.get("content")
        if not isinstance(surrounding, str):
            raise JournalError("Journal returned an invalid history page")
        masked = self.coordinator._redact_history_text(surrounding, preserve_offsets=True)
        page_start = offset - start
        page["content"] = masked[page_start:page_start + page_chars]
        return page

    def _read_context_epoch(self) -> int | None:
        """Read the context epoch when the selected context service exposes it."""
        if self.coordinator.context_service is None:
            return self._context_epoch
        epoch = getattr(self.coordinator.context_service, "epoch", None)
        if type(epoch) is int and epoch >= 0:
            return epoch
        snapshot = getattr(self.coordinator.context_service, "snapshot", None)
        if callable(snapshot):
            try:
                value = snapshot()
            except Exception:
                return None
            if isinstance(value, ContextSnapshot):
                return value.epoch
        return None

    def _observe_context_epoch(
        self, epoch: int | None, *, config: ConfigSnapshot | None = None,
    ) -> None:
        """Latch EPOCH settings only after the context reports a newer epoch."""
        if type(epoch) is not int or epoch < 0:
            return
        if self._epoch_config_epoch is None:
            # The initial epoch could not be inspected; treat the first observed
            # value as a baseline rather than an unverified epoch transition.
            self._epoch_config_epoch = epoch
            if self._context_overlay_epoch is not None and self._context_overlay_epoch != epoch:
                self._context_overlay.clear()
                self._context_overlay_epoch = epoch
        elif epoch > self._epoch_config_epoch:
            self._epoch_config = self.coordinator._current_config() if config is None else config
            self._epoch_config_epoch = epoch
            self._context_overlay.clear()
            self._context_overlay_epoch = epoch

    def _prepare_context(self, text: str, request_id: str) -> ContextSnapshot:
        if self.coordinator.context_service is not None:
            prepare = getattr(self.coordinator.context_service, "prepare_request", None)
            if callable(prepare):
                snapshot = prepare(text, request_id)
                if not isinstance(snapshot, ContextSnapshot):
                    raise TypeError("Context service must return a ContextSnapshot")
                overlay = tuple(
                    ("user", message) for epoch, message in self._context_overlay
                    if epoch == snapshot.epoch
                ) if self._context_overlay_epoch == snapshot.epoch else ()
                if overlay:
                    insertion = (
                        len(snapshot.messages) - 1
                        if snapshot.messages and snapshot.messages[-1] == ("user", text)
                        else len(snapshot.messages)
                    )
                    messages = list(snapshot.messages)
                    phases = list(snapshot.message_phases)
                    messages[insertion:insertion] = overlay
                    phases[insertion:insertion] = [None] * len(overlay)
                    snapshot = replace(
                        snapshot, messages=tuple(messages), message_phases=tuple(phases),
                        images=tuple((i + len(overlay) if i >= insertion else i, image)
                                     for i, image in snapshot.images),
                    )
                return snapshot
            # The v1 ContextService protocol exposes reset policy and usage
            # accounting but not mutation methods. Keep a minimal compatible
            # append-only snapshot until a service supplies the richer adapter.
            needs_reset = getattr(self.coordinator.context_service, "needs_reset", None)
            if callable(needs_reset) and needs_reset():
                self._context.clear()
                self._context_images.clear()
                self._context_epoch += 1
        return ContextSnapshot(self._context_epoch, (*self._context, ("user", text)), images=tuple(self._context_images))

    def _latest_context(self) -> ContextSnapshot:
        snapshot = getattr(self.coordinator.context_service, "snapshot", None) if self.coordinator.context_service is not None else None
        if callable(snapshot):
            value = snapshot()
            if not isinstance(value, ContextSnapshot):
                raise TypeError("Context service snapshot() must return ContextSnapshot")
            if self._context_overlay and self._context_overlay_epoch == value.epoch:
                additions = tuple(
                    ("user", text) for epoch, text in self._context_overlay if epoch == value.epoch
                )
                value = replace(
                    value,
                    messages=(*value.messages, *additions),
                    message_phases=(*value.message_phases, *((None,) * len(additions))),
                )
            return value
        return ContextSnapshot(self._context_epoch, tuple(self._context), images=tuple(self._context_images))

    def _append_steering_context(self, text: str, request_id: str) -> None:
        """Append steering after the completed cell's observation, never mid-request."""
        if self.coordinator.context_service is None:
            self._context.append(("user", text))
            return
        add = getattr(self.coordinator.context_service, "add", None)
        if callable(add):
            result = add("user", text, refs=(request_id,))
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                raise TypeError("Context service add() must be synchronous")
            if callable(getattr(self.coordinator.context_service, "snapshot", None)):
                return
            self._context.append(("user", text))
            return
        if not callable(getattr(self.coordinator.context_service, "snapshot", None)):
            self._context.append(("user", text))
            return
        epoch = self.coordinator._read_context_epoch()
        if epoch is None:
            epoch = self._context_epoch
        if self._context_overlay_epoch != epoch:
            self._context_overlay.clear()
            self._context_overlay_epoch = epoch
        self._context_overlay.append((epoch, text))

    async def _archive_context_outputs(self) -> None:
        archive = getattr(self.coordinator.context_service, "archive_execution_outputs", None)
        store = getattr(self.coordinator.executor, "store_outputs", None)
        if callable(archive) and callable(store):
            # An executor lacking this explicit control capability leaves old
            # observations intact rather than claiming nonexistent references.
            await archive(store)

    def _abandon_context(self, request_id: str) -> None:
        if self.coordinator.context_service is None:
            return
        abandon = getattr(self.coordinator.context_service, "abandon_request", None)
        if callable(abandon):
            abandon(request_id)

    def _commit_context(self, request_id: str, user_text: str, assistant_text: str,
                        observation=None, phase: str | None = None, *, include_user: bool = True) -> None:
        if self.coordinator.context_service is None:
            text = None
            if observation is not None:
                if isinstance(observation, str):
                    text = observation
                else:
                    text = json.dumps(observation, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            from .images import MAX_CONTEXT_IMAGE_BYTES, MAX_CONTEXT_IMAGES, ImageAttachment
            records = observation.get("_images", ()) if isinstance(observation, Mapping) else ()
            if isinstance(observation, Mapping):
                text = json.dumps({k: v for k, v in observation.items() if k != "_images"},
                                  ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            self.coordinator._commit_fallback_context(user_text, assistant_text, text, include_user=include_user)
            index = len(self._context) - 1
            self._context_images.extend((index, ImageAttachment.from_record(record)) for record in records)
            self._context_images = self._context_images[-MAX_CONTEXT_IMAGES:]
            while sum(len(image.data) for _, image in self._context_images) > MAX_CONTEXT_IMAGE_BYTES:
                self._context_images.pop(0)
            return
        commit = getattr(self.coordinator.context_service, "commit_response", None)
        if callable(commit):
            commit(request_id, assistant_text, observation=observation, phase=phase)
            if callable(getattr(self.coordinator.context_service, "snapshot", None)):
                return
        text = observation if isinstance(observation, str) else (
            json.dumps(observation, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            if observation is not None else None
        )
        self.coordinator._commit_fallback_context(user_text, assistant_text, text, include_user=include_user)

    def _context_export_payload(self) -> dict[str, object]:
        """Capture current context plus the exact most recent provider request."""
        snapshot = self.coordinator._latest_context()

        def snapshot_data(value: ContextSnapshot) -> dict[str, object]:
            return {
                "epoch": value.epoch,
                "images": [{"message_index": i, **image.record()} for i, image in value.images],
                "messages": [
                    {"role": role, "content": content, "phase": phase}
                    for (role, content), phase in zip(
                        value.messages, value.message_phases, strict=True,
                    )
                ],
                "transform_trace": list(value.transform_trace),
            }

        request = self.coordinator._lifecycle._active_model_request
        request_data = None
        if request is not None:
            request_data = {
                "origin": vars(request.origin),
                "model": request.model,
                "options": dict(request.options),
                "transform_trace": list(request.transform_trace),
                "context": snapshot_data(request.context),
            }

        context = getattr(self.coordinator.context_service, "context", None)
        raw_groups = []
        for group in getattr(context, "groups", ()):
            raw_groups.append({
                "messages": [dict(message) for message in getattr(group, "messages", ())],
                "refs": list(getattr(group, "refs", ())),
                "execution_output_indexes": list(
                    getattr(group, "execution_output_indexes", ()),
                ),
            })
        archives = []
        for index, raw in sorted(getattr(context, "collapsed", {}).items()):
            try:
                content = json.loads(raw)
            except (TypeError, ValueError):
                content = raw
            archives.append({"index": index, "content": content})

        capabilities = getattr(self.coordinator.executor, "capabilities", None)
        capability_data = vars(capabilities) if isinstance(capabilities, ExecutorCapabilities) else None
        core_commands = getattr(self.coordinator.command_registry, "commands", {})
        return {
            "export": {
                "format": "py-context-html-v1",
                "session_id": self.coordinator.session_id,
                "provider_id": self.coordinator.provider_id,
                "model": self.coordinator.model,
                "config_revision": self.coordinator.config_revision,
                "effort_override": self.coordinator._effort_override,
                "warning": "Contains sensitive, unredacted session context.",
            },
            "current_context": snapshot_data(snapshot),
            "last_model_request": request_data,
            "runtime": {
                "services": {
                    "router": type(self.coordinator.router).__name__,
                    "provider": type(self.coordinator.provider).__name__,
                    "interpreter": type(self.coordinator.interpreter).__name__,
                    "executor": type(self.coordinator.executor).__name__,
                    "context": type(self.coordinator.context_service).__name__
                    if self.coordinator.context_service is not None else None,
                    "observations": type(self.coordinator.observations).__name__
                    if self.coordinator.observations is not None else None,
                },
                "executor_capabilities": capability_data,
                "commands": sorted(
                    set(core_commands) | set(self.coordinator.external_commands) | _COORDINATOR_COMMANDS
                ),
                "context_transforms": [
                    item.qualified_name for item in self.coordinator._plugin_runtime.transforms["context"]
                ],
                "model_request_transforms": [
                    item.qualified_name
                    for item in self.coordinator._plugin_runtime.transforms["model-request"]
                ],
                "model_tools": {
                    "registered": [],
                    "note": (
                        "This agent does not send structured tool definitions to the model; "
                        "Python/IPython execution is specified by the system prompt, and its "
                        "observations are included in the messages above."
                    ),
                },
            },
            "raw_context": {
                "groups": raw_groups,
                "reported_input_tokens": getattr(context, "reported_input_tokens", None),
                "window_tokens": getattr(context, "window_tokens", None),
            },
            "collapsed_archives": archives,
        }

    def _context_command(self, arguments: str) -> str:
        if not isinstance(arguments, str) or len(arguments) > 4096:
            return _CONTEXT_USAGE
        try:
            tokens = shlex.split(arguments, posix=True)
        except ValueError:
            return _CONTEXT_USAGE
        if tokens and tokens[0] != "save":
            return _CONTEXT_USAGE
        if len(tokens) > 2:
            return _CONTEXT_USAGE
        path = default_export_path(self.coordinator.session_id) if len(tokens) < 2 else Path(tokens[1])
        try:
            saved = write_context_html(path, self.coordinator._context_export_payload())
        except FileExistsError:
            return f"Context export refused to overwrite existing file: {path}"
        except OSError as exc:
            return f"Context export failed: {exc}"
        return f"Saved private context explorer to {saved} (file mode 0600)."

    def _history_command(self, arguments: str) -> str:
        if getattr(self.coordinator.journal, "persisted", None) is False:
            return (
                "History persistence is disabled: this session selected the explicit "
                "no-persistence journal. No history is stored or replayed."
            )
        if getattr(self.coordinator.journal, "persisted", None) is not True:
            return "History viewing is unavailable for the selected journal service."
        if any(not callable(getattr(self.coordinator.journal, name, None)) for name in ("recent", "search", "read")):
            return "History viewing is unavailable for the selected journal service."
        if not isinstance(arguments, str) or len(arguments) > HISTORY_MAX_COMMAND_CHARS:
            return _HISTORY_USAGE
        try:
            tokens = shlex.split(arguments, posix=True)
        except ValueError:
            return _HISTORY_USAGE
        operation = tokens[0] if tokens else "recent"
        if operation == "recent":
            values = tokens[1:]
            count = self.coordinator._history_count(values, default=HISTORY_DEFAULT_LIMIT)
            if count is None:
                return _HISTORY_USAGE
            return self.coordinator._history_recent(count)
        if operation.isdecimal() and len(tokens) == 1:
            count = self.coordinator._history_count(tokens, default=HISTORY_DEFAULT_LIMIT)
            return self.coordinator._history_recent(count) if count is not None else _HISTORY_USAGE
        if operation == "search":
            return self.coordinator._history_search(tokens[1:])
        if operation in {"page", "read"}:
            return self.coordinator._history_page(tokens[1:])
        return _HISTORY_USAGE

    def _history_recent(self, count: int) -> str:
        try:
            entries = self.coordinator.journal.recent(self.coordinator.session_id, limit=count)
        except Exception:
            return "History is unavailable because the journal could not be read."
        if not entries:
            return "No history entries are recorded for this session. History is read-only; nothing is replayed."
        lines = ["Recent journal events (newest first; read-only, never replayed):"]
        for entry in entries[:count]:
            if not isinstance(entry, dict):
                continue
            event_id = entry.get("id")
            kind = entry.get("kind")
            request_id = entry.get("request_id")
            excerpt = entry.get("excerpt")
            if not isinstance(event_id, str) or not isinstance(kind, str) or not isinstance(excerpt, str):
                continue
            safe_excerpt = self.coordinator._redact_history_text(excerpt[:240])
            safe_id = event_id[:128]
            safe_kind = kind[:100]
            request = f" request={request_id[:128]}" if isinstance(request_id, str) else ""
            lines.append(f"{safe_id} {safe_kind}{request}: {safe_excerpt}")
        if len(lines) == 1:
            return "No readable history entries are available. History is read-only; nothing is replayed."
        return "\n".join(lines)

    def _history_search(self, tokens: list[str]) -> str:
        count = HISTORY_DEFAULT_LIMIT
        kind = None
        query_tokens = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token == "--limit" and not query_tokens and index + 1 < len(tokens):
                parsed = self.coordinator._history_count([tokens[index + 1]], default=HISTORY_DEFAULT_LIMIT)
                if parsed is None:
                    return _HISTORY_USAGE
                count = parsed
                index += 2
                continue
            if token == "--kind" and not query_tokens and index + 1 < len(tokens):
                kind = tokens[index + 1]
                if not kind or len(kind) > 100 or "\x00" in kind:
                    return _HISTORY_USAGE
                index += 2
                continue
            if token.startswith("--") and not query_tokens:
                return _HISTORY_USAGE
            query_tokens.append(token)
            index += 1
        query = " ".join(query_tokens)
        if not query or len(query) > MAX_HISTORY_SEARCH_QUERY_CHARS:
            return _HISTORY_USAGE
        try:
            entries = self.coordinator.journal.search(
                self.coordinator.session_id, query, kind=kind, limit=count,
                scan_limit=MAX_HISTORY_SEARCH_SCAN,
            )
        except Exception:
            return "History search is unavailable; check the query and journal."
        lines = [
            f"History search (at most {MAX_HISTORY_SEARCH_SCAN} events / "
            f"{MAX_HISTORY_SEARCH_BYTES} bytes checked; read-only, never replayed):"
        ]
        for entry in entries[:count]:
            if not isinstance(entry, dict):
                continue
            event_id = entry.get("id")
            event_kind = entry.get("kind")
            excerpt = entry.get("excerpt")
            if not isinstance(event_id, str) or not isinstance(event_kind, str) or not isinstance(excerpt, str):
                continue
            request_id = entry.get("request_id")
            request = f" request={request_id[:128]}" if isinstance(request_id, str) else ""
            offset = entry.get("offset")
            location = (
                f" around={offset}"
                if type(offset) is int and 0 <= offset <= HISTORY_MAX_OFFSET else ""
            )
            safe_excerpt = self.coordinator._redact_history_text(excerpt[:400])
            lines.append(
                f"{event_id[:128]} {event_kind[:100]}{request}{location}: {safe_excerpt}"
            )
        if len(lines) == 1:
            lines.append("No matches.")
        return "\n".join(lines)

    def _history_page(self, tokens: list[str]) -> str:
        if not 1 <= len(tokens) <= 3 or re.fullmatch(r"e[0-9]{12}", tokens[0]) is None:
            return _HISTORY_USAGE
        offset = self.coordinator._history_integer(
            tokens[1], maximum=HISTORY_MAX_OFFSET,
        ) if len(tokens) >= 2 else 0
        limit = self.coordinator._history_integer(
            tokens[2], maximum=HISTORY_MAX_PAGE_CHARS,
        ) if len(tokens) >= 3 else min(2_000, HISTORY_MAX_PAGE_CHARS)
        if offset is None or limit is None or limit < 1:
            return _HISTORY_USAGE
        try:
            page = self.coordinator._read_history_page(tokens[0], offset, limit)
        except KeyError:
            return "No history event matches that ID in this session."
        except Exception:
            return "History page is unavailable because the journal could not be read."
        content = page.get("content")
        next_offset = page.get("next_offset")
        total_chars = page.get("total_chars")
        if (not isinstance(content, str) or type(next_offset) is not int
                or type(total_chars) is not int or next_offset < offset
                or next_offset > HISTORY_MAX_OFFSET or total_chars < next_offset
                or total_chars > HISTORY_MAX_OFFSET):
            return "History page is unavailable because the journal returned invalid data."
        lines = [
            f"Journal event {tokens[0]} characters {offset}-{next_offset} of {total_chars} "
            "(read-only; history is never replayed):",
            content,
        ]
        if page.get("truncated") is True:
            lines.append(f"Next page: /history page {tokens[0]} {next_offset} {limit}")
        return "\n".join(lines)

