"""`python -m scadbuddy.worker`: the render worker (spec 2026-09-27 §3.5). The same image
as the API, run as its own Deployment; it serves `/healthz` and `/metrics` on 9090."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable, Generator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import TYPE_CHECKING, Literal

import uvicorn
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from temporalio.client import Client
from temporalio.service import RPCError

from scadbuddy.core.config import ACTIVITY_TIMEOUT_MARGIN, INSTALL_CONCURRENCY, Config
from scadbuddy.core.logging import configure_logging
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.pg_listener import PgListener
from scadbuddy.core.settings import Settings
from scadbuddy.core.tracing import configure_tracing
from scadbuddy.library.assets import AssetStore
from scadbuddy.library.fonts import FontService
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.libraries import CheckoutFetcher, CheckoutGate, LibraryStore
from scadbuddy.library.library_seed import seed_libraries
from scadbuddy.library.settings_store import load_render_store_settings
from scadbuddy.render.jobs import prune_revision_exports
from scadbuddy.render.projection import JobProjection
from scadbuddy.render.solids import WRAPPER_PREFIX
from scadbuddy.store import BlobRefs
from scadbuddy.store.bambuddy import RenderSettingsSource
from scadbuddy.store.cache import CachedBlobStore
from scadbuddy.store.factory import StoreBundle, build_store, store_health
from scadbuddy.workflows.activities import RenderActivities, WorkerDeps
from scadbuddy.workflows.client import connect, drained, is_current, make_current, render_worker
from scadbuddy.workflows.pipelines import TRANSFER

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState

logger = logging.getLogger(__name__)

HEALTH_PORT = 9090
#: Seconds between drain checks after stop.
DRAIN_POLL = 5.0
#: How long a stopping worker whose build is still current keeps serving its pinned
#: runs before leaving them to the next pod of the same build.
DRAIN_CURRENT_GRACE = 30.0
#: Making the build current, retried once the worker polls: Temporal 1.28 ignores
#: `allow_no_pollers` and answers NOT_FOUND until the build's first poll reaches it.
#: The waits between attempts, then every `MAKE_CURRENT_EVERY`, for `_DEADLINE` seconds.
MAKE_CURRENT_BACKOFF = (1.0, 2.0, 4.0)
MAKE_CURRENT_EVERY = 5.0
MAKE_CURRENT_DEADLINE = 60.0


def build_worker_deps(settings: Settings) -> tuple[WorkerDeps, StoreBundle]:
    config = settings.to_config()
    paths = DataPaths(root=settings.data_dir)
    paths.ensure()
    # The image's libraries (#169), as the API's boot seeds them: a worker on an
    # emptyDir would otherwise clone BOSL2 from the network for its first render.
    seed_libraries_dir = settings.resolve_seed_libraries_dir()
    if seed_libraries_dir is not None:
        try:
            seed_libraries(paths, seed_libraries_dir)
        except OSError:
            logger.exception("could not seed library checkouts from the image")
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
        projection.pool,
        max_total_bytes=config.asset_max_total_bytes,
        max_count=config.asset_max_count,
    )
    try:
        source = RenderSettingsSource(projection.pool, settings)
        current = load_render_store_settings(projection.pool, settings)
        source.seed(current)
        backend = current.store_backend
        store = build_store(
            backend=backend,
            current=current,
            config=config,
            paths=paths,
            pool=projection.pool,
            source=source,
            # On bambuddy a worker has no git: the API makes the snapshots it renders from.
            history=None,
            fonts=FontService(settings.data_dir),
            metrics=metrics,
        )
    except BaseException:
        # A refused start (an unready store) closes what it opened before propagating.
        projection.close()
        raise
    deps = WorkerDeps(
        config=config,
        paths=paths,
        assets=assets,
        blobs=store.blobs,
        refs=BlobRefs(projection.pool),
        projection=projection,
        history=history if backend == "local" else None,
        checkouts=checkouts,
        fetcher=fetcher,
        thumbnail_executor=ThreadPoolExecutor(
            max_workers=config.render_concurrency, thread_name_prefix="thumbnail"
        ),
        metrics=metrics,
        snapshots=store.snapshots,
        fonts_mirror=store.fonts,
        remote_assets=store.remote_assets,
    )
    return deps, store


def worker_deps_from_state(state: AppState) -> WorkerDeps:
    """SCADBUDDY_TEMPORAL_WORKER_INPROCESS: the worker on the API's own stores and
    gates, so its renders lease the same checkouts the routes do. The thumbnail pool is
    its own; the lifespan shuts it down with the worker."""
    return WorkerDeps(
        config=state.config,
        paths=state.paths,
        assets=state.assets,
        blobs=state.store.blobs,
        refs=state.refs,
        projection=state.projection,
        history=state.history,
        checkouts=state.checkouts,
        fetcher=CheckoutFetcher(state.libraries, state.installs, state.checkouts),
        thumbnail_executor=ThreadPoolExecutor(
            max_workers=state.config.render_concurrency, thread_name_prefix="thumbnail"
        ),
        metrics=state.metrics,
        snapshots=state.store.snapshots,
        fonts_mirror=state.store.fonts,
        remote_assets=state.store.remote_assets,
    )


DrainOutcome = Literal["drained", "current", "timed_out"]


async def _drain(
    still_current: Callable[[], Awaitable[bool]],
    is_drained: Callable[[], Awaitable[bool]],
    *,
    timeout: float,
    poll: float,
    grace: float,
) -> DrainOutcome:
    """Poll until no run is pinned to this build (``drained``) or ``timeout`` passes
    (``timed_out``). A build that is still, or again, current (``current``) ends it
    too, because another worker of the same build serves its pinned runs (#874); but
    only after ``grace``, during which this worker keeps serving them itself, in case
    the pod that replaces it is slow to come, or never comes."""
    loop = asyncio.get_running_loop()
    trust_current_at = loop.time() + grace
    try:
        async with asyncio.timeout(timeout):
            while True:
                if await is_drained():
                    return "drained"
                if loop.time() >= trust_current_at and await still_current():
                    return "current"
                await asyncio.sleep(poll)
    except TimeoutError:
        return "timed_out"


async def make_current_until_polled(
    set_current: Callable[[], Awaitable[None]],
    *,
    build_id: str,
    backoff: Sequence[float],
    every: float,
    deadline: float,
) -> bool:
    """Make the build current, retrying an `RPCError` until ``deadline`` seconds have
    passed; False when it never took. Any other error propagates."""
    loop = asyncio.get_running_loop()
    give_up = loop.time() + deadline
    attempt = 0
    while True:
        attempt += 1
        try:
            await set_current()
        except RPCError as error:
            delay = backoff[attempt - 1] if attempt <= len(backoff) else every
            logger.warning(
                "could not make this build current yet",
                extra={"build_id": build_id, "attempt": attempt, "error": str(error)},
            )
            if loop.time() + delay > give_up:
                logger.error(
                    "could not make this build current; polling anyway",
                    extra={"build_id": build_id, "attempts": attempt, "error": str(error)},
                )
                return False
            await asyncio.sleep(delay)
        else:
            logger.info("made this build current", extra={"build_id": build_id})
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

    async def still_current() -> bool:
        try:
            return await is_current(client, namespace=client.namespace, build_id=build_id)
        except RPCError:
            logger.warning("could not read the deployment's current build", exc_info=True)
            return False

    async with worker:
        # Phase 1 runs one replica: the newest worker is current. Entering the worker
        # started its polling, so the retry runs beside it; stop cancels the retry.
        current = asyncio.create_task(
            make_current_until_polled(
                lambda: make_current(client, namespace=client.namespace, build_id=build_id),
                build_id=build_id,
                backoff=MAKE_CURRENT_BACKOFF,
                every=MAKE_CURRENT_EVERY,
                deadline=MAKE_CURRENT_DEADLINE,
            )
        )
        stopped = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait({current, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if current in done:
                current.result()  # an error other than RPCError stops the worker, as before
                await stopped
        finally:
            current.cancel()
            stopped.cancel()
            await asyncio.wait({current, stopped})
        if not drain:
            return

        # A workflow is PINNED to the build that started it: one waiting between two
        # activities is served by no other build, so keep polling until none is left,
        # unless this build is still current: a restart of the same build (a manifest
        # change, a node drain) leaves its runs to the next pod of that build.
        drain_timeout = 2 * config.activity_timeout + 120
        logger.info(
            "stopping: draining this build's workflows",
            extra={"build_id": build_id, "timeout_s": drain_timeout},
        )
        outcome = await _drain(
            still_current,
            is_drained,
            timeout=drain_timeout,
            poll=DRAIN_POLL,
            grace=min(DRAIN_CURRENT_GRACE, drain_timeout),
        )
        if outcome == "drained":
            logger.info("drained", extra={"build_id": build_id})
        elif outcome == "current":
            logger.warning(
                "stopping without draining: this build is still current, so its pinned"
                " workflows are left to the next worker of this build; until one polls,"
                " they wait",
                extra={"build_id": build_id},
            )
        else:
            logger.warning(
                "drain timed out; exiting with workflows still running on this build",
                extra={"build_id": build_id, "timeout_s": drain_timeout},
            )


class _HealthServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        # `main` owns SIGTERM/SIGINT: they set `stop`, which stops this server too.
        yield


async def _refresh_store_metrics(metrics: Metrics, store: StoreBundle) -> None:
    """The store gauges this process owns: its piece cache and the key it holds. The
    store's usage is the API's to export (one database, one set of numbers)."""
    health = await store_health(store)
    metrics.store_render_key_fallback.set(1 if health.render_key_fallback else 0)
    if isinstance(store.blobs, CachedBlobStore):
        metrics.worker_cache_bytes.set(await asyncio.to_thread(store.blobs.cached_bytes))


def _health_app(settings: Settings, metrics: Metrics, store: StoreBundle) -> Starlette:
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "build_id": settings.revision,
                "task_queue": settings.temporal_task_queue_render,
                "store": (await store_health(store)).model_dump(),
            }
        )

    async def exposition(_: Request) -> Response:
        try:
            await _refresh_store_metrics(metrics, store)
        except Exception:
            # Like the API's: keep the last values, never fail the scrape.
            logger.exception("could not read the store's gauges")
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    return Starlette(routes=[Route("/healthz", healthz), Route("/metrics", exposition)])


def _health_server(
    settings: Settings, metrics: Metrics, store: StoreBundle, port: int
) -> _HealthServer:
    app = _health_app(settings, metrics, store)
    return _HealthServer(
        uvicorn.Config(app, host="0.0.0.0", port=port, log_config=None, access_log=False)
    )


def _upload_grace(config: Config) -> float:
    """How long an upload a worker fetched stays unused before its sweep removes it.
    The API's grace (at least `MIN_ASSET_SWEEP_GRACE`, an hour), and never less than
    one activity's whole budget: an activity marks the uploads it needs used when it
    brings them in (`RemoteAssets.ensure`), then reads them within its openscad
    timeout and its transfers, so none is swept under a render in flight. A worker
    holds no references; what it needs again comes back from the store."""
    return max(config.asset_sweep_grace, config.activity_timeout + 3 * TRANSFER.total_seconds())


def _own_volume(paths: DataPaths) -> bool:
    """Whether the volume is the worker's own. The API's /data holds the templates in
    `models/`; a worker never writes there, so one still mounting the shared volume
    leaves its uploads and exports to the API's sweeps, which know the references."""
    return not (paths.models.is_dir() and any(paths.models.iterdir()))


def _housekeep(deps: WorkerDeps) -> None:
    """One pass of the worker's sweep (final review I2): its piece cache down to
    SCADBUDDY_WORKER_CACHE_MAX_BYTES, then, on its own volume, the revision exports
    it materialized (by last use, on the API's `SCADBUDDY_JOB_TTL`) and the uploads it
    fetched (by last use, `_upload_grace`). Library checkouts stay: a checkout is a
    pinned library, and nothing prunes those. Each step is best effort; the next
    pass retries."""
    if isinstance(deps.blobs, CachedBlobStore):
        try:
            evicted = deps.blobs.evict()
            if evicted:
                logger.info("evicted cached pieces", extra={"count": len(evicted)})
        except Exception:
            logger.exception("could not evict the piece cache")
    if not _own_volume(deps.paths):
        return
    try:
        pruned = prune_revision_exports(deps.paths, deps.config.job_ttl)
        if pruned:
            logger.info("pruned revision exports", extra={"count": len(pruned)})
    except Exception:
        logger.exception("could not prune revision exports")
    if deps.remote_assets is not None:
        try:
            # Its copies only: the rows are the API's (#591), and its sweep decides.
            pruned_uploads = deps.assets.prune_local(grace=_upload_grace(deps.config))
            if pruned_uploads:
                logger.info("pruned fetched uploads", extra={"count": len(pruned_uploads)})
        except Exception:
            logger.exception("could not prune fetched uploads")


#: How often a worker housekeeps when the upload sweep is off (seconds): the piece
#: cache must still be evicted.
WORKER_CACHE_EVICT_INTERVAL = 300.0


async def _housekeep_periodically(deps: WorkerDeps, interval: float) -> None:
    """`_housekeep` every ``interval`` (see `_start_housekeeping`)."""
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(_housekeep, deps)
        except Exception:
            logger.exception("the worker's sweep failed; the next one retries")


def _start_housekeeping(deps: WorkerDeps, sweep_interval: float) -> asyncio.Task[None] | None:
    """Housekeep on a timer whenever the worker's blobs are a piece cache (the bambuddy
    store; a local-store worker shares the API's volume, whose sweeps are the API's):
    every SCADBUDDY_ASSET_SWEEP_INTERVAL when that is on, else every
    `WORKER_CACHE_EVICT_INTERVAL`. Turning the upload sweep off never stops eviction."""
    if not isinstance(deps.blobs, CachedBlobStore):
        return None
    interval = sweep_interval if sweep_interval > 0 else WORKER_CACHE_EVICT_INTERVAL
    logger.info("housekeeping the worker's cache and volume every %.0f s", interval)
    return asyncio.create_task(_housekeep_periodically(deps, interval))


async def run_worker(
    settings: Settings,
    *,
    stop: asyncio.Event | None = None,
    health_port: int | None = HEALTH_PORT,
    client: Client | None = None,
) -> None:
    stop = stop or asyncio.Event()
    # Off the loop, as the API's boot seeds its libraries: the seed copies trees, and
    # the rest opens the projection's pool and reads the store settings.
    deps, store = await asyncio.to_thread(build_worker_deps, settings)
    assert deps.metrics is not None and deps.thumbnail_executor is not None
    evicting = _start_housekeeping(deps, deps.config.asset_sweep_interval)
    try:
        if client is None:
            client = await connect(settings.temporal_address, settings.temporal_namespace)
        server = (
            _health_server(settings, deps.metrics, store, health_port)
            if health_port is not None
            else None
        )
        serving = asyncio.create_task(server.serve()) if server is not None else None
        try:
            await _poll(settings, deps, client, stop, drain=True)
        finally:
            if server is not None and serving is not None:
                server.should_exit = True
                await serving
    finally:
        if evicting is not None:
            evicting.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await evicting
        await store.aclose()
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
    configure_tracing("scadbuddy-worker", version=settings.version, revision=settings.revision)
    asyncio.run(_main(settings))


if __name__ == "__main__":
    main()
