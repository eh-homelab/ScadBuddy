"""`python -m scadbuddy.worker`: the render worker (spec 2026-09-27 §3.5). The same image
as the API, run as its own Deployment; it serves `/healthz` and `/metrics` on 9090."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import uvicorn
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from temporalio.client import Client
from temporalio.service import RPCError

from scadbuddy.api.deps import AppState
from scadbuddy.core.config import ACTIVITY_TIMEOUT_MARGIN, INSTALL_CONCURRENCY
from scadbuddy.core.logging import configure_logging
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.pg_listener import PgListener
from scadbuddy.core.settings import Settings
from scadbuddy.library.assets import AssetStore
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.libraries import CheckoutFetcher, CheckoutGate, LibraryStore
from scadbuddy.render.projection import JobProjection
from scadbuddy.render.solids import WRAPPER_PREFIX
from scadbuddy.store import BlobRefs
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.workflows.activities import RenderActivities, WorkerDeps
from scadbuddy.workflows.client import connect, drained, make_current, render_worker

logger = logging.getLogger(__name__)

HEALTH_PORT = 9090
#: Seconds between drain checks after stop.
DRAIN_POLL = 5.0


def build_worker_deps(settings: Settings) -> WorkerDeps:
    config = settings.to_config()
    paths = DataPaths(root=settings.data_dir)
    paths.ensure()
    history = ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX, timeout=config.git_timeout)
    metrics = Metrics()
    metrics.build_info.labels(settings.version, settings.revision).set(1)
    checkouts = CheckoutGate()
    libraries = LibraryStore(paths, max_bytes=config.library_max_bytes)
    fetcher = CheckoutFetcher(libraries, asyncio.Semaphore(INSTALL_CONCURRENCY), checkouts)
    # The projection's job events go out on the API's bus: `publish_in` writes and
    # NOTIFYs in the row's transaction, so the listener is never started here.
    events = PgNotifyEventBus(
        settings.database_url, listener=PgListener(settings.database_url), metrics=metrics
    )
    projection = JobProjection(
        settings.database_url, pool_size=settings.database_pool_size, events=events
    )
    projection.open()
    assets = AssetStore(
        paths.assets,
        max_total_bytes=config.asset_max_total_bytes,
        max_count=config.asset_max_count,
    )
    return WorkerDeps(
        config=config,
        paths=paths,
        assets=assets,
        blobs=LocalBlobStore(paths.blobs),
        refs=BlobRefs(projection.pool),
        projection=projection,
        history=history,
        checkouts=checkouts,
        fetcher=fetcher,
        thumbnail_executor=ThreadPoolExecutor(
            max_workers=config.render_concurrency, thread_name_prefix="thumbnail"
        ),
        metrics=metrics,
    )


def worker_deps_from_state(state: AppState) -> WorkerDeps:
    """SCADBUDDY_TEMPORAL_WORKER_INPROCESS: the worker on the API's own stores and
    gates, so its renders lease the same checkouts the routes do. The thumbnail pool is
    its own; the lifespan shuts it down with the worker."""
    assert state.projection is not None and state.blobs is not None and state.refs is not None
    return WorkerDeps(
        config=state.config,
        paths=state.paths,
        assets=state.assets,
        blobs=state.blobs,
        refs=state.refs,
        projection=state.projection,
        history=state.history,
        checkouts=state.checkouts,
        fetcher=CheckoutFetcher(state.libraries, state.installs, state.checkouts),
        thumbnail_executor=ThreadPoolExecutor(
            max_workers=state.config.render_concurrency, thread_name_prefix="thumbnail"
        ),
        metrics=state.metrics,
    )


async def _wait_drained(
    is_drained: Callable[[], Awaitable[bool]], *, timeout: float, poll: float
) -> bool:
    """Poll `is_drained` until it says so (True) or `timeout` passes (False)."""
    try:
        async with asyncio.timeout(timeout):
            while not await is_drained():
                await asyncio.sleep(poll)
    except TimeoutError:
        return False
    return True


async def _poll(
    settings: Settings, deps: WorkerDeps, client: Client, stop: asyncio.Event, *, drain: bool
) -> None:
    config = deps.config
    build_id = settings.revision
    worker = render_worker(
        client,
        settings.temporal_task_queue_render,
        RenderActivities(deps),
        build_id=build_id,
        max_concurrent_activities=config.render_concurrency,
        graceful_shutdown_timeout=timedelta(
            seconds=config.render_timeout + ACTIVITY_TIMEOUT_MARGIN
        ),
    )

    async def is_drained() -> bool:
        try:
            return await drained(client, namespace=client.namespace, build_id=build_id)
        except RPCError:
            logger.warning("could not count this build's running workflows", exc_info=True)
            return False

    async with worker:
        # Phase 1 runs one replica: the newest worker is current.
        try:
            await make_current(client, namespace=client.namespace, build_id=build_id)
        except RPCError:
            logger.exception(
                "could not make this build current; polling anyway", extra={"build_id": build_id}
            )
        else:
            logger.info("made this build current", extra={"build_id": build_id})
        await stop.wait()
        if not drain:
            return

        # A workflow is PINNED to the build that started it: one waiting between two
        # activities is served by no other build, so keep polling until none is left.
        drain_timeout = 2 * config.activity_timeout + 120
        logger.info(
            "stopping: draining this build's workflows",
            extra={"build_id": build_id, "timeout_s": drain_timeout},
        )
        if await _wait_drained(is_drained, timeout=drain_timeout, poll=DRAIN_POLL):
            logger.info("drained", extra={"build_id": build_id})
        else:
            logger.warning(
                "drain timed out; exiting with workflows still running on this build",
                extra={"build_id": build_id, "timeout_s": drain_timeout},
            )


class _HealthServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        # `main` owns SIGTERM/SIGINT: they set `stop`, which stops this server too.
        yield


def _health_server(settings: Settings, metrics: Metrics, port: int) -> _HealthServer:
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "build_id": settings.revision,
                "task_queue": settings.temporal_task_queue_render,
            }
        )

    async def exposition(_: Request) -> Response:
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    app = Starlette(routes=[Route("/healthz", healthz), Route("/metrics", exposition)])
    return _HealthServer(
        uvicorn.Config(app, host="0.0.0.0", port=port, log_config=None, access_log=False)
    )


async def run_worker(
    settings: Settings,
    *,
    stop: asyncio.Event | None = None,
    health_port: int | None = HEALTH_PORT,
    client: Client | None = None,
) -> None:
    stop = stop or asyncio.Event()
    deps = build_worker_deps(settings)
    assert deps.metrics is not None and deps.thumbnail_executor is not None
    try:
        if client is None:
            client = await connect(settings.temporal_address, settings.temporal_namespace)
        server = (
            _health_server(settings, deps.metrics, health_port) if health_port is not None else None
        )
        serving = asyncio.create_task(server.serve()) if server is not None else None
        try:
            await _poll(settings, deps, client, stop, drain=True)
        finally:
            if server is not None and serving is not None:
                server.should_exit = True
                await serving
    finally:
        deps.projection.close()
        deps.thumbnail_executor.shutdown(wait=False, cancel_futures=True)


async def run_inprocess_worker(
    settings: Settings, deps: WorkerDeps, client: Client, stop: asyncio.Event
) -> None:
    """SCADBUDDY_TEMPORAL_WORKER_INPROCESS: the same worker on the API's own deps. It
    does not drain on stop: as the only worker it stays current, so new runs would hold
    the API's shutdown to the drain's bound."""
    await _poll(settings, deps, client, stop, drain=False)


async def _main(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await run_worker(settings, stop=stop)


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    asyncio.run(_main(settings))


if __name__ == "__main__":
    main()
