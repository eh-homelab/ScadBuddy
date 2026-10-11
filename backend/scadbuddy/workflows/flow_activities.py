"""A flow run's activities on the `projects` queue (plan 2026-10-09 Rulings 7 and 8):
its row, and the `close` that ends its execution."""

from collections.abc import Callable
from typing import Any

from temporalio import activity

from scadbuddy.flows.store import FlowStore
from scadbuddy.workflows.flow_models import FlowRecord, ProjectionWrite
from scadbuddy.workflows.flow_steps import FLOW_PROJECT

FLOW_RECORD = "flow_record"
FLOW_CLOSE = "flow_close"


class FlowActivities:
    def __init__(self, store: FlowStore) -> None:
        self._store = store

    @activity.defn(name=FLOW_RECORD)
    async def record(self, record: FlowRecord) -> None:
        """Insert the run's row; a retry finds it there."""
        await self._store.insert_run(record)

    @activity.defn(name=FLOW_PROJECT)
    async def project(self, write: ProjectionWrite) -> None:
        """One of the run's own writes to its row (a local activity)."""
        await self._store.project(write)

    @activity.defn(name=FLOW_CLOSE)
    async def close(self, workflow_id: str) -> None:
        """The harness's public `close` Signal, sent to the run's own workflow as the
        harness's web app sends it: the execution then completes."""
        await activity.client().get_workflow_handle(workflow_id).signal("close")

    def all(self) -> list[Callable[..., Any]]:
        return [self.record, self.project, self.close]
