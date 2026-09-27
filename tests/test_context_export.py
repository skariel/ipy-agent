"""Context HTML export is complete, private, and safe to open locally."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re

import pytest

from py_agent import cli
from py_agent.context_export import render_context_html
from py_agent.contracts import ExecutionResult, ExecutorCapabilities, ModelResponse, SayOutput
from py_agent.coordinator import State


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


@pytest.mark.asyncio
async def test_queued_context_save_and_direct_cell_complete_before_next_generation(tmp_path):
    first_started, release_first = asyncio.Event(), asyncio.Event()
    second_started, release_second = asyncio.Event(), asyncio.Event()
    target = tmp_path / "boundary.html"
    payload_at_direct = []

    class Provider:
        model = "offline/context-boundary"
        calls = 0

        async def generate(self, _request):
            self.calls += 1
            if self.calls == 1:
                first_started.set()
                await release_first.wait()
            else:
                second_started.set()
                await release_second.wait()
            return ModelResponse("print('committed boundary output')")

    class Executor:
        capabilities = ExecutorCapabilities(persistent=True, interrupt=True)

        def __init__(self):
            self.requests = []

        async def start(self):
            pass

        async def close(self):
            pass

        async def interrupt(self):
            pass

        async def store_collapsed(self, _text):
            return 1

        async def execute(self, request):
            self.requests.append(request)
            if request.author == "user":
                # The export must precede the shell action, not merely finish
                # sometime before the next generation.
                payload_at_direct.append(_embedded_payload(target.read_text(encoding="utf-8")))
                return ExecutionResult(request.origin, "success", stdout="/working/directory")
            final = sum(item.author == "agent" for item in self.requests) == 2
            return ExecutionResult(
                request.origin, "success", stdout="committed boundary output", final=final,
                say_outputs=(SayOutput("done", final=True),) if final else (),
            )

    coordinator = cli._build_coordinator("fake")
    executor = Executor()
    coordinator.provider, coordinator.executor = Provider(), executor
    await coordinator.start()
    active = asyncio.create_task(coordinator.submit("terminal", "ongoing task"))
    try:
        await asyncio.wait_for(first_started.wait(), 3)
        save = await coordinator.enqueue("terminal", f'/context save "{target}"')
        direct = await coordinator.enqueue("terminal", "!pwd")
        assert not target.exists()
        release_first.set()
        await asyncio.wait_for(second_started.wait(), 3)
        saved, executed = await asyncio.wait_for(asyncio.gather(save.completion, direct.completion), 3)
        assert saved.status == executed.status == "completed"
        assert saved.submission.action.origin == save.origin
        assert "Saved private context explorer" in saved.submission.message
        assert executed.submission.result.stdout == "/working/directory"
        assert os.stat(target).st_mode & 0o777 == 0o600
        assert len(payload_at_direct) == 1
        payload = payload_at_direct[0]
        assert any(
            message["role"] == "observation" and "committed boundary output" in message["content"]
            for message in payload["current_context"]["messages"]
        )
        assert payload["last_model_request"] is not None
        assert [request.author for request in executor.requests] == ["agent", "user"]
        assert not active.done()
        assert coordinator.state is State.GENERATING
        release_second.set()
        await asyncio.wait_for(active, 3)
    finally:
        release_first.set()
        release_second.set()
        await coordinator.close()


def test_context_command_usage_does_not_create_a_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    coordinator = cli._build_coordinator("fake")
    response = asyncio.run(coordinator._dispatch_command("context unknown", None))
    assert response == "Usage: /context save [PATH]"
    assert list(Path.cwd().iterdir()) == []
