"""Lossless observation packing: no provider, worker or source execution."""

from __future__ import annotations

from copy import deepcopy
import random

import pytest

from py_agent.observations import pack_observations


def output(identifier, text, stream="stdout", **metadata):
    return {"id": identifier, "stream": stream, "text": text, **metadata}


def test_split_print_arguments_are_one_lossless_stream_run():
    events = [output(f"e{i}", text) for i, text in enumerate(["f", " ", "file.py", "\n"])]
    assert pack_observations(events) == {
        "events": [output("e0", "f file.py\n", last_id="e3")],
        "truncated": False,
        "omitted_events": 0,
    }


def test_many_fragments_coalesce_without_metadata_consuming_a_budget():
    events = [output(f"e{i}", "x") for i in range(20000)]
    result = pack_observations(events)
    assert result["events"] == [output("e0", "x" * 20000, last_id="e19999")]
    assert not result["truncated"]


def test_stream_say_and_display_boundaries_preserve_order():
    events = [
        output("e0", "one"),
        output("e1", "\n"),
        output("e2", "error", "stderr"),
        {"id": "e3", "say": "progress", "final": False},
        output("e4", "42", "display"),
        output("e5", "43", "display"),
        output("e6", "last"),
        output("e7", "\n"),
    ]
    assert pack_observations(events)["events"] == [
        output("e0", "one\n", last_id="e1"),
        *events[2:6],
        output("e6", "last\n", last_id="e7"),
    ]


def test_origin_metadata_prevents_cross_cell_or_late_stream_merging():
    events = [
        output("e0", "one", cell_id="c1"),
        output("e1", "two", cell_id="c2"),
        output("e2", "late", cell_id="c2", asynchronous=True),
    ]
    assert pack_observations(events)["events"] == events


@pytest.mark.parametrize("unit", ["x", "雪", "🐍", '"\\\n\t\x00', "e\u0301"])
@pytest.mark.parametrize("count", [4000, 8000, 20000])
def test_text_is_preserved_regardless_of_unicode_or_json_byte_size(unit, count):
    value = unit * count
    events = [output("e0", value)]
    assert pack_observations(events) == {"events": events, "truncated": False, "omitted_events": 0}


def test_many_alternating_events_are_not_omitted():
    events = [output(f"e{i}", f"line {i}\n", "stdout" if i % 2 else "stderr") for i in range(500)]
    assert pack_observations(events)["events"] == events


def test_structured_say_is_preserved_without_serialized_excerpts():
    value = {"begin": "a" * 20000, "end": [1, 2, 3]}
    event = {"id": "e0", "say": value, "final": True}
    assert pack_observations([event])["events"] == [event]


def test_input_is_not_mutated_or_aliased():
    events = [
        output("e0", "a" * 10000, metadata={"labels": ["unchanged"]}),
        output("e1", "tail"),
        {"id": "e2", "say": {"nested": [1]}, "final": False},
    ]
    before = deepcopy(events)
    result = pack_observations(events)
    result["events"][0]["metadata"]["labels"].append("changed result")
    result["events"][-1]["say"]["nested"].append(2)
    assert events == before


def test_empty_input_has_no_omitted_events():
    assert pack_observations([]) == {"events": [], "truncated": False, "omitted_events": 0}


@pytest.mark.parametrize("events", [None, {}, ["not a record"]])
def test_invalid_event_containers_fail_explicitly(events):
    with pytest.raises(TypeError, match="list of dictionaries"):
        pack_observations(events)


def test_randomized_mixed_events_are_lossless_and_do_not_mutate_input():
    rng = random.Random(3101)
    for _ in range(100):
        events = [
            output(
                f"e{index}",
                rng.choice(["snow雪", '"quoted"\n', "tail 🐍", "\x00\\"]) * rng.randrange(200),
                rng.choice(["stdout", "stderr", "display"]),
            )
            for index in range(rng.randrange(1, 30))
        ]
        before = deepcopy(events)
        packed = pack_observations(events)
        assert "".join(event["text"] for event in packed["events"]) == "".join(event["text"] for event in events)
        assert not packed["truncated"]
        assert packed["omitted_events"] == 0
        assert events == before
