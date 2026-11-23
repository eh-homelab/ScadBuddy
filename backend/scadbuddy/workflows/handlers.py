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
    https://github.com/temporalio/sdk-python/releases/tag/1.36.0). It checks only once
    the condition holds: a cancel that arrives while the condition is false leaves the
    wait waiting, as before 1.36, and the raise comes when the condition then holds. In
    a handler that failed the Update with "Cancellation raised within update",
    although the run went on to answer it. Here the condition held, so the handler
    answers. Only a cancel of the handler's own task (an eviction) ends the wait with
    the condition false, and that is raised. `tests/test_handler_wait.py` pins both."""
    try:
        await workflow.wait_condition(condition)
    except asyncio.CancelledError:
        if not condition():
            raise
