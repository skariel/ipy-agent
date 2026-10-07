"""Settle admitted side effects before propagating caller cancellation."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable


async def settle[T](awaitable: Awaitable[T]) -> T:
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
            if task.done():
                result = task.result()
                break
    if cancelled:
        raise asyncio.CancelledError
    return result
