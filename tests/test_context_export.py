"""Context HTML export is complete, private, and safe to open locally."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re

from py_agent import cli
from py_agent.context_export import render_context_html


def _embedded_payload(document: str) -> dict:
    match = re.search(
        r'<script id="snapshot" type="application/json">(.*?)</script>',
        document,
        re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def test_html_embeds_untrusted_context_as_data_not_markup():
    document = render_context_html({
        "current_context": {
            "epoch": 1,
            "messages": [
                {
                    "role": "system",
                    "content": "</script><img src=x onerror=alert(1)>",
                    "phase": None,
                }
            ],
        },
        "last_model_request": None,
        "runtime": {},
        "raw_context": {},
        "collapsed_archives": [],
        "export": {},
    })

    assert "</script><img" not in document
    assert _embedded_payload(document)["current_context"]["messages"][0]["content"].startswith("</script>")
    assert "textContent" in document


def test_context_command_writes_private_complete_explorer(tmp_path):
    coordinator = cli._build_coordinator("fake")
    coordinator.context_service.prepare_request("keep <this> exact", "request-1")
    target = tmp_path / "snapshot.html"

    response = asyncio.run(coordinator._dispatch_command(f'context save "{target}"', None))

    assert "Saved private context explorer" in response
    assert target.exists()
    assert os.stat(target).st_mode & 0o777 == 0o600
    payload = _embedded_payload(target.read_text(encoding="utf-8"))
    messages = payload["current_context"]["messages"]
    assert messages[0]["role"] == "system"
    assert "coding agent" in messages[0]["content"]
    assert messages[-1]["content"].endswith("keep <this> exact")
    assert payload["runtime"]["model_tools"]["registered"] == []
    assert payload["raw_context"]["groups"]

    refused = asyncio.run(coordinator._dispatch_command(f'context save "{target}"', None))
    assert "refused to overwrite" in refused


def test_context_command_usage_does_not_create_a_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    coordinator = cli._build_coordinator("fake")
    response = asyncio.run(coordinator._dispatch_command("context unknown", None))
    assert response == "Usage: /context save [PATH]"
    assert list(Path.cwd().iterdir()) == []
