"""Supervisor integration with literal, trusted test cells only.

The test-local launcher intentionally bypasses isolation to validate coordination,
NOT confinement. It is never used by production or with external model output.
Real Sandbox enforcement is covered separately by opt-in integration tests.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import json
import os
import re
from pathlib import Path
import signal
import sqlite3
import sys

import pytest

from py_agent.history import Journal
from py_agent.limits import Limits, LimitExceeded
from py_agent.provider import Completion, FakeProvider, ProviderError
from py_agent.protocol import encode_frame, decode_frame
from py_agent.supervisor import Supervisor

WORKER = Path(__file__).resolve().parents[1] / "src/py_agent/worker.py"

# Fixed test-only bootstrap, intentionally kept in the test launcher rather than
# adding a production path override. Only output's tempfile reference changes.
WORKER_BOOTSTRAP = r'''
import runpy, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
worker, artifacts = map(Path, sys.argv[1:])
sys.path.insert(0, str(worker.parents[1]))
import py_agent.output as output

def local_mkstemp(**kwargs):
    assert kwargs["dir"] == "/tmp"
    return tempfile.mkstemp(**(kwargs | {"dir": artifacts}))

output.tempfile = SimpleNamespace(mkstemp=local_mkstemp)
runpy.run_path(str(worker), run_name="__main__")
'''


class TrustedFixtureLauncher:
    """Fixed worker bootstrap for test literals; not an execution backend."""
    def __init__(self, directory):
        self.directory = directory
        self.process = None
        self.starts = 0

    async def start(self):
        self.starts += 1
        artifacts = self.directory / "output-artifacts"
        artifacts.mkdir(mode=0o700, exist_ok=True)
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-c", WORKER_BOOTSTRAP, str(WORKER), str(artifacts),
            cwd=self.directory,
            env={"HOME": str(self.directory), "IPYTHONDIR": str(self.directory / "profile"),
                 "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PAGER": "/bin/cat"},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
        return self.process

    async def close(self):
        if self.process is not None:
            if self.process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
            await self.process.wait()

    async def interrupt(self):
        await self.close()


class Observed:
    def __init__(self):
        self.events = []
        self.changed = asyncio.Event()

    def __call__(self, event):
        self.events.append(event)
        self.changed.set()

    async def until(self, predicate, timeout=6):
        try:
            async with asyncio.timeout(timeout):
                while not predicate():
                    self.changed.clear()
                    await self.changed.wait()
        except TimeoutError as exc:
            diagnostics = [(e["kind"], str(e["content"])[:4096]) for e in self.events
                           if e["kind"] in {"error", "launcher_stderr", "cell_uncertain", "state"}][-8:]
            raise AssertionError(f"Supervisor wait timed out; latest diagnostics: {diagnostics!r}") from exc

    def kind(self, kind):
        return [event for event in self.events if event["kind"] == kind]

    async def state(self, supervisor, state):
        await self.until(lambda: supervisor.state == state)


@asynccontextmanager
async def running(tmp_path, responses=(), *, provider=None, limits=None, context_window_tokens=None):
    launcher = TrustedFixtureLauncher(tmp_path)
    journal = Journal(tmp_path / "host" / "journal.sqlite", "test_run")
    observed = Observed()
    provider = provider or FakeProvider(responses)
    supervisor = Supervisor(provider, launcher, journal, limits=limits, on_event=observed,
                            context_window_tokens=context_window_tokens)
    await supervisor.start()
    try:
        yield supervisor, observed, provider, launcher
    finally:
        await supervisor.close()
        journal.close()


def all_events(supervisor, kind=None):
    events = [json.loads(row[0]) for row in supervisor.journal.db.execute("SELECT payload FROM events ORDER BY seq")]
    return events if kind is None else [e for e in events if e["kind"] == kind]


async def test_final_is_staged_until_success_and_errors_keep_partial_effects(tmp_path):
    responses = ["value = 41\nsay('not final', final=True)\nraise ValueError('later failure')",
                 "say(value + 1, final=True)"]
    async with running(tmp_path, responses) as (sup, seen, provider, _):
        sup.submit("Do the fixed test")
        await seen.state(sup, "DONE")
        assert [e["content"] for e in seen.kind("say")] == [42]
        assert len(all_events(sup, "say_staged")) == 2
        assert seen.kind("final_discarded")
        assert len(provider.requests) == 2
        statuses = [e["content"]["status"] for e in seen.kind("cell_end")]
        assert statuses == ["error", "success"]


async def test_question_prose_returns_format_error_then_automatically_retries(tmp_path):
    # Fixed trusted reproducer: IPython used to silently turn this into help?.
    async with running(tmp_path, ["Hi! How can I help?", "say('Hi!', final=True)"]) as (sup, seen, provider, _):
        sup.submit("hi")
        await seen.state(sup, "DONE")
        assert [e["content"]["status"] for e in seen.kind("cell_end")] == ["error", "success"]
        feedback = provider.requests[1]["messages"][-1]["content"]
        assert '"status":"error"' in feedback
        assert "SyntaxError" in feedback
        assert "Signature:" not in feedback
        assert [e["content"] for e in seen.kind("say")] == ["Hi!"]


async def test_codex_first_cell_executes_before_next_model_turn_not_its_later_claim(tmp_path, monkeypatch):
    # Fixed trusted cells through the real adapter and test-local worker. No live
    # model, credentials, or arbitrary recorded source is used in this test.
    import httpx
    import json
    from types import SimpleNamespace
    from py_agent.codex import CodexProvider
    from test_codex import message, terminal

    monkeypatch.setattr("py_agent.codex.read_codex_credentials",
                        lambda _: SimpleNamespace(access="mock-token", account_id="mock-account"))
    responses = [
        terminal(output=[message("value = 42\nprint('verified', value)", phase="commentary"),
                         message("say('invented missing results', final=True)", phase="final_answer")]),
        terminal(output=[message("say(value, final=True)", phase="final_answer")]),
    ]
    requests = []
    def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses[len(requests) - 1])
    provider = CodexProvider("openai-codex/test", transport=httpx.MockTransport(transport))
    async with running(tmp_path, provider=provider) as (sup, seen, _, launcher):
        sup.submit("inspect the fixed value and report it")
        await seen.state(sup, "DONE")
        assert [e["content"] for e in seen.kind("say")] == [42]
        assert len(requests) == len(seen.kind("source")) == 2
        assert launcher.starts == 1
        assistant = next(m for m in requests[1]["input"] if m["role"] == "assistant")
        assert assistant["phase"] == "commentary"
        assert "invented" not in assistant["content"][0]["text"]
        observation = requests[1]["input"][-1]["content"][0]["text"]
        assert "verified 42" in observation
        assert '"status":"success"' in observation
        assert '"truncated":false' in observation


async def test_nonfinal_cell_automatically_returns_stdout_stderr_and_display_to_model(tmp_path):
    responses = ["import sys\nvalue = 42\nsay('Investigating')\nprint('stdout evidence')\nprint('stderr evidence', file=sys.stderr)\nvalue",
                 "say(value, final=True)"]
    async with running(tmp_path, responses) as (sup, seen, provider, launcher):
        sup.submit("investigate")
        await seen.state(sup, "DONE")
        assert len(provider.requests) == 2
        observations = "\n".join(m["content"] for m in provider.requests[1]["messages"]
                                 if m["content"].startswith("[RUNTIME OBSERVATION"))
        assert "stdout evidence" in observations
        assert "stderr evidence" in observations
        assert "42" in observations
        assert '"status":"success"' in observations
        assert [e["content"] for e in seen.kind("say")] == ["Investigating", 42]
        assert launcher.starts == 1


async def test_wait_yields_without_execution_after_it_and_reuses_kernel(tmp_path):
    async with running(tmp_path, ["value = 9\nsay('question')\nwait()\nvalue = 100",
                                  "say(value, final=True)"]) as (sup, seen, provider, launcher):
        sup.submit("begin")
        await seen.until(lambda: bool(seen.kind("cell_end")) and sup.state == "IDLE")
        assert len(provider.requests) == 1
        sup.submit("continue")
        await seen.state(sup, "DONE")
        assert [e["content"] for e in seen.kind("say")] == ["question", 9]
        assert launcher.starts == 1


async def test_wait_and_final_conflict_is_not_completion(tmp_path):
    async with running(tmp_path, ["say('bad final', final=True)\nwait()", "say('fixed', final=True)"]) as (sup, seen, _, _):
        sup.submit("begin")
        await seen.state(sup, "DONE")
        assert seen.kind("cell_end")[0]["content"]["status"] == "invalid_control"
        assert [e["content"] for e in seen.kind("say")] == ["fixed"]


@pytest.mark.parametrize("completion", [Completion("effect = 100", finish_reason="length"),
                                        Completion("effect = 100", rejection_reason="refusal"),
                                        Completion("")])
async def test_failed_completions_never_dispatch_partial_source(tmp_path, completion):
    async with running(tmp_path, [completion, "say('effect' in globals(), final=True)"]) as (sup, seen, provider, _):
        sup.submit("begin")
        await seen.state(sup, "DONE")
        assert [e["content"] for e in seen.kind("say")] == [False]
        assert len(seen.kind("source")) == 1
        assert seen.kind("retry")
        feedback = provider.requests[1]["messages"][-1]["content"]
        assert '"executed":false' in feedback
        assert "Correct the reported error" in feedback
        assert (completion.rejection_reason or completion.finish_reason) in feedback


async def test_large_valid_cell_executes_then_context_compacts_without_extra_model_turn(tmp_path):
    large = "# " + "x" * 22000 + "\neffect = 999"
    async with running(tmp_path, [large, "say(effect, final=True)"]) as (sup, seen, provider, launcher):
        uid = sup.submit("preserve this active task: report the computed effect")
        await seen.state(sup, "DONE")
        assert [e["content"] for e in seen.kind("say")] == [999]
        assert [e["content"] for e in seen.kind("source")] == [large, "say(effect, final=True)"]
        assert len(provider.requests) == 2 and launcher.starts == 1
        assert sup.context.epoch == 2
        messages = provider.requests[1]["messages"]
        assert not any(m["role"] == "user" and m["content"] == "preserve this active task: report the computed effect" for m in messages)
        assert len(messages) == 1  # whole dispatched conversation is cleared
        assert not any(m["role"] == "assistant" and m["content"] == large for m in messages)
        commit = all_events(sup, "epoch_commit")[-1]
        assert uid in commit["content"]["evicted"]
        sup.context.check(messages)
        assert not seen.kind("retry") and not seen.kind("error")
        assert not seen.kind("checkpoint")
        assert not any(e["content"].get("checkpoint") for e in seen.kind("generation_request"))


async def test_retry_bound_pauses_without_execution(tmp_path):
    bad = Completion("effect = 100", finish_reason="length")
    async with running(tmp_path, [bad] * 5, limits=Limits(generation_retries=1)) as (sup, seen, provider, _):
        uid = sup.submit("begin")
        await seen.state(sup, "FAILED")
        assert len(provider.requests) == 2
        assert "Last rejection: length" in seen.kind("error")[-1]["content"]
        assert not seen.kind("source")
        assert not seen.kind("user_accepted")
        assert [e["id"] for e in sup.pending] == [uid]


async def test_generation_steering_cancels_then_includes_input_once_in_order(tmp_path):
    gate = asyncio.Event()
    provider = FakeProvider(["effect = 100", "say('effect' in globals(), final=True)"], gate=gate)
    async with running(tmp_path, provider=provider) as (sup, seen, _, _):
        first = sup.submit("first exact input")
        await provider.started.wait()
        second = sup.submit("second exact input")
        await seen.until(lambda: bool(seen.kind("generation_cancelled")))
        assert not seen.kind("user_accepted")
        gate.set()
        await seen.state(sup, "DONE")
        assert provider.cancelled == 1
        assert [e["content"] for e in seen.kind("say")] == [False]
        messages = provider.requests[-1]["messages"]
        assert [m["content"] for m in messages if m["content"] in {"first exact input", "second exact input"}] == ["first exact input", "second exact input"]
        assert [e["content"] for e in seen.kind("user_accepted")] == [first, second]
        assert seen.kind("generation_cancelled")[0]["content"]["usage"] == "unknown"


class CancellationResistantProvider(FakeProvider):
    async def generate(self, messages, *, max_tokens):
        if not self.requests:
            self.requests.append({"messages": [m.copy() for m in messages], "max_tokens": max_tokens, "model": self.model})
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return Completion("say('stale should not run', final=True)", usage={"reported": 7, "normalized": {"input_tokens": 24000}})
        return await super().generate(messages, max_tokens=max_tokens)


async def test_cancellation_resistant_stale_completion_is_discarded(tmp_path):
    provider = CancellationResistantProvider(["say('fresh', final=True)"])
    async with running(tmp_path, provider=provider) as (sup, seen, _, _):
        sup.submit("first")
        await provider.started.wait()
        sup.submit("steer")
        await seen.state(sup, "DONE")
        assert [e["content"] for e in seen.kind("say")] == ["fresh"]
        assert len(seen.kind("source")) == 1
        discarded = seen.kind("generation_response")[0]
        assert discarded["stale"] and discarded["content"]["usage"]["normalized"]["input_tokens"] == 24000
        assert sup.context.epoch == 1
        assert len(all_events(sup, "epoch_commit")) == 1


@pytest.mark.parametrize("control", ["wait()", "say('first done', final=True)"])
async def test_execution_steering_survives_wait_final_boundary(tmp_path, control):
    source = ("from pathlib import Path\nimport time\nvalue = 8\nsay('running')\n"
              "while not Path('release').exists():\n    time.sleep(0.005)\n" + control)
    async with running(tmp_path, [source, "say(value + 1, final=True)"]) as (sup, seen, provider, _):
        sup.submit("first")
        await seen.until(lambda: any(e["content"] == "running" for e in seen.kind("say")))
        second = sup.submit("steering during execution")
        assert len(provider.requests) == 1
        (tmp_path / "release").touch()
        await seen.state(sup, "DONE")
        assert len(provider.requests) == 2
        assert seen.kind("say")[-1]["content"] == 9
        assert second in [e["content"] for e in seen.kind("user_accepted")]
        assert "steering during execution" in [m["content"] for m in provider.requests[-1]["messages"]]


async def test_provider_retry_exhaustion_keeps_underlying_error_visible(tmp_path):
    async with running(tmp_path, [ProviderError("endpoint returned HTTP 503")],
                       limits=Limits(generation_retries=0)) as (sup, seen, _, _):
        sup.submit("begin")
        await seen.state(sup, "FAILED")
        assert "HTTP 503" in seen.kind("error")[-1]["content"]


@pytest.mark.parametrize("kind", ["response_format", "response_error"])
async def test_provider_wire_or_json_error_is_not_blindly_retried(tmp_path, kind):
    provider = FakeProvider([ProviderError("model is not supported", kind=kind)])
    async with running(tmp_path, provider=provider) as (sup, seen, _, _):
        sup.submit("begin")
        await seen.state(sup, "FAILED")
        assert len(provider.requests) == 1
        assert "model is not supported" in seen.kind("error")[-1]["content"]
        assert not seen.kind("source")


async def test_authentication_failure_pauses_without_blind_retries(tmp_path):
    provider = FakeProvider([ProviderError("Refresh through pi: /login openai-codex", kind="authentication")])
    async with running(tmp_path, provider=provider) as (sup, seen, _, _):
        sup.submit("begin")
        await seen.state(sup, "FAILED")
        assert len(provider.requests) == 1
        assert not seen.kind("source")
        assert "/login openai-codex" in seen.kind("error")[-1]["content"]


async def test_codex_serialized_request_is_journaled_without_auth_reads(tmp_path):
    from py_agent.codex import CodexProvider
    adapter = CodexProvider("openai-codex/test-model")
    provider = FakeProvider(["say('done', final=True)"], model=adapter.model)
    provider.request_details = adapter.request_details
    async with running(tmp_path, provider=provider) as (sup, seen, _, _):
        sup.submit("begin")
        await seen.state(sup, "DONE")
        request = seen.kind("generation_request")[0]["content"]
        assert request["provider_request"]["body"] == adapter.build_request(provider.requests[0]["messages"], max_tokens=sup.limits.output_tokens)
        assert request["provider_request"]["output_limit_enforcement"] == "local_only"
        assert "Authorization" not in json.dumps(request)


async def test_every_journaled_request_matches_provider_submission(tmp_path):
    async with running(tmp_path, ["print(3)", "say('done', final=True)"]) as (sup, seen, provider, _):
        sup.submit("begin")
        await seen.state(sup, "DONE")
        for event, request in zip(seen.kind("generation_request"), provider.requests, strict=True):
            for key in ("messages", "max_tokens", "model"):
                assert event["content"][key] == request[key]
        first, second = [r["messages"] for r in provider.requests]
        assert second[:len(first)] == first


async def test_live_memories_variables_and_functions_survive_reset_without_injection_or_replay(tmp_path):
    first = ("assert memories == []\nmemories.extend(['short-term finding', lambda: 17])\n"
             "notes_id = id(memories)\nvalue = 17\ndef compute():\n    return value + 1\n"
             "print('evidence-marker')\nwait()")
    final = ("assert id(memories) == notes_id\nassert memories[0] == 'short-term finding'\n"
             "assert memories[1]() == value == 17\nassert compute() == 18\n"
             "say(history.search('evidence-marker', kind='output', limit=1), final=True)")
    async with running(tmp_path, [first, "wait()", final]) as (sup, seen, provider, launcher):
        sup.submit("begin")
        await seen.until(lambda: len(seen.kind("cell_end")) == 1 and sup.state == "IDLE")
        assert not (tmp_path / "memory.md").exists()
        assert not hasattr(sup, "memory") and not hasattr(sup.context, "snapshot")
        initial_prompt = provider.requests[0]["messages"][0]["content"]
        assert "This context started with 0 memories" in initial_prompt
        assert sup._memories_count == 2
        assert sup.context.contract == initial_prompt
        sup.submit("one turn without resetting")
        await seen.until(lambda: len(seen.kind("cell_end")) == 2 and sup.state == "IDLE")
        sup.request_reset()
        await asyncio.sleep(0)
        assert len(provider.requests) == 2  # reset never runs an inspection/save cell
        sup.submit("continue after reset")
        await seen.state(sup, "DONE")
        assert sup.context.epoch == 2 and launcher.starts == 1
        assert provider.requests[1]["messages"][0]["content"] == initial_prompt
        assert "This context started with 2 memories" in provider.requests[2]["messages"][0]["content"]
        assert len(provider.requests) == len(seen.kind("source")) == 3
        assert "short-term finding" not in json.dumps(provider.requests[-1]["messages"])
        assert not any(m["role"] == "assistant" and "value = 17" in m["content"]
                       for m in provider.requests[-1]["messages"])
        assert seen.kind("say")[-1]["content"][0]["kind"] == "output"
        assert len([e for e in seen.kind("source") if e["content"] == first]) == 1
        assert not seen.kind("checkpoint") and not seen.kind("memory_draft")
        commits = all_events(sup, "epoch_commit")
        assert len(commits) == 2 and commits[-1]["content"]["evicted"]
        assert commits[-1]["starting_memories_count"] == 2
        assert commits[-1]["system_prompt"] == sup.context.contract
        assert all("snapshot" not in event["content"] for event in commits)
        assert not (tmp_path / "memory.md").exists()


async def test_automatic_compaction_preserves_live_memories_without_inserting_them(tmp_path):
    first = "# " + "x" * 22000 + "\nmemories.append('kernel-only note')\nvalue = 17"
    final = "say({'note': memories[0], 'value': value}, final=True)"
    async with running(tmp_path, [first, final]) as (sup, seen, provider, launcher):
        uid = sup.submit("perform the fixed task")
        await seen.state(sup, "DONE")
        assert seen.kind("say")[-1]["content"] == {"note": "kernel-only note", "value": 17}
        assert sup.context.epoch == 2 and launcher.starts == 1
        assert len(provider.requests) == 2 and len(seen.kind("source")) == 2
        assert "kernel-only note" not in json.dumps(provider.requests[1]["messages"])
        assert len(provider.requests[1]["messages"]) == 1
        assert "This context started with 1 memories" in provider.requests[1]["messages"][0]["content"]
        commit = all_events(sup, "epoch_commit")[-1]
        assert uid in commit["content"]["evicted"]
        assert commit["content"]["retained"] == []
        assert seen.kind("source")[0]["id"] in commit["content"]["evicted"]
        assert "snapshot" not in commit["content"]
        assert not seen.kind("memory_draft") and not seen.kind("checkpoint")


async def test_steering_cancels_normal_generation_after_synchronous_reset(tmp_path):
    provider = FakeProvider(["wait()", "say('stale answer', final=True)", "say('done', final=True)"])
    async with running(tmp_path, provider=provider) as (sup, seen, _, _):
        sup.submit("original active goal")
        await seen.until(lambda: len(seen.kind("cell_end")) == 1 and sup.state == "IDLE")
        provider.gate = asyncio.Event()
        provider.started.clear()
        sup.request_reset()
        sup.submit("continue")
        await provider.started.wait()
        assert sup.context.epoch == 2  # reset itself never awaited a model turn
        uid = sup.submit("new steering after reset")
        await seen.until(lambda: bool(seen.kind("generation_cancelled")))
        provider.gate.set()
        await seen.state(sup, "DONE")
        assert sup.context.epoch == 2
        assert not any(e["content"] == "stale answer" for e in seen.kind("say"))
        assert uid in [e["content"] for e in seen.kind("user_accepted")]
        final_input = [m["content"] for m in provider.requests[-1]["messages"] if m["role"] == "user"]
        assert "original active goal" not in final_input
        assert "continue" in final_input and "new steering after reset" in final_input
        assert len(all_events(sup, "epoch_commit")) == 2
        assert len(seen.kind("source")) == 2
        assert not seen.kind("checkpoint")


@pytest.mark.parametrize("file_kind", ["missing", "text", "oversize", "invalid_utf8", "symlink", "fifo"])
async def test_memory_md_is_ordinary_file_never_observed_or_injected(tmp_path, file_kind):
    path = tmp_path / "memory.md"
    if file_kind in {"text", "oversize", "invalid_utf8"}:
        path.write_bytes({"text": b"file-only instruction", "oversize": b"x" * 32769,
                          "invalid_utf8": b"\xff"}[file_kind])
    elif file_kind == "symlink":
        target = tmp_path / "ordinary-file"
        target.write_text("file-only instruction")
        path.symlink_to(target)
    elif file_kind == "fifo":
        os.mkfifo(path)
    original = path.lstat() if file_kind != "missing" else None
    async with running(tmp_path, ["value = 17\nwait()", "say(value, final=True)"]) as (sup, seen, provider, launcher):
        sup.submit("begin")
        await seen.until(lambda: len(seen.kind("cell_end")) == 1 and sup.state == "IDLE")
        sup.request_reset()
        sup.submit("continue")
        await seen.state(sup, "DONE")
        assert seen.kind("say")[-1]["content"] == 17
        assert sup.context.epoch == 2 and launcher.starts == 1
        assert len(provider.requests) == len(seen.kind("source")) == 2
        assert not any(event["kind"].startswith("memory_") for event in seen.events)
        assert "file-only instruction" not in json.dumps(provider.requests)
        assert not seen.kind("checkpoint") and not seen.kind("error")
        if original is None:
            assert not path.exists()
        else:
            current = path.lstat()
            assert (current.st_ino, current.st_size, current.st_mtime_ns) == (original.st_ino, original.st_size, original.st_mtime_ns)


async def test_new_kernel_has_fresh_memories_and_no_variable_restore(tmp_path):
    for index, source in enumerate(("memories.append('old session')\nold_value = 17\nsay('done', final=True)",
                                   "say([memories, 'old_value' in globals()], final=True)")):
        directory = tmp_path / str(index)
        directory.mkdir()
        async with running(directory, [source]) as (sup, seen, provider, _):
            sup.submit("run the fixed fixture")
            await seen.state(sup, "DONE")
            assert len(provider.requests) == 1
            if index:
                assert seen.kind("say")[-1]["content"] == [[], False]
            assert not (directory / "memory.md").exists()


@pytest.mark.parametrize("window", [None, 272000])
async def test_context_window_status_matches_effective_eviction_capacity(tmp_path, window):
    async with running(tmp_path, context_window_tokens=window) as (sup, _, _, _):
        expected = Limits().input_tokens if window is None else window
        assert sup.status()["context_window_tokens"] == sup.context.window_tokens == expected
        assert sup.limits.input_tokens == Limits().input_tokens
        assert sup.context.messages() == [{"role": "system", "content": sup.context.contract}]



async def test_reported_low_tokens_keep_large_byte_context_append_only(tmp_path):
    source = "# " + "x" * 26000 + "\nvalue = 17"
    responses = [Completion(source, usage={"normalized": {"input_tokens": 1000}}),
                 "say(value, final=True)"]
    async with running(tmp_path, responses) as (sup, seen, provider, _):
        sup.submit("keep the entire conversation")
        await seen.state(sup, "DONE")
        assert sup.context.epoch == 1 and len(all_events(sup, "epoch_commit")) == 1
        first, second = [request["messages"] for request in provider.requests]
        assert second[:len(first)] == first
        assert any(message["content"] == source for message in second)
        assert sup.context.estimate(second) > sup.context.window_tokens
        assert sup.context.reported_input_tokens == 1000  # later missing usage doesn't erase it
        assert not sup.context.needs_reset()
        assert seen.kind("say")[-1]["content"] == 17


async def test_near_capacity_reported_usage_clears_entire_dispatched_context(tmp_path):
    responses = [Completion("value = 17", usage={"normalized": {"input_tokens": 22799}}),
                 Completion("value += 1", usage={"normalized": {"input_tokens": 22800}}),
                 Completion("say(value, final=True)", usage={"normalized": {"input_tokens": 1100}})]
    async with running(tmp_path, responses) as (sup, seen, provider, launcher):
        uid = sup.submit("calculate this unfinished task")
        await seen.state(sup, "DONE")
        assert sup.context.epoch == 2 and launcher.starts == 1
        first, second, third = [request["messages"] for request in provider.requests]
        assert second[:len(first)] == first  # 94.995% must not trigger early eviction
        assert len(third) == 1 and third[0]["role"] == "system"
        assert "calculate this unfinished task" not in json.dumps(third)
        commit = all_events(sup, "epoch_commit")[-1]
        assert uid in commit["content"]["evicted"] and commit["content"]["retained"] == []
        assert seen.kind("say")[-1]["content"] == 18
        assert sup.status()["context_input_tokens"] == 1100
        assert not seen.kind("checkpoint") and len(seen.kind("source")) == 3


async def test_late_orphan_usage_cannot_change_new_epoch_capacity(tmp_path):
    responses = [Completion("wait()", usage={"normalized": {"input_tokens": 1000}}),
                 Completion("wait()", usage={"normalized": {"input_tokens": 2000}}),
                 "say('done', final=True)"]
    async with running(tmp_path, responses) as (sup, seen, provider, _):
        sup.submit("start")
        await seen.until(lambda: len(seen.kind("cell_end")) == 1 and sup.state == "IDLE")
        sup.request_reset()
        sup.submit("new epoch")
        await seen.until(lambda: len(seen.kind("cell_end")) == 2 and sup.state == "IDLE")
        assert sup.context.epoch == 2 and sup.context.reported_input_tokens == 2000
        orphan = asyncio.get_running_loop().create_future()
        orphan.set_result(Completion("raise AssertionError('must not run')",
                                     usage={"normalized": {"input_tokens": 24000}}))
        sup._late_generation(orphan, "a1:g-old-orphan")
        assert sup.usage[-1]["normalized"]["input_tokens"] == 24000  # retained for audit only
        assert sup.status()["context_input_tokens"] == 2000
        assert not sup.context.needs_reset()
        sup.submit("continue")
        await seen.state(sup, "DONE")
        assert sup.context.epoch == 2 and len(all_events(sup, "epoch_commit")) == 2
        assert len(seen.kind("source")) == len(provider.requests) == 3


async def test_missing_or_unavailable_memories_count_is_unknown_at_next_reset(tmp_path):
    async with running(tmp_path, ["memories = 17\nwait()", "say('done', final=True)"]) as (sup, seen, provider, _):
        sup.submit("rebind the optional variable")
        await seen.until(lambda: len(seen.kind("cell_end")) == 1 and sup.state == "IDLE")
        assert sup._memories_count is None
        assert "started with 0 memories" in sup.context.contract
        sup.request_reset()
        sup.submit("continue")
        await seen.state(sup, "DONE")
        assert "started with an unknown number of memories" in provider.requests[-1]["messages"][0]["content"]


async def test_ordinary_cell_error_after_reset_uses_normal_correction_loop(tmp_path):
    async with running(tmp_path, ["wait()", "raise ValueError('ordinary cell failed')", "say('corrected', final=True)"]) as (sup, seen, provider, _):
        sup.submit("begin")
        await seen.until(lambda: len(seen.kind("cell_end")) == 1 and sup.state == "IDLE")
        sup.request_reset()
        sup.submit("continue")
        await seen.state(sup, "DONE")
        assert sup.context.epoch == 2
        assert len(all_events(sup, "epoch_commit")) == 2
        assert len(provider.requests) == 3
        assert [e["content"]["status"] for e in seen.kind("cell_end")] == ["wait", "error", "success"]
        assert [e["content"] for e in seen.kind("say")] == ["corrected"]
        assert not seen.kind("checkpoint")


async def test_reset_clears_completed_and_unfinished_dispatched_instructions(tmp_path):
    async with running(tmp_path, ["say('first done', final=True)", "value = 17\nwait()", "say(value, final=True)"]) as (sup, seen, provider, _):
        old_id = sup.submit("old completed instruction")
        await seen.state(sup, "DONE")
        active_id = sup.submit("new unfinished task")
        await seen.until(lambda: len(seen.kind("cell_end")) == 2 and sup.state == "IDLE")
        sup.request_reset()
        new_id = sup.submit("finish the new task")
        await seen.state(sup, "DONE")
        user_messages = [m["content"] for m in provider.requests[-1]["messages"] if m["role"] == "user"]
        assert user_messages == ["finish the new task"]
        commit = all_events(sup, "epoch_commit")[-1]
        assert {old_id, active_id} <= set(commit["content"]["evicted"])
        assert commit["content"]["retained"] == [new_id]
        assert not hasattr(sup, "_active_inputs")


async def test_steering_during_execution_is_carried_verbatim_into_next_context_reset(tmp_path):
    active_cell = ("from pathlib import Path\nimport time\nsay('ordinary cell running')\n"
                   "while not Path('release').exists():\n    time.sleep(0.005)\n")
    async with running(tmp_path, [active_cell, "say('done', final=True)"]) as (sup, seen, provider, _):
        active_id = sup.submit("original task")
        await seen.until(lambda: any(e["content"] == "ordinary cell running" for e in seen.kind("say")))
        sup.request_reset()
        uid = sup.submit("new exact instruction during execution")
        (tmp_path / "release").touch()
        await seen.state(sup, "DONE")
        commit = all_events(sup, "epoch_commit")[-1]
        assert uid in commit["content"]["pending"]
        assert commit["content"]["retained"] == [uid]
        assert active_id in commit["content"]["evicted"]
        assert "new exact instruction during execution" in [m["content"] for m in provider.requests[-1]["messages"]]
        assert len(provider.requests) == len(seen.kind("source")) == 2
        assert not seen.kind("checkpoint")


async def test_history_broker_limits_pages_and_keeps_retrieval_provenance(tmp_path):
    async with running(tmp_path, ["say(history.read(history.search('needle', kind='user')[0]['id'], limit=999999), final=True)"],
                       limits=Limits(input_tokens=50000)) as (sup, seen, _, _):
        sup.submit("needle " + "a" * 9000)
        await seen.state(sup, "DONE")
        page = seen.kind("say")[-1]["content"]
        assert len(page["content"]) == 8000 and page["truncated"]
        assert all_events(sup, "retrieval")
        assert not any(e["kind"] == "retrieval" for e in sup.journal.search("needle"))


@pytest.mark.parametrize("source,limits", [
    ("while True: pass", Limits(cell_seconds=0.05)),
    ("say('x' * 3000)", Limits(max_output_bytes=2048)),
    ("import os\nos._exit(7)", Limits()),
])
async def test_timeout_output_flood_crash_stop_without_replay(tmp_path, source, limits):
    async with running(tmp_path, [source, "say('must not run', final=True)"], limits=limits) as (sup, seen, provider, launcher):
        sup.submit("begin")
        await seen.state(sup, "FAILED")
        assert len(provider.requests) == 1
        assert len(seen.kind("source")) == 1
        await seen.until(lambda: bool(seen.kind("cell_uncertain")))
        await launcher.process.wait()
        assert launcher.process.returncode is not None
        if limits.cell_seconds == 0.05:
            errors = [event["content"] for event in seen.kind("error")]
            assert len(errors) == 1
            assert "Cell deadline exceeded (0.05s)" in errors[0]
            assert "--cell-seconds" in errors[0]
        with pytest.raises(RuntimeError, match="No live kernel"):
            sup.submit("new work")


async def test_oversized_output_reaches_model_as_file_notice_and_kernel_continues(tmp_path):
    class ReadArtifact(FakeProvider):
        artifact = None

        async def generate(self, messages, *, max_tokens):
            if len(self.requests) == 1:
                observation = messages[-1]["content"]
                assert "size=2000000 chars, lines=400000" in observation
                assert "Output too long to display here" in observation
                match = re.search(r"Saved to (/[^\n]*?/py-output-[\w-]+\.txt)", observation)
                assert match is not None
                self.artifact = Path(match[1])
                assert self.artifact.parent == tmp_path / "output-artifacts"
                # Fixed trusted fixture, with only its generated filename inserted.
                self.responses.append(
                    f"with open({str(self.artifact)!r}, encoding='utf-8') as saved:\n"
                    "    chunk = saved.read(4000)\n"
                    "print(chunk, end='')\n"
                    "say({'runs': runs, 'read_chars': len(chunk)}, final=True)"
                )
            return await super().generate(messages, max_tokens=max_tokens)

    provider = ReadArtifact(["runs = globals().get('runs', 0) + 1\nprint('line\\n' * 400000, end='')"])
    async with running(tmp_path, provider=provider, limits=Limits(max_output_bytes=8192)) as (sup, seen, _, _):
        sup.submit("produce a large listing and read a small chunk")
        await seen.state(sup, "DONE")
        assert len(provider.requests) == 2
        assert len(seen.kind("source")) == 2
        assert [event["content"] for event in seen.kind("say")] == [{"runs": 1, "read_chars": 4000}]
        assert not seen.kind("error") and not seen.kind("cell_uncertain")
        assert provider.artifact.stat().st_size == 2000000
        assert "line\n" * 800 == "".join(event["content"] for event in seen.kind("output")
                                          if event["cell_id"] == "a1:c000002")
        assert sum(len(event["content"]) for event in seen.kind("output")) < 6000
    # pytest owns the entire artifact directory; no notice-driven unlinking.


@pytest.mark.parametrize("character", ["x", "🐍"])
@pytest.mark.parametrize("count", [7999, 8000, 8001])
async def test_output_character_boundary_survives_transport_and_next_cell(tmp_path, character, count):
    async with running(tmp_path, [f"kept = 42\nprint({character!r} * {count}, end='')",
                                  "say(kept, final=True)"]) as (sup, seen, provider, _):
        sup.submit("exercise the output character boundary")
        await seen.state(sup, "DONE")
        outputs = "".join(event["content"] for event in seen.kind("output"))
        artifacts = {Path(name) for name in re.findall(r"[Ss]aved to (/[^\n]*?/py-output-[\w-]+\.txt)", outputs)}
        assert all(path.parent == tmp_path / "output-artifacts" for path in artifacts)
        if count <= 8000:
            assert outputs == character * count
            assert not artifacts
        else:
            assert "size=8001 chars, lines=1" in outputs
            assert "Output too long to display here" in provider.requests[1]["messages"][-1]["content"]
            assert any(path.read_text(encoding="utf-8") == character * count
                       for path in artifacts if path.exists())
        assert seen.kind("say")[-1]["content"] == 42
        assert len(provider.requests) == 2
        assert not seen.kind("error") and not seen.kind("cell_uncertain")


async def test_generation_interrupt_stops_without_replay_and_can_accept_steering(tmp_path):
    gate = asyncio.Event()
    provider = FakeProvider(["say('cancelled', final=True)", "say('new', final=True)"], gate=gate)
    async with running(tmp_path, provider=provider) as (sup, seen, _, _):
        sup.submit("begin")
        await provider.started.wait()
        await sup.interrupt()
        assert sup.state == "INTERRUPTED"
        assert not seen.kind("source")
        gate.set()
        sup.submit("steer after interrupt")
        await seen.state(sup, "DONE")
        assert [e["content"] for e in seen.kind("say")] == ["new"]


async def test_execution_interrupt_marks_kernel_lost_and_preserves_no_replay(tmp_path):
    async with running(tmp_path, ["say('running')\nwhile True: pass"]) as (sup, seen, _, launcher):
        sup.submit("begin")
        await seen.until(lambda: bool(seen.kind("say")))
        await sup.interrupt()
        assert sup.state == "INTERRUPTED"
        assert len(seen.kind("source")) == 1
        assert seen.kind("cell_uncertain")
        assert launcher.process.returncode is not None


async def test_oversize_input_rejected_before_journal_acceptance(tmp_path):
    async with running(tmp_path, limits=Limits(max_user_bytes=8)) as (sup, seen, provider, _):
        with pytest.raises(LimitExceeded):
            sup.submit("x" * 9)
        assert not seen.kind("user")
        assert not provider.requests


async def test_request_budget_is_host_enforced(tmp_path):
    async with running(tmp_path, ["print('one')", "say('not allowed', final=True)"],
                       limits=Limits(max_requests=1)) as (sup, seen, provider, _):
        sup.submit("begin")
        await seen.state(sup, "FAILED")
        assert len(provider.requests) == 1
        assert len(seen.kind("source")) == 1


class ScriptedProcess:
    """In-memory malicious-peer fixture; does not execute any source."""
    def __init__(self, responder):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = self
        self.responder = responder
        self.closed = False
        self.stdout.feed_data(encode_frame({"v": 1, "type": "ready", "kernel_pid": 123}))

    def write(self, data):
        command = decode_frame(data)
        if command["type"] == "execute":
            for frame in self.responder(command):
                self.stdout.feed_data(encode_frame(frame))

    async def drain(self):
        await asyncio.sleep(0)

    async def start(self):
        return self

    async def close(self):
        if not self.closed:
            self.closed = True
            self.stdout.feed_eof()
            self.stderr.feed_eof()

    async def interrupt(self):
        await self.close()


@pytest.mark.parametrize("responder", [
    lambda cmd: [{"v": 1, "type": "output", "cell_id": "a1:c999999", "stream": "stdout", "text": "forged"}],
    lambda cmd: [{"v": 1, "type": "ready", "kernel_pid": 456}],
    lambda cmd: [{"v": 1, "type": "cell_end", "cell_id": cmd["cell_id"], "status": "success", "execution_count": 1}] * 2,
])
async def test_untrusted_unknown_cells_unsolicited_ready_duplicate_end_stop_peer(tmp_path, responder):
    peer = ScriptedProcess(responder)
    journal = Journal(tmp_path / "journal.sqlite", "frames")
    seen = Observed()
    sup = Supervisor(FakeProvider(["pass"]), peer, journal, on_event=seen)
    try:
        await sup.start()
        sup.submit("begin")
        await seen.until(lambda: sup._kernel_dead)
        await seen.until(lambda: peer.closed)
        assert peer.closed
        assert not [e for e in seen.kind("output") if e["content"] == "forged"]
    finally:
        await sup.close()
        journal.close()


async def test_nonempty_journal_is_not_a_kernel_resume(tmp_path):
    peer = ScriptedProcess(lambda cmd: pytest.fail("must not dispatch"))
    provider = FakeProvider(["pass"])
    with Journal(tmp_path / "old.sqlite", "old-run") as journal:
        prior = journal.append("source", "memories.append('old historical note')")
        sup = Supervisor(provider, peer, journal)
        try:
            with pytest.raises(RuntimeError, match="Reopening runs is not implemented"):
                await sup.start()
            assert sup.process is None and not provider.requests
            assert journal.read(prior["id"])["content"]
        finally:
            await sup.close()


# Regression specifications: these should pass once the cited host defects are
# fixed. They are deliberately not xfailed so safety regressions remain visible.
async def test_failed_steered_generation_pauses_without_consuming_extra_retry(tmp_path):
    gate = asyncio.Event()
    provider = FakeProvider(["pass", Completion("partial = 1", finish_reason="length"),
                             "say('unexpected retry', final=True)"], gate=gate)
    async with running(tmp_path, provider=provider, limits=Limits(generation_retries=0)) as (sup, seen, _, _):
        sup.submit("begin")
        await provider.started.wait()
        sup.submit("steer")
        await seen.until(lambda: bool(seen.kind("generation_cancelled")))
        gate.set()
        await seen.until(lambda: sup.state in {"FAILED", "DONE"})
        assert len(provider.requests) == 2, "Stale wakeup bypassed configured generation retry bound"
        assert not seen.kind("source")


async def test_journal_quota_stops_visibly_without_crashing_supervisor_task(tmp_path):
    source = ("from pathlib import Path\nimport time\nsay('ready for quota')\n"
              "while not Path('release').exists():\n    time.sleep(0.005)\n"
              "print('output exceeds remaining journal quota')\nwait()")
    async with running(tmp_path, [source]) as (sup, seen, _, launcher):
        sup.submit("begin")
        await seen.until(lambda: bool(seen.kind("say")))
        sup.journal.max_bytes = sup.journal.db.execute("SELECT SUM(size) FROM events").fetchone()[0]
        (tmp_path / "release").touch()
        await asyncio.wait({sup._driver}, timeout=1)
        error = sup._driver.exception() if sup._driver.done() and not sup._driver.cancelled() else None
        assert error is None, f"Quota failure escaped the supervisor actor: {error!r}"
        assert sup.state == "FAILED"
        assert launcher.process.returncode is not None


async def test_successful_final_after_reset_is_a_normal_task_completion(tmp_path):
    async with running(tmp_path, ["wait()", "say('completed normally', final=True)"]) as (sup, seen, provider, _):
        sup.submit("begin")
        await seen.until(lambda: len(seen.kind("cell_end")) == 1 and sup.state == "IDLE")
        sup.request_reset()
        sup.submit("continue")
        await seen.state(sup, "DONE")
        assert [e["content"] for e in seen.kind("say") if e.get("final")] == ["completed normally"]
        assert len(provider.requests) == len(seen.kind("source")) == 2
        assert not seen.kind("final_discarded") and not seen.kind("checkpoint")


async def test_interrupt_rejects_cancellation_resistant_completion(tmp_path):
    provider = CancellationResistantProvider([])
    async with running(tmp_path, provider=provider) as (sup, seen, _, _):
        sup.submit("begin")
        await provider.started.wait()
        interrupt = asyncio.create_task(sup.interrupt())
        try:
            await seen.until(lambda: bool(seen.kind("source")) or sup.state == "INTERRUPTED")
            assert not seen.kind("source"), "Cancelled generation dispatched after interrupt"
        finally:
            interrupt.cancel()
            with suppress(asyncio.CancelledError):
                await interrupt


async def test_unicode_output_compacts_context_without_memory_save_turn(tmp_path):
    async with running(tmp_path, ['print("😀" * 5000)', "say('done', final=True)"],
                       limits=Limits(input_tokens=8000)) as (sup, seen, provider, _):
        sup.submit("begin")
        await seen.state(sup, "DONE")
        assert len(provider.requests) == 2 and sup.context.epoch == 2
        sup.context.check(provider.requests[-1]["messages"])
        assert "😀" * 5000 in "".join(e["content"] for e in all_events(sup, "output"))
        assert not seen.kind("checkpoint")
        assert not hasattr(sup.context, "snapshot")
        assert not seen.kind("memory_draft")


async def test_sqlite_disk_failure_terminates_idle_kernel(tmp_path, monkeypatch):
    async with running(tmp_path, []) as (sup, seen, _, launcher):
        def unavailable(*args, **kwargs):
            raise sqlite3.OperationalError("database or disk is full")
        monkeypatch.setattr(sup.journal, "append", unavailable)
        with pytest.raises(sqlite3.OperationalError):
            sup.submit("must fail before acceptance")
        await asyncio.wait_for(launcher.process.wait(), 3)
        assert sup.state == "FAILED" and sup._storage_failed
        assert not sup.pending
        assert any(e.get("persisted") is False for e in seen.events)


async def test_epoch_storage_failure_keeps_old_context_and_stops_kernel(tmp_path, monkeypatch):
    async with running(tmp_path, ["say('must not run', final=True)"]) as (sup, seen, provider, launcher):
        def unavailable(*args, **kwargs):
            raise sqlite3.OperationalError("database or disk is full")
        monkeypatch.setattr(sup.journal, "commit_epoch", unavailable)
        sup.request_reset()
        sup.submit("reset the conversation window")
        await seen.state(sup, "FAILED")
        await asyncio.wait_for(launcher.process.wait(), 3)
        assert sup.context.epoch == 1
        assert not provider.requests and not seen.kind("source")
        assert sup._storage_failed


async def test_close_kills_worker_without_waiting_for_uncooperative_provider(tmp_path):
    release = asyncio.Event()
    class Stubborn(FakeProvider):
        async def generate(self, messages, *, max_tokens):
            self.started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
            return Completion("say('must not execute', final=True)")
    provider = Stubborn([])
    async with running(tmp_path, provider=provider) as (sup, seen, _, launcher):
        sup.submit("begin")
        await provider.started.wait()
        try:
            await asyncio.wait_for(sup.close(), 2)
            assert launcher.process.returncode is not None
            assert not seen.kind("source")
        finally:
            release.set()
            await asyncio.sleep(0)


async def test_worker_fatal_diagnostic_is_in_user_visible_failure(tmp_path):
    # A trusted fixture forces the NEXT capture thread to fail deterministically.
    poison = "import threading\ndef no_threads(*args, **kwargs):\n    raise RuntimeError(\"can't start new thread\")\nthreading.Thread.start = no_threads"
    async with running(tmp_path, [poison, "print('must not execute')"]) as (sup, seen, _, _):
        sup.submit("begin")
        await seen.state(sup, "FAILED")
        assert any("can't start new thread" in e["content"] for e in seen.kind("error"))
        assert seen.kind("cell_uncertain")


async def test_close_rejects_cancellation_resistant_completion(tmp_path):
    provider = CancellationResistantProvider([])
    async with running(tmp_path, provider=provider, limits=Limits(cell_seconds=0.1)) as (sup, seen, _, _):
        sup.submit("begin")
        await provider.started.wait()
        await sup.close()
        assert not seen.kind("source"), "Closing supervisor dispatched cancelled source"
