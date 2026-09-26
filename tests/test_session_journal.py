"""Durable coordinator-history contracts; no provider or worker calls."""
from __future__ import annotations

import json
import sqlite3
import stat

import pytest

from py_agent.contracts import (
    ContextSnapshot,
    ExecutionRequest,
    ExecutionResult,
    ModelRequest,
    ModelResponse,
    Origin,
    SayOutput,
)
from py_agent.session_journal import (
    JournalError,
    MAX_HISTORY_SEARCH_QUERY_CHARS,
    MAX_HISTORY_SEARCH_SCAN,
    NoPersistenceJournal,
    SQLiteSessionJournal,
)


def private_path(tmp_path, name="history.sqlite"):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    return directory / name


def test_journal_records_only_safe_request_usage_source_and_bounded_result(tmp_path):
    path = private_path(tmp_path)
    journal = SQLiteSessionJournal(path, max_stream_chars=5, max_say_outputs=1)
    session_id = "session-1"
    secret = "credential-that-must-not-be-written"
    origin = Origin(session_id, "request-1", "terminal", 7, "generation-1")
    request = ModelRequest(
        origin,
        ContextSnapshot(2, (("system", "system prompt"), ("user", f"api_key={secret}"))),
        "model/test",
        {"temperature": "0.2", "api_key": secret},
    )
    response = ModelResponse(
        "secret response text is not persisted",
        usage={"source": "reported", "normalized": {"input_tokens": 19}, "authorization": secret},
        provider_id="offline",
        model="model/test",
        adapter_metadata={"headers": {"authorization": secret}},
    )
    execution = ExecutionRequest(
        Origin(session_id, "request-1", "terminal", 7, "generation-1", "execution-1"),
        f"token = 'Bearer {secret}'",
        "agent",
    )
    result = ExecutionResult(
        execution.origin,
        "success",
        stdout="1234567890",
        stderr="api_key=" + secret,
        say_outputs=(SayOutput("first output"), SayOutput("second output")),
    )

    journal.start(session_id, 7, "offline", "model/test")
    journal.record_model_request(request)
    journal.record_provider_usage(request, response, outcome="returned")
    journal.record_execution_source(execution)
    journal.record_execution_result(execution, result)

    rows = journal.db.execute(
        "SELECT event_id, kind, config_revision, payload FROM events WHERE session_id=? ORDER BY seq",
        (session_id,),
    ).fetchall()
    records = [json.loads(row[3]) for row in rows]
    assert [row[1] for row in rows] == [
        "session_start", "model_request", "provider_usage", "execution_source", "execution_result",
    ]
    assert all(row[2] == 7 for row in rows)
    model_record = records[1]
    assert model_record["content"]["model"] == "model/test"
    assert model_record["content"]["context"]["messages"][0] == ["system", "system prompt"]
    assert model_record["content"]["options"]["api_key"] == "[REDACTED]"
    assert secret not in rows[1][3]
    assert "adapter_metadata" not in rows[2][3]
    assert secret not in rows[2][3]
    usage = records[2]["content"]["usage"]
    assert usage["normalized"]["input_tokens"] == 19
    assert usage["authorization"] == "[REDACTED]"
    assert records[3]["metadata"]["author"] == "agent"
    assert records[3]["metadata"]["frontend_id"] == "terminal"
    assert records[3]["metadata"]["execution_id"] == "execution-1"
    assert secret not in rows[3][3]

    output = records[4]["content"]
    assert output["status"] == "success"
    assert output["stdout"] == {
        "text": "12345", "original_chars": 10, "truncated": True, "redacted": False,
    }
    assert output["stderr"]["redacted"] is True
    assert output["say_outputs_omitted"] == 1
    assert len(output["say_outputs"]) == 1
    assert secret.encode() not in path.read_bytes()

    journal.end(session_id, 7, "closed")
    journal.close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_journal_history_pages_searches_and_scopes_sessions(tmp_path):
    journal = SQLiteSessionJournal(private_path(tmp_path))
    journal.start("one", 1, "offline", "model")
    journal.start("two", 1, "offline", "model")
    event = journal._append("note", "one", "req-1", 1, {"text": "雪 needle " + "x" * 200})
    journal._append("note", "two", "req-2", 1, {"text": "needle in another session"})

    assert journal.recent("one", 2)[0]["id"] == event["id"]
    assert journal.search("one", "NEEDLE")[0]["id"] == event["id"]
    assert journal.search("one", "needle", kind="missing") == []
    first = journal.read("one", event["id"], limit=40)
    second = journal.read("one", event["id"], offset=first["next_offset"], limit=40)
    assert first["truncated"]
    assert second["offset"] == first["next_offset"]
    assert journal.read("one", event["id"], limit=64_000)["total_chars"] > 40
    with pytest.raises(KeyError):
        journal.read("two", event["id"])
    with pytest.raises(ValueError):
        journal.read("one", event["id"], limit=64_001)
    with pytest.raises(ValueError):
        journal.recent("one", True)
    journal.close()


def test_history_search_has_query_and_scan_bounds(tmp_path):
    journal = SQLiteSessionJournal(private_path(tmp_path))
    journal.start("session", 0, "offline", "model")
    journal._append("note", "session", "older", 0, {"text": "needle in older event"})
    journal._append("note", "session", "newer", 0, {"text": "newest event"})

    assert journal.search("session", "needle", scan_limit=1) == []
    assert journal.search("session", "needle", scan_limit=MAX_HISTORY_SEARCH_SCAN)
    with pytest.raises(ValueError):
        journal.search("session", "x" * (MAX_HISTORY_SEARCH_QUERY_CHARS + 1))
    with pytest.raises(ValueError):
        journal.search("session", "needle", scan_limit=MAX_HISTORY_SEARCH_SCAN + 1)
    journal.close()


def test_sensitive_config_values_are_redacted_before_durable_commit(tmp_path):
    journal = SQLiteSessionJournal(private_path(tmp_path))
    secret = "private-config-key-value"
    journal.set_sensitive_values((secret,))
    journal.start("session", 0, "offline", "model")
    event = journal._append(
        "note", "session", "request", 0,
        {"value": secret, "text": f"contains {secret}"},
    )
    request = ModelRequest(
        Origin("session", "request-model", "terminal", 0, "generation"),
        ContextSnapshot(0, (("user", f"prompt contains {secret}"),)),
        "offline/model",
        {"option": secret},
    )
    journal.record_model_request(request)
    execution = ExecutionRequest(
        Origin("session", "request-exec", "terminal", 0, None, "execution"),
        f"print({secret!r})", "user",
    )
    journal.record_execution_source(execution)
    journal.record_execution_result(
        execution,
        ExecutionResult(
            execution.origin, "success", stdout=secret,
            say_outputs=(SayOutput(secret),),
        ),
    )

    stored = journal.path.read_bytes()
    assert secret.encode() not in stored
    page = journal.read("session", event["id"])
    assert page["content"].count("[REDACTED]") >= 2
    with pytest.raises(JournalError, match="redaction limits"):
        journal.set_sensitive_values(("x" * 8_193,))
    journal.close()


def test_required_record_failure_is_not_swallowed_and_db_is_append_only(tmp_path):
    journal = SQLiteSessionJournal(private_path(tmp_path))
    journal.start("session", 0, "offline", "model")
    journal.db.execute(
        "CREATE TRIGGER simulated_storage_failure BEFORE INSERT ON events "
        "WHEN NEW.kind='model_request' BEGIN SELECT RAISE(ABORT, 'disk full'); END"
    )
    request = ModelRequest(
        Origin("session", "request", "terminal", 0, "generation"),
        ContextSnapshot(0, (("user", "hello"),)),
        "model",
    )
    with pytest.raises(JournalError, match="commit required journal event"):
        journal.record_model_request(request)
    assert len(journal.recent("session")) == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        journal.db.execute("DELETE FROM events")
    journal.close()


def test_private_path_rejects_symlinks_and_no_persistence_is_explicit(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(JournalError, match="symlinks"):
        SQLiteSessionJournal(link / "history.sqlite")
    service = NoPersistenceJournal()
    assert service.persisted is False
    service.start("session", 0, "fake", "fake")
    service.close()
