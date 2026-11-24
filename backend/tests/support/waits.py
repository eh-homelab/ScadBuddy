"""A wait on a condition, not on a clock (#6309)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

#: Turns of the loop ``until`` gives the work that was ready once its deadline passed.
LATE_TURNS = 100


async def until(
    condition: Callable[[], bool],
    budget: float = 10.0,
    *,
    interval: float = 0.01,
    what: str | None = None,
) -> None:
    """Wait until ``condition()`` holds; ``budget`` seconds only end a test that hangs.

    A loop blocked past the deadline (a long GC pause on a loaded xdist worker) wakes this
    wait and its timeout in one turn, before work already under way can finish (#7014).
    So the ready work runs, then the condition is read again, with no more time. Raises
    `TimeoutError`, or `AssertionError` with ``what`` when it is given."""
    try:
        async with asyncio.timeout(budget):
            while not condition():
                await asyncio.sleep(interval)
    except TimeoutError:
        for _ in range(LATE_TURNS):
            if condition():
                return
            await asyncio.sleep(0)
        if what is not None:
            raise AssertionError(what) from None
        raise


async def reaches(count: Callable[[], int], at_least: int) -> None:
    """Wait until ``count()`` is ``at_least`` (`until`)."""
    await until(lambda: count() >= at_least)
