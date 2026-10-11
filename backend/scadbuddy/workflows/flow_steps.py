"""A flow host call's step on the run's row (plan 2026-10-09-durable-phase-6-flows.md
Ruling 7): written when the call starts and again when it ends, by the run's own
workflow, through the `flow_project` local activity.

The sequence number comes from the running `ProjectWorkflow` (`workflow.instance()`),
so it is the workflow's state and replays with it.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Literal, Protocol

from temporalio import workflow
from temporalio.common import RetryPolicy

from scadbuddy.workflows.flow_models import FlowStep, FlowWaiting, ProjectionWrite

FLOW_PROJECT = "flow_project"


class StepOwner(Protocol):
    """What `step` needs of the running workflow."""

    @property
    def run_id(self) -> str: ...

    def next_seq(self) -> int: ...

    @property
    def approval_timeout_s(self) -> int: ...

    def mark_started(self, call_id: str) -> None: ...

    def started(self, call_id: str) -> bool: ...


async def project(write: ProjectionWrite) -> None:
    """One write to the run's row. Retried without limit: a projection write never
    fails the script."""
    await workflow.execute_local_activity(
        FLOW_PROJECT,
        write,
        start_to_close_timeout=timedelta(seconds=8),
        retry_policy=RetryPolicy(maximum_attempts=0),
    )


@asynccontextmanager
async def step(
    fn: str,
    call_id: str,
    *,
    outward: bool,
    waiting: Literal["approval", "answer"] | None = None,
    prompt: str | None = None,
) -> AsyncIterator[None]:
    """Record a host call around its body: `running` on enter (and parked, with
    `waiting`), `succeeded` or `failed` on exit. A failure records only the exception's
    type, never its message, and is re-raised."""
    owner: StepOwner = workflow.instance()
    info = workflow.info()
    started = FlowStep(
        seq=owner.next_seq(),
        fn=fn,
        call_id=call_id,
        status="running",
        outward=outward,
        started_at=workflow.now(),
        history_length=info.get_current_history_length(),
    )
    parked = (
        FlowWaiting(call_id=call_id, kind=waiting, fn=fn, prompt=prompt, since=workflow.now())
        if waiting is not None
        else None
    )
    await project(
        ProjectionWrite(
            run_id=owner.run_id, workflow_run_id=info.run_id, step=started, waiting_add=parked
        )
    )
    error: str | None = None
    try:
        yield
    except BaseException as err:
        error = type(err).__name__
        raise
    finally:
        await project(
            ProjectionWrite(
                run_id=owner.run_id,
                workflow_run_id=workflow.info().run_id,
                step=started.model_copy(
                    update={
                        "status": "failed" if error is not None else "succeeded",
                        "ended_at": workflow.now(),
                        "error": error,
                    }
                ),
                waiting_remove=call_id if parked is not None else None,
            )
        )
