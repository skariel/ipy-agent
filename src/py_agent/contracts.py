"""Frontend-neutral execution contracts. API v1 draft, independent of the UI."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
import math
from types import MappingProxyType
from typing import Awaitable, Callable, Literal, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class Origin:
    session_id: str
    request_id: str
    frontend_id: str
    config_revision: int
    generation_id: str | None = None
    execution_id: str | None = None


@dataclass(frozen=True)
class UserAction:
    origin: Origin
    text: str
    metadata: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


MAX_QUEUED_ACTION_CHARS = 65_536
MAX_FRONTEND_ID_CHARS = 128


class QueueFullError(RuntimeError):
    """The coordinator's bounded pending-action queue has no free slots."""


@dataclass(frozen=True)
class QueueOutcome:
    """Terminal result for one accepted queued action.

    ``submission`` is a coordinator Submission for independently dispatched
    work. A ``steered`` outcome has no submission: its text was committed as
    user context in the active agent turn.
    """

    origin: Origin
    status: Literal["steered", "completed", "failed", "interrupted", "closed"]
    submission: Submission | None = field(default=None, repr=False, compare=False)
    error: str | None = None

    def __post_init__(self):
        if not isinstance(self.origin, Origin):
            raise TypeError("QueueOutcome origin must be an Origin")
        if self.status not in {"steered", "completed", "failed", "interrupted", "closed"}:
            raise ValueError("QueueOutcome status is unsupported")
        if self.error is not None and not isinstance(self.error, str):
            raise TypeError("QueueOutcome error must be text or None")
        if self.status == "completed":
            if not isinstance(self.submission, Submission):
                raise TypeError("Completed QueueOutcome requires a Submission")
            if self.submission.action.origin != self.origin:
                raise ValueError("QueueOutcome submission origin must match its request")
        elif self.submission is not None:
            raise ValueError("Only completed QueueOutcome records may carry a Submission")


@dataclass(frozen=True)
class QueueTicket:
    """Stable identity and completion handle returned by ``Coordinator.enqueue``."""

    origin: Origin
    kind: Literal["ask", "execute", "command"]
    position: int
    completion: Awaitable[QueueOutcome] = field(repr=False, compare=False)

    def __post_init__(self):
        if not isinstance(self.origin, Origin):
            raise TypeError("QueueTicket origin must be an Origin")
        if self.kind not in {"ask", "execute", "command"}:
            raise ValueError("QueueTicket kind is unsupported")
        if type(self.position) is not int or self.position < 1:
            raise ValueError("QueueTicket position must be positive")
        if not hasattr(self.completion, "__await__"):
            raise TypeError("QueueTicket completion must be awaitable")


@dataclass(frozen=True)
class RoutedAction:
    origin: Origin
    kind: Literal["ask", "execute", "command"]
    source: str
    language: str | None = None


MAX_INPUT_PROMPT_CHARS = 8_192
MAX_INPUT_VALUE_CHARS = 65_536
MAX_INPUT_REQUESTS_PER_EXECUTION = 64


class InputUnavailableError(RuntimeError):
    """The owning frontend cannot answer an interactive input request."""


class InputCancelledError(RuntimeError):
    """The owning frontend cancelled an interactive input request."""


@dataclass(frozen=True)
class InputRequest:
    origin: Origin
    sequence: int
    owner_frontend_id: str
    prompt: str
    password: bool = False

    def __post_init__(self):
        if (not isinstance(self.origin, Origin)
                or not isinstance(self.origin.execution_id, str)
                or not 0 < len(self.origin.execution_id) <= 128
                or "\x00" in self.origin.execution_id):
            raise ValueError("InputRequest requires an execution origin")
        if (type(self.sequence) is not int
                or not 1 <= self.sequence <= MAX_INPUT_REQUESTS_PER_EXECUTION):
            raise ValueError("InputRequest sequence is outside its execution bound")
        if (not isinstance(self.owner_frontend_id, str)
                or not 0 < len(self.owner_frontend_id) <= 128
                or "\x00" in self.owner_frontend_id
                or self.owner_frontend_id != self.origin.frontend_id):
            raise ValueError("InputRequest owner must match its originating frontend")
        if not isinstance(self.prompt, str) or len(self.prompt) > MAX_INPUT_PROMPT_CHARS:
            raise ValueError("InputRequest prompt must be bounded text")
        if type(self.password) is not bool:
            raise TypeError("InputRequest password flag must be a boolean")


@dataclass(frozen=True)
class InputReply:
    origin: Origin
    sequence: int
    owner_frontend_id: str
    value: str = field(repr=False)
    password: bool = False

    def __post_init__(self):
        if (not isinstance(self.origin, Origin)
                or not isinstance(self.origin.execution_id, str)
                or not 0 < len(self.origin.execution_id) <= 128
                or "\x00" in self.origin.execution_id):
            raise ValueError("InputReply requires an execution origin")
        if (type(self.sequence) is not int
                or not 1 <= self.sequence <= MAX_INPUT_REQUESTS_PER_EXECUTION):
            raise ValueError("InputReply sequence is outside its execution bound")
        if (not isinstance(self.owner_frontend_id, str)
                or not 0 < len(self.owner_frontend_id) <= 128
                or "\x00" in self.owner_frontend_id
                or self.owner_frontend_id != self.origin.frontend_id):
            raise ValueError("InputReply owner must match its originating frontend")
        if type(self.password) is not bool:
            raise TypeError("InputReply password flag must be a boolean")
        if not isinstance(self.value, str) or len(self.value) > MAX_INPUT_VALUE_CHARS:
            raise ValueError("InputReply value must be bounded text")


InputHandler = Callable[[InputRequest], Awaitable[InputReply]]


@dataclass(frozen=True)
class ExecutionRequest:
    origin: Origin
    source: str
    author: Literal["user", "agent"]
    language: str = "ipython"
    allow_stdin: bool = False
    input_handler: InputHandler | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if type(self.allow_stdin) is not bool:
            raise TypeError("ExecutionRequest allow_stdin must be a boolean")
        if self.input_handler is not None and not callable(self.input_handler):
            raise TypeError("ExecutionRequest input_handler must be callable or None")


@dataclass(frozen=True)
class SayOutput:
    """One typed message emitted by ``say`` during an execution cell."""

    content: object
    final: bool = False

    def __post_init__(self):
        if type(self.final) is not bool:
            raise TypeError("SayOutput final must be a boolean")
        try:
            json.dumps(self.content, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("SayOutput content must be finite JSON data") from exc
        object.__setattr__(self, "content", deepcopy(self.content))


def _freeze_json_value(value: object, *, depth: int = 0, budget: list[int] | None = None) -> object:
    """Copy JSON-shaped data into recursively immutable containers."""
    if budget is None:
        budget = [100_000]
    budget[0] -= 1
    if budget[0] < 0 or depth > 32:
        raise ValueError("Output data exceeds its structural limit")
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Output data must contain finite numbers")
        return value
    if isinstance(value, Mapping):
        copied = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("Output data mapping keys must be text")
            copied[key] = _freeze_json_value(item, depth=depth + 1, budget=budget)
        return MappingProxyType(copied)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item, depth=depth + 1, budget=budget) for item in value)
    raise TypeError("Output data must contain only finite JSON values")


def _freeze_json_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    frozen = _freeze_json_value(value)
    assert isinstance(frozen, Mapping)
    return frozen


@dataclass(frozen=True)
class ExecutionOutput:
    """One ordered worker output frame, before coordinator sequencing."""

    kind: Literal["stream", "display", "execute_result", "update", "clear"]
    data: Mapping[str, object] = field(default_factory=dict)
    display_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in {"stream", "display", "execute_result", "update", "clear"}:
            raise ValueError("ExecutionOutput kind is unsupported")
        if self.display_id is not None and not isinstance(self.display_id, str):
            raise TypeError("ExecutionOutput display_id must be text or None")
        object.__setattr__(self, "data", _freeze_json_mapping(self.data, "ExecutionOutput data"))
        object.__setattr__(self, "metadata", _freeze_json_mapping(self.metadata, "ExecutionOutput metadata"))


@dataclass(frozen=True)
class ExecutionResult:
    origin: Origin
    status: Literal["success", "error", "cancelled", "uncertain"]
    error: str | None = None
    stdout: str = ""
    stderr: str = ""
    say_outputs: tuple[SayOutput, ...] = ()
    # True only when a say(final=True) was received and its cell succeeded.
    final: bool = False
    output_events: tuple[ExecutionOutput, ...] = ()

    def __post_init__(self):
        if type(self.final) is not bool:
            raise TypeError("ExecutionResult final must be a boolean")
        outputs = tuple(self.say_outputs)
        if any(not isinstance(output, SayOutput) for output in outputs):
            raise TypeError("ExecutionResult say_outputs must contain SayOutput records")
        requested = any(output.final for output in outputs)
        if self.final and (self.status != "success" or not requested):
            raise ValueError("A final execution result requires a successful final say output")
        if self.status == "success" and self.final != requested:
            raise ValueError("Successful final say output and result marker must agree")
        if self.status != "success" and self.final:
            raise ValueError("Failed executions cannot commit final output")
        object.__setattr__(self, "say_outputs", outputs)
        output_events = tuple(self.output_events)
        if any(not isinstance(event, ExecutionOutput) for event in output_events):
            raise TypeError("ExecutionResult output_events must contain ExecutionOutput records")
        object.__setattr__(self, "output_events", output_events)


@dataclass(frozen=True)
class OutputEvent:
    """Immutable output record delivered to observers in sequence order."""

    origin: Origin
    sequence: int
    kind: Literal[
        "stream", "display", "execute_result", "update", "clear", "error", "progress",
    ]
    data: Mapping[str, object] = field(default_factory=dict)
    display_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    author: Literal["user", "agent"] | None = None

    def __post_init__(self):
        if not isinstance(self.origin, Origin):
            raise TypeError("OutputEvent origin must be an Origin")
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("OutputEvent sequence must be a nonnegative integer")
        if self.kind not in {
            "stream", "display", "execute_result", "update", "clear", "error", "progress",
        }:
            raise ValueError("OutputEvent kind is unsupported")
        if self.display_id is not None and not isinstance(self.display_id, str):
            raise TypeError("OutputEvent display_id must be text or None")
        if self.author is not None and self.author not in ("user", "agent"):
            raise ValueError("OutputEvent author must be user, agent, or None")
        object.__setattr__(self, "data", _freeze_json_mapping(self.data, "OutputEvent data"))
        object.__setattr__(self, "metadata", _freeze_json_mapping(self.metadata, "OutputEvent metadata"))


# Async request-local sink for immutable progress and output events. The
# coordinator awaits each callback before advancing, providing bounded
# backpressure. Events describe stage boundaries and completed-cell output only;
# they do not expose model reasoning or claim to stream generated text.
ProgressCallback = Callable[[OutputEvent], Awaitable[None]]


@dataclass(frozen=True)
class ContextSnapshot:
    epoch: int
    messages: tuple[tuple[str, str], ...]
    message_phases: tuple[str | None, ...] = ()
    transform_trace: tuple[str, ...] = ()

    def __post_init__(self):
        if type(self.epoch) is not int or self.epoch < 0:
            raise ValueError("Context epoch must be a nonnegative integer")
        try:
            supplied = tuple(self.messages)
        except TypeError:
            raise TypeError("Context messages must be a sequence of role/content pairs") from None
        messages = []
        for pair in supplied:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise ValueError("Context messages must contain role/content pairs")
            role, content = pair
            if (
                not isinstance(role, str)
                or role not in {"system", "user", "assistant", "observation"}
                or not isinstance(content, str)
            ):
                raise ValueError("Context messages require a supported role and text content")
            messages.append((role, content))
        try:
            phases = tuple(self.message_phases)
        except TypeError:
            raise TypeError("Context message phases must be a sequence") from None
        if not phases:
            phases = (None,) * len(messages)
        if len(phases) != len(messages) or any(
            phase is not None
            and (
                not isinstance(phase, str)
                or phase not in {"commentary", "final_answer"}
                or messages[index][0] != "assistant"
            )
            for index, phase in enumerate(phases)
        ):
            raise ValueError("Context phases must align with assistant messages")
        trace = tuple(self.transform_trace)
        if any(not isinstance(stage, str) or not stage.strip() for stage in trace):
            raise ValueError("Context transform trace must contain nonempty stage names")
        object.__setattr__(self, "messages", tuple(messages))
        object.__setattr__(self, "message_phases", phases)
        object.__setattr__(self, "transform_trace", trace)


@dataclass(frozen=True)
class ModelRequest:
    origin: Origin
    context: ContextSnapshot
    model: str
    options: Mapping[str, str] = field(default_factory=dict)
    transform_trace: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.context, ContextSnapshot):
            raise TypeError("ModelRequest context must be a ContextSnapshot")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("ModelRequest model must be nonempty text")
        if not isinstance(self.options, Mapping):
            raise TypeError("ModelRequest options must be a mapping")
        copied = dict(self.options)
        if any(not isinstance(key, str) or not isinstance(value, str)
               for key, value in copied.items()):
            raise TypeError("ModelRequest options must map text keys to text values")
        trace = tuple(self.transform_trace)
        if any(not isinstance(stage, str) or not stage.strip() for stage in trace):
            raise ValueError("Model transform trace must contain nonempty stage names")
        object.__setattr__(self, "options", MappingProxyType(copied))
        object.__setattr__(self, "transform_trace", trace)


@dataclass(frozen=True)
class ModelResponse:
    text: str
    reasoning: str = ""
    finish_status: str = "complete"
    # Provider adapters retain the complete reported usage envelope (including
    # source, raw counters, samples, or an explicit unknown marker). It is not a
    # token-only mapping: missing counters must stay missing.
    usage: Mapping[str, object] = field(default_factory=dict)
    provider_id: str | None = None
    model: str | None = None
    rejection_reason: str | None = None
    phase: str | None = None
    adapter_metadata: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        object.__setattr__(self, "usage", MappingProxyType(deepcopy(dict(self.usage))))
        object.__setattr__(self, "adapter_metadata", MappingProxyType(deepcopy(dict(self.adapter_metadata))))


@dataclass(frozen=True)
class Submission:
    """Frontend-neutral result of one coordinator submission."""

    action: RoutedAction
    result: ExecutionResult | None = None
    message: str = ""
    execution: ExecutionRequest | None = None
    response: ModelResponse | None = None
    say_outputs: tuple[SayOutput, ...] = ()
    executions: tuple[tuple[ExecutionRequest, ExecutionResult], ...] = ()
    events: tuple[OutputEvent, ...] = ()

    def __post_init__(self):
        events = tuple(self.events)
        if any(not isinstance(event, OutputEvent) for event in events):
            raise TypeError("Submission events must contain OutputEvent records")
        object.__setattr__(self, "events", events)


@dataclass(frozen=True)
class AgentDecision:
    kind: Literal["execute", "finish", "wait", "reject"]
    source: str = ""
    reason: str = ""


@dataclass(frozen=True)
class ExecutorCapabilities:
    persistent: bool
    completion: bool = False
    inspection: bool = False
    rich_output: bool = False
    input: bool = False
    interrupt: bool = False

    def __post_init__(self):
        if any(type(value) is not bool for value in (
            self.persistent, self.completion, self.inspection,
            self.rich_output, self.input, self.interrupt,
        )):
            raise TypeError("Executor capabilities must be booleans")


class ExecutorCapabilityError(RuntimeError):
    """The explicitly selected executor does not implement a requested feature."""

    def __init__(self, capability: str):
        self.capability = capability
        super().__init__(f"Selected executor does not support {capability}")


@dataclass(frozen=True)
class CompletionResult:
    matches: tuple[str, ...]
    cursor_start: int
    cursor_end: int
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if isinstance(self.matches, (str, bytes)):
            raise TypeError("Completion matches must be a sequence of strings")
        matches = tuple(self.matches)
        if any(type(match) is not str for match in matches):
            raise TypeError("Completion matches must be text")
        if (type(self.cursor_start) is not int or type(self.cursor_end) is not int
                or self.cursor_start < 0 or self.cursor_end < self.cursor_start):
            raise ValueError("Completion cursor range is invalid")
        object.__setattr__(self, "matches", matches)
        object.__setattr__(self, "metadata", _freeze_json_mapping(self.metadata, "Completion metadata"))


@dataclass(frozen=True)
class InspectionResult:
    found: bool
    data: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if type(self.found) is not bool:
            raise TypeError("Inspection found flag must be a boolean")
        object.__setattr__(self, "data", _freeze_json_mapping(self.data, "Inspection data"))
        object.__setattr__(self, "metadata", _freeze_json_mapping(self.metadata, "Inspection metadata"))
        if not self.found and self.data:
            raise ValueError("Unsuccessful inspection cannot carry data")


@dataclass(frozen=True)
class CompletenessResult:
    status: Literal["complete", "incomplete", "invalid"]
    indent: str = ""

    def __post_init__(self):
        if self.status not in ("complete", "incomplete", "invalid") or not isinstance(self.indent, str):
            raise ValueError("Completeness result is invalid")


class ProviderService(Protocol):
    """Asynchronous model adapter selected by the coordinator."""

    async def generate(self, request: ModelRequest) -> ModelResponse: ...


class ContextService(Protocol):
    """Adapt immutable coordinator snapshots and account only reported usage."""

    def provider_messages(self, snapshot: ContextSnapshot) -> Sequence[Mapping[str, str]]: ...
    def record_response(self, response: ModelResponse) -> None: ...
    def needs_reset(self) -> bool: ...


class ObservationService(Protocol):
    """Pack execution evidence for later model-facing observation construction."""

    def pack(self, events: list[dict]) -> Mapping[str, object]: ...


class ContextTransform(Protocol):
    """Async context stage; return a new snapshot rather than mutating input."""

    async def transform(self, snapshot: ContextSnapshot) -> ContextSnapshot: ...


class ModelRequestTransform(Protocol):
    """Async provider-request stage; return a new ModelRequest."""

    async def transform(self, request: ModelRequest) -> ModelRequest: ...


class OutputObserver(Protocol):
    """Async observer; awaited delivery provides backpressure."""

    async def observe(self, event: OutputEvent) -> None: ...


class Router(Protocol):
    def route(self, action: UserAction) -> RoutedAction: ...


class CompletionExecutor(Protocol):
    """Optional executor capability operating on the executor's live namespace."""

    async def complete(self, code: str, cursor_pos: int) -> CompletionResult: ...


class InspectionExecutor(Protocol):
    """Optional, non-evaluating inspection of the executor's live namespace."""

    async def inspect(self, code: str, cursor_pos: int, detail_level: int = 0) -> InspectionResult: ...


class Executor(Protocol):
    """One owner serializes execute calls; cancellation never implies rollback.

    Services are created by registered factories. The coordinator owns start and
    close; plugin registration must not start processes. Results carry bounded
    stream output and typed say messages; interactive input is capability-specific.
    Completion and inspection are optional methods advertised by capabilities.
    """

    capabilities: ExecutorCapabilities

    async def start(self) -> None: ...
    async def execute(self, request: ExecutionRequest) -> ExecutionResult: ...
    async def interrupt(self) -> None: ...
    async def close(self) -> None: ...
