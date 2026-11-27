"""What an Update handler waits on. Workflow code: nothing here does I/O."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from temporalio import workflow


async def handler_wait(condition: Callable[[], bool]) -> None:
    """Wait in an Update handler until ``condition`` holds.

    A workflow cancel goes to the run's own task, never to a handler's. Whether
    `wait_condition` raises `CancelledError` in a handler's task under that cancel
    depends on the activation order. With workflow logic flag 2 on (activation jobs in one
    batch: temporalio 1.36's default, kept by every history recorded on 1.36), it raises
    when the cancel request arrived after the wait began and before its condition held
    (`workflow_wait_condition`'s `cancellation_arrived`;
    https://github.com/temporalio/sdk-python/releases/tag/1.36.0); it checks only once
    the condition holds. In a handler that failed the Update with "Cancellation raised
    within update", although the run went on to answer it. With the flag off (1.37's
    default for a new run, https://github.com/temporalio/sdk-python/pull/2121) it does
    not raise. Either way the condition held here, so the handler answers. Only a cancel
    of the handler's own task (an eviction) ends the wait with the condition false, and
    that is raised. `tests/test_handler_wait.py` pins both orders (#8318)."""
    try:
        await workflow.wait_condition(condition)
    except asyncio.CancelledError:
        if not condition():
            raise
