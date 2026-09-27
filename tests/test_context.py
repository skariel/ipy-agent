"""Stable context epochs and provisional limits; no model calls."""

from __future__ import annotations

import pytest

from py_agent.context import CONTRACT, Context, session_metadata
from py_agent.limits import Limits


def test_system_only_prefix_omits_session_inventory_and_memory_count():
    ctx = Context(Limits())
    assert ctx.messages() == [{"role": "system", "content": ctx.contract}]
    assert ctx.contract == CONTRACT
    assert "Session (" not in ctx.contract
    assert "Kernel bindings" not in ctx.contract
    assert "started with" not in ctx.contract
    assert "namespace persists" in CONTRACT
    assert "No tools or function calls are available" in CONTRACT
    assert "ordinary assistant message" in CONTRACT
    assert "not as a tool call, JSON, or Markdown" in CONTRACT
    assert "sandbox" not in CONTRACT
    assert "Large stdout/stderr" in CONTRACT
    assert "ask_rw_approval" not in CONTRACT
    assert "memory.md" not in CONTRACT
    assert not hasattr(ctx, "snapshot")
    assert not hasattr(ctx, "memory_path")


def test_append_only_epoch_and_frozen_count_until_commit():
    ctx = Context(Limits(), memories_count=2)
    prefix = ctx.messages()
    ctx.add("user", "real instruction", ["a1:e1"])
    before = ctx.messages()
    group = ctx.add("assistant", "memories.append('another note')", ["a1:e2"])
    ctx.observation({"status": "success"}, ["a1:e3"], group=group)
    ctx.record_usage({"normalized": {"input_tokens": 100}})
    assert ctx.messages()[: len(before)] == before
    assert ctx.messages()[:1] == prefix
    retained, evicted = ctx.retention(set(), memories_count=3)
    assert not retained
    assert evicted == ctx.groups
    assert ctx.messages()[:1] == prefix  # preparing is not committing
    ctx.commit(retained, memories_count=3)
    assert ctx.epoch == 2
    assert ctx.groups == []
    assert ctx.contract == CONTRACT
    assert ctx.starting_memories_count == 3
    assert ctx.reported_input_tokens is None


@pytest.mark.parametrize("count", [None, -1, True, "not a count"])
def test_unknown_starting_count_is_never_invented(count):
    ctx = Context(Limits(), memories_count=count)
    assert ctx.contract == CONTRACT
    assert ctx.starting_memories_count == count


def test_reset_discards_all_dispatched_groups_and_keeps_only_pending_input():
    ctx = Context(Limits())
    accepted = ctx.add("user", "old unfinished instruction", ["a1:accepted"])
    old_cell = ctx.add("assistant", "old = 1", ["a1:source"])
    ctx.observation({"status": "success"}, ["a1:result"], group=old_cell)
    pending = ctx.add("user", "must survive", ["a1:pending"])
    retained, evicted = ctx.retention({"a1:pending"})
    assert retained == [pending]
    assert evicted == [accepted, old_cell]
    ctx.commit(retained)
    assert ctx.messages()[1:] == [{"role": "user", "content": "must survive"}]


def test_reset_never_trims_or_byte_rejects_pending_input():
    ctx = Context(Limits(input_tokens=24000))
    pending = ctx.add("user", "p" * 30000, ["a1:pending"])
    ctx.record_usage({"normalized": {"input_tokens": 23000}})
    original = ctx.messages()
    retained, evicted = ctx.retention({"a1:pending"}, memories_count=99)
    assert retained == [pending]
    assert evicted == []
    assert ctx.messages() == original
    assert ctx.groups == [pending]
    assert ctx.epoch == 1
    assert ctx.starting_memories_count == 0


def test_large_byte_size_does_not_evict_low_reported_token_context():
    ctx = Context(Limits(input_tokens=24000))
    ctx.add("user", "🐍" * 10000)
    ctx.record_usage({"normalized": {"input_tokens": 6000}})
    assert ctx.estimate(ctx.messages()) > ctx.window_tokens
    assert not ctx.needs_reset()
    ctx.check(ctx.messages())  # no obsolete byte-based rejection


@pytest.mark.parametrize("tokens,reset", [(0, False), (22799, False), (22800, True), (24000, True)])
def test_eviction_threshold_uses_95_percent_reported_input(tokens, reset):
    ctx = Context(Limits(input_tokens=24000))
    ctx.record_usage({"normalized": {"input_tokens": tokens, "cache_read_tokens": tokens}})
    assert ctx.needs_reset() is reset  # no cache arithmetic


@pytest.mark.parametrize("kwargs", [{}, {"context_window_tokens": 272000}])
def test_default_and_explicit_window_allow_272k_without_old_24k_cap(kwargs):
    ctx = Context(Limits(), **kwargs)
    assert ctx.window_tokens == Limits().input_tokens == 272000
    ctx.record_usage({"normalized": {"input_tokens": 24000}})
    assert not ctx.needs_reset()
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


def test_unknown_usage_never_invents_byte_based_token_measurements_or_eviction():
    ctx = Context(Limits(input_tokens=24000))
    ctx.add("assistant", "# " + "x" * 500000)
    assert ctx.reported_input_tokens is None
    assert not ctx.needs_reset()
    assert ctx.check(ctx.messages()) > ctx.window_tokens
    ctx.record_usage({"normalized": {"input_tokens": 10}})
    assert not ctx.needs_reset()
    ctx.record_usage({"normalized": {}})
    assert ctx.reported_input_tokens == 10
    assert not ctx.needs_reset()
    ctx.commit([])
    assert ctx.reported_input_tokens is None


def test_required_first_request_has_no_byte_based_rejection_or_trimming():
    ctx = Context(Limits(input_tokens=24000))
    ctx.add("user", "λ" * 20000, ["a1:e1"])
    original = ctx.messages()
    assert ctx.check(original) > 40000
    assert not ctx.needs_reset()
    assert ctx.messages() == original


@pytest.mark.parametrize(
    "kwargs",
    [
        {"input_tokens": -1},
        {"input_tokens": True},
        {"input_tokens": 1.5},
        {"output_tokens": 0},
        {"output_tokens": True},
        {"generation_retries": -1},
    ],
)
def test_context_and_explicit_provider_configuration_reject_invalid_values(kwargs):
    with pytest.raises(ValueError):
        Limits(**kwargs)


def test_zero_retries_are_valid():
    assert Limits(generation_retries=0).generation_retries == 0


def test_execution_and_observation_quotas_are_not_configuration_fields():
    limits = Limits()
    assert limits.output_tokens is None
    for key in ("cell_seconds", "max_requests", "max_output_bytes", "max_user_bytes", "observation_chars"):
        assert not hasattr(limits, key)
        with pytest.raises(TypeError):
            Limits(**{key: 100})


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
    assert retained == [new]
    assert evicted == [old]


def test_session_metadata_is_filesystem_only(tmp_path):
    resolved = tmp_path.resolve()
    assert session_metadata(tmp_path) == {"current_path": str(resolved), "git": None}


def test_system_prompt_does_not_send_session_metadata():
    session = {
        "current_path": "/work/project",
        "git": {"root": "/work/project", "branch": "feature", "status": "clean", "status_truncated": False},
    }
    ctx = Context(Limits(), session_summary=session)
    assert ctx.contract == CONTRACT
    assert ctx.session_summary["current_path"] == "/work/project"
    session["current_path"] = "/changed"
    ctx.commit([], memories_count=1)
    assert ctx.contract == CONTRACT
    assert ctx.session_summary["current_path"] == "/work/project"
