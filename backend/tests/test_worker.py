"""`python -m scadbuddy.worker`: the render worker as a process, over a dev server."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, cast

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
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.settings_store import RenderStoreSettings, StoreNotReadyError
from scadbuddy.render.job_models import Job
from scadbuddy.render.job_store import render_key
from scadbuddy.render.projection import JobProjection, workflow_id_for
from scadbuddy.render.solids import WRAPPER_PREFIX
from scadbuddy.store.bambuddy import RenderSettingsSource
from scadbuddy.store.cache import CachedBlobStore
from scadbuddy.store.content import ContentStore
from scadbuddy.store.factory import StoreBundle
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.worker import _poll, _wait_drained, run_worker
from scadbuddy.workflows.activities import WorkerDeps
from scadbuddy.workflows.client import DEPLOYMENT_NAME, drained, make_current
from scadbuddy.workflows.models import piece_key
from scadbuddy.workflows.pipelines import TemplatePipeline
from tests.conftest import UNUSED_DATABASE_URL, fake_3mf_openscad
from tests.support.temporal import temporal_client


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
    # Refreshed per scrape from this process's store (the local one: no cache to size).
    assert samples["scadbuddy_store_render_key_fallback"] == 0
    assert samples["scadbuddy_worker_cache_bytes"] == 0


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


class _Source:
    async def current(self) -> RenderStoreSettings:
        return RenderStoreSettings(
            store_backend="bambuddy",
            bambuddy_url="http://bambuddy.test",
            api_key="full",
            key_is_fallback=True,
            library_folder_id=7,
        )


async def test_the_worker_exports_its_cache_size_and_whether_it_holds_the_full_key(
    tmp_path: Path,
) -> None:
    local = LocalBlobStore(tmp_path / "blobs")
    cache = CachedBlobStore(
        local, cast(ContentStore, SimpleNamespace(name="bambuddy")), max_bytes=0, min_age=0
    )
    (local.dir_for("k") / "m").write_bytes(b"12345")
    store = StoreBundle(
        "bambuddy", cache, None, None, None, None, cast(RenderSettingsSource, _Source())
    )
    app = worker_module._health_app(
        Settings(data_dir=tmp_path, database_url=UNUSED_DATABASE_URL), Metrics(), store
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://worker") as http:
        metrics = (await http.get("/metrics")).text
    samples = {
        line.rsplit(" ", 1)[0]: float(line.rsplit(" ", 1)[1])
        for line in metrics.splitlines()
        if line and not line.startswith("#")
    }
    assert samples["scadbuddy_worker_cache_bytes"] == 5
    assert samples["scadbuddy_store_render_key_fallback"] == 1


def test_a_refused_store_closes_the_projection_the_worker_opened(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[JobProjection] = []

    class Recording(JobProjection):
        def open(self) -> None:
            super().open()
            opened.append(self)

    def refuse(**_: object) -> NoReturn:
        raise StoreNotReadyError("refused")

    monkeypatch.setattr(worker_module, "JobProjection", Recording)
    monkeypatch.setattr(worker_module, "build_store", refuse)
    with pytest.raises(StoreNotReadyError):
        worker_module.build_worker_deps(settings)
    assert len(opened) == 1 and opened[0].pool.closed


async def test_a_worker_on_an_empty_volume_seeds_the_images_libraries(
    settings: Settings, tmp_path: Path
) -> None:
    """Final review I1: as the API's boot does, so a BOSL2 render needs no network."""
    commit = "f47030c41d88d0676bca73be1c6b7ba58564f9dd"
    seed = tmp_path / "image-libraries"
    (seed / "BOSL2" / commit / "BOSL2").mkdir(parents=True)
    (seed / "BOSL2" / commit / "BOSL2" / "std.scad").write_text("// std\n")
    data = tmp_path / "worker-data"
    cfg = settings.model_copy(update={"data_dir": data, "seed_libraries_dir": seed})
    deps, store = worker_module.build_worker_deps(cfg)
    try:
        checkout = deps.paths.libraries / "BOSL2" / commit / "BOSL2" / "std.scad"
        assert checkout.read_text() == "// std\n"
    finally:
        await store.aclose()
        deps.projection.close()
