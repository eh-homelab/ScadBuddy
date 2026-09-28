"""`python -m scadbuddy.worker`: the render worker as a process, over a dev server."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from temporalio import workflow
from temporalio.client import Client
from temporalio.common import VersioningBehavior
from temporalio.worker import (
    UnsandboxedWorkflowRunner,
    Worker,
    WorkerDeploymentConfig,
    WorkerDeploymentVersion,
)

from scadbuddy import worker as worker_module
from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.history import ModelHistory
from scadbuddy.render.job_models import Job, render_key
from scadbuddy.render.projection import JobProjection, workflow_id_for
from scadbuddy.render.solids import WRAPPER_PREFIX
from scadbuddy.worker import _poll, _wait_drained, run_inprocess_worker, run_worker
from scadbuddy.workflows.activities import WorkerDeps
from scadbuddy.workflows.client import DEPLOYMENT_NAME, connect_lazily, drained, make_current
from scadbuddy.workflows.models import piece_key
from scadbuddy.workflows.pipelines import TemplatePipeline
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS, fake_3mf_openscad
from tests.support.temporal import current_address, temporal_client


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


async def _healthy(http: httpx.AsyncClient, worker: asyncio.Task[None]) -> dict[str, Any]:
    for _ in range(300):
        if worker.done():
            worker.result()  # raises what stopped it
            raise AssertionError("the worker exited before serving /healthz")
        try:
            response = await http.get("/healthz")
        except httpx.TransportError:
            await asyncio.sleep(0.1)
            continue
        assert response.status_code == 200
        body: dict[str, Any] = response.json()
        return body
    raise AssertionError("/healthz never answered")


@pytest.mark.requires_postgres
@pytest.mark.requires_temporal
@pytest.mark.requires_git
async def test_the_worker_renders_a_job_and_serves_health_and_metrics(
    settings: Settings, paths: DataPaths, model: str, pg_conninfo: str, tmp_path: Path
) -> None:
    queue = f"t-{uuid.uuid4().hex[:8]}"
    build_id = f"test-{uuid.uuid4().hex[:8]}"
    cfg = settings.model_copy(
        update={
            "openscad": fake_3mf_openscad(tmp_path / "bin"),
            "temporal_task_queue_render": queue,
            "revision": build_id,
        }
    )
    # The template at a revision, so the piece is keyed by one.
    revision = ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX).ensure_repo()
    assert revision is not None
    port = _free_port()
    params: dict[str, Any] = {"width": 12}
    job = Job(
        id=uuid.uuid4().hex,
        slug=model,
        params=params,
        inputs={"params": params},
        model_version=revision,
        created_at=datetime.now(UTC),
    )
    projection = JobProjection(pg_conninfo, pool_size=2)
    projection.open()
    try:
        async with temporal_client() as client:
            stop = asyncio.Event()
            worker = asyncio.create_task(
                run_worker(cfg, stop=stop, health_port=port, client=client)
            )
            try:
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
                    health = await _healthy(http, worker)
                    assert health == {
                        "ok": True,
                        "build_id": build_id,
                        "task_queue": queue,
                        "store": {
                            "backend": "local",
                            "configured_backend": "local",
                            "render_key_fallback": False,
                            "multi_worker": False,
                        },
                    }

                    projection.submit(job, render_key(model, params, revision))
                    await asyncio.wait_for(
                        client.execute_workflow(
                            TemplatePipeline.run, job, id=workflow_id_for(job.id), task_queue=queue
                        ),
                        timeout=120,
                    )
                    metrics = (await http.get("/metrics")).text
            finally:
                stop.set()
                await asyncio.wait_for(worker, timeout=30)

        stored = projection.read(job.id)
    finally:
        projection.close()

    assert stored.state == "done", stored.error
    assert stored.result is not None
    key = piece_key(model, revision, "model.scad", params)
    assert (cfg.data_dir / "blobs" / key).is_dir()
    # The worker's own bus published the job's events.
    with psycopg.connect(pg_conninfo) as conn:
        kinds = {
            row[0]
            for row in conn.execute(
                "SELECT kind FROM events WHERE payload->>'job_id' = %s", (job.id,)
            )
        }
    assert {"job.running", "job.done"} <= kinds
    # Observed, not merely declared: the HELP line is there from the first scrape.
    samples = {
        line.rsplit(" ", 1)[0]: float(line.rsplit(" ", 1)[1])
        for line in metrics.splitlines()
        if line and not line.startswith("#")
    }
    assert samples['scadbuddy_render_duration_seconds_count{outcome="done"}'] == 1
    assert samples['scadbuddy_render_job_latency_seconds_count{outcome="done"}'] == 1
    assert samples['scadbuddy_render_jobs_finished_total{outcome="done"}'] == 1
    for stage in ("source", "render", "split", "solids", "thumbnail", "write"):
        assert samples[f'scadbuddy_render_stage_seconds_count{{stage="{stage}"}}'] == 1, stage
    assert f'revision="{build_id}"' in metrics


# ── the drain after stop ───────────────────────────────────────────────────────


async def test_the_drain_polls_until_the_version_is_drained() -> None:
    answers = iter([False, False, True])
    calls = 0

    async def drained() -> bool:
        nonlocal calls
        calls += 1
        return next(answers)

    assert await _wait_drained(drained, timeout=5, poll=0.01)
    assert calls == 3


async def test_the_drain_gives_up_at_its_bound() -> None:
    calls = 0

    async def drained() -> bool:
        nonlocal calls
        calls += 1
        return False

    assert not await asyncio.wait_for(_wait_drained(drained, timeout=0.1, poll=0.01), 5)
    assert calls > 1


@workflow.defn(name="BlocksUntilReleased")
class _BlocksUntilReleased:
    def __init__(self) -> None:
        self.released = False

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self.released)

    @workflow.signal
    def release(self) -> None:
        self.released = True


async def _until_drained_is(
    expected: bool, client: Client, build_id: str, timeout: float = 30
) -> None:
    """Visibility lags the run by a moment: wait for the count to catch up."""

    async def poll() -> None:
        while await drained(client, namespace=client.namespace, build_id=build_id) != expected:
            await asyncio.sleep(0.2)

    await asyncio.wait_for(poll(), timeout)


@pytest.mark.requires_temporal
async def test_drained_sees_a_running_pinned_workflow() -> None:
    build_id = f"test-{uuid.uuid4().hex[:8]}"
    queue = f"t-{uuid.uuid4().hex[:8]}"
    async with (
        temporal_client() as client,
        Worker(
            client,
            task_queue=queue,
            workflows=[_BlocksUntilReleased],
            workflow_runner=UnsandboxedWorkflowRunner(),
            deployment_config=WorkerDeploymentConfig(
                version=WorkerDeploymentVersion(deployment_name=DEPLOYMENT_NAME, build_id=build_id),
                use_worker_versioning=True,
                default_versioning_behavior=VersioningBehavior.PINNED,
            ),
        ),
    ):
        await make_current(client, namespace=client.namespace, build_id=build_id)
        handle = await client.start_workflow(
            _BlocksUntilReleased.run, id=f"blocks-{uuid.uuid4().hex}", task_queue=queue
        )
        await _until_drained_is(False, client, build_id)

        await handle.signal(_BlocksUntilReleased.release)
        await asyncio.wait_for(handle.result(), 30)
        await _until_drained_is(True, client, build_id)


@pytest.mark.requires_temporal
async def test_the_in_process_worker_stops_without_draining(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def never(*_: object, **__: object) -> bool:
        raise AssertionError("the in-process worker must not drain")

    monkeypatch.setattr(worker_module, "drained", never)
    settings = Settings(
        database_url=UNUSED_DATABASE_URL,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        data_dir=tmp_path,
        revision=f"test-{uuid.uuid4().hex[:8]}",
        temporal_task_queue_render=f"t-{uuid.uuid4().hex[:8]}",
    )
    deps = WorkerDeps(
        config=Config(openscad="openscad", data_dir=tmp_path),
        paths=DataPaths(tmp_path),
        assets=None,  # type: ignore[arg-type]
        blobs=None,  # type: ignore[arg-type]
        refs=None,  # type: ignore[arg-type]
        projection=None,  # type: ignore[arg-type]
    )
    stop = asyncio.Event()
    stop.set()
    async with temporal_client() as client:
        await asyncio.wait_for(_poll(settings, deps, client, stop, drain=False), 30)


@pytest.mark.requires_temporal
async def test_the_in_process_worker_runs_a_workflow_and_ends_on_its_stop_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def never(*_: object, **__: object) -> bool:
        raise AssertionError("the in-process worker must not drain")

    monkeypatch.setattr(worker_module, "drained", never)
    started: list[str] = []
    marked = threading.Event()

    class _Projection:
        """Only what the pipeline's first activity needs: the `project` activity's
        `mark_started`, which proves the worker polled and ran it."""

        def mark_started(self, job_id: str) -> None:
            started.append(job_id)
            marked.set()

    settings = Settings(
        database_url=UNUSED_DATABASE_URL,
        data_dir=tmp_path,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        revision=f"test-{uuid.uuid4().hex[:8]}",
        temporal_task_queue_render=f"t-{uuid.uuid4().hex[:8]}",
    )
    deps = WorkerDeps(
        config=Config(openscad="openscad", data_dir=tmp_path),
        paths=DataPaths(tmp_path),
        assets=None,  # type: ignore[arg-type]
        blobs=None,  # type: ignore[arg-type]
        refs=None,  # type: ignore[arg-type]
        projection=_Projection(),  # type: ignore[arg-type]
    )
    job = Job(
        id=uuid.uuid4().hex,
        slug="m",
        params={},
        inputs={"params": {}},
        model_version=None,
        created_at=datetime.now(UTC),
    )
    stop = asyncio.Event()
    async with temporal_client() as client:
        worker = asyncio.create_task(run_inprocess_worker(settings, deps, client, stop))
        handle = await client.start_workflow(
            TemplatePipeline.run,
            job,
            id=workflow_id_for(job.id),
            task_queue=settings.temporal_task_queue_render,
        )
        try:
            assert await asyncio.wait_for(asyncio.to_thread(marked.wait, 60), 65)
            assert started == [job.id]
            # The workflow is still running (its next activity cannot succeed on these
            # deps): the worker ends on `stop` all the same, without draining.
            stop.set()
            await asyncio.wait_for(worker, 30)
        finally:
            stop.set()
            await handle.terminate()
            if not worker.done():
                worker.cancel()


def test_a_lazy_client_is_built_without_an_event_loop() -> None:
    client = connect_lazily(UNUSED_TEMPORAL_ADDRESS, "somewhere")
    assert client.namespace == "somewhere"


async def test_a_lazy_client_is_built_inside_a_running_loop() -> None:
    # `create_app` runs inside uvicorn's loop (`--factory`), so it cannot `asyncio.run`.
    client = connect_lazily(UNUSED_TEMPORAL_ADDRESS, "default")
    with pytest.raises(RuntimeError, match="Failed client connect"):
        await client.count_workflows()


@pytest.mark.requires_temporal
async def test_a_lazy_client_connects_on_its_first_call() -> None:
    async with temporal_client() as server:
        client = connect_lazily(current_address(server), server.namespace)
        assert (await client.count_workflows("WorkflowId = 'nothing-here'")).count == 0


class _Cache:
    def __init__(self) -> None:
        self.calls = 0

    def evict(self) -> list[str]:
        self.calls += 1
        if self.calls == 1:
            raise OSError("a transient disk error")
        return ["k"]


async def test_the_worker_evicts_its_piece_cache_on_each_sweep_and_survives_a_failure() -> None:
    cache = _Cache()
    evicting = asyncio.create_task(
        worker_module._evict_periodically(cache, 0.01)  # type: ignore[arg-type]
    )
    try:
        async with asyncio.timeout(5):
            while cache.calls < 3:
                await asyncio.sleep(0.01)
    finally:
        evicting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await evicting
    assert cache.calls >= 3
