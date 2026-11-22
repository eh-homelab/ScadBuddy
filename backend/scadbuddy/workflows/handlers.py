"""What an Update handler waits on. Workflow code: nothing here does I/O."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from temporalio import workflow


async def handler_wait(condition: Callable[[], bool]) -> None:
    """Wait in an Update handler until ``condition`` holds.

    A workflow cancel goes to the run's own task, never to a handler's. Since temporalio
    1.36 (activation jobs in one batch, workflow logic flag 2), `wait_condition` raises
    `CancelledError` in any task when the cancel request arrived after the wait began
    and before its condition held, so that a run can catch the cancel and go on
    (`workflow_wait_condition`'s `cancellation_arrived`;
    https://github.com/temporalio/sdk-python/releases/tag/1.36.0). In a handler that
    failed the Update with "Cancellation raised within update", although the run went
    on to answer it. Here the condition held, so the handler answers. A real cancel of
    the handler's task (an eviction), with the condition still false, is raised."""
    try:
        await workflow.wait_condition(condition)
    except asyncio.CancelledError:
        if not condition():
            raise
