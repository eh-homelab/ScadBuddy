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

import httpx
from temporalio.client import Client

from scadbuddy.api.components import component_dep
from scadbuddy.api.deps import transactional_events
from scadbuddy.auth.component import AUTH
from scadbuddy.auth.service_credentials import InProcessProvider, ServiceAuth
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.core.settings import Settings
from scadbuddy.flows.agent_tools import AgentTools
from scadbuddy.flows.definitions import FlowDefinitions
from scadbuddy.flows.keys import approval_key, payload_keys, run_key
from scadbuddy.flows.repository import FlowHistory
from scadbuddy.flows.states import FlowStates
from scadbuddy.flows.store import FlowStore
from scadbuddy.library.repo_lock import flows_coordinator
from scadbuddy.workflows.flow_activities import FlowActivities
from scadbuddy.workflows.flow_grants import FlowGrants
from scadbuddy.workflows.flow_routes import FlowRoutes, api_client
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
    #: The versions: scripts in `data/flows`, indexed in `store` (#2503).
    definitions: FlowDefinitions
    #: The payload keys, with the KEK; None without one.
    keys: PgPayloadKeys | None
    #: The agent's tools and their tiers now, from the agent beside this API (#2704).
    agent_tools: AgentTools
    #: A connection to the database holding the keys (forgetting a run takes its lock).
    connect: Connect | None = None
    #: Connected when the app runs; None without a KEK (or before then).
    client: Client | None = field(default=None)
    #: The `projects` queue's operation activities (the flow decisions), built when the
    #: in-process worker starts: the operations component reads this one's kinds.
    operation_activities: Callable[[], list[Callable[..., Any]]] = field(default=lambda: [])
    #: The key a run's token is MACed under (`auth/run_tokens.py`); None without a KEK.
    run_key: bytes | None = None
    #: The projects credential (#4975 phase 5), which this process's own calls on the
    #: API's routes present: the in-process worker's host calls and a grant's reads. Minted
    #: in process (#5494).
    api_auth: httpx.Auth | None = None

    @property
    def queue(self) -> str:
        return self.settings.temporal_task_queue_projects

    @property
    def search_attributes(self) -> bool:
        return self.settings.temporal_search_attributes


FLOWS: Key[Flows] = Key("flows")


def _build(core: Core, components: Components) -> Flows:
    keys = payload_keys(core.settings)
    auth = components.get(AUTH)

    def projects_operations() -> list[Callable[..., Any]]:
        from scadbuddy.operations.component import OPERATIONS

        ops = components.get(OPERATIONS)
        kinds = {name: kind for name, kind in ops.kinds.items() if kind.queue == "projects"}
        return operation_activities(ops.store, core.settings_store, kinds)

    store = FlowStore(core.projection.pool, events=transactional_events(core.events))
    # Every replica commits to the one repository on the share, as for models (#2385).
    history = FlowHistory(
        core.paths.flows,
        timeout=core.config.git_timeout,
        coordinator=flows_coordinator(core.settings.database_url),
    )
    return Flows(
        store=store,
        settings=core.settings,
        definitions=FlowDefinitions(history, store, FlowStates(core.projection.pool)),
        keys=keys[0] if keys is not None else None,
        agent_tools=AgentTools(core.settings.agent_internal_url),
        connect=keys[1] if keys is not None else None,
        operation_activities=projects_operations,
        run_key=run_key(core.settings),
        # #5494 (spec 2026-11-05 §7.1): minted in this process, which holds the
        # database, with `proof = inprocess`; no view, no KEK.
        api_auth=ServiceAuth(InProcessProvider("projects", lambda: auth.credentials)),
    )


def serves_projects(settings: Settings) -> bool:
    """Whether this process serves the `projects` queue itself."""
    return settings.temporal_worker_inprocess or settings.temporal_projects_worker_inprocess


@asynccontextmanager
async def _run(flows: Flows) -> AsyncIterator[None]:
    settings = flows.settings
    # Before anything serves: the versions Postgres held move into git first (#2503).
    await flows.definitions.start()
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
    # Without SCADBUDDY_API_INTERNAL_URL a flow's render, print and arrange fail,
    # naming it; the rest of a flow runs.
    api = api_client(settings.api_internal_url, flows.api_auth)
    routes = FlowRoutes(api)
    grants = FlowGrants(flows.store, api, approval_key(settings))
    worker = projects_worker(
        flows.client,
        flows.queue,
        [
            *FlowActivities(flows.store).all(),
            *routes.all(),
            *grants.all(),
            *flows.operation_activities(),
        ],
    )
    task = asyncio.create_task(worker.run())
    try:
        yield
    finally:
        await worker.shutdown()
        await asyncio.gather(task, return_exceptions=True)
        await routes.aclose()


COMPONENT = Component(FLOWS, build=_build, run=_run)

FlowsDep = Annotated[Flows, component_dep(FLOWS)]
