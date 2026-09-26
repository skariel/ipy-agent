"""Lossless range replacement and generation-boundary collapse policy."""

from __future__ import annotations

import asyncio
import json

import pytest

from py_agent.context import COLLAPSE_FORCED, COLLAPSE_REMINDER
from py_agent.contracts import ModelResponse
from py_agent.limits import Limits
from py_agent.production_services import ProductionContextAdapter


def populated():
    adapter = ProductionContextAdapter(limits=Limits(input_tokens=1000))
    adapter.prepare_request("old request " * 100, "old")
    adapter.commit_response("old", "print('old')", observation={"output": "old output " * 100})
    adapter.prepare_request("  exact end\nλ\x00\n", "new")
    adapter.commit_response("new", "print('tail')", observation={"output": "tail"})
    return adapter


def boundary_ids(adapter):
    return [g.messages[0]["boundary_id"] for g in adapter.context.groups
            if g.messages and "boundary_id" in g.messages[0]]


class Store:
    def __init__(self):
        self.archives = {}

    async def __call__(self, text):
        index = len(self.archives) + 1
        self.archives[index] = text
        return index


@pytest.mark.asyncio
async def test_exclusive_user_end_preserves_exact_text_start_id_and_tail():
    adapter = populated()
    store = Store()
    original_end = adapter.context.groups[2].messages[0]["content"]
    original_tail = adapter.context.groups[3]
    receipt = await adapter.collapse("u1", "u2", "Remember the bug.", store)
    assert receipt == "Collapsed [u1, u2). Originals retained in collapsed[1]."
    assert len(adapter.context.groups) == 2
    merged = adapter.context.groups[0]
    assert merged.messages[0]["boundary_id"] == "u1"
    assert merged.messages[0]["content"].endswith("\n\n" + original_end)
    assert "Remember the bug." in merged.messages[0]["content"]
    assert merged.refs == ["old", "new"]
    assert adapter.context.groups[1] is original_tail
    archive = json.loads(store.archives[1])
    assert archive["groups"][0]["messages"][0]["content"] == "old request " * 100
    assert archive["groups"][1]["messages"][1]["content"] == "old output " * 100
    assert archive["merged_end"]["messages"][0]["content"] == original_end
    assert adapter.context.collapsed[1] == store.archives[1]
    # Cancellation cleanup for either request must never delete merged history.
    adapter.abandon_request("old")
    adapter.abandon_request("new")
    assert adapter.context.groups[0] is merged


@pytest.mark.asyncio
async def test_marker_end_is_removed_without_placeholder_text_and_nested_archive_survives():
    adapter = populated()
    store = Store()
    adapter.context.add_marker()
    await adapter.collapse("u1", "m1", "Key state " * 30, store)
    first = adapter.context.groups[0].messages[0]["content"]
    assert "Automatic collapse boundary" not in first
    assert "exact end" not in first  # It was inside the collapsed range.
    adapter.commit_response("new", "# " + "extra code " * 100, observation={"output": "more"})
    adapter.context.add_marker()
    await adapter.collapse("u1", "m2", "Key state", store)
    assert boundary_ids(adapter) == ["u1"]
    assert len(adapter.context.collapsed) == 2
    assert json.loads(store.archives[2])["groups"][0]["messages"][0]["content"] == first
    assert "old request " in store.archives[1]


@pytest.mark.asyncio
async def test_summary_with_marker_id_as_end_preserves_its_content():
    adapter = ProductionContextAdapter()
    adapter.prepare_request("ancient " * 200, "r")
    adapter.context.add_marker()
    adapter.commit_response("r", "# " + "recent " * 200)
    adapter.context.add_marker()
    store = Store()
    await adapter.collapse("m1", "m2", "Recent state", store)
    end = adapter.context.groups[1].messages[0]["content"]
    await adapter.collapse("u1", "m1", "Ancient state", store)
    assert adapter.context.groups[0].messages[0]["content"].endswith("\n\n" + end)


@pytest.mark.asyncio
@pytest.mark.parametrize("start,end,summary,error", [
    ("u2", "u1", "state", "precede"),
    ("u1", "u1", "state", "precede"),
    ("assistant1", "u2", "state", "Unknown"),
    ("u1", "m100", "state", "Unknown"),
    ("u1", "u2", "", "empty"),
    ("u1", "u2", "x" * 10000, "reduce"),
])
async def test_bad_collapse_is_nonmutating_and_does_not_archive(start, end, summary, error):
    adapter = populated()
    before = adapter.snapshot()
    store = Store()
    with pytest.raises(ValueError, match=error):
        await adapter.collapse(start, end, summary, store)
    assert adapter.snapshot() == before
    assert not store.archives


@pytest.mark.asyncio
async def test_failed_or_cancelled_archive_retains_originals():
    adapter = populated()
    before = adapter.snapshot()

    async def fail(_text):
        raise OSError("storage failed")

    with pytest.raises(OSError, match="storage failed"):
        await adapter.collapse("u1", "u2", "state", fail)
    assert adapter.snapshot() == before

    async def cancel(_text):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await adapter.collapse("u1", "u2", "state", cancel)
    assert adapter.snapshot() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("ack", [True, 0, -1, "1", None])
async def test_invalid_ack_does_not_mutate_context(ack):
    adapter = populated()
    before = adapter.snapshot()

    async def store(_text):
        return ack

    with pytest.raises(ValueError, match="positive"):
        await adapter.collapse("u1", "u2", "state", store)
    assert adapter.snapshot() == before


@pytest.mark.asyncio
async def test_changed_context_during_archive_is_not_overwritten():
    adapter = populated()
    first = adapter.context.groups[0]

    async def store(_text):
        adapter.add("user", "new arrival", refs=("steering",))
        return 7

    with pytest.raises(ValueError, match="Context changed"):
        await adapter.collapse("u1", "u2", "state", store)
    assert adapter.context.groups[0] is first
    assert "new arrival" in str(adapter.snapshot().messages)
    assert 7 in adapter.context.collapsed


def test_only_users_and_markers_expose_ids_and_preview_matches_dispatch():
    adapter = ProductionContextAdapter()
    rendered = adapter.render_user("steering", "request")
    assert rendered == "[context boundary u1]\nsteering"
    adapter.add("user", "steering", refs=("request",))
    adapter.commit_response("request", "print(1)", observation={"output": "1"})
    assert adapter.snapshot().messages[1:] == (
        ("user", rendered), ("assistant", "print(1)"), ("observation", "1"),
    )
    assert "boundary_id" not in adapter.context.groups[1].messages[0]
    assert "boundary_id" not in adapter.context.groups[1].messages[1]


def test_markers_every_ten_completed_cells_after_results():
    adapter = ProductionContextAdapter()
    adapter.prepare_request("task", "r")
    for number in range(1, 21):
        adapter.commit_response("r", f"print({number})", observation={"output": str(number)})
        adapter.completed_cell()
        assert len(boundary_ids(adapter)) == 1 + (number - 1) // 10
        adapter.prepare_generation()
        assert len(boundary_ids(adapter)) == 1 + number // 10
        if number % 10 == 0:
            assert adapter.context.groups[-2].messages[-1]["content"] == str(number)
            assert adapter.context.groups[-1].messages[0]["boundary_kind"] == "marker"
    # Snapshotting and repeated generation preparation cannot invent more markers.
    adapter.prepare_generation()
    adapter.snapshot()
    assert boundary_ids(adapter) == ["u1", "m1", "m2"]


def test_reminder_every_fifty_responses_not_stored_as_history():
    adapter = ProductionContextAdapter()
    for number in range(1, 102):
        adapter.record_response(ModelResponse("pass"))
        adapter.prepare_generation()
        assert (("system", COLLAPSE_REMINDER) in adapter.snapshot().messages) is (number % 50 == 0)
    assert adapter.context.groups == []


@pytest.mark.asyncio
async def test_force_latched_before_generation_and_success_invalidates_stale_usage():
    adapter = populated()
    adapter.record_response(ModelResponse("pass", usage={"normalized": {"input_tokens": 900}}))
    adapter.prepare_generation()
    assert not adapter.force_collapse
    adapter.record_response(ModelResponse("pass", usage={"normalized": {"input_tokens": 901}}))
    assert not adapter.force_collapse  # Current response was not prompted in force mode.
    adapter.prepare_generation()
    assert adapter.force_collapse
    assert boundary_ids(adapter)[-1] == "m1"
    assert ("system", COLLAPSE_FORCED) in adapter.snapshot().messages
    adapter.record_response(ModelResponse("pass", usage={"normalized": {"input_tokens": 10}}))
    adapter.prepare_generation()
    assert adapter.force_collapse  # Only successful collapse releases the latch.
    assert boundary_ids(adapter).count("m1") == 1
    await adapter.collapse("u1", "m1", "state", Store())
    assert not adapter.force_collapse
    assert adapter.reported_input_tokens is None
    adapter.prepare_generation()
    assert not adapter.force_collapse
    assert ("system", COLLAPSE_FORCED) not in adapter.snapshot().messages


def test_production_never_evicts_history_at_old_reset_threshold():
    adapter = populated()
    old_group = adapter.context.groups[0]
    adapter.record_response(ModelResponse("pass", usage={"normalized": {"input_tokens": 990}}))
    assert not adapter.needs_reset()
    adapter.prepare_request("new request", "next")
    assert adapter.context.groups[0] is old_group
    assert adapter.epoch == 1


@pytest.mark.asyncio
async def test_end_group_trailing_observation_and_archive_indexes_survive():
    adapter = populated()
    end_group = adapter.context.groups[2]
    end_group.messages.append({"role": "observation", "content": "attached end result"})
    end_group.execution_output_indexes.append(1)
    await adapter.collapse("u1", "u2", "state", Store())
    trailing = adapter.context.groups[1]
    assert trailing.messages == [{"role": "observation", "content": "attached end result"}]
    assert trailing.execution_output_indexes == [0]
