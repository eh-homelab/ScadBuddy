"""Flow runs whose execution closed without finishing them (#1057, plan
2026-10-09-durable-phase-6-flows.md Task B6): terminated by an operator, reset away, or
gone. A run's row is written only by its own workflow, so nothing else would end it."""

from __future__ import annotations

from datetime import timedelta

from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.flows.store import FlowStore

#: A row that moved more recently than this is left alone: its run is alive.
QUIET = timedelta(minutes=10)
_DESCRIBE = timedelta(seconds=5)


async def sweep_flow_runs(
    store: FlowStore, client: Client, *, quiet: timedelta = QUIET
) -> list[str]:
    """End the open rows whose execution is closed or gone; follow a Reset's new run
    id. Returns the runs it ended."""
    ended: list[str] = []
    for run in await store.open_runs(quiet):
        try:
            described = await client.get_workflow_handle(run.workflow_id).describe(
                rpc_timeout=_DESCRIBE
            )
        except RPCError as err:
            if err.status != RPCStatusCode.NOT_FOUND:
                raise
            described = None
        if described is None or described.status != WorkflowExecutionStatus.RUNNING:
            if await store.mark_terminated(run.id) is not None:
                ended.append(run.id)
        elif described.run_id != run.workflow_run_id:
            # A Reset the row has not caught up with: the run goes on under a new id.
            await store.follow_run(run.id, described.run_id)
    return ended
