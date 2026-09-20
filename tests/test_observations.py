"""Pure packing tests: no provider, worker, filesystem output, or code execution."""
from copy import deepcopy
import json
import random

import pytest

from py_agent.observations import pack_observations


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))


def output(identifier, text, stream="stdout", **metadata):
    return {"id": identifier, "stream": stream, "text": text, **metadata}


def test_split_print_arguments_are_one_lossless_stream_run():
    events = [output(f"e{i}", text) for i, text in enumerate(["f", " ", "file.py", "\n"])]
    packed = pack_observations(events, 1000)
    assert packed == {"events": [output("e0", "f file.py\n", last_id="e3")],
                      "truncated": False, "omitted_events": 0}


def test_many_fragments_coalesce_without_metadata_consuming_the_budget():
    events = [output(f"e{i}", "x") for i in range(6000)]
    packed = pack_observations(events, 8000)
    assert len(packed["events"]) == 1
    assert packed["events"][0]["text"] == "x" * 6000
    assert packed["events"][0]["last_id"] == "e5999"
    assert not packed["truncated"]


def test_stream_say_and_display_boundaries_preserve_order():
    events = [output("e0", "one"), output("e1", "\n"), output("e2", "error", "stderr"),
              {"id": "e3", "say": "progress", "final": False},
              output("e4", "42", "display"), output("e5", "43", "display"),
              output("e6", "last"), output("e7", "\n")]
    packed = pack_observations(events, 8000)
    assert packed["events"] == [output("e0", "one\n", last_id="e1"), *events[2:6],
                                 output("e6", "last\n", last_id="e7")]
    assert not packed["truncated"]


def test_optional_origin_metadata_prevents_cross_cell_or_late_stream_merging():
    events = [output("e0", "one", cell_id="c1"), output("e1", "two", cell_id="c2"),
              output("e2", "late", cell_id="c2", asynchronous=True)]
    assert pack_observations(events, 8000)["events"] == events


def test_exact_encoded_budget_preserves_complete_events():
    events = [output("e0", '雪 "quoted"\n'), {"id": "e1", "say": {"list": [1, 2]}, "final": True}]
    result = pack_observations(events, encoded_size(events))
    assert result["events"] == events
    assert result["truncated"] is False
    assert result["omitted_events"] == 0


def test_large_output_retains_typed_raw_head_and_tail_not_partial_json():
    text = "HEAD: project\n" + "x" * 20000 + "\nTAIL: traceback.py:42\n"
    result = pack_observations([output("e0", text)], 600)
    event, = result["events"]
    assert result["truncated"] and result["omitted_events"] == 0
    assert event["id"] == "e0" and event["stream"] == "stdout"
    assert event["excerpt_field"] == "text" and event["truncated"]
    assert event["original_bytes"] == len(text.encode("utf-8"))
    assert event["head"].startswith("HEAD: project\n")
    assert event["tail"].endswith("\nTAIL: traceback.py:42\n")
    assert text.startswith(event["head"]) and text.endswith(event["tail"])
    assert "text" not in event and "excerpt" not in event
    assert encoded_size(result["events"]) <= 600


@pytest.mark.parametrize("unit", ["雪", "😀", '"\\\n\t\x00', "plain", "e\u0301"])
@pytest.mark.parametrize("budget", [0, 1, 2, 32, 256, 1000])
def test_utf8_and_json_escaping_respect_the_actual_serialized_budget(unit, budget):
    text = unit * 1000
    result = pack_observations([output("e0", text)], budget)
    assert result["truncated"]
    if budget >= 2:
        assert encoded_size(result["events"]) <= budget
    else:
        assert result["events"] == []
    if result["events"]:
        event = result["events"][0]
        assert event["original_bytes"] == len(text.encode("utf-8"))
        assert text.startswith(event["head"]) and text.endswith(event["tail"])
        assert len(event["head"]) + len(event["tail"]) < len(text)
    else:
        assert result["omitted_events"] == 1


def test_small_events_keep_their_full_content_and_donate_unused_budget():
    events = [output("e0", "a" * 10000), output("e1", "a short diagnostic\n", "stderr")]
    result = pack_observations(events, 4000)
    assert result["events"][1] == events[1]
    clipped = result["events"][0]
    assert len(clipped["head"]) + len(clipped["tail"]) > 3000
    assert 3996 <= encoded_size(result["events"]) <= 4000


def test_many_alternating_events_keep_both_ends_with_explicit_omission_count():
    events = [output(f"e{i}", f"line {i}\n", "stdout" if i % 2 else "stderr") for i in range(100)]
    result = pack_observations(events, 1200)
    assert [event["id"] for event in result["events"]] == ["e0", "e1", "e98", "e99"]
    assert result["truncated"] and result["omitted_events"] == 96
    assert encoded_size(result["events"]) <= 1200


def test_structured_say_excerpts_are_labelled_json_and_keep_final_metadata():
    value = {"begin": "a" * 5000, "end": [1, 2, 3]}
    event = {"id": "e0", "say": value, "final": True}
    result = pack_observations([event], 400)
    excerpt, = result["events"]
    original = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    assert excerpt["final"] is True and excerpt["excerpt_field"] == "say"
    assert excerpt["excerpt_format"] == "json"
    assert original.startswith(excerpt["head"]) and original.endswith(excerpt["tail"])
    assert excerpt["original_bytes"] == len(original.encode("utf-8"))
    assert encoded_size(result["events"]) <= 400


def test_input_is_not_mutated_or_aliased_by_complete_or_truncated_results():
    events = [output("e0", "a" * 10000, metadata={"labels": ["unchanged"]}),
              output("e1", "tail"), {"id": "e2", "say": {"nested": [1]}, "final": False}]
    before = deepcopy(events)
    for budget in (500, 20000):
        result = pack_observations(events, budget)
        assert events == before
        result["events"][0]["metadata"]["labels"].append("changed result")
        assert events == before
    complete = pack_observations(events, 20000)
    complete["events"][-1]["say"]["nested"].append(2)
    assert events == before


@pytest.mark.parametrize("budget", [0, 1, 2, 100])
def test_empty_input_has_no_omitted_events(budget):
    assert pack_observations([], budget) == {"events": [], "truncated": False, "omitted_events": 0}


@pytest.mark.parametrize("budget", [-1, True, 1.5, None])
def test_invalid_budgets_fail_explicitly(budget):
    with pytest.raises(ValueError, match="nonnegative integer"):
        pack_observations([], budget)


@pytest.mark.parametrize("events", [None, {}, ["not a record"]])
def test_invalid_event_containers_fail_explicitly(events):
    with pytest.raises(TypeError, match="list of dictionaries"):
        pack_observations(events, 100)


def test_randomized_mixed_events_remain_bounded_without_mutating_input():
    rng = random.Random(3101)
    for _ in range(100):
        events = []
        for index in range(rng.randrange(1, 30)):
            text = rng.choice(["snow雪", '"quoted"\n', "tail 😀", "\x00\\"]) * rng.randrange(0, 200)
            events.append(output(f"e{index}", text, rng.choice(["stdout", "stderr", "display"])))
        before = deepcopy(events)
        budget = rng.randrange(2, 4000)
        result = pack_observations(events, budget)
        assert encoded_size(result["events"]) <= budget
        assert events == before
