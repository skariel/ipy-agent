from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
import threading

import pytest

from py_agent.journal_worker import SQLiteJournalWorker
from py_agent.session_journal import JournalError


@pytest.fixture
def worker(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    worker = SQLiteJournalWorker(directory / "journal.db")
    yield worker
    worker.close()


@pytest.mark.asyncio
async def test_connection_owned_by_worker_and_acknowledged(worker):
    await worker.call("start", "s", 0, "fake", "model")
    records = await worker.call("recent", "s", limit=10)
    assert len(records) == 1
    await worker.aclose()
    with pytest.raises(JournalError, match="closed"):
        await worker.call("recent", "s", limit=10)


@pytest.mark.asyncio
async def test_lock_contention_does_not_block_loop(worker):
    db = sqlite3.connect(Path(worker.path), isolation_level=None)
    db.execute("BEGIN IMMEDIATE")
    task = asyncio.create_task(worker.call("start", "s", 0, "fake", "model"))
    try:
        await asyncio.sleep(.03)
        assert not task.done()
        db.execute("ROLLBACK")
        await asyncio.wait_for(task, timeout=2)
        assert len(await worker.call("recent", "s", limit=10)) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cancel_waits_for_commit_without_replay(worker):
    entered, release = threading.Event(), threading.Event()
    original = worker._journal.start
    def slow(*args):
        entered.set()
        release.wait(2)
        return original(*args)
    worker._journal.start = slow
    task = asyncio.create_task(worker.call("start", "s", 0, "fake", "model"))
    await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(.02)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(await worker.call("recent", "s", limit=10)) == 1


@pytest.mark.asyncio
async def test_admission_is_bounded(worker):
    worker._max_pending = 1
    release = threading.Event()
    original = worker._journal.start
    worker._journal.start = lambda *args: (release.wait(2), original(*args))[1]
    task = asyncio.create_task(worker.call("start", "s", 0, "fake", "model"))
    await asyncio.sleep(.02)
    try:
        with pytest.raises(JournalError, match="admission"):
            await worker.call("recent", "s", limit=10)
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_coordinator_acknowledges_request_and_result(worker):
    from py_agent.builtin_services import BuiltinPlugin
    from py_agent.coordinator import Coordinator, State
    from py_agent.plugins import PluginRuntime
    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(runtime, router="default", provider="fake",
                              interpreter="basic", executor="local", journal=worker)
    await coordinator.start()
    try:
        submission = await coordinator.submit("terminal", "@x = 2\nprint(x)")
        assert submission.result.status == "success"
        assert coordinator.state is State.IDLE
        records = await worker.call("recent", coordinator.session_id, limit=20)
        kinds = {entry["kind"] for entry in records}
        assert {"execution_source", "execution_result"} <= kinds
    finally:
        await coordinator.close()
    assert coordinator.state is State.CLOSED


@pytest.mark.asyncio
async def test_journal_failure_before_dispatch_pauses_session(worker):
    from py_agent.builtin_services import BuiltinPlugin
    from py_agent.coordinator import Coordinator, State
    from py_agent.plugins import PluginRuntime
    runtime = PluginRuntime.load(builtins={"builtin": BuiltinPlugin()})
    coordinator = Coordinator(runtime, router="default", provider="fake",
                              interpreter="basic", executor="local", journal=worker)
    await coordinator.start()
    def fail(*args, **kwargs):
        raise JournalError("disk failed")
    worker._journal.record_execution_source = fail
    try:
        with pytest.raises(JournalError):
            await coordinator.submit("terminal", "@x = 2")
        assert coordinator.state is State.FAILED
        records = await worker.call("recent", coordinator.session_id, limit=20)
        assert "execution_result" not in {entry["kind"] for entry in records}
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_typed_commit_is_acknowledged_and_off_loop(worker):
    loop_thread = threading.get_ident()
    callback_threads = []

    def start(journal):
        callback_threads.append(threading.get_ident())
        journal.start("typed", 0, "fake", "model")

    await worker.commit(start)
    assert callback_threads and callback_threads[0] != loop_thread
    assert len(worker.recent("typed", limit=10)) == 1


@pytest.mark.asyncio
async def test_typed_commit_cancellation_waits_for_admitted_work(worker):
    entered = threading.Event()
    release = threading.Event()

    def start(journal):
        entered.set()
        assert release.wait(timeout=3)
        journal.start("cancelled", 0, "fake", "model")

    task = asyncio.create_task(worker.commit(start))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(worker.recent("cancelled", limit=10)) == 1
    finally:
        release.set()
