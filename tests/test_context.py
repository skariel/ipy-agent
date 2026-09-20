"""Stable context epochs and provisional limits; no model calls."""
from __future__ import annotations

import pytest

from py_agent.context import CONTRACT, Context
from py_agent.limits import LimitExceeded, Limits


def test_system_only_prefix_describes_live_memories_without_injection():
    ctx = Context(Limits())
    assert ctx.messages() == [{"role": "system", "content": ctx.contract}]
    assert ctx.contract.startswith(CONTRACT)
    assert ctx.contract.endswith("This context started with 0 memories. Inspect the memories variable if you need earlier context.")
    assert "memories is a predefined Python list" in CONTRACT
    assert "does not automatically save or insert the list's contents" in CONTRACT
    assert "There is no resume" in CONTRACT
    assert "memory.md" not in CONTRACT
    assert not hasattr(ctx, "snapshot") and not hasattr(ctx, "memory_path")


def test_append_only_epoch_and_frozen_count_until_commit():
    ctx = Context(Limits(), memories_count=2)
    prefix = ctx.messages()
    ctx.add("user", "real instruction", ["a1:e1"])
    before = ctx.messages()
    group = ctx.add("assistant", "memories.append('another note')", ["a1:e2"])
    ctx.observation({"status": "success"}, ["a1:e3"], group=group)
    ctx.record_usage({"normalized": {"input_tokens": 100}})
    assert ctx.messages()[:len(before)] == before
    assert ctx.messages()[:1] == prefix
    retained, evicted = ctx.retention(set(), memories_count=3)
    assert not retained and evicted == ctx.groups
    assert ctx.messages()[:1] == prefix  # preparing is not committing
    ctx.commit(retained, memories_count=3)
    assert ctx.epoch == 2 and ctx.groups == []
    assert "started with 3 memories" in ctx.contract
    assert ctx.reported_input_tokens is None


@pytest.mark.parametrize("count", [None, -1, True, "not a count"])
def test_unknown_starting_count_is_never_invented(count):
    ctx = Context(Limits(), memories_count=count)
    assert "started with an unknown number of memories" in ctx.contract


def test_reset_discards_all_dispatched_groups_and_keeps_only_pending_input():
    ctx = Context(Limits())
    accepted = ctx.add("user", "old unfinished instruction", ["a1:accepted"])
    old_cell = ctx.add("assistant", "old = 1", ["a1:source"])
    ctx.observation({"status": "success"}, ["a1:result"], group=old_cell)
    pending = ctx.add("user", "must survive", ["a1:pending"])
    retained, evicted = ctx.retention({"a1:pending"})
    assert retained == [pending] and evicted == [accepted, old_cell]
    ctx.commit(retained)
    assert ctx.messages()[1:] == [{"role": "user", "content": "must survive"}]


def test_reset_never_trims_pending_input_or_commits_on_failure():
    ctx = Context(Limits())
    pending = ctx.add("user", "p" * 30000, ["a1:pending"])
    ctx.record_usage({"normalized": {"input_tokens": 23000}})
    original = ctx.messages()
    with pytest.raises(LimitExceeded, match="nothing was silently trimmed"):
        ctx.retention({"a1:pending"}, memories_count=99)
    assert ctx.messages() == original
    assert ctx.groups == [pending] and ctx.epoch == 1
    assert ctx.starting_memories_count == 0


def test_large_byte_size_does_not_evict_low_reported_token_context():
    ctx = Context(Limits())
    ctx.add("user", "🐍" * 10000)
    ctx.record_usage({"normalized": {"input_tokens": 6000}})
    assert ctx.estimate(ctx.messages()) > ctx.window_tokens
    assert not ctx.needs_reset()
    ctx.check(ctx.messages())  # no obsolete byte-based rejection


@pytest.mark.parametrize("tokens,reset", [(0, False), (22799, False), (22800, True), (24000, True)])
def test_eviction_threshold_uses_95_percent_reported_input(tokens, reset):
    ctx = Context(Limits())
    ctx.record_usage({"normalized": {"input_tokens": tokens, "cache_read_tokens": tokens}})
    assert ctx.needs_reset() is reset  # no cache arithmetic


def test_configured_window_controls_threshold_and_fresh_input_check():
    ctx = Context(Limits(), context_window_tokens=272000)
    assert ctx.window_tokens == 272000
    ctx.record_usage({"normalized": {"input_tokens": 258399}})
    assert not ctx.needs_reset()
    ctx.record_usage({"normalized": {"input_tokens": 258400}})
    assert ctx.needs_reset()
    ctx.commit([], memories_count=1)
    assert not ctx.needs_reset()
    assert ctx.window_tokens == 272000


@pytest.mark.parametrize("window", [True, 0, -1, 1.2, "272000"])
def test_invalid_context_capacity_is_rejected(window):
    with pytest.raises(ValueError, match="positive integer"):
        Context(Limits(), context_window_tokens=window)


def test_unknown_usage_fallback_is_conservative_without_inventing_reported_tokens():
    ctx = Context(Limits())
    ctx.add("assistant", "# " + "x" * 23000)
    assert ctx.reported_input_tokens is None and ctx.needs_reset()
    with pytest.raises(LimitExceeded):
        ctx.check(ctx.messages())
    ctx.record_usage({"normalized": {"input_tokens": 10}})
    assert not ctx.needs_reset()
    ctx.record_usage({"normalized": {}})
    assert ctx.reported_input_tokens == 10 and not ctx.needs_reset()
    ctx.commit([])
    assert ctx.reported_input_tokens is None


def test_required_first_request_rejects_oversize_without_trimming():
    ctx = Context(Limits())
    ctx.add("user", "λ" * 20000, ["a1:e1"])
    original = ctx.messages()
    with pytest.raises(LimitExceeded, match="nothing was silently trimmed"):
        ctx.check(original)
    assert ctx.messages() == original


@pytest.mark.parametrize("kwargs", [
    {"cell_seconds": float("nan")}, {"cell_seconds": float("inf")},
    {"input_tokens": -1}, {"output_tokens": 0},
    {"max_requests": True}, {"max_requests": 1.5}, {"input_tokens": 3000},
])
def test_limits_reject_invalid_or_unreserved_values(kwargs):
    with pytest.raises(ValueError):
        Limits(**kwargs)


def test_zero_retries_are_valid():
    assert Limits(generation_retries=0).generation_retries == 0


def test_default_cell_deadline_allows_longer_test_and_build_cells():
    assert Limits().cell_seconds == 300
    assert Limits(cell_seconds=0.05).cell_seconds == 0.05


def test_checkpoint_and_tail_limits_are_not_configuration_fields():
    limits = Limits()
    assert not hasattr(limits, "checkpoint_tokens")
    assert not hasattr(limits, "checkpoint_retries")
    assert not hasattr(limits, "tail_groups")
    with pytest.raises(TypeError):
        Limits(tail_groups=0)


def test_equal_valued_groups_are_not_accidentally_retained():
    ctx = Context(Limits())
    old = ctx.add("user", "repeat", ["old"])
    new = ctx.add("user", "repeat", ["pending"])
    retained, evicted = ctx.retention({"pending"})
    assert retained == [new] and evicted == [old]
