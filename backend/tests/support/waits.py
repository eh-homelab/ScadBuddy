"""A wait on a condition, not on a clock (#6309)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable


async def reaches(count: Callable[[], int], at_least: int) -> None:
    """Wait until ``count()`` is ``at_least``; the deadline only ends a test that hangs."""
    async with asyncio.timeout(10):
        while count() < at_least:
            await asyncio.sleep(0.01)
