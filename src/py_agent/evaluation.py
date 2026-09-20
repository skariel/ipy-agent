"""Deterministic, offline trace accounting. No model calls or source execution.

Reported counters are grouped by model and usage source, never added to byte
estimates or inferred cache/cost figures. A late response can resolve previously
unknown cancelled-call usage; duplicate lifecycle events do not bill it twice.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from copy import deepcopy
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

COUNTERS = ("input_tokens", "output_tokens", "total_tokens", "cache_read_tokens",
            "cache_creation_tokens", "reasoning_tokens")


def journal_events(path: str | Path) -> Iterable[dict]:
    """Iterate a consistent read-only SQLite snapshot in original sequence order.

    Does not instantiate Journal (which creates schema and changes permissions),
    load extensions, re-render live objects, or execute stored Python source.
    Missing databases fail instead of being created. One complete record is read
    at a time; the database's existing run/agent identity is retained.
    """
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("Journal must be a regular SQLite file")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("BEGIN")
        rows = connection.execute("SELECT payload FROM events ORDER BY seq")
        for (payload,) in rows:
            if payload is None:
                raise ValueError("Journal record has no payload")
            event = json.loads(payload, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON value")))
            if not isinstance(event, dict):
                raise ValueError("Journal event must be a JSON object")
            yield event
    finally:
        connection.close()


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _key(event, identifier):
    # IDs are run-scoped; two journals can both contain a1:g000001.
    return (str(event.get("run_id", "unknown")), str(event.get("agent_id", "unknown")), identifier)


def _counter_summary(values):
    return {name: {"reported_sum": sum(v[name] for v in values if name in v),
                   "reported_requests": sum(name in v for v in values),
                   "missing_requests": sum(name not in v for v in values)} for name in COUNTERS}


def account_trace(events: Iterable[Mapping[str, Any]]) -> dict:
    """Summarize original events without returning transcript/source content.

    Usage coverage is field-by-field: a reported output counter does not make
    missing input/cache counters known. ``unknown_usage_requests`` counts calls
    without any valid normalized counters, not calls with fully known billing.
    Cost is always unknown because no versioned price source is configured.
    """
    generations = {}
    cells = {}
    epoch_counts = Counter()
    counts = Counter()
    anomalies = Counter()
    for event in events:
        if not isinstance(event, Mapping):
            raise ValueError("Trace events must be mappings")
        kind = event.get("kind")
        content = event.get("content")
        obj = content if isinstance(content, Mapping) else {}
        counts["events"] += 1
        if kind == "generation_request" or kind in {"generation_response", "generation_stale", "generation_cancelled", "generation_error"}:
            gid = event.get("generation_id", obj.get("generation_id"))
            if not isinstance(gid, str):
                anomalies["generation_event_without_id"] += 1
                continue
            key = _key(event, gid)
            generation = generations.setdefault(key, {"request": None, "response": None, "cancelled": False,
                                                       "stale": False, "errored": False, "terminal_timestamp": None})
            if kind == "generation_request":
                if generation["request"] is not None:
                    anomalies["duplicate_request"] += 1
                else:
                    generation["request"] = deepcopy(dict(event))
            elif kind == "generation_response":
                if generation["response"] is not None:
                    anomalies["duplicate_response"] += 1
                # Last exposed metadata is authoritative; never sum responses for
                # the same request, including cancelled calls reporting late.
                generation["response"] = deepcopy(dict(event))
                generation["stale"] |= event.get("stale") is True
            else:
                generation[{"generation_cancelled": "cancelled", "generation_stale": "stale",
                            "generation_error": "errored"}[kind]] = True
                generation["stale"] |= obj.get("stale") is True
            if kind in {"generation_response", "generation_cancelled", "generation_error"}:
                generation["terminal_timestamp"] = event.get("timestamp")
        elif kind == "epoch_commit":
            epoch_counts[_key(event, "epochs")[:2]] += 1
        elif kind in {"source", "dispatch", "cell_end", "cell_uncertain"}:
            cid = event.get("cell_id")
            if not isinstance(cid, str):
                anomalies["cell_event_without_id"] += 1
                continue
            cell = cells.setdefault(_key(event, cid), {})
            if kind in cell:
                anomalies["duplicate_" + kind] += 1
            cell.setdefault(kind, deepcopy(dict(event)))
        elif kind == "say" and event.get("final") is True:
            counts["published_finals"] += 1
        elif kind == "say_staged":
            counts["staged_finals"] += 1
        elif kind == "final_discarded":
            counts["discarded_final_cells"] += 1
        elif kind == "retrieval":
            counts["retrieval_calls"] += 1
        elif kind == "checkpoint":
            # Historical journals only; the runtime no longer creates save turns.
            counts["checkpoint_notices"] += 1
        elif kind in {"error", "memory_error", "fatal_limit", "generation_error"}:
            counts[kind] += 1

    requests = []
    groups = {}
    latency_total = 0.0
    latency_reported = latency_derived = 0
    estimated_values = []
    actual_bytes = []
    for (run_id, agent_id, gid), generation in sorted(generations.items()):
        request_event = generation["request"]
        response_event = generation["response"]
        if request_event is None:
            anomalies["generation_without_request"] += 1
        request = request_event.get("content", {}) if request_event else {}
        response = response_event.get("content", {}) if response_event else {}
        request = request if isinstance(request, Mapping) else {}
        response = response if isinstance(response, Mapping) else {}
        usage = response.get("usage", {})
        usage = usage if isinstance(usage, Mapping) else {}
        normalized = usage.get("normalized", {})
        normalized = normalized if isinstance(normalized, Mapping) else {}
        valid = {k: v for k, v in normalized.items() if k in COUNTERS and type(v) is int and v >= 0}
        if any(k in COUNTERS and (type(v) is not int or v < 0) for k, v in normalized.items()):
            anomalies["invalid_usage_counter"] += 1
        source = usage.get("source", "unknown")
        if not isinstance(source, str):
            source = "unknown"
        model = request.get("model", "unknown")
        if not isinstance(model, str):
            model = "unknown"
        groups.setdefault((model, source), []).append(valid)
        estimate = request.get("estimated_input_tokens")
        if type(estimate) is int and estimate >= 0:
            estimated_values.append(estimate)
        else:
            estimate = None
        messages = request.get("messages")
        byte_count = None
        if isinstance(messages, list) and all(isinstance(m, Mapping) and isinstance(m.get("content"), str) for m in messages):
            byte_count = sum(len(m["content"].encode("utf-8")) for m in messages)
            actual_bytes.append(byte_count)
        duration = response_event.get("duration") if response_event else None
        latency_source = "reported_duration"
        if _number(duration):
            latency_reported += 1
        else:
            start = request_event.get("timestamp") if request_event else None
            end = generation["terminal_timestamp"]
            if _number(start) and _number(end) and end >= start:
                duration = end - start
                latency_source = "journal_wall_clock_difference"
                latency_derived += 1
            else:
                duration, latency_source = None, "unknown"
        if duration is not None:
            latency_total += duration
        rejected = response_event is not None and (
            response.get("finish_reason") != "stop" or response.get("rejection_reason") is not None
            or not isinstance(response.get("text"), str) or not response["text"].strip())
        requests.append({"run_id": run_id, "agent_id": agent_id, "generation_id": gid,
                         "request_recorded": request_event is not None, "model": model,
                         "checkpoint": request.get("checkpoint") is True,
                         "cancelled": generation["cancelled"], "discarded_stale": generation["stale"],
                         "errored": generation["errored"], "rejected_response": rejected,
                         "response_recorded": response_event is not None,
                         "usage": {"source": source, "normalized": valid,
                                   "raw": deepcopy(usage.get("raw")),
                                   "samples": deepcopy(usage.get("samples"))},
                         "usage_unknown": not bool(valid),
                         "estimated_input_bytes_with_overhead": estimate,
                         "message_content_utf8_bytes": byte_count,
                         "latency_seconds": duration, "latency_source": latency_source})

    cell_statuses = Counter()
    cell_latency = 0.0
    cell_latency_known = 0
    for cell in cells.values():
        end = cell.get("cell_end", {})
        status = end.get("content", {}).get("status") if isinstance(end.get("content"), Mapping) else None
        if status in {"success", "error", "wait", "invalid_control"}:
            cell_statuses[status] += 1
        elif end:
            anomalies["unknown_cell_status"] += 1
        start_time = cell.get("dispatch", {}).get("timestamp")
        end_time = end.get("timestamp")
        if _number(start_time) and _number(end_time) and end_time >= start_time:
            cell_latency += end_time - start_time
            cell_latency_known += 1
    return {
        "schema_version": 1,
        "scope": "offline accounting only; no execution, task-quality, repetition, price or actual-cache-hit inference",
        "requests": {"count": sum(r["request_recorded"] for r in requests),
                     "observed_generations": len(requests),
                     "checkpoint_calls": sum(r["checkpoint"] for r in requests),
                     "cancelled": sum(r["cancelled"] for r in requests),
                     "discarded_stale": sum(r["discarded_stale"] for r in requests),
                     "errored": sum(r["errored"] for r in requests),
                     "rejected_responses": sum(r["rejected_response"] for r in requests),
                     "unknown_usage_requests": sum(r["usage_unknown"] for r in requests),
                     "records": requests},
        "reported_usage_by_model_and_source": [
            {"model": model, "source": source, "requests": len(values), "counters": _counter_summary(values)}
            for (model, source), values in sorted(groups.items())],
        "input_size": {"estimate_method": "Context.estimate: UTF-8 content bytes plus 64 per message, not provider tokens",
                       "estimated_bytes_with_overhead_sum": sum(estimated_values),
                       "estimates_known": len(estimated_values),
                       "message_content_utf8_bytes_sum": sum(actual_bytes),
                       "content_sizes_known": len(actual_bytes)},
        "generation_latency": {"seconds_sum": latency_total, "reported_durations": latency_reported,
                               "derived_wall_clock_durations": latency_derived,
                               "unknown": len(requests) - latency_reported - latency_derived},
        "cells": {"sources": sum("source" in c for c in cells.values()),
                  "dispatched": sum("dispatch" in c for c in cells.values()),
                  "completed": sum("cell_end" in c for c in cells.values()),
                  "statuses": dict(sorted(cell_statuses.items())),
                  "uncertain": sum("cell_uncertain" in c for c in cells.values()),
                  "latency_seconds_sum": cell_latency, "latency_known": cell_latency_known,
                  "published_finals": counts["published_finals"], "staged_finals": counts["staged_finals"],
                  "discarded_final_cells": counts["discarded_final_cells"]},
        "context": {"epoch_commits": sum(epoch_counts.values()),
                    "transitions": sum(max(0, n - 1) for n in epoch_counts.values()),
                    "checkpoint_notices": counts["checkpoint_notices"], "retrieval_calls": counts["retrieval_calls"]},
        "errors": {"runtime_notifications": counts["error"], "memory": counts["memory_error"],
                   "fatal_limits": counts["fatal_limit"], "provider_requests": sum(r["errored"] for r in requests)},
        "events": counts["events"], "anomalies": dict(sorted(anomalies.items())),
        "cost": {"status": "unknown", "amount": None, "currency": None, "price_source": None},
    }

