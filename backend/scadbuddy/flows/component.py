"""Flows (#1057) as a component (`core/components.py`): the record, the client flow runs
start on, and, in a one-process deployment, the `projects` worker.

A flow's payloads are sealed per run (plan 2026-10-09-durable-phase-6-flows.md Ruling
10), so the client needs the KEK (`SCADBUDDY_SECRET_KEY_FILE`). Without one, `client` is
None and flows are unavailable: they never run unsealed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any

import psycopg
from temporalio.client import Client

from scadbuddy.api.components import component_dep
from scadbuddy.api.deps import transactional_events
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.core.secrets import SecretKeyError, load_kek
from scadbuddy.core.settings import Settings
from scadbuddy.flows.store import FlowStore
from scadbuddy.workflows.flow_activities import FlowActivities
from scadbuddy.workflows.flows_client import connect_flows
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

    @property
    def queue(self) -> str:
        return self.settings.temporal_task_queue_projects

    @property
    def search_attributes(self) -> bool:
        return self.settings.temporal_search_attributes


FLOWS: Key[Flows] = Key("flows")


def payload_keys(settings: Settings) -> tuple[PgPayloadKeys, Connect] | None:
    """The flow payload keys over the database's `ai_payload_keys`, or None without a
    usable KEK (logged, never raised: the rest of the app runs without flows)."""
    if settings.secret_key_file is None:
        return None
    try:
        kek = load_kek(settings.secret_key_file)
    except SecretKeyError as err:
        logger.warning("flows are unavailable: %s", err)
        return None
    url = settings.database_url

    @asynccontextmanager
    async def connect() -> AsyncIterator[psycopg.AsyncConnection[Any]]:
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
            yield conn

    return PgPayloadKeys(connect, kek), connect


def _build(core: Core, components: Components) -> Flows:
    keys = payload_keys(core.settings)
    return Flows(
        store=FlowStore(core.projection.pool, events=transactional_events(core.events)),
        settings=core.settings,
        keys=keys[0] if keys is not None else None,
        connect=keys[1] if keys is not None else None,
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
    worker = projects_worker(flows.client, flows.queue, FlowActivities(flows.store).all())
    task = asyncio.create_task(worker.run())
    try:
        yield
    finally:
        await worker.shutdown()
        await asyncio.gather(task, return_exceptions=True)


COMPONENT = Component(FLOWS, build=_build, run=_run)

FlowsDep = Annotated[Flows, component_dep(FLOWS)]
