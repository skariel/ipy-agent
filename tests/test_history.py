from __future__ import annotations

import json
import sqlite3
import stat

import pytest

from py_agent.history import Journal, JournalError


def test_exact_events_paging_cell_group_and_reopen(tmp_path):
    path = tmp_path / "private" / "journal.sqlite3"
    with Journal(path, "run-1") as journal:
        first = journal.append("source", "x = '雪'\n", "a1:c0001")
        second = journal.append("stdout", "snow\r\n", "a1:c0001")
        journal.append("cell_end", {"status": "success"}, "a1:c0001")
        parts, offset = [], 0
        while True:
            result = journal.read("a1:c0001", offset=offset, limit=51)
            parts.append(result["content"])
            offset = result["next_offset"]
            if not result["truncated"]:
                break
        events = [json.loads(line) for line in "".join(parts).splitlines()]
        assert [event["kind"] for event in events] == ["source", "stdout", "cell_end"]
        assert events[0]["content"] == "x = '雪'\n"
        assert events[1]["content"] == "snow\r\n"
        assert journal.read(first["id"])["content"] == json.dumps(first, ensure_ascii=False, separators=(",", ":"))
    with Journal(path, "run-1") as reopened:
        third = reopened.append("user", "continue")
        assert third["seq"] > second["seq"]
        assert reopened.read(first["id"])["content"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_history_paging_search_and_scoping(tmp_path):
    path = tmp_path / "history.db"
    with Journal(path, "r") as parent, Journal(path, "r", "a2") as child:
        parent.append("source", "failed assertion 雪", "a1:c1")
        parent.append("retrieval", "failed assertion repeated")
        parent.append("stdout", "x" * 10000)
        child_event = child.append("source", "failed assertion child", "a2:c1")
        assert len(parent.search("failed assertion")) == 1
        assert parent.search("failed assertion")[0]["kind"] == "source"
        assert len(parent.search("failed", kind="retrieval")) == 1
        assert parent.search("FAILED ASSERTION")[0]["excerpt"] == "failed assertion 雪"
        assert len(parent.recent(10**9)) == 3
        page = parent.read(parent.recent(1)[0]["id"], limit=10**9)
        assert len(page["content"]) > 10000
        assert not page["truncated"]
        assert len(parent.read(parent.recent(1)[0]["id"])["content"]) == 8000  # default page only
        assert len(parent.recent(1)[0]["excerpt"]) == 240
        assert parent.search("", limit=0) == []
        with pytest.raises(KeyError):
            parent.read(child_event["id"])
        with pytest.raises(ValueError):
            parent.append("source", "wrong scope", "a2:c2")
        for value in [-1, True, "100"]:
            with pytest.raises(ValueError):
                parent.recent(value)
            with pytest.raises(ValueError):
                parent.read("a1:c1", offset=value)
        assert len(parent.search("x" * 1001)) == 1
        with pytest.raises(ValueError):
            parent.search(None)


def test_failed_sql_insert_rolls_back_atomically_and_ids_do_not_reuse(tmp_path):
    with Journal(tmp_path / "history.db", "r") as journal:
        first = journal.append("user", "hello")
        journal.db.execute(
            "CREATE TRIGGER simulated_failure BEFORE INSERT ON events "
            "WHEN NEW.kind='simulated_failure' BEGIN SELECT RAISE(ABORT, 'storage failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="storage failure"):
            journal.append("simulated_failure", "z" * 1000)
        assert len(journal.recent()) == 1
        second = journal.append("user", "next")
        assert second["seq"] == first["seq"] + 1
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            journal.db.execute("DELETE FROM events")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            journal.db.execute("UPDATE events SET kind='forged'")
        with pytest.raises(ValueError):
            journal.append("source", "x", id="fake")


def test_existing_journal_size_no_longer_imposes_a_session_quota(tmp_path):
    with Journal(tmp_path / "history.db", "r") as journal:
        # Seed the old accounting column without physically writing 64 MiB.
        original = {
            "id": "a1:e00000001",
            "seq": 1,
            "run_id": "r",
            "agent_id": "a1",
            "timestamp": 0,
            "kind": "user",
            "cell_id": None,
            "content": "legacy evidence",
        }
        journal.db.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, "r", "a1", original["id"], "user", None, json.dumps(original), "legacy evidence", 64 * 1024 * 1024 + 1),
        )
        event = journal.append("user", "still recording beyond the former quota")
        assert event["seq"] == 2
        assert not hasattr(journal, "max_bytes")
        assert json.loads(journal.read(event["id"])["content"])["content"] == event["content"]
        message = "storage diagnostic " * 1000
        assert journal.append_emergency(message)["content"] == message


def test_requested_result_counts_are_not_silently_clamped(tmp_path):
    with Journal(tmp_path / "history.db", "r") as journal:
        for index in range(125):
            journal.append("user", f"needle {index}")
        assert len(journal.recent(125)) == 125
        assert len(journal.search("needle", limit=125)) == 125
        assert len(journal.recent()) == 10
        assert len(journal.search("needle")) == 20


def test_epoch_is_one_durable_record(tmp_path):
    path = tmp_path / "history.db"
    with Journal(path, "r") as journal:
        event = journal.commit_epoch(["a1:e2"], ["a1:e1"], ["a1:e3"], epoch_id="e2", kernel_epoch="k1")
    with Journal(path, "r") as journal:
        value = json.loads(journal.read(event["id"])["content"])["content"]
        assert value == {"retained": ["a1:e2"], "evicted": ["a1:e1"], "pending": ["a1:e3"], "epoch_id": "e2"}
    with pytest.raises(JournalError, match="different run"):
        Journal(path, "wrong-run")


def test_historical_snapshot_events_remain_readable_without_memory_runtime(tmp_path):
    path = tmp_path / "historical.db"
    content = {
        "snapshot": {"content": "historical notes", "sha256": "old", "size_bytes": 16},
        "retained": [],
        "evicted": [],
        "pending": [],
        "epoch_id": "x1",
    }
    with Journal(path, "old-run") as journal:
        event = journal.append("epoch_commit", content)
    with Journal(path, "old-run") as journal:
        assert json.loads(journal.read(event["id"])["content"])["content"] == content


def test_symlink_journal_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    with pytest.raises(JournalError, match="symlinks"):
        Journal(tmp_path / "link" / "db", "r")


def test_invalid_json_does_not_leave_transaction_open(tmp_path):
    with Journal(tmp_path / "db", "r") as journal:
        with pytest.raises(ValueError):
            journal.append("usage", {"tokens": float("nan")})
        with pytest.raises(TypeError):
            journal.append("usage", "value", extra=object())
        assert journal.append("user", "still valid")["seq"] == 1
