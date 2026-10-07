"""`python -m scadbuddy.worker`: the render worker as a process, over a dev server."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, cast

import httpx
import psycopg
import pytest
from temporalio import workflow
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client
from temporalio.common import VersioningBehavior
from temporalio.service import RPCError, RPCStatusCode
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
from scadbuddy.library.assets import AssetStore
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.settings_store import RenderStoreSettings, StoreNotReadyError
from scadbuddy.render.job_models import Job
from scadbuddy.render.projection import JobProjection, workflow_id_for
from scadbuddy.render.solids import WRAPPER_PREFIX
from scadbuddy.store import BlobRefs
from scadbuddy.store.assets import RemoteAssets
from scadbuddy.store.bambuddy import RenderSettingsSource
from scadbuddy.store.cache import CachedBlobStore
from scadbuddy.store.content import ContentStore
from scadbuddy.store.factory import StoreBundle
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.worker import (
    MAKE_CURRENT_BACKOFF,
    MAKE_CURRENT_EVERY,
    _drain,
    _on_signal,
    _poll,
    make_current_until_polled,
    run_inprocess_worker,
    run_worker,
)
from scadbuddy.workflows.activities import WorkerDeps
from scadbuddy.workflows.client import (
    DEPLOYMENT_NAME,
    connect_lazily,
    drained,
    is_current,
    make_current,
)
from scadbuddy.workflows.models import piece_key
from scadbuddy.workflows.pipelines import TemplatePipeline
from tests.conftest import (
    UNUSED_DATABASE_URL,
    UNUSED_TEMPORAL_ADDRESS,
    PgPool,
    fake_3mf_openscad,
)
from tests.support.renders import render_to_end, start_of
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
                            "settings_current": True,
                        },
                    }

                    job = await render_to_end(client, queue, job)
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


async def _never() -> bool:
    return False


async def test_the_drain_polls_until_the_version_is_drained() -> None:
    answers = iter([False, False, True])
    calls = 0

    async def drained() -> bool:
        nonlocal calls
        calls += 1
        return next(answers)

    assert await _drain(_never, drained, timeout=5, poll=0.01, grace=0) == "drained"
    assert calls == 3


async def test_the_drain_gives_up_at_its_bound() -> None:
    calls = 0

    async def drained() -> bool:
        nonlocal calls
        calls += 1
        return False

    outcome = await asyncio.wait_for(_drain(_never, drained, timeout=0.1, poll=0.01, grace=0), 5)
    assert outcome == "timed_out" and calls > 1


async def _always() -> bool:
    return True


async def test_a_build_that_is_still_current_stops_after_the_grace() -> None:
    """#874: a same-build restart: another pod of this build serves its pinned runs.
    Until the grace has passed it keeps serving them itself, in case none comes."""
    loop = asyncio.get_running_loop()
    began = loop.time()
    outcome = await asyncio.wait_for(_drain(_always, _never, timeout=5, poll=0.01, grace=0.2), 5)
    assert outcome == "current"  # told apart from "drained" in the log
    assert loop.time() - began >= 0.2


async def test_a_still_current_build_whose_runs_finish_in_the_grace_is_drained() -> None:
    answers = iter([False, True])

    async def drained() -> bool:
        return next(answers)

    assert await _drain(_always, drained, timeout=5, poll=0.01, grace=5) == "drained"


async def test_the_drain_ends_when_the_build_becomes_current_again() -> None:
    answers = iter([False, False, True])

    async def still_current() -> bool:
        return next(answers)

    assert await _drain(still_current, _never, timeout=5, poll=0.01, grace=0) == "current"


async def test_a_stop_now_cuts_the_drain_short() -> None:
    """#605: a second SIGTERM ends the drain, even while a count hangs."""
    stop_now = asyncio.Event()

    async def hangs() -> bool:
        await asyncio.Event().wait()
        raise AssertionError

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop_now.set()

    stopping = asyncio.create_task(stop_soon())
    outcome = await asyncio.wait_for(
        _drain(_never, hangs, timeout=60, poll=0.01, grace=0, stop_now=stop_now), 5
    )
    await stopping
    assert outcome == "stopped"


async def test_the_second_signal_stops_now() -> None:
    stop, stop_now = asyncio.Event(), asyncio.Event()
    handler = _on_signal(stop, stop_now)
    handler()
    assert stop.is_set() and not stop_now.is_set()
    handler()
    assert stop_now.is_set()


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
        # As the worker does: Temporal 1.28 takes the build only once it polls.
        assert await make_current_until_polled(
            lambda: make_current(client, namespace=client.namespace, build_id=build_id),
            build_id=build_id,
            backoff=(0.1,),
            every=0.2,
            deadline=30,
        )
        handle = await client.start_workflow(
            _BlocksUntilReleased.run, id=f"blocks-{uuid.uuid4().hex}", task_queue=queue
        )
        await _until_drained_is(False, client, build_id)

        await handle.signal(_BlocksUntilReleased.release)
        await asyncio.wait_for(handle.result(), 30)
        await _until_drained_is(True, client, build_id)


@pytest.mark.requires_temporal
async def test_is_current_names_the_deployments_current_build() -> None:
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
        assert await make_current_until_polled(
            lambda: make_current(client, namespace=client.namespace, build_id=build_id),
            build_id=build_id,
            backoff=MAKE_CURRENT_BACKOFF,
            every=MAKE_CURRENT_EVERY,
            deadline=60,
        )
        assert await _is_current_answer(client, build_id)
        assert not await _is_current_answer(client, "other")


#: `is_current`'s attempts and the longest wait between two (`_is_current_answer`).
IS_CURRENT_ATTEMPTS = 12
IS_CURRENT_MAX_WAIT = 5.0


async def _is_current_answer(client: Client, build_id: str) -> bool:
    """`is_current`, asked again while Temporal cannot answer yet. Just after a build is
    made current, the deployment's own workflow is still propagating it (over 13 s on a
    loaded host), and a describe meanwhile times out or is refused as "too many
    requests"; the worker's `still_current` reads either as not current. Counted
    attempts, not a deadline: this host's clocks step."""
    for attempt in range(IS_CURRENT_ATTEMPTS):
        try:
            return await is_current(client, namespace=client.namespace, build_id=build_id)
        except RPCError:
            if attempt == IS_CURRENT_ATTEMPTS - 1:
                raise
            await asyncio.sleep(min(2.0**attempt * 0.5, IS_CURRENT_MAX_WAIT))
    raise AssertionError("unreachable")


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
        """Only what the pipeline's first activities need: `render_accept`'s insert,
        then the `project` activity's `mark_started`, which proves the worker polled
        and ran it."""

        def accept(self, job: Job, key: str, **_: object) -> Job:
            return job

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
            start_of(job),
            id=workflow_id_for(job.id),
            task_queue=settings.temporal_task_queue_render,
        )
        try:
            assert await asyncio.wait_for(asyncio.to_thread(marked.wait, 60), 65)
            assert len(started) == 1
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


# ── making the build current (Temporal 1.28 needs a poller first) ─────────────


def _not_found() -> RPCError:
    """What Temporal 1.28 answers until the build's first poll: it ignores
    `allow_no_pollers`."""
    return RPCError(
        f"workflow not found for ID: temporal-sys-worker-deployment:{DEPLOYMENT_NAME}",
        RPCStatusCode.NOT_FOUND,
        b"",
    )


async def _pollers(client: Client, queue: str) -> int:
    described = await client.workflow_service.describe_task_queue(
        DescribeTaskQueueRequest(
            namespace=client.namespace,
            task_queue=TaskQueue(name=queue),
            task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
        )
    )
    return len(described.pollers)


def _in_process_settings(tmp_path: Path) -> tuple[Settings, WorkerDeps]:
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
    return settings, deps


async def test_making_the_build_current_retries_until_the_server_takes_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = 0

    async def set_current() -> None:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise _not_found()

    with caplog.at_level(logging.INFO, logger="scadbuddy.worker"):
        made = await asyncio.wait_for(
            make_current_until_polled(
                set_current, build_id="b", backoff=(0.01, 0.01), every=0.01, deadline=5
            ),
            5,
        )

    assert made
    assert calls == 3
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.__dict__["attempt"] for r in warnings] == [1, 2]
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.INFO] == [
        "made this build current"
    ]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.requires_temporal
async def test_a_build_never_made_current_keeps_the_worker_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    attempts = 0

    async def refuses(*_: object, **__: object) -> None:
        nonlocal attempts
        attempts += 1
        raise _not_found()

    monkeypatch.setattr(worker_module, "make_current", refuses)
    monkeypatch.setattr(worker_module, "MAKE_CURRENT_BACKOFF", (0.01, 0.02))
    monkeypatch.setattr(worker_module, "MAKE_CURRENT_EVERY", 0.05)
    monkeypatch.setattr(worker_module, "MAKE_CURRENT_DEADLINE", 0.3)
    settings, deps = _in_process_settings(tmp_path)
    stop = asyncio.Event()

    def errors() -> list[logging.LogRecord]:
        return [r for r in caplog.records if r.levelno >= logging.ERROR]

    async with temporal_client() as client:
        with caplog.at_level(logging.WARNING, logger="scadbuddy.worker"):
            polling = asyncio.create_task(_poll(settings, deps, client, stop, drain=False))
            try:
                async with asyncio.timeout(30):
                    while not errors():
                        assert not polling.done(), polling.result()
                        await asyncio.sleep(0.02)
                # Gave up, and the worker polls on: Temporal sees its poller.
                async with asyncio.timeout(10):
                    while not await _pollers(client, settings.temporal_task_queue_render):
                        await asyncio.sleep(0.1)
                assert not polling.done()
            finally:
                stop.set()
                await asyncio.wait_for(polling, 30)

    assert attempts >= 3
    [error] = errors()
    assert error.getMessage() == "could not make this build current; polling anyway"
    assert error.__dict__["attempts"] == attempts
    assert "workflow not found" in error.__dict__["error"]


@pytest.mark.requires_temporal
async def test_stopping_the_worker_cancels_the_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    async def refuses(*_: object, **__: object) -> None:
        nonlocal attempts
        attempts += 1
        raise _not_found()

    monkeypatch.setattr(worker_module, "make_current", refuses)
    settings, deps = _in_process_settings(tmp_path)
    stop = asyncio.Event()
    async with temporal_client() as client:
        polling = asyncio.create_task(_poll(settings, deps, client, stop, drain=False))
        try:
            async with asyncio.timeout(30):
                while attempts == 0:
                    await asyncio.sleep(0.02)
        finally:
            stop.set()
            # The retry would run for a minute; stop cancels it.
            await asyncio.wait_for(polling, 10)
    assert attempts == 1


async def test_the_workers_housekeeping_runs_each_interval_and_survives_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def housekeep(deps: WorkerDeps) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("a transient disk error")

    monkeypatch.setattr(worker_module, "_housekeep", housekeep)
    running = asyncio.create_task(
        worker_module._housekeep_periodically(cast(WorkerDeps, None), 0.01)
    )
    try:
        async with asyncio.timeout(5):
            while calls < 3:
                await asyncio.sleep(0.01)
    finally:
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running
    assert calls >= 3


SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="4" height="4"/></svg>'


def _own_volume_deps(tmp_path: Path, pg_pool: PgPool) -> WorkerDeps:
    paths = DataPaths(tmp_path / "worker")
    paths.ensure()
    return WorkerDeps(
        config=Config(data_dir=paths.root),
        paths=paths,
        assets=AssetStore(paths.assets, pg_pool),
        blobs=LocalBlobStore(paths.blobs),
        refs=cast(BlobRefs, None),
        projection=cast(JobProjection, None),
        remote_assets=cast(RemoteAssets, object()),
    )


def _aged(*paths: Path, days: float = 7) -> None:
    old = time.time() - days * 86400
    for path in paths:
        os.utime(path, (old, old))


def _last_used_long_ago(pg_pool: PgPool, asset_id: str, days: float = 7) -> None:
    """The shared row's last use (#591): what a worker's prune goes by."""
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE assets SET last_used_at = %s WHERE id = %s",
            (datetime.now(UTC) - timedelta(days=days), asset_id),
        )


def _rows(pg_pool: PgPool) -> list[str]:
    with pg_pool.connection() as conn:
        return sorted(row["id"] for row in conn.execute("SELECT id FROM assets"))


def test_one_housekeeping_pass_prunes_old_exports_and_uploads_and_keeps_fresh_ones(
    tmp_path: Path, pg_pool: PgPool
) -> None:
    """Final review I2: what a worker fetched is pruned on its own volume, by last use.
    Its files only: the rows are the API's (#591), shared, and the API's sweep decides."""
    deps = _own_volume_deps(tmp_path, pg_pool)
    old_export = deps.paths.model_revision_dir("demo", "a" * 40)
    fresh_export = deps.paths.model_revision_dir("demo", "b" * 40)
    for export in (old_export, fresh_export):
        export.mkdir(parents=True)
        (export / "model.scad").write_text("cube(1);")
    _aged(old_export)
    old = deps.assets.put(SVG, "old.svg")
    fresh = deps.assets.put(SVG.replace(b'"4"', b'"5"'), "fresh.svg")
    _aged(deps.assets.blob_path(old))
    _last_used_long_ago(pg_pool, old.id)

    worker_module._housekeep(deps)

    assert not old_export.exists() and fresh_export.is_dir()
    assert deps.assets.ids() == [fresh.id]
    assert _rows(pg_pool) == sorted([old.id, fresh.id])


def test_housekeeping_leaves_a_volume_shared_with_the_api_alone(
    tmp_path: Path, pg_pool: PgPool
) -> None:
    """A worker that still mounts the API's /data holds none of the references: its
    uploads and exports are the API's to sweep."""
    deps = _own_volume_deps(tmp_path, pg_pool)
    deps.paths.model_dir("demo").mkdir(parents=True)  # the API's templates are here
    export = deps.paths.model_revision_dir("demo", "a" * 40)
    export.mkdir(parents=True)
    _aged(export)
    upload = deps.assets.put(SVG, "old.svg")
    _aged(deps.assets.blob_path(upload))
    _last_used_long_ago(pg_pool, upload.id)

    worker_module._housekeep(deps)

    assert export.is_dir()
    assert deps.assets.ids() == [upload.id]


@pytest.mark.parametrize(("sweep", "interval"), [(0.0, 300.0), (60.0, 60.0)])
async def test_a_piece_cache_is_evicted_even_with_the_upload_sweep_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sweep: float, interval: float
) -> None:
    """SCADBUDDY_ASSET_SWEEP_INTERVAL=0 turns the upload sweep off, not the worker's
    housekeeping (its cache eviction), which then runs every WORKER_CACHE_EVICT_INTERVAL."""
    intervals: list[float] = []

    async def housekeep(_deps: object, every: float) -> None:
        intervals.append(every)

    monkeypatch.setattr(worker_module, "_housekeep_periodically", housekeep)
    cache = CachedBlobStore.__new__(CachedBlobStore)
    task = worker_module._start_housekeeping(cast(WorkerDeps, SimpleNamespace(blobs=cache)), sweep)
    assert task is not None
    await task
    assert intervals == [interval]
    local = cast(WorkerDeps, SimpleNamespace(blobs=LocalBlobStore(tmp_path)))
    assert worker_module._start_housekeeping(local, sweep) is None
    assert intervals == [interval]


class _Source:
    fresh = True

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
        Settings(
            data_dir=tmp_path,
            database_url=UNUSED_DATABASE_URL,
            temporal_address=UNUSED_TEMPORAL_ADDRESS,
        ),
        Metrics(),
        store,
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


async def test_the_worker_builds_its_deps_and_seeds_off_the_loop(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#674 gate: the seed copies trees, so it runs in a thread, as the API's does."""
    loops: list[asyncio.AbstractEventLoop | None] = []

    def build(_settings: Settings) -> NoReturn:
        try:
            loops.append(asyncio.get_running_loop())
        except RuntimeError:
            loops.append(None)  # no loop in this thread: off the loop, as it should be
        raise StoreNotReadyError("stop here")

    monkeypatch.setattr(worker_module, "build_worker_deps", build)
    with pytest.raises(StoreNotReadyError):
        await worker_module.run_worker(settings, health_port=None)
    assert loops == [None]


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
