"""Flows (#1057) as a component (`core/components.py`): the record, the client flow runs
start on, and, in a one-process deployment, the `projects` worker.

A flow's payloads are sealed per run (plan 2026-10-09-durable-phase-6-flows.md Ruling
10), so the client needs the KEK (`SCADBUDDY_SECRET_KEY_FILE`). Without one, `client` is
None and flows are unavailable: they never run unsealed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any

from temporalio.client import Client

from scadbuddy.api.components import component_dep
from scadbuddy.api.deps import transactional_events
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.core.settings import Settings
from scadbuddy.flows.keys import payload_keys
from scadbuddy.flows.store import FlowStore
from scadbuddy.workflows.flow_activities import FlowActivities
from scadbuddy.workflows.flows_client import connect_flows
from scadbuddy.workflows.operation_activities import operation_activities
from scadbuddy.workflows.payload_codec import Connect, PgPayloadKeys
from scadbuddy.workflows.projects_worker import projects_worker

logger = logging.getLogger(__name__)


@dataclass
class Flows:
    """What the flow routes need, and what the in-process worker serves."""

    store: FlowStore
    settings: Settings
    #: The payload keys, with the KEK; None without one.
    keys: PgPayloadKeys | None
    #: A connection to the database holding the keys (forgetting a run takes its lock).
    connect: Connect | None = None
    #: Connected when the app runs; None without a KEK (or before then).
    client: Client | None = field(default=None)
    #: The `projects` queue's operation activities (the flow decisions), built when the
    #: in-process worker starts: the operations component reads this one's kinds.
    operation_activities: Callable[[], list[Callable[..., Any]]] = field(default=lambda: [])

    @property
    def queue(self) -> str:
        return self.settings.temporal_task_queue_projects

    @property
    def search_attributes(self) -> bool:
        return self.settings.temporal_search_attributes


FLOWS: Key[Flows] = Key("flows")


def _build(core: Core, components: Components) -> Flows:
    keys = payload_keys(core.settings)

    def projects_operations() -> list[Callable[..., Any]]:
        from scadbuddy.operations.component import OPERATIONS

        ops = components.get(OPERATIONS)
        kinds = {name: kind for name, kind in ops.kinds.items() if kind.queue == "projects"}
        return operation_activities(ops.store, core.settings_store, kinds)

    return Flows(
        store=FlowStore(core.projection.pool, events=transactional_events(core.events)),
        settings=core.settings,
        keys=keys[0] if keys is not None else None,
        connect=keys[1] if keys is not None else None,
        operation_activities=projects_operations,
    )


def serves_projects(settings: Settings) -> bool:
    """Whether this process serves the `projects` queue itself."""
    return settings.temporal_worker_inprocess or settings.temporal_projects_worker_inprocess


@asynccontextmanager
async def _run(flows: Flows) -> AsyncIterator[None]:
    settings = flows.settings
    if flows.keys is None or not settings.temporal_address:
        yield
        return
    inprocess = serves_projects(settings)
    # Eager for a worker, which cannot run on a lazy client; lazy otherwise, so the API
    # boots while Temporal is down.
    flows.client = await connect_flows(
        settings.temporal_address, settings.temporal_namespace, flows.keys, lazy=not inprocess
    )
    if not inprocess:
        yield
        return
    worker = projects_worker(
        flows.client,
        flows.queue,
        [*FlowActivities(flows.store).all(), *flows.operation_activities()],
    )
    task = asyncio.create_task(worker.run())
    try:
        yield
    finally:
        await worker.shutdown()
        await asyncio.gather(task, return_exceptions=True)


COMPONENT = Component(FLOWS, build=_build, run=_run)

FlowsDep = Annotated[Flows, component_dep(FLOWS)]
