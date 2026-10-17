"""`python -m scadbuddy.worker`: the render worker as a process, over a dev server."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, cast, get_args

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
    MAKE_CURRENT_ERROR_AFTER,
    MAKE_CURRENT_EVERY,
    _drain,
    _health_server,
    _on_signal,
    _poll,
    deployment_writes,
    make_current_until_polled,
    run_inprocess_worker,
    run_worker,
)
from scadbuddy.workflows.activities import WorkerDeps
from scadbuddy.workflows.client import (
    DEPLOYMENT_NAME,
    connect_lazily,
    current_routing,
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


async def test_the_health_server_serves_on_the_socket_it_is_handed(
    settings: Settings, health_socket: socket.socket
) -> None:
    """#3807: on a socket already bound, not a port it binds again later, so nothing
    else can take the port in between."""
    server = _health_server(settings, Metrics(), None, health_socket, "q")
    serving = asyncio.create_task(server.serve())
    try:
        port = health_socket.getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            assert await _healthy(http, serving) == {
                "ok": True,
                "build_id": settings.revision,
                "task_queue": "q",
            }
    finally:
        server.should_exit = True
        await serving


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
    settings: Settings,
    paths: DataPaths,
    model: str,
    pg_conninfo: str,
    tmp_path: Path,
    health_socket: socket.socket,
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
    port = health_socket.getsockname()[1]
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
                run_worker(cfg, stop=stop, health_port=health_socket, client=client)
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
    assert samples['scadbuddy_render_duration_seconds_count{kind="render",outcome="done"}'] == 1
    assert samples['scadbuddy_render_job_latency_seconds_count{kind="render",outcome="done"}'] == 1
    assert samples['scadbuddy_render_jobs_finished_total{kind="render",outcome="done"}'] == 1
    for stage in ("source", "render", "split", "solids", "thumbnail", "write"):
        assert samples[f'scadbuddy_render_stage_seconds_count{{stage="{stage}"}}'] == 1, stage
    # A model with no scene.json (not a mesh model) never runs the paint stage.
    assert samples.get('scadbuddy_render_stage_seconds_count{stage="paint"}', 0) == 0
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


def test_the_projects_queue_is_a_choice() -> None:
    assert worker_module.parse_queue(["--queue", "projects"]) == "projects"


async def test_the_projects_queue_runs_the_projects_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[str] = []

    async def run(settings: Settings, **kwargs: Any) -> None:
        ran.append("projects")

    monkeypatch.setattr(worker_module, "run_projects_worker", run)
    await worker_module._main(cast(Settings, None), "projects")
    assert ran == ["projects"]


async def test_the_projects_worker_refuses_to_start_without_a_key(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        database_url=UNUSED_DATABASE_URL,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        secret_key_file=None,
    )
    with pytest.raises(worker_module.ProjectsKeyMissingError, match="SCADBUDDY_SECRET_KEY_FILE"):
        await worker_module.run_projects_worker(settings, health_port=None)


async def test_the_projects_worker_refuses_to_start_without_the_api_url(tmp_path: Path) -> None:
    key = tmp_path / "kek"
    key.write_text(base64.b64encode(b"\x07" * 32).decode())
    settings = Settings(
        data_dir=tmp_path,
        database_url=UNUSED_DATABASE_URL,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        secret_key_file=key,
        api_internal_url=None,
    )
    with pytest.raises(worker_module.ApiUrlMissingError, match="--queue projects"):
        await worker_module.run_projects_worker(settings, health_port=None)


@pytest.mark.parametrize("queue", ["render", "bambuddy"])
async def test_both_workers_are_handed_the_second_signal(
    queue: worker_module.Queue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both workers get the second signal, which cuts a drain short."""
    handed: dict[str, Any] = {}

    async def run(settings: Settings, **kwargs: Any) -> None:
        handed.update(kwargs)

    monkeypatch.setattr(worker_module, "run_worker", run)
    monkeypatch.setattr(worker_module, "run_print_worker", run)
    await worker_module._main(cast(Settings, None), queue)
    assert isinstance(handed["stop_now"], asyncio.Event)


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
        await asyncio.wait_for(
            make_current_until_polled(
                lambda: make_current(client, namespace=client.namespace, build_id=build_id),
                build_id=build_id,
                backoff=(0.1,),
                every=0.2,
                error_after=30,
            ),
            30,
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
        await asyncio.wait_for(
            make_current_until_polled(
                lambda: make_current(client, namespace=client.namespace, build_id=build_id),
                build_id=build_id,
                backoff=MAKE_CURRENT_BACKOFF,
                every=MAKE_CURRENT_EVERY,
                error_after=MAKE_CURRENT_ERROR_AFTER,
            ),
            MAKE_CURRENT_WITHIN,
        )
        assert await _is_current_answer(client, build_id)
        assert not await _is_current_answer(client, "other")


async def _write_refusal(
    write: Callable[[], Awaitable[None]],
    routing: Callable[[], Awaitable[str | None]],
    build_id: str,
    *,
    attempts: int | None = None,
    max_wait: float | None = None,
) -> RPCStatusCode | None:
    """A deployment write's refusal, or None when it is accepted; asked again while the
    server is busy, the way `_is_current_answer` is, so a transient error is never read as
    the conflict (or as the acceptance) the test is about (#3293).

    A transient error can come after the server applied the write: sent again, the same
    token is stale by then and refused. So before each retry the routing is read, and a
    build already current is the write accepted. A server busy through every attempt is
    said as such, not returned as a status that reads like the wrong refusal.

    That check is best-effort. Just after a write the deployment's own workflow is still
    propagating it (`_routing_once_current`), so the routing is read a few times before
    the retry; a read the busy server refuses counts as "not known current" (#3293)."""
    attempts = IS_CURRENT_ATTEMPTS if attempts is None else attempts
    max_wait = IS_CURRENT_MAX_WAIT if max_wait is None else max_wait
    assert attempts >= 1, f"a write takes at least one attempt, not {attempts}"
    every = min(0.25, max_wait)
    for attempt in range(attempts):
        if attempt > 0 and await _known_current(routing, build_id, every=every):
            return None
        try:
            await write()
        except RPCError as error:
            if error.status not in IS_CURRENT_TRANSIENT:
                return error.status
            if attempt == attempts - 1:
                # The last write may have been applied before its answer was lost, as
                # any other may (#3479 review): read once more before saying busy.
                if await _known_current(routing, build_id, every=every):
                    return None
                raise AssertionError(
                    f"the server stayed busy through {attempts} attempts ({error.status.name})"
                ) from error
            await asyncio.sleep(min(2.0**attempt * 0.5, max_wait))
        else:
            return None
    raise AssertionError("unreachable")


async def _known_current(
    routing: Callable[[], Awaitable[str | None]],
    build_id: str,
    *,
    reads: int | None = None,
    every: float = 0.25,
) -> bool:
    """Whether ``build_id`` is seen current within ``reads`` reads of the routing. A
    transient refusal of a read is a read that saw nothing; any other error is raised."""
    for read in range(ROUTING_READS if reads is None else reads):
        if read > 0:
            await asyncio.sleep(every)
        try:
            if await routing() == build_id:
                return True
        except RPCError as error:
            if error.status not in IS_CURRENT_TRANSIENT:
                raise
    return False


async def _routing_once_current(
    client: Client, deployment: str, build_id: str
) -> tuple[str | None, bytes]:
    """`current_routing` once it names ``build_id``: just after a write the deployment's
    own workflow is still propagating it (see `_is_current_answer`)."""
    for attempt in range(IS_CURRENT_ATTEMPTS):
        try:
            build, token = await current_routing(
                client, namespace=client.namespace, deployment_name=deployment
            )
        except RPCError as error:
            if error.status not in IS_CURRENT_TRANSIENT:
                raise
        else:
            if build == build_id:
                return build, token
        await asyncio.sleep(min(2.0**attempt * 0.5, IS_CURRENT_MAX_WAIT))
    raise AssertionError(f"{build_id} never became current")


@pytest.mark.requires_temporal
async def test_temporal_refuses_a_write_with_a_stale_conflict_token() -> None:
    """#2538: what `deployment_writes` relies on. A token read before another build was
    made current is refused; the token read since is accepted."""
    deployment = f"d-{uuid.uuid4().hex[:8]}"
    old, new = f"old-{uuid.uuid4().hex[:8]}", f"new-{uuid.uuid4().hex[:8]}"

    def versioned(client: Client, build_id: str) -> Worker:
        return Worker(
            client,
            task_queue=f"t-{uuid.uuid4().hex[:8]}",
            workflows=[_BlocksUntilReleased],
            workflow_runner=UnsandboxedWorkflowRunner(),
            deployment_config=WorkerDeploymentConfig(
                version=WorkerDeploymentVersion(deployment_name=deployment, build_id=build_id),
                use_worker_versioning=True,
                default_versioning_behavior=VersioningBehavior.PINNED,
            ),
        )

    async def made_current(client: Client, build_id: str, token: bytes = b"") -> None:
        await make_current(
            client,
            namespace=client.namespace,
            build_id=build_id,
            deployment_name=deployment,
            conflict_token=token,
        )

    async def polled_then_current(client: Client, build_id: str) -> bytes:
        await asyncio.wait_for(
            make_current_until_polled(
                lambda: made_current(client, build_id),
                build_id=build_id,
                backoff=MAKE_CURRENT_BACKOFF,
                every=MAKE_CURRENT_EVERY,
                error_after=MAKE_CURRENT_ERROR_AFTER,
            ),
            MAKE_CURRENT_WITHIN,
        )
        return (await _routing_once_current(client, deployment, build_id))[1]

    async def write_refusal(client: Client, build_id: str, token: bytes) -> RPCStatusCode | None:
        async def routing() -> str | None:
            return (
                await current_routing(
                    client, namespace=client.namespace, deployment_name=deployment
                )
            )[0]

        return await _write_refusal(
            lambda: made_current(client, build_id, token), routing, build_id
        )

    async with temporal_client() as client, versioned(client, old), versioned(client, new):
        stale = await polled_then_current(client, old)
        fresh = await polled_then_current(client, new)

        assert await write_refusal(client, old, stale) == STALE_TOKEN_REFUSAL
        assert (await _routing_once_current(client, deployment, new))[0] == new
        assert await write_refusal(client, old, fresh) is None
        await _routing_once_current(client, deployment, old)


#: How long a healthy worker start takes to make its build current, with room: the
#: test's own bound, not the worker's logging policy (`MAKE_CURRENT_ERROR_AFTER`).
MAKE_CURRENT_WITHIN = 60.0
#: `is_current`'s attempts and the longest wait between two (`_is_current_answer`).
IS_CURRENT_ATTEMPTS = 12
IS_CURRENT_MAX_WAIT = 5.0
#: How Temporal refuses a deployment write carrying a conflict token read before another
#: write (#2538): what `deployment_writes` relies on.
STALE_TOKEN_REFUSAL = RPCStatusCode.FAILED_PRECONDITION
#: How many times `_write_refusal` reads the routing before it resends a write.
ROUTING_READS = 3
#: What a busy server answers meanwhile; anything else is a real error, raised at once.
IS_CURRENT_TRANSIENT = frozenset(
    {
        RPCStatusCode.DEADLINE_EXCEEDED,
        RPCStatusCode.RESOURCE_EXHAUSTED,
        RPCStatusCode.UNAVAILABLE,
    }
)


async def _is_current_answer(client: Client, build_id: str) -> bool:
    """`is_current`, asked again while Temporal cannot answer yet. Just after a build is
    made current, the deployment's own workflow is still propagating it (over 13 s on a
    loaded host), and a describe meanwhile times out or is refused as "too many
    requests"; the worker's `still_current` reads either as not current. Counted
    attempts, not a deadline: this host's clocks step."""
    for attempt in range(IS_CURRENT_ATTEMPTS):
        try:
            return await is_current(client, namespace=client.namespace, build_id=build_id)
        except RPCError as error:
            if error.status not in IS_CURRENT_TRANSIENT or attempt == IS_CURRENT_ATTEMPTS - 1:
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
async def test_the_worker_names_its_image_and_openscad_for_every_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What `OutputRecord.image_revision` / `openscad_version` read (§8.4)."""

    async def never(*_: object, **__: object) -> bool:
        raise AssertionError("the in-process worker must not drain")

    monkeypatch.setattr(worker_module, "drained", never)
    openscad = tmp_path / "openscad"
    openscad.write_text("#!/bin/sh\necho 'OpenSCAD version 2026.09.28' >&2\n")
    openscad.chmod(0o755)
    settings = Settings(
        database_url=UNUSED_DATABASE_URL,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        data_dir=tmp_path,
        revision=f"test-{uuid.uuid4().hex[:8]}",
        temporal_task_queue_render=f"t-{uuid.uuid4().hex[:8]}",
    )
    deps = WorkerDeps(
        config=Config(openscad=str(openscad), data_dir=tmp_path),
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
    assert deps.revision == settings.revision
    assert deps.openscad_version == "OpenSCAD version 2026.09.28"


@pytest.mark.requires_temporal
async def test_a_worker_given_the_apis_openscad_version_does_not_run_openscad_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-process worker takes the API's probe (`worker_deps_from_state`): a second
    `openscad --version`, in a task beside the API's first requests, would race them."""

    async def never(*_: object, **__: object) -> bool:
        raise AssertionError("the in-process worker must not drain")

    monkeypatch.setattr(worker_module, "drained", never)
    ran = tmp_path / "ran"
    openscad = tmp_path / "openscad"
    openscad.write_text(f"#!/bin/sh\ntouch {ran}\necho 'OpenSCAD version 2099.01.01' >&2\n")
    openscad.chmod(0o755)
    settings = Settings(
        database_url=UNUSED_DATABASE_URL,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        data_dir=tmp_path,
        revision=f"test-{uuid.uuid4().hex[:8]}",
        temporal_task_queue_render=f"t-{uuid.uuid4().hex[:8]}",
    )
    deps = WorkerDeps(
        config=Config(openscad=str(openscad), data_dir=tmp_path),
        paths=DataPaths(tmp_path),
        assets=None,  # type: ignore[arg-type]
        blobs=None,  # type: ignore[arg-type]
        refs=None,  # type: ignore[arg-type]
        projection=None,  # type: ignore[arg-type]
        openscad_version="OpenSCAD version 2026.09.28",
    )
    stop = asyncio.Event()
    stop.set()
    async with temporal_client() as client:
        await asyncio.wait_for(_poll(settings, deps, client, stop, drain=False), 30)
    assert deps.openscad_version == "OpenSCAD version 2026.09.28"
    assert not ran.exists()


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
        await asyncio.wait_for(
            make_current_until_polled(
                set_current, build_id="b", backoff=(0.01, 0.01), every=0.01, error_after=5
            ),
            5,
        )

    assert calls == 3
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.__dict__["attempt"] for r in warnings] == [1, 2]
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.INFO] == [
        "made this build current"
    ]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_making_the_build_current_does_not_give_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Past ``error_after`` it logs errors and keeps trying, and still takes (#2341): a
    worker that gave up left the deployment's current build with no poller."""
    calls = 0

    async def set_current() -> None:
        nonlocal calls
        calls += 1
        if calls <= 20:
            raise _not_found()

    with caplog.at_level(logging.INFO, logger="scadbuddy.worker"):
        await asyncio.wait_for(
            make_current_until_polled(
                set_current, build_id="b", backoff=(0.01,), every=0.01, error_after=0.05
            ),
            5,
        )

    assert calls == 21
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors
    assert {r.getMessage() for r in errors} == {"could not make this build current yet"}
    assert caplog.records[-1].getMessage() == "made this build current"


async def _no_current_build(*_: object, **__: object) -> tuple[str | None, bytes]:
    return None, b""


async def test_a_superseded_build_stops_trying_to_be_current(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#2538: an old pod still retrying when the next rollout's worker makes its build
    current stops, rather than win the last write and skip its drain on SIGTERM."""
    calls = 0
    currents = iter(["old", "old", "new"])

    async def set_current() -> None:
        nonlocal calls
        calls += 1
        raise _not_found()

    async def current() -> str | None:
        return next(currents)

    with caplog.at_level(logging.INFO, logger="scadbuddy.worker"):
        await asyncio.wait_for(
            make_current_until_polled(
                set_current,
                build_id="mine",
                backoff=(0.01,),
                every=0.01,
                error_after=5,
                current=current,
            ),
            5,
        )
    # Read at the start ("old"), before attempt 2 ("old"), before attempt 3 ("new").
    assert calls == 2
    assert caplog.records[-1].getMessage() == (
        "another build was made current; this one no longer tries to be"
    )


async def test_a_build_already_current_or_none_keeps_trying() -> None:
    """Only a third build stops it: none yet, or this build, or the one current when it
    began are what a fresh rollout sees."""
    calls = 0
    currents = iter([None, None, "mine", "old-is-none"])

    async def set_current() -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _not_found()

    async def current() -> str | None:
        return next(currents)

    await asyncio.wait_for(
        make_current_until_polled(
            set_current,
            build_id="mine",
            backoff=(0.01,),
            every=0.01,
            error_after=5,
            current=current,
        ),
        5,
    )
    assert calls == 3


class _Deployment:
    """A workflow service whose deployment a newer rollout changes between reads."""

    def __init__(self) -> None:
        self.tokens = iter([b"t1", b"t2"])
        self.builds = iter(["old", "new"])
        self.written: list[bytes] = []

    async def describe_worker_deployment(self, *_: object, **__: object) -> object:
        build_id = next(self.builds)
        return SimpleNamespace(
            conflict_token=next(self.tokens),
            worker_deployment_info=SimpleNamespace(
                routing_config=SimpleNamespace(
                    current_deployment_version=SimpleNamespace(build_id=build_id)
                )
            ),
        )

    async def set_worker_deployment_current_version(self, request: Any, **_: object) -> None:
        self.written.append(request.conflict_token)
        # The deployment changed since that token's read.
        raise RPCError("conflict token mismatch", RPCStatusCode.FAILED_PRECONDITION, b"")


async def test_each_write_carries_its_reads_conflict_token() -> None:
    """#2538: a rollout landing between the read and the write makes Temporal refuse the
    write (its token is stale), and the next read sees the new build and stops."""
    service = _Deployment()
    client = cast(Client, SimpleNamespace(namespace="default", workflow_service=service))
    set_current, read_current = deployment_writes(client, "mine", DEPLOYMENT_NAME)
    await asyncio.wait_for(
        make_current_until_polled(
            set_current,
            build_id="mine",
            backoff=(0.01,),
            every=0.01,
            error_after=5,
            current=read_current,
        ),
        5,
    )
    assert service.written == [b"t1"]


async def test_a_refusal_past_error_after_retries_once_per_error_every(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#2538: a persistent failure logs an ERROR at most every ``error_every``, not
    every ``every``, for the pod's whole life."""

    async def set_current() -> None:
        raise _not_found()

    with (
        caplog.at_level(logging.WARNING, logger="scadbuddy.worker"),
        pytest.raises(TimeoutError),
    ):
        await asyncio.wait_for(
            make_current_until_polled(
                set_current,
                build_id="b",
                backoff=(0.01,),
                every=0.01,
                error_after=0.05,
                error_every=0.5,
            ),
            1.2,
        )
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    # Without the back-off, ~100 errors in that time.
    assert 1 <= len(errors) <= 4


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
    # No other build takes over meanwhile, whatever another test makes current.
    monkeypatch.setattr(worker_module, "current_routing", _no_current_build)
    monkeypatch.setattr(worker_module, "MAKE_CURRENT_BACKOFF", (0.01, 0.02))
    monkeypatch.setattr(worker_module, "MAKE_CURRENT_EVERY", 0.05)
    monkeypatch.setattr(worker_module, "MAKE_CURRENT_ERROR_AFTER", 0.3)
    monkeypatch.setattr(worker_module, "MAKE_CURRENT_ERROR_EVERY", 0.05)
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
                # Still refused, and the worker polls on: Temporal sees its poller.
                async with asyncio.timeout(10):
                    while not await _pollers(client, settings.temporal_task_queue_render):
                        await asyncio.sleep(0.1)
                # And it keeps trying rather than giving up (#2341).
                seen = attempts
                async with asyncio.timeout(10):
                    while attempts == seen:
                        await asyncio.sleep(0.02)
                assert not polling.done()
            finally:
                stop.set()
                await asyncio.wait_for(polling, 30)

    assert attempts >= 3
    error = errors()[0]
    assert error.getMessage() == "could not make this build current yet"
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
    # No other build takes over meanwhile, whatever another test makes current.
    monkeypatch.setattr(worker_module, "current_routing", _no_current_build)
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
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "left-over").write_bytes(b"x" * 100_000)
    os.link(tmp_path / "scratch" / "left-over", tmp_path / "scratch" / "linked")
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
        "render",
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
    # The whole data directory, as allocated on disk: the piece cache and the scratch
    # file beside it, which the cache gauge alone would miss (#1785).
    # As du counts it: directories too, and the linked file once.
    du = subprocess.run(
        ["du", "-s", "-B1", str(tmp_path)], capture_output=True, text=True, check=True
    )
    allocated = int(du.stdout.split()[0])
    assert samples["scadbuddy_render_data_bytes"] == allocated >= 100_000


async def test_the_data_directory_is_measured_when_the_database_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The eviction gauge needs no database, so an outage does not freeze it (#1785)."""

    async def down(_: StoreBundle) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(worker_module, "store_health", down)
    (tmp_path / "scratch").write_bytes(b"x" * 100_000)
    metrics = Metrics()
    store = cast(StoreBundle, SimpleNamespace())
    with pytest.raises(OSError):
        await worker_module._refresh_store_metrics(
            metrics, store, worker_module._DataUsage(tmp_path)
        )
    measured = metrics.registry.get_sample_value("scadbuddy_render_data_bytes")
    assert measured is not None and measured >= 100_000


async def test_the_data_directory_is_walked_again_only_once_its_value_is_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2020: a scrape every 15-30 s does not walk all of /data each time."""
    walks: list[Path] = []
    real = worker_module._allocated_bytes

    def counted(root: Path) -> int:
        walks.append(root)
        return real(root)

    monkeypatch.setattr(worker_module, "_allocated_bytes", counted)
    now = [1000.0]
    data = worker_module._DataUsage(tmp_path, clock=lambda: now[0])
    first = await data.read()
    (tmp_path / "scratch").write_bytes(b"x" * 100_000)

    now[0] += worker_module.DATA_USAGE_MAX_AGE - 1
    assert await data.read() == first
    assert len(walks) == 1

    now[0] += 1
    assert await data.read() >= first + 100_000
    assert len(walks) == 2


def test_a_symlinked_directory_counts_its_own_blocks_not_its_targets(tmp_path: Path) -> None:
    """As du counts it, without -L (#2025)."""
    root = tmp_path / "data"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "big").write_bytes(b"x" * 100_000)
    (root / "link").symlink_to(elsewhere, target_is_directory=True)
    du = subprocess.run(["du", "-s", "-B1", str(root)], capture_output=True, text=True, check=True)
    assert worker_module._allocated_bytes(root) == int(du.stdout.split()[0])


def test_a_refused_store_closes_the_projection_the_worker_opened(
    settings: Settings, pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[JobProjection] = []

    class Recording(JobProjection):
        def open(self, *, migrate_schema: bool = True) -> None:
            assert not migrate_schema  # the worker never migrates (#601)
            super().open(migrate_schema=migrate_schema)
            opened.append(self)

    def refuse(**_: object) -> NoReturn:
        raise StoreNotReadyError("refused")

    monkeypatch.setattr(worker_module, "JobProjection", Recording)
    monkeypatch.setattr(worker_module, "build_store", refuse)
    with pytest.raises(StoreNotReadyError):
        worker_module.build_worker_deps(settings)
    assert len(opened) == 1 and opened[0].pool.closed


async def test_a_worker_stopped_while_the_schema_is_behind_closes_and_returns(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#601: on an unmigrated schema the worker waits for the API rather than
    migrating; a stop ends the wait, and what it opened is closed."""
    opened: list[JobProjection] = []

    class Recording(JobProjection):
        def open(self, *, migrate_schema: bool = True) -> None:
            super().open(migrate_schema=migrate_schema)
            opened.append(self)

    monkeypatch.setattr(worker_module, "JobProjection", Recording)
    stop = asyncio.Event()
    worker = asyncio.create_task(worker_module.run_worker(settings, stop=stop, health_port=None))
    for _ in range(600):
        if opened:
            break
        await asyncio.sleep(0.05)
    assert not worker.done()
    stop.set()
    await asyncio.wait_for(worker, timeout=30)
    assert len(opened) == 1 and opened[0].pool.closed
    with psycopg.connect(settings.database_url) as conn:
        ledger = conn.execute("SELECT to_regclass('scadbuddy_migrations')").fetchone()
    assert ledger == (None,)


async def test_the_worker_builds_its_deps_and_seeds_off_the_loop(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#674 gate: the seed copies trees, so it runs in a thread, as the API's does."""
    loops: list[asyncio.AbstractEventLoop | None] = []

    def build(_settings: Settings, **_: object) -> NoReturn:
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
    settings: Settings, pg_pool: PgPool, tmp_path: Path
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


@pytest.mark.requires_postgres
async def test_the_render_workers_projection_writes_activity(
    settings: Settings, pg_pool: PgPool, tmp_path: Path
) -> None:
    """Without it a render's state changes on a worker are missing from activity until
    the repair sweep."""
    cfg = settings.model_copy(update={"data_dir": tmp_path / "worker-data"})
    deps, store = worker_module.build_worker_deps(cfg)
    try:
        assert deps.projection.activity is not None
    finally:
        await store.aclose()
        deps.projection.close()


@pytest.mark.parametrize(
    ("queue", "service"),
    [
        ("render", "scadbuddy-render"),
        ("bambuddy", "scadbuddy-print"),
        ("projects", "scadbuddy-projects"),
    ],
)
def test_each_worker_reports_under_its_deployments_name(
    monkeypatch: pytest.MonkeyPatch, queue: str, service: str
) -> None:
    """#2292: the render and print workers were one ``scadbuddy-worker`` in Tempo."""
    named: list[str] = []
    monkeypatch.setattr(sys, "argv", ["scadbuddy.worker", "--queue", queue])
    monkeypatch.setattr(worker_module, "configure_tracing", lambda name, **_: named.append(name))
    monkeypatch.setattr(worker_module, "configure_logging", lambda level: None)
    monkeypatch.setattr(
        worker_module,
        "Settings",
        lambda: SimpleNamespace(log_level="INFO", version="v", revision="r"),
    )

    def ran(main: Any) -> None:
        main.close()

    monkeypatch.setattr(asyncio, "run", ran)

    worker_module.main()

    assert named == [service]


def test_every_queue_has_a_service_name() -> None:
    """#2894: a queue added without a ``service.name`` would fail the worker at start
    with a bare KeyError; here it fails in CI instead."""
    assert set(worker_module.SERVICE_NAMES) == set(get_args(worker_module.Queue))


def test_a_queue_without_a_service_name_stops_the_worker_saying_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#3020 review: an explicit exit, not an assert python -O would strip."""
    monkeypatch.setattr(worker_module, "parse_queue", lambda: "unnamed")
    monkeypatch.setattr(worker_module, "configure_logging", lambda level: None)
    monkeypatch.setattr(
        worker_module,
        "Settings",
        lambda: SimpleNamespace(log_level="INFO", version="", revision=""),
    )

    def past_the_guard(*args: object, **__: object) -> None:
        for arg in args:
            if asyncio.iscoroutine(arg):
                arg.close()  # never awaited, so no warning beside the failure (#3491 review)
        raise AssertionError("main() went past the service-name check")

    # A backstop, not the first point of failure: should the check regress, building
    # tracing's arguments likely fails on the missing name first. Either way the test
    # fails rather than starting a worker (#3354, #3491 review).
    monkeypatch.setattr(worker_module, "configure_tracing", past_the_guard)
    monkeypatch.setattr(asyncio, "run", past_the_guard)

    with pytest.raises(SystemExit, match="queue 'unnamed' has no service name"):
        try:
            worker_module.main()
        except KeyError as error:
            # The likelier failure of a regressed check: said as what it means (#3491 review).
            raise AssertionError("main() went past the service-name check") from error


def _transient() -> RPCError:
    return RPCError("busy", RPCStatusCode.UNAVAILABLE, b"")


async def test_a_write_applied_before_a_transient_error_is_accepted_not_refused() -> None:
    """#3293: the server applied the write, then answered UNAVAILABLE. Sent again, the
    same token is stale and refused; the routing read first shows it was accepted."""
    current: list[str | None] = ["new"]
    sent: list[int] = []

    async def write() -> None:
        sent.append(1)
        if len(sent) == 1:
            current[0] = "old"  # applied, then the answer was lost
            raise _transient()
        raise RPCError("stale", RPCStatusCode.FAILED_PRECONDITION, b"")

    async def routing() -> str | None:
        return current[0]

    assert await _write_refusal(write, routing, "old", max_wait=0) is None
    assert len(sent) == 1


async def test_a_server_busy_through_every_attempt_is_said_not_returned_as_a_refusal() -> None:
    """#3293: the last transient status is not handed back as if it were the refusal."""

    async def write() -> None:
        raise _transient()

    async def routing() -> str | None:
        return "new"

    with pytest.raises(AssertionError, match="stayed busy through 3 attempts"):
        await _write_refusal(write, routing, "old", attempts=3, max_wait=0)


async def test_a_conflict_is_returned_as_the_refusal() -> None:
    """#3293: a refusal that is not transient is handed back at once."""

    async def write() -> None:
        raise RPCError("stale", RPCStatusCode.FAILED_PRECONDITION, b"")

    async def routing() -> str | None:
        return "new"

    assert await _write_refusal(write, routing, "old", max_wait=0) == STALE_TOKEN_REFUSAL


async def test_a_last_write_applied_before_its_transient_error_is_accepted() -> None:
    """#3479 review: the last attempt's write can be applied too; it is read before the
    helper says the server stayed busy."""
    current: list[str | None] = ["new"]
    sent: list[int] = []

    async def write() -> None:
        sent.append(1)
        if len(sent) == 2:
            current[0] = "old"  # applied, then the answer was lost
        raise _transient()

    async def routing() -> str | None:
        return current[0]

    assert await _write_refusal(write, routing, "old", attempts=2, max_wait=0) is None
    assert len(sent) == 2


async def test_no_attempts_is_said_as_such() -> None:
    async def write() -> None:
        raise AssertionError("never sent")

    async def routing() -> str | None:
        return None

    with pytest.raises(AssertionError, match="at least one attempt, not 0"):
        await _write_refusal(write, routing, "old", attempts=0, max_wait=0)


async def test_a_busy_routing_read_is_not_known_current_and_the_write_is_resent() -> None:
    """#3293 review: the busy server can refuse the routing read too; that is a read that
    saw nothing, not a failed test."""
    sent: list[int] = []

    async def write() -> None:
        sent.append(1)
        if len(sent) == 1:
            raise _transient()

    async def routing() -> str | None:
        raise _transient()

    assert await _write_refusal(write, routing, "old", max_wait=0) is None
    assert len(sent) == 2


async def test_a_write_still_propagating_is_seen_before_it_is_resent() -> None:
    """#3293 review: just after a write the routing can still name the old build; a few
    reads see the write land rather than resending a token it has made stale."""
    reads: list[str] = ["new", "old"]
    sent: list[int] = []

    async def write() -> None:
        sent.append(1)
        if len(sent) == 1:
            raise _transient()
        raise RPCError("stale", RPCStatusCode.FAILED_PRECONDITION, b"")

    async def routing() -> str | None:
        return reads.pop(0) if len(reads) > 1 else reads[0]

    assert await _write_refusal(write, routing, "old", max_wait=0) is None
    assert len(sent) == 1
