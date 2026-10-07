"""Per-request ownership and callbacks; never stored on the session runtime."""

from __future__ import annotations

from dataclasses import dataclass, field

from .configuration import ConfigSnapshot
from .contracts import (
    InputHandler,
    Origin,
    ProgressCallback,
    RoutedAction,
)
from .coordinator_support import _QueuedAction
from .runtime_status import Recovery


@dataclass
class RequestScope:
    origin: Origin
    operation_id: str
    config: ConfigSnapshot | None
    allow_stdin: bool
    input_handler: InputHandler | None
    on_progress: ProgressCallback | None
    routed: RoutedAction | None = None
    resume_checkpoint: Recovery | None = None
    context_pending: bool = False
    steering_commit_started: bool = False
    steering_awaiting_dispatch: list[_QueuedAction] = field(default_factory=list)
