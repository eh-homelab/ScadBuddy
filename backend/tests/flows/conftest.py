"""Shared fixtures of the flow tests: one Temporal dev server for the module set, and a
`projects` worker over a test schema."""

import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest

from scadbuddy.flows.store import FlowStore
from scadbuddy.render.projection import JobProjection
from scadbuddy.workflows.flow_activities import FlowActivities
from scadbuddy.workflows.flows_client import connect_flows
from scadbuddy.workflows.projects_worker import projects_worker
from tests.crud.crud_support import served_flow_store
from tests.flows.fake_api import FakeApi, Outward, outward_worker
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
    # The worker's store is the CRUD routes', as the projects worker's (#5298).
    async with (
        served_flow_store(store, client=client) as remote,
        projects_worker(client, queue, FlowActivities(remote).all()),
    ):
        flows = Flows(client, queue, store, remote)
        try:
            yield flows
        finally:
            for worker, task in flows.cleanup:
                await worker.shutdown()
                await task


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
async def outward(
    temporal_address: str, jobs: JobProjection, api: FakeApi
) -> AsyncIterator[Outward]:
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        async for flows in outward_worker(temporal_address, jobs, client):
            yield flows
