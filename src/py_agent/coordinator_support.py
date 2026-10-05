"""Shared coordinator types, limits, and pure observation helpers."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
import re

from .configuration import ConfigSnapshot
from .contracts import InputHandler, ProgressCallback, QueueOutcome, RoutedAction
from .session_journal import MAX_EVENT_BYTES

MAX_PENDING_ACTIONS = 32
HISTORY_DEFAULT_LIMIT = 10
HISTORY_MAX_ITEMS = 20
HISTORY_MAX_PAGE_CHARS = 4_000
HISTORY_MAX_COMMAND_CHARS = 1_024
HISTORY_MAX_OFFSET = MAX_EVENT_BYTES
HISTORY_MAX_SENSITIVE_VALUES = 256
HISTORY_MAX_SENSITIVE_CHARS = 65_536
HISTORY_MAX_SENSITIVE_VALUE_CHARS = 256
MAX_AGENT_RESPONSE_CHARS = 8_000
MODEL_OBSERVATION_MAX_EVENTS = 256
MODEL_OBSERVATION_MAX_DISPLAY_CHARS = 8_000
MODEL_OBSERVATION_MAX_MIME_TYPES = 16
_OBSERVATION_MIME_TYPE = re.compile(r"[A-Za-z0-9!#$&^_.+-]{1,64}/[A-Za-z0-9!#$&^_.+-]{1,64}\Z")
_TERMINAL_ESCAPE = re.compile(
    r"(?:"
    r"\x1b\].*?(?:\x07|\x1b\\|\x9c)|\x9d.*?(?:\x07|\x1b\\|\x9c)|"
    r"\x1b[P^_X].*?(?:\x1b\\|\x9c)|[\x90\x98\x9e\x9f].*?\x9c|"
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x9b[0-?]*[ -/]*[@-~]|\x1b[@-_]"
    r")",
    re.DOTALL,
)
_TERMINAL_INCOMPLETE_ESCAPE = re.compile(
    r"(?:\x1b\].*|\x9d.*|\x1b[P^_X].*|[\x90\x98\x9e\x9f].*|"
    r"\x1b\[[0-?]*[ -/]*|\x9b[0-?]*[ -/]*)\Z",
    re.DOTALL,
)
_HISTORY_USAGE = (
    "Usage: /history [recent [COUNT]] | /history search [--limit COUNT] "
    "[--kind KIND] QUERY | /history page EVENT_ID [OFFSET [CHARS]]"
)
_CONTEXT_USAGE = "Usage: /context save [PATH]"
_MODEL_USAGE = "Usage: /model [MODEL_ID]"
_EFFORT_PRESETS = ("none", "minimal", "low", "medium", "high", "xhigh")
_EFFORT_USAGE = "Usage: /effort [none|minimal|low|medium|high|xhigh]"
_COORDINATOR_COMMANDS = frozenset({"history", "context", "model", "effort"})


def _strip_observation_terminal_controls(text: str) -> str:
    safe = _TERMINAL_ESCAPE.sub("", text)
    safe = _TERMINAL_INCOMPLETE_ESCAPE.sub("", safe)
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", safe)


def _bounded_plain_fallback(
    text: str, limit: int = MODEL_OBSERVATION_MAX_DISPLAY_CHARS,
) -> str:
    """Keep bounded plain text, stripping terminal control sequences."""
    truncated = len(text) > limit
    safe = _strip_observation_terminal_controls(text[:limit])
    if truncated:
        marker = "\n[plain-text fallback truncated]"
        if limit <= len(marker):
            return marker[:limit]
        safe = safe[:limit - len(marker)] + marker
    return safe


def _observation_mime_types(data: object) -> tuple[list[str], bool]:
    if not hasattr(data, "keys"):
        return [], False
    mime_types = []
    for name in data:
        if not isinstance(name, str) or _OBSERVATION_MIME_TYPE.fullmatch(name) is None:
            continue
        if len(mime_types) >= MODEL_OBSERVATION_MAX_MIME_TYPES:
            return mime_types, True
        mime_types.append(name)
    return mime_types, False


class State(str, Enum):
    NEW = "new"
    IDLE = "idle"
    GENERATING = "generating"
    EXECUTING = "executing"
    WAITING_FOR_INPUT = "waiting-for-input"
    COMMAND = "command"
    STOPPING = "stopping"
    FAILED = "failed"
    CLOSED = "closed"


@dataclass
class _QueuedAction:
    action: RoutedAction
    text: str
    config: ConfigSnapshot | None
    allow_stdin: bool
    input_handler: InputHandler | None
    on_progress: ProgressCallback | None
    completion: asyncio.Future[QueueOutcome]


