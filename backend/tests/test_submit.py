"""RenderService: submit -> start_workflow -> reconcile, over a dev server and a real
projection, with the openscad activities faked."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus

from scadbuddy.core.config import load_config
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.pg_listener import PgListener
from scadbuddy.library.assets import AssetStore
from scadbuddy.render.job_models import Job
from scadbuddy.render.projection import JobProjection, workflow_id_for
from scadbuddy.render.submit import RenderService
from scadbuddy.store import BlobRefs
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.workflows.activities import RenderActivities, WorkerDeps
from scadbuddy.workflows.models import Projection
from tests.support.temporal import temporal_client
from tests.test_workflows import FakeActivities, _worker

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

SLUG = "demo"


class ProjectingActivities(FakeActivities):
    """The fakes, but `project` writes the row as the real activity does."""

    def __init__(self, deps: WorkerDeps, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._real = RenderActivities(deps)

    @activity.defn(name="project")
    async def project(self, projection: Projection) -> None:
        await super().project(projection)
        await self._real.project(projection)


def _sample(metrics: Metrics, name: str) -> float:
    return metrics.registry.get_sample_value(name) or 0.0


async def _settled(projection: JobProjection, job_id: str, timeout: float = 30) -> Job:
    async with asyncio.timeout(timeout):
        while True:
            job = await asyncio.to_thread(projection.read, job_id)
            if job.state in ("done", "failed", "cancelled"):
                return job
            await asyncio.sleep(0.05)


@pytest.fixture
async def projection(pg_conninfo: str) -> AsyncIterator[JobProjection]:
    bus = PgNotifyEventBus(pg_conninfo, listener=PgListener(pg_conninfo))
    store = JobProjection(pg_conninfo, pool_size=4, events=bus)
    await asyncio.to_thread(store.open)
    try:
        yield store
    finally:
        await asyncio.to_thread(store.close)


@pytest.fixture
def deps(projection: JobProjection, paths: DataPaths) -> WorkerDeps:
    config = replace(load_config({}), data_dir=paths.root)
    return WorkerDeps(
        config=config,
        paths=paths,
        assets=AssetStore(paths.assets),
        blobs=LocalBlobStore(paths.blobs),
        refs=BlobRefs(projection.pool),
        projection=projection,
    )


ServiceFactory = Callable[..., RenderService]


@pytest.fixture
def make_service(projection: JobProjection, deps: WorkerDeps) -> ServiceFactory:
    def make(client: Client, task_queue: str, **kwargs: Any) -> RenderService:
        return RenderService(
            projection=projection,
            client=client,
            task_queue=task_queue,
            config=deps.config,
            paths=deps.paths,
            metrics=Metrics(),
            **kwargs,
        )

    return make


async def test_a_submit_starts_the_workflow_its_row_names(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        async with _worker(client, queue, ProjectingActivities(deps)):
            job = await service.submit(SLUG, {"width": 1})
            # The workflow the row names runs to its end.
            await asyncio.wait_for(
                client.get_workflow_handle(workflow_id_for(job.id)).result(), timeout=30
            )
        await service.aclose()
        done = await asyncio.to_thread(projection.read, job.id)

    assert done.state == "done", done.error
    assert done.result is not None
    assert _sample(service.metrics, "scadbuddy_render_jobs_submitted_total") == 1


async def test_a_submit_whose_start_failed_is_started_by_the_reconciler(
    make_service: ServiceFactory,
    deps: WorkerDeps,
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue, reconcile_after=0.5, reconcile_interval=0.1)

        async def unavailable(*_: object, **__: object) -> None:
            raise RuntimeError("temporal is down")

        with monkeypatch.context() as patched:
            patched.setattr(client, "start_workflow", unavailable)
            job = await service.submit(SLUG, {"width": 2})
        # The row is committed and waits for its workflow.
        assert (await asyncio.to_thread(projection.read, job.id)).state == "pending"
        assert (
            service.metrics.registry.get_sample_value(
                "scadbuddy_render_store_errors_total", {"operation": "start_workflow"}
            )
            == 1
        )

        async with _worker(client, queue, ProjectingActivities(deps)):
            await service.start()
            try:
                done = await _settled(projection, job.id)
            finally:
                await service.aclose()

    assert done.state == "done", done.error


async def test_superseding_the_last_claim_cancels_the_workflow(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        gate = asyncio.Event()
        acts = ProjectingActivities(deps, block_solids=gate)
        async with _worker(client, queue, acts):
            first = await service.submit(SLUG, {"width": 3})
            async with asyncio.timeout(30):
                while "render_solids" not in acts.calls:
                    await asyncio.sleep(0.05)
            second = await service.submit(SLUG, {"width": 4}, supersedes=first.id)
            handle = client.get_workflow_handle(workflow_id_for(first.id))
            async with asyncio.timeout(30):
                while (await handle.describe()).status == WorkflowExecutionStatus.RUNNING:
                    await asyncio.sleep(0.05)
            gate.set()
            cancelled = await _settled(projection, first.id)
            done = await _settled(projection, second.id)
        described = await handle.describe()
        await service.aclose()

    assert cancelled.state == "cancelled"
    assert described.status == WorkflowExecutionStatus.CANCELED
    assert done.state == "done", done.error


async def test_an_identical_submit_coalesces_and_starts_nothing_new(
    make_service: ServiceFactory, projection: JobProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue, reconcile_after=0.0)
        started: list[str] = []
        start = client.start_workflow

        async def counting(*args: Any, **kwargs: Any) -> Any:
            started.append(kwargs["id"])
            return await start(*args, **kwargs)

        monkeypatch.setattr(client, "start_workflow", counting)
        # No worker: the first job stays pending, so the second is answered by it.
        first = await service.submit(SLUG, {"width": 5})
        second = await service.submit(SLUG, {"width": 5})
        submitted = list(started)
        # Pending, but its workflow is running (waiting for a worker): not restarted.
        reconciled = await service.reconcile_once()
        await service.aclose()

    assert second.id == first.id
    assert submitted == [workflow_id_for(first.id)]
    assert reconciled == 0
    assert _sample(service.metrics, "scadbuddy_render_jobs_coalesced_total") == 1
    assert (await asyncio.to_thread(projection.read, first.id)).claims == 2
