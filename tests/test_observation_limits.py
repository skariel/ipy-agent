"""Raw output and presentation overhead have separate bounded budgets."""
from __future__ import annotations

import pytest

from py_agent.production_services import ProductionObservationAdapter


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("text", ["x" * 8_000, '"\\\n' * 2_666 + "xy", "界" * 8_000])
def test_visible_limit_stream_survives_presentation_labels(stream, text):
    assert len(text) == 8_000
    assert ProductionObservationAdapter().pack([
        {"stream": stream, "text": text},
    ]) == {"output": f"{stream}:\n{text}"}


def test_visible_limit_across_streams_survives_labels_and_separator():
    stdout, stderr = "x" * 4_000, "y" * 4_000
    assert ProductionObservationAdapter().pack([
        {"stream": "stdout", "text": stdout},
        {"stream": "stderr", "text": stderr},
    ]) == {"output": f"stdout:\n{stdout}\nstderr:\n{stderr}"}


def test_fragmented_visible_output_stays_visible_within_coordinator_event_bound():
    events = [
        {"stream": "stdout" if index % 2 else "stderr", "text": "x" * 31}
        for index in range(256)
    ]
    events[-1]["text"] += "x" * 64
    packed = ProductionObservationAdapter().pack(events)
    assert "_output_already_omitted" not in packed
    assert packed["output"].count("x") == 8_000
    assert 8_000 < len(packed["output"]) < 16_000


@pytest.mark.parametrize("events", [
    [{"stream": "stdout", "text": "x" * 8_001}],
    [{"stream": "stdout", "text": "x" * 4_000},
     {"stream": "stderr", "text": "y" * 4_001}],
    [{"display": "x" * 8_001}],
    [{"error": "x" * 8_001}],
    [{"display": ""}] * 4_000,
])
def test_content_and_rendered_envelope_remain_bounded(events):
    packed = ProductionObservationAdapter().pack(events)
    assert packed["_output_already_omitted"] is True
    assert len(packed["output"]) < 100
    assert "Output too long" in packed["output"]


def test_short_feedback_order_and_omission_marker_are_preserved():
    assert ProductionObservationAdapter().pack([
        {"stream": "stdout", "text": "hi", "request_id": "hidden"},
        {"display": "table"},
        {"error": "failed"},
        {"omitted_events": 3},
        {"say": "progress only"},
    ]) == {"output": "stdout:\nhi\nOut:\ntable\nExecution error: failed\n[3 output events omitted]"}
