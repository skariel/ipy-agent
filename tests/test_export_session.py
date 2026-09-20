"""Offline HTML export: journal strings stay data; existing paths stay untouched."""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/export_session.py"
SPEC = importlib.util.spec_from_file_location("export_session", SCRIPT)
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


def database(path, records):
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY, payload TEXT)")
        connection.executemany(
            "INSERT INTO events VALUES (?, ?)", [(index, json.dumps(record)) for index, record in enumerate(records, 1)]
        )
    return path


class Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.pres = []
        self._pre = None

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "pre":
            self._pre = []

    def handle_data(self, data):
        if self._pre is not None:
            self._pre.append(data)

    def handle_endtag(self, tag):
        if tag == "pre" and self._pre is not None:
            self.pres.append("".join(self._pre))
            self._pre = None


def test_full_context_and_records_are_escaped_chronological_data(tmp_path):
    injected = '</pre></details><script>alert(1)</script><img src="https://invalid.example/exfil" onerror="alert(2)"> & "quoted"'
    messages = [
        {"role": "system", "content": "Python only\n" + injected},
        {"role": "user", "content": "memory α\nline two"},
        {"role": "assistant", "content": "print('actual cell')"},
        {"role": "user", "content": "[RUNTIME OBSERVATION]\nstdout: 42\nstderr: diagnostic"},
    ]
    records = [
        {"id": injected, "seq": 1, "kind": "source", "content": injected},
        {
            "id": "a1:e2",
            "seq": 2,
            "kind": "generation_request",
            "content": {
                "messages": messages,
                "provider_request": {"body": {"instructions": injected, "input": messages}},
                "unknown_metadata": {"nested": [1, True, None]},
            },
        },
        {"id": "a1:e3", "seq": 3, "kind": injected, "content": {"unfamiliar": "retained completely"}},
    ]
    journal = database(tmp_path / "journal.sqlite", records)
    original = journal.read_bytes()
    original_stat = journal.stat()
    output, count = exporter.export_session(journal)
    assert count == 3
    assert output == tmp_path / "session.html"
    html = output.read_text()
    parsed = Document()
    parsed.feed(html)
    assert "<script>" not in html
    assert "<img " not in html
    assert {tag for tag, _ in parsed.tags} <= {
        "html",
        "head",
        "meta",
        "title",
        "style",
        "body",
        "h1",
        "h3",
        "p",
        "code",
        "details",
        "summary",
        "pre",
    }
    assert all(not name.startswith("on") for _, attrs in parsed.tags for name in attrs)
    policy = next(
        attrs["content"]
        for tag, attrs in parsed.tags
        if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy"
    )
    assert "default-src 'none'" in policy
    assert "base-uri 'none'" in policy
    assert "form-action 'none'" in policy
    stored = [json.loads(text) for text in parsed.pres if text.startswith('{\n  "id"')]
    assert stored == records
    assert all(message["content"] in parsed.pres for message in messages)
    assert "not a live view" in html
    assert journal.read_bytes() == original
    assert journal.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_source_is_never_executed(tmp_path):
    marker = tmp_path / "executed"
    source = f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')"
    journal = database(tmp_path / "journal.sqlite", [{"kind": "source", "content": source}])
    output, count = exporter.export_session(journal)
    assert count == 1
    assert not marker.exists()
    assert source == json.loads(stored_event_json(output))["content"]


def stored_event_json(path):
    document = Document()
    document.feed(path.read_text())
    return document.pres[-1]


@pytest.mark.parametrize("destination", ["file", "symlink", "dangling_symlink", "journal"])
def test_existing_paths_are_never_overwritten(tmp_path, destination):
    journal = database(tmp_path / "journal.sqlite", [{"kind": "user", "content": "private"}])
    target = tmp_path / "target"
    output = tmp_path / "export.html"
    if destination == "file":
        output.write_text("KEEP EXISTING")
    elif destination in {"symlink", "dangling_symlink"}:
        if destination == "symlink":
            target.write_text("KEEP TARGET")
        output.symlink_to(target)
    else:
        output = journal
    original = journal.read_bytes()
    with pytest.raises(FileExistsError):
        exporter.export_session(journal, output)
    assert journal.read_bytes() == original
    if destination == "file":
        assert output.read_text() == "KEEP EXISTING"
    elif destination == "symlink":
        assert output.is_symlink()
        assert target.read_text() == "KEEP TARGET"
    elif destination == "dangling_symlink":
        assert output.is_symlink()
        assert not target.exists()


@pytest.mark.parametrize("payload", ["{bad JSON", "[]", '{"number": NaN}'])
def test_malformed_record_cleans_only_new_partial_output(tmp_path, payload):
    journal = database(tmp_path / "journal.sqlite", [{"kind": "user", "content": "first record"}])
    with sqlite3.connect(journal) as connection:
        connection.execute("INSERT INTO events VALUES (2, ?)", (payload,))
    unrelated = tmp_path / "keep.html"
    unrelated.write_text("KEEP")
    original = hashlib.sha256(journal.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        exporter.export_session(journal)
    assert not (tmp_path / "session.html").exists()
    assert unrelated.read_text() == "KEEP"
    assert hashlib.sha256(journal.read_bytes()).hexdigest() == original


def test_cleanup_does_not_unlink_a_replacement_destination(tmp_path, monkeypatch):
    journal = database(tmp_path / "journal.sqlite", [])
    output = tmp_path / "export.html"

    def replaced(_):
        yield {"kind": "user", "content": "first"}
        output.unlink()
        output.write_text("replacement must survive")
        raise ValueError("forced export failure")

    monkeypatch.setattr(exporter, "journal_events", replaced)
    with pytest.raises(ValueError, match="forced export failure"):
        exporter.export_session(journal, output)
    assert output.read_text() == "replacement must survive"


def test_missing_database_does_not_create_any_output(tmp_path):
    journal = tmp_path / "missing.sqlite"
    with pytest.raises(FileNotFoundError):
        exporter.export_session(journal)
    assert not journal.exists()
    assert not (tmp_path / "session.html").exists()


def test_cli_emits_file_uri_and_separate_privacy_warning(tmp_path):
    journal = database(tmp_path / "journal.sqlite", [{"kind": "user", "content": "user secret"}])
    output = tmp_path / "session export.html"
    result = subprocess.run([sys.executable, str(SCRIPT), str(journal), str(output)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == output.as_uri()
    assert "%20" in result.stdout
    assert "may contain secrets" in result.stderr
    assert "not a live view" in result.stderr
    assert "user secret" not in result.stdout + result.stderr
    existing = output.read_bytes()
    retry = subprocess.run([sys.executable, str(SCRIPT), str(journal), str(output)], text=True, capture_output=True)
    assert retry.returncode == 1
    assert retry.stdout == ""
    assert output.read_bytes() == existing


def test_cli_malformed_database_reports_failure_without_partial_html(tmp_path):
    journal = tmp_path / "not-sqlite"
    journal.write_text("not a database")
    result = subprocess.run([sys.executable, str(SCRIPT), str(journal)], text=True, capture_output=True)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "export_session:" in result.stderr
    assert not (tmp_path / "session.html").exists()
