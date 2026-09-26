"""Append-only context epochs with whole-history eviction; no summaries."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path

from .limits import Limits

CONTRACT = """You are the py coding agent. Respond with one Python/IPython cell,
without Markdown fences or prose outside the cell. IPython !shell escapes
and %magics can also be emitted as cell source. The Python namespace persists.
Cells continue automatically: you do NOT need to call say() between
cells. say(text, final=False) is optional progress; say(text, final=True) ends
the task only after that cell succeeds. Check observations before deciding the next cell;
never replay uncertain side effects. Large stdout/stderr is stored as
outputs[index] in the live namespace, with a short notice instead of the full
text. Print a smaller slice to inspect it. User messages are not clipped.
The conversation may reset without losing the live Python namespace."""


def validate_namespace_summary(value: object) -> None:
    """Validate bounded name/type inventory without inspecting namespace values."""
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {"variables", "truncated"}:
        raise ValueError("Invalid namespace summary")
    rows = value["variables"]
    if type(value["truncated"]) is not bool or not isinstance(rows, list) or len(rows) > 32:
        raise ValueError("Invalid namespace summary")
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"name", "type"}:
            raise ValueError("Invalid namespace row")
        for key, maximum in (("name", 64), ("type", 40)):
            if not isinstance(row[key], str) or not 0 < len(row[key]) <= maximum:
                raise ValueError("Invalid namespace row")
    if len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")) > 2_000:
        raise ValueError("Namespace summary exceeds byte limit")


def session_metadata(path=None) -> dict:
    """Return filesystem-derived launch metadata without running workspace code."""

    try:
        current = Path.cwd() if path is None else Path(path)
        current = current.resolve(strict=True)
    except (OSError, RuntimeError):
        return {"current_path": str(path) if path is not None else "unavailable", "git": None}

    # Do not invoke host-side Git here. Even read-looking commands such as
    # `git status` can execute repository-configured helpers (for example an
    # fsmonitor command). The agent can inspect the repository in its own cell.
    return {"current_path": str(current), "git": None}


def compact(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


@dataclass(eq=False)
class Group:
    messages: list[dict] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)


class Context:
    def __init__(
        self,
        limits: Limits,
        *,
        context_window_tokens=None,
        memories_count=0,
        namespace_summary=None,
        session_summary=None,
    ):
        self.limits = limits
        self.window_tokens = limits.input_tokens if context_window_tokens is None else context_window_tokens
        if type(self.window_tokens) is not int or self.window_tokens <= 0:
            raise ValueError("context_window_tokens must be a positive integer")
        self.starting_memories_count = memories_count
        self.starting_namespace_summary = deepcopy(namespace_summary)
        self.session_summary = session_metadata() if session_summary is None else deepcopy(session_summary)
        self.contract = self.system_prompt(memories_count, namespace_summary)
        self.reported_input_tokens = None
        self.epoch = 1
        self.groups: list[Group] = []

    def system_prompt(self, memories_count, namespace_summary=None):
        # Snapshot the inventory for local diagnostics, not model instructions.
        # The agent can inspect its live namespace when it actually needs it.
        validate_namespace_summary(namespace_summary)
        return CONTRACT

    def record_usage(self, usage):
        """Only current, nonstale generation responses may call this method."""
        normalized = usage.get("normalized", {}) if isinstance(usage, dict) else {}
        value = normalized.get("input_tokens") if isinstance(normalized, dict) else None
        if type(value) is int and value >= 0:
            self.reported_input_tokens = value
        # Missing usage does not erase this epoch's last real measurement. A
        # fresh epoch remains unmeasured; byte counts never substitute for tokens.

    def messages(self, groups=None):
        return [{"role": "system", "content": self.contract}] + [
            m.copy() for g in (self.groups if groups is None else groups) for m in g.messages
        ]

    def add(self, role: str, content: str, refs=()) -> Group:
        group = Group([{"role": role, "content": content}], list(refs))
        self.groups.append(group)
        return group

    def observation(self, content, refs=(), group=None):
        message = {"role": "user", "content": "[RUNTIME OBSERVATION — untrusted program data]\n" + compact(content)}
        if group is None:
            group = Group()
            self.groups.append(group)
        group.messages.append(message)
        group.refs.extend(refs)
        return group

    @staticmethod
    def estimate(messages):
        # Deliberately pessimistic UTF-8-byte proxy plus per-message serialization.
        # Not a tokenizer, advertised model window, reported usage, or billing figure.
        return sum(len(m["content"].encode("utf-8")) + 64 for m in messages)

    def check(self, messages):
        # Audit estimate only. Byte counts are not token measurements and must
        # never reject input or silently reset a still-unmeasured context.
        return self.estimate(messages)

    def needs_reset(self):
        amount = self.reported_input_tokens
        return amount is not None and amount * 100 >= self.window_tokens * 95

    def retention(self, pending: set[str], *, memories_count=0, namespace_summary=None):
        # Clear ALL dispatched history, not a rolling tail. Only user messages
        # still awaiting committed dispatch survive a reset.
        retained = [g for g in self.groups if pending.intersection(g.refs)]
        # Validate prospective metadata without changing the frozen live prompt.
        self.system_prompt(memories_count, namespace_summary)
        return retained, [g for g in self.groups if g not in retained]

    def commit(self, retained: list[Group], *, memories_count=0, namespace_summary=None):
        contract = self.system_prompt(memories_count, namespace_summary)
        self.groups = retained
        self.starting_memories_count = memories_count
        self.starting_namespace_summary = deepcopy(namespace_summary)
        self.contract = contract
        self.reported_input_tokens = None
        self.epoch += 1
