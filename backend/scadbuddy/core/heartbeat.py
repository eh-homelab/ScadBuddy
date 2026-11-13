"""One heartbeat-while-waiting helper for the print worker's activities (#5298 Task 32
review M1): `workflows/print_activities.py`, `workflows/slicing_activities.py`,
`printers/print_activities.py` and `bambuddy/output_reader.py` all use it. Here, in
`core/`, so `output_reader.py` need not import `workflows/print_activities.py`, which
imports it."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from contextlib import suppress
from typing import Any

from temporalio import activity

#: How often a wait tells Temporal it is alive (the print activities' heartbeat timeout
#: is 30 s).
HEARTBEAT_EVERY = 10.0


async def heartbeating[T](work: Coroutine[Any, Any, T], every: float | None = None) -> T:
    """Await ``work``. Inside an activity, heartbeat first and then every ``every``
    seconds (`HEARTBEAT_EVERY` when ``None``) until it ends; outside one, just await it.
    Cancelled, it cancels ``work`` and waits for it to end."""
    if not activity.in_activity():
        return await work
    activity.heartbeat()
    task = asyncio.create_task(work)
    try:
        while True:
            done, _ = await asyncio.wait(
                {task}, timeout=HEARTBEAT_EVERY if every is None else every
            )
            if done:
                return task.result()
            activity.heartbeat()
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
