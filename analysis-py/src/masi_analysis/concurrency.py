"""Small structured-concurrency helpers for bounded outbound fan-out."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterable


async def gather_cancel_on_error[T](awaitables: Iterable[Awaitable[T]]) -> list[T]:
    """Preserve input order while cancelling and joining siblings on first error."""
    tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
