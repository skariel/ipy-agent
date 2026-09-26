"""Collapse audit records retain control source, provenance, and redaction."""
from __future__ import annotations

import json

import pytest

from py_agent.contracts import ContextSnapshot, ModelRequest, Origin
from py_agent.session_journal import JournalError, NoPersistenceJournal, SQLiteSessionJournal


def request():
    return ModelRequest(
        Origin("session", "request", "terminal", 7, "generation"),
        ContextSnapshot(1, (("user", "task"),)),
        "offline/model",
    )


def journal_at(tmp_path, **kwargs):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    journal = SQLiteSessionJournal(private / "history.sqlite", **kwargs)
    journal.start("session", 7, "offline", "offline/model")
    return journal


def test_collapse_source_receipt_and_generation_are_audited(tmp_path):
    journal = journal_at(tmp_path)
    source = 'collapse("u1", "m1", "Keep active goal")'
    receipt = "Collapsed [u1, m1). Originals retained in collapsed[7]."
    try:
        journal.record_context_collapse(request(), source, outcome="requested")
        journal.record_context_collapse(request(), source, outcome="succeeded", detail=receipt)
        journal.record_context_collapse(request(), source, outcome="rejected", detail="stale boundary")
        records = [json.loads(row[0]) for row in journal.db.execute(
            "SELECT payload FROM events WHERE kind='context_collapse' ORDER BY seq",
        )]
        assert [record["content"]["outcome"] for record in records] == [
            "requested", "succeeded", "rejected",
        ]
        assert all(record["content"]["source"] == source for record in records)
        assert records[0]["content"]["detail"] == ""
        assert records[1]["content"]["detail"] == receipt
        assert records[2]["content"]["detail"] == "stale boundary"
        assert all(record["session_id"] == "session" for record in records)
        assert all(record["request_id"] == "request" for record in records)
        assert all(record["config_revision"] == 7 for record in records)
        assert all(record["metadata"] == {
            "frontend_id": "terminal", "generation_id": "generation",
        } for record in records)
        assert len(journal.search("session", "Keep active goal", kind="context_collapse")) == 3
    finally:
        journal.close()


def test_collapse_audit_redacts_source_and_detail(tmp_path):
    journal = journal_at(tmp_path)
    journal.set_sensitive_values(("custom-sensitive-value",))
    try:
        journal.record_context_collapse(
            request(), 'collapse("u1", "m1", "api_key=secret-value")',
            outcome="rejected", detail="custom-sensitive-value",
        )
        payload = journal.db.execute(
            "SELECT payload FROM events WHERE kind='context_collapse'",
        ).fetchone()[0]
        assert "secret-value" not in payload
        assert "custom-sensitive-value" not in payload
        assert "[REDACTED]" in payload
    finally:
        journal.close()


@pytest.mark.parametrize("kwargs", [
    {"source": "pass", "outcome": "executed"},
    {"source": None, "outcome": "requested"},
    {"source": "pass", "outcome": "requested", "detail": None},
])
def test_collapse_audit_rejects_invalid_records(tmp_path, kwargs):
    journal = journal_at(tmp_path)
    try:
        with pytest.raises(JournalError):
            journal.record_context_collapse(request(), **kwargs)
        assert journal.db.execute(
            "SELECT count(*) FROM events WHERE kind='context_collapse'",
        ).fetchone()[0] == 0
    finally:
        journal.close()


def test_collapse_audit_never_truncates_oversized_source(tmp_path):
    journal = journal_at(tmp_path, max_event_bytes=1024)
    try:
        with pytest.raises(JournalError, match="record limit"):
            journal.record_context_collapse(request(), "x" * 2048, outcome="requested")
        assert journal.db.execute(
            "SELECT count(*) FROM events WHERE kind='context_collapse'",
        ).fetchone()[0] == 0
    finally:
        journal.close()


def test_no_persistence_collapse_audit_is_noop():
    journal = NoPersistenceJournal()
    assert journal.record_context_collapse(request(), "collapse('u1', 'm1', 'summary')",
                                           outcome="requested") is None
    assert journal.recent("session") == []
