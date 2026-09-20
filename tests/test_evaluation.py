"""Offline accounting fixtures. Stored source is data and is never executed."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from py_agent.evaluation import account_trace, journal_events
from py_agent.history import Journal

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/replay_trace.py"


def event(kind, content, *, timestamp=0, **metadata):
    return {"run_id": "r1", "agent_id": "a1", "kind": kind, "content": content, "timestamp": timestamp, **metadata}


def request(number, *, checkpoint=False):
    return event(
        "generation_request",
        {
            "model": "example/model",
            "checkpoint": checkpoint,
            "messages": [{"role": "user", "content": "😀" if number == 1 else "x"}],
            "estimated_input_tokens": number * 100 + 64,
        },
        generation_id=f"g{number}",
        timestamp=number * 10,
    )


def response(number, usage, *, reason="stop", stale=False, duration=None, timestamp=None):
    extra = {} if duration is None else {"duration": duration}
    return event(
        "generation_response",
        {"text": "SECRET SOURCE MUST NOT APPEAR", "finish_reason": reason, "rejection_reason": None, "usage": usage},
        generation_id=f"g{number}",
        stale=stale,
        timestamp=number * 10 if timestamp is None else timestamp,
        **extra,
    )


def reported(**counters):
    return {"source": "reported_by_litelm", "normalized": counters, "raw": {"provider_metadata": "original"}}


def known_trace():
    return [
        event("epoch_commit", {"epoch_id": "x1"}),
        request(1),
        response(
            1,
            reported(input_tokens=100, output_tokens=20, total_tokens=120, cache_read_tokens=90, reasoning_tokens=3),
            duration=2,
        ),
        event("source", "raise AssertionError('must never execute stored source')", cell_id="c1"),
        event("dispatch", {}, cell_id="c1", timestamp=10),
        event("say_staged", "SECRET USER TEXT", cell_id="c1", final=True),
        event("cell_end", {"status": "success"}, cell_id="c1", timestamp=12),
        event("say", "SECRET USER TEXT", cell_id="c1", final=True),
        request(2),
        event("generation_cancelled", {"generation_id": "g2", "usage": "unknown", "stale": True}, timestamp=23),
        event("checkpoint", "secret checkpoint instructions"),
        request(3, checkpoint=True),
        event("generation_cancelled", {"generation_id": "g3", "usage": "unknown", "stale": True}, timestamp=32),
        response(3, reported(input_tokens=80, output_tokens=10, cache_creation_tokens=12), stale=True, timestamp=34),
        event("generation_stale", {"generation_id": "g3"}),
        event("epoch_commit", {"epoch_id": "x2"}),
        event("retrieval", {"secret_original": "not reported"}),
        request(4),
        response(4, reported(input_tokens=110, output_tokens=5), reason="length", duration=5),
        request(5),
        event("generation_error", {"generation_id": "g5", "usage": "unknown", "error": "SECRET ERROR"}, timestamp=56),
        event("source", "invalid literal", cell_id="c2"),
        event("dispatch", {}, cell_id="c2", timestamp=60),
        event("say_staged", "not final", cell_id="c2", final=True),
        event("cell_end", {"status": "error"}, cell_id="c2", timestamp=63),
        event("final_discarded", "cell failed", cell_id="c2"),
        event("source", "pass", cell_id="c3"),
        event("dispatch", {}, cell_id="c3", timestamp=70),
        event("cell_uncertain", "unknown effects", cell_id="c3"),
        event("error", "do not copy the original error"),
        event("memory_error", "private path"),
        event("fatal_limit", "disk full"),
    ]


def test_known_trace_exact_counts_separate_usage_and_unknowns():
    report = account_trace(iter(known_trace()))
    assert {key: value for key, value in report["requests"].items() if key != "records"} == {
        "count": 5,
        "observed_generations": 5,
        "checkpoint_calls": 1,
        "cancelled": 2,
        "discarded_stale": 2,
        "errored": 1,
        "rejected_responses": 1,
        "unknown_usage_requests": 2,
    }
    groups = report["reported_usage_by_model_and_source"]
    assert [(g["source"], g["requests"]) for g in groups] == [("reported_by_litelm", 3), ("unknown", 2)]
    counters = groups[0]["counters"]
    assert counters["input_tokens"] == {"reported_sum": 290, "reported_requests": 3, "missing_requests": 0}
    assert counters["output_tokens"]["reported_sum"] == 35
    assert counters["total_tokens"] == {"reported_sum": 120, "reported_requests": 1, "missing_requests": 2}
    assert counters["cache_read_tokens"]["reported_sum"] == 90
    assert counters["cache_creation_tokens"]["reported_sum"] == 12
    assert counters["reasoning_tokens"]["reported_sum"] == 3
    # Cache and reasoning subsets/premiums were NOT added into input/output.
    assert report["input_size"] == {
        "estimate_method": "Context.estimate: UTF-8 content bytes plus 64 per message, not provider tokens",
        "estimated_bytes_with_overhead_sum": 1820,
        "estimates_known": 5,
        "message_content_utf8_bytes_sum": 8,
        "content_sizes_known": 5,
    }
    assert report["generation_latency"] == {
        "seconds_sum": 20.0,
        "reported_durations": 2,
        "derived_wall_clock_durations": 3,
        "unknown": 0,
    }
    assert report["cells"] == {
        "sources": 3,
        "dispatched": 3,
        "completed": 2,
        "statuses": {"error": 1, "success": 1},
        "uncertain": 1,
        "latency_seconds_sum": 5.0,
        "latency_known": 2,
        "published_finals": 1,
        "staged_finals": 2,
        "discarded_final_cells": 1,
    }
    assert report["context"] == {"epoch_commits": 2, "transitions": 1, "checkpoint_notices": 1, "retrieval_calls": 1}
    assert report["errors"] == {"runtime_notifications": 1, "memory": 1, "fatal_limits": 1, "provider_requests": 1}
    assert report["cost"] == {"status": "unknown", "amount": None, "currency": None, "price_source": None}
    assert report["anomalies"] == {}
    encoded = json.dumps(report)
    assert "SECRET" not in encoded
    assert "secret checkpoint" not in encoded
    assert "invalid literal" not in encoded


def test_current_requests_need_no_legacy_checkpoint_fields():
    current = request(1)
    del current["content"]["checkpoint"]
    report = account_trace([current, response(1, reported(output_tokens=3))])
    assert report["requests"]["count"] == 1
    assert report["requests"]["checkpoint_calls"] == 0
    assert report["context"]["checkpoint_notices"] == 0


def test_late_usage_resolves_cancellation_without_double_counting():
    events = [
        request(1),
        event("generation_cancelled", {"generation_id": "g1", "usage": "unknown"}),
        response(1, reported(input_tokens=10, output_tokens=2), stale=True),
        response(1, reported(input_tokens=10, output_tokens=2), stale=True),
    ]
    report = account_trace(events)
    assert report["requests"]["cancelled"] == 1
    assert report["requests"]["unknown_usage_requests"] == 0
    assert report["reported_usage_by_model_and_source"][0]["counters"]["input_tokens"]["reported_sum"] == 10
    assert report["anomalies"] == {"duplicate_response": 1}


def test_partial_usage_invalid_counts_and_samples_remain_distinct():
    usage = reported(output_tokens=0, input_tokens=True, reasoning_tokens=-1, total_tokens="5")
    usage["samples"] = [{"prompt_tokens": 3}, {"completion_tokens": 0}]
    report = account_trace([request(1), response(1, usage)])
    record = report["requests"]["records"][0]
    assert record["usage"]["normalized"] == {"output_tokens": 0}
    assert record["usage_unknown"] is False  # reported zero is not missing
    assert record["usage"]["samples"] == usage["samples"]
    assert record["usage"]["raw"] == usage["raw"]
    assert report["anomalies"] == {"invalid_usage_counter": 1}
    counter = report["reported_usage_by_model_and_source"][0]["counters"]["input_tokens"]
    assert counter == {"reported_sum": 0, "reported_requests": 0, "missing_requests": 1}


def test_separate_run_agent_model_and_usage_provenance():
    first = request(1)
    second = {**request(1), "run_id": "r2"}
    second["content"]["model"] = "different/model"
    events = [
        first,
        response(1, reported(input_tokens=7)),
        second,
        {**response(1, {"source": "other_convention", "normalized": {"input_tokens": 8}}), "run_id": "r2"},
        event("epoch_commit", {}),
        {**event("epoch_commit", {}), "run_id": "r2"},
    ]
    report = account_trace(events)
    assert report["requests"]["count"] == 2
    assert report["context"]["transitions"] == 0
    assert len(report["reported_usage_by_model_and_source"]) == 2
    assert {r["run_id"] for r in report["requests"]["records"]} == {"r1", "r2"}


def test_orphan_lifecycle_and_missing_latency_are_visible():
    report = account_trace([
        event("generation_request", {}),
        {k: v for k, v in response(1, {}).items() if k != "timestamp"},
    ])
    assert report["requests"]["count"] == 0
    assert report["requests"]["observed_generations"] == 1
    assert report["generation_latency"]["unknown"] == 1
    assert report["requests"]["unknown_usage_requests"] == 1
    assert report["anomalies"] == {"generation_event_without_id": 1, "generation_without_request": 1}
    assert account_trace([])["cost"]["status"] == "unknown"


def test_read_only_journal_preserves_content_permissions_and_does_not_execute(tmp_path):
    path = tmp_path / "space ? # journal.sqlite"
    marker = tmp_path / "MUST_NOT_EXIST"
    with Journal(path, "stored") as journal:
        journal.append("source", f"from pathlib import Path; Path({str(marker)!r}).touch()", cell_id="a1:c1")
        journal.append("dispatch", {}, cell_id="a1:c1")
    before, mode = path.read_bytes(), path.stat().st_mode
    report = account_trace(journal_events(path))
    assert report["cells"]["dispatched"] == 1
    assert path.read_bytes() == before
    assert path.stat().st_mode == mode
    assert not marker.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]
    with pytest.raises(FileNotFoundError):
        list(journal_events(tmp_path / "missing.sqlite"))
    assert not (tmp_path / "missing.sqlite").exists()


def test_reader_rejects_invalid_but_accepts_large_records(tmp_path):
    path = tmp_path / "invalid.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE events(seq INTEGER, payload TEXT)")
    db.execute("INSERT INTO events VALUES(1, '[]')")
    db.commit()
    with pytest.raises(ValueError, match="object"):
        list(journal_events(path))
    content = "x" * (8 * 1024 * 1024 + 1)
    db.execute("UPDATE events SET payload=?", (json.dumps({"content": content}),))
    db.commit()
    assert list(journal_events(path)) == [{"content": content}]
    db.execute("UPDATE events SET payload=NULL")
    db.commit()
    with pytest.raises(ValueError, match="no payload"):
        list(journal_events(path))
    db.close()


def test_cli_multiple_journals_json_and_terminal_escape_safety(tmp_path):
    path = tmp_path / "journal.sqlite"
    with Journal(path, "cli") as journal:
        journal.append(
            "generation_request",
            {"model": "hostile\x1b]52;title\x07\u202e", "messages": [], "max_tokens": 2},
            generation_id="a1:g1",
        )
        journal.append("output", "ORIGINAL OUTPUT MUST NOT PRINT \x1b[31m", cell_id="a1:c1")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(path), "--journal", str(path)], capture_output=True, text=True, check=True
    )
    value = json.loads(result.stdout)
    assert len(value["journals"]) == 2
    assert "\x1b" not in result.stdout
    assert "\u202e" not in result.stdout
    assert "\\u001b" in result.stdout
    assert "\\u202e" in result.stdout
    assert "ORIGINAL OUTPUT" not in result.stdout
    assert result.stderr == ""


def test_cli_requires_a_journal_and_has_no_synthetic_benchmark_options():
    missing = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert missing.returncode == 2
    assert "supply a journal" in missing.stderr
    removed = subprocess.run([sys.executable, str(SCRIPT), "--compare-tails", "0", "3"], capture_output=True, text=True)
    assert removed.returncode == 2
    assert "unrecognized arguments" in removed.stderr
    help_result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, check=True)
    assert "--compare-tails" not in help_result.stdout
    assert "--turns" not in help_result.stdout
    assert "--reset-every" not in help_result.stdout


def test_cli_missing_database_reports_only_escaped_stderr(tmp_path):
    path = tmp_path / "missing\x1b[31m.sqlite"
    result = subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "\x1b" not in result.stderr
    assert "\\u001b" in result.stderr or "\\\\x1b" in result.stderr
    assert not path.exists()
