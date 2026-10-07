from __future__ import annotations

import asyncio

import pytest

from py_agent.async_commit import settle


@pytest.mark.asyncio
async def test_repeated_cancel_settles_once():
    entered, release = asyncio.Event(), asyncio.Event()
    committed = []
    async def write():
        entered.set()
        await release.wait()
        committed.append("one")
        return 1
    task = asyncio.create_task(settle(write()))
    await entered.wait()
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert committed == ["one"]


@pytest.mark.asyncio
async def test_commit_failure_is_not_hidden_by_cancellation():
    entered, release = asyncio.Event(), asyncio.Event()
    async def write():
        entered.set()
        await release.wait()
        raise RuntimeError("commit failed")
    task = asyncio.create_task(settle(write()))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(RuntimeError, match="commit failed"):
        await task


@pytest.mark.asyncio
async def test_inner_cancellation_does_not_spin():
    async def cancelled():
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(settle(cancelled()), timeout=.1)
