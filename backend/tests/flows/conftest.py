"""Shared fixtures of the flow tests: one Temporal dev server for the module set, and a
`projects` worker over a test schema."""

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest

from scadbuddy.flows.store import FlowStore
from scadbuddy.render.projection import JobProjection
from scadbuddy.workflows.flow_activities import FlowActivities
from scadbuddy.workflows.flows_client import connect_flows
from scadbuddy.workflows.projects_worker import projects_worker
from tests.flows.flows_support import Flows, Keys
from tests.support.temporal import temporal_available, temporal_server


@pytest.fixture(scope="session")
def temporal_address() -> Iterator[str]:
    if not temporal_available():
        pytest.skip("no Temporal")
    with temporal_server() as address:
        yield address


@pytest.fixture
def jobs(pg_conninfo: str) -> Iterator[JobProjection]:
    store = JobProjection(pg_conninfo, pool_size=4)
    store.open()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
async def flows(temporal_address: str, jobs: JobProjection) -> AsyncIterator[Flows]:
    client = await connect_flows(temporal_address, "default", Keys())
    store = FlowStore(jobs.pool)
    queue = f"projects-{uuid.uuid4().hex[:8]}"
    async with projects_worker(client, queue, FlowActivities(store).all()):
        yield Flows(client, queue, store)
