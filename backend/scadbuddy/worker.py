"""`python -m scadbuddy.worker`: the render worker (spec 2026-09-27 §3.5). The same image
as the API, run as its own Deployment; it serves `/healthz` and `/metrics` on 9090."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import faulthandler
import logging
import os
import signal
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import httpx
import uvicorn
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from temporalio.client import Client
from temporalio.runtime import Runtime
from temporalio.service import RPCError
from temporalio.worker import Worker

from scadbuddy.activity.index import ActivityIndex
from scadbuddy.bambuddy.archive_cache import ArchiveCache
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.follow import FollowActivities, Follower
from scadbuddy.bambuddy.operations import bambuddy_kinds_over
from scadbuddy.bambuddy.outcomes import PrintOutcomeStore
from scadbuddy.bambuddy.output_reader import REMOTE_TIMEOUT, RemoteBlobs, RemoteOutputs
from scadbuddy.bambuddy.print_links import PrintLinkStore
from scadbuddy.bambuddy.progress import (
    PrintProgress,
    ProgressObserver,
    lan_first,
    library_progress,
    progress_for,
)
from scadbuddy.bambuddy.runs import PrintRunStore, RecentRuns, newest_failed_from
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.core.config import ACTIVITY_TIMEOUT_MARGIN, INSTALL_CONCURRENCY, Config
from scadbuddy.core.logging import configure_logging
from scadbuddy.core.loop_watch import LoopWatch
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.pg_listener import PgListener
from scadbuddy.core.secrets import Kek
from scadbuddy.core.settings import Settings
from scadbuddy.core.tracing import configure_tracing
from scadbuddy.flows.keys import approval_key, payload_keys
from scadbuddy.flows.operations import flow_kinds
from scadbuddy.flows.store import FlowStore
from scadbuddy.library.assets import AssetStore
from scadbuddy.library.fonts import FontService
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.libraries import (
    CheckoutFetcher,
    CheckoutGate,
    CheckoutLeases,
    InstallPermits,
    LibraryStore,
)
from scadbuddy.library.library_seed import seed_libraries
from scadbuddy.library.output_prints import OutputPrintStore
from scadbuddy.library.outputs import OutputMeta
from scadbuddy.library.settings_store import (
    SettingsStore,
    load_render_store_settings,
    settings_kek,
)
from scadbuddy.operations.store import OperationStore
from scadbuddy.print_library.reads import LibraryReads
from scadbuddy.print_library.remote import RemoteLibrary
from scadbuddy.printers.print_activities import LanActivities, LanDeps
from scadbuddy.printers.progress import LanProgress, lan_last_print
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.store import PrinterStore
from scadbuddy.printers.watch import WatchActivities
from scadbuddy.rack.usage import RackUsageStore, lan_settle_hook, settle_hook
from scadbuddy.render.jobs import prune_revision_exports
from scadbuddy.render.projection import JobProjection
from scadbuddy.render.runner import probe_openscad_version
from scadbuddy.render.solids import WRAPPER_PREFIX
from scadbuddy.render.worker_role import WorkerStoppedError, wait_until_ready
from scadbuddy.slicing.catalogue import slices_native, warm_catalogue
from scadbuddy.slicing.held_presets import HeldPresetStore
from scadbuddy.slicing.profiles import Profiles
from scadbuddy.slicing.start import native_slice_starter
from scadbuddy.slicing.store import NativeSliceStore
from scadbuddy.spools.component import Spools
from scadbuddy.spools.providers import SettingsOnly, discover_providers
from scadbuddy.spools.store import SpoolStore
from scadbuddy.store import BlobRefs
from scadbuddy.store.bambuddy import RenderSettingsSource
from scadbuddy.store.cache import CachedBlobStore
from scadbuddy.store.factory import StoreBundle, build_store, store_health
from scadbuddy.store.geometry import GeometryIndex
from scadbuddy.workflows.activities import RenderActivities, WorkerDeps
from scadbuddy.workflows.client import (
    DEPLOYMENT_NAME,
    POLLER_SEEN_WITHIN,
    PRINT_DEPLOYMENT_NAME,
    PROJECTS_DEPLOYMENT_NAME,
    any_serving,
    bambuddy_worker,
    clear_draining,
    connect,
    current_routing,
    drained,
    draining_marks,
    follow_worker,
    is_current,
    make_current,
    mark_draining,
    pollers_elsewhere,
    projects_deployment,
    render_worker,
    routes_to,
)
from scadbuddy.workflows.flow_activities import FlowActivities
from scadbuddy.workflows.flow_grants import FlowGrants
from scadbuddy.workflows.flow_routes import FlowRoutes, api_client
from scadbuddy.workflows.flows_client import connect_flows
from scadbuddy.workflows.operation_activities import operation_activities
from scadbuddy.workflows.pipeline_activities import PipelineActivities
from scadbuddy.workflows.pipelines import TRANSFER
from scadbuddy.workflows.print_activities import PrintActivities, PrintDeps
from scadbuddy.workflows.projects_worker import projects_worker
from scadbuddy.workflows.sdk_metrics import SDK_READ_TIMEOUT, sdk_exposition, sdk_runtime

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState

logger = logging.getLogger(__name__)

HEALTH_PORT = 9090
#: Seconds between drain checks after stop.
DRAIN_POLL = 5.0
#: How long a stopping worker whose build is still current, and polled by another
#: worker of the build, keeps serving its pinned runs before leaving them to that one.
DRAIN_CURRENT_GRACE = 30.0
#: How long past a peer's drain bound and `POLLER_SEEN_WITHIN` its drain mark still
#: counts (#4948): room for the pods' clocks to differ. A pod of one build has the same
#: drain bound as this one, and its last poll looks recent for `POLLER_SEEN_WITHIN` after
#: its drain ends, so a mark younger than the sum may be a peer still draining.
DRAIN_MARK_SLACK = 60.0
#: Making the build current, retried once the worker polls: Temporal 1.28 ignores
#: `allow_no_pollers` and answers NOT_FOUND until the build's first poll reaches it.
#: The waits between attempts, then every `MAKE_CURRENT_EVERY` until it takes or the
#: worker stops: a worker that gave up left a deployment whose current build had no
#: poller once the old pods went, and every render hung (#2341). After
#: `MAKE_CURRENT_ERROR_AFTER` seconds each failure logs an error instead of a warning.
MAKE_CURRENT_BACKOFF = (1.0, 2.0, 4.0)
MAKE_CURRENT_EVERY = 5.0
MAKE_CURRENT_ERROR_AFTER = 60.0
#: How often a make-current that is still refused past `MAKE_CURRENT_ERROR_AFTER` tries
#: again, and so logs its error: once a minute, not every `MAKE_CURRENT_EVERY` (#2538).
MAKE_CURRENT_ERROR_EVERY = 60.0
#: How long a versioned worker that reports readiness (`/readyz`, #4146) waits, once its
#: build is current, for its queue to route to it before it reports ready anyway, and
#: how often it asks. The API's library worker waits as long (`main.LIBRARY_ROUTED_WAIT`).
ROUTED_WAIT = 60.0
ROUTED_POLL = 0.5


def stored_solid_concurrency(source: RenderSettingsSource) -> Callable[[], Awaitable[int]]:
    """The Settings page's per-job colour slots, through the `render_settings` view on
    ``source``'s TTL: a worker holds no settings store, so this is how the live setting
    reaches it (#601). ``deps.config`` is the one read at start."""

    async def current() -> int:
        return (await source.current()).solid_concurrency

    return current


def build_worker_deps(
    settings: Settings, *, stopping: Callable[[], bool] = lambda: False
) -> tuple[WorkerDeps, StoreBundle]:
    """``stopping`` ends the wait for the API's schema and grants
    (`WorkerStoppedError`)."""
    # Editor checks run in the API, never here: no CPU is kept back for them.
    config = replace(settings.to_config(), serves_checks=False)
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
    # The projection's job events go out on the API's bus: `publish_in` writes and
    # NOTIFYs in the row's transaction, so the listener is never started here.
    events = PgNotifyEventBus(
        settings.database_url, listener=PgListener(settings.database_url), metrics=metrics
    )
    projection = JobProjection(
        settings.database_url, pool_size=settings.database_pool_size, events=events
    )
    # Never migrated here (#601): the worker's own role may not, and in a rollout the
    # API, which does, may not have started yet. Wait for it instead.
    projection.open(migrate_schema=False)
    # The `project` activity writes `mark_started`/`finish` here: its state changes reach
    # activity in the same transaction, and NOTIFY on the API's listeners.
    projection.activity = ActivityIndex(projection.pool, events=events, metrics=metrics)
    # The leases in Postgres, where the API's removals see them (#872).
    checkouts = CheckoutGate(CheckoutLeases(projection.pool, paths.libraries))
    libraries = LibraryStore(paths, max_bytes=config.library_max_bytes)
    fetcher = CheckoutFetcher(
        libraries, InstallPermits(INSTALL_CONCURRENCY, projection.pool), checkouts
    )
    assets = AssetStore(
        paths.assets,
        projection.pool,
        max_total_bytes=config.asset_max_total_bytes,
        max_count=config.asset_max_count,
    )
    try:
        wait_until_ready(projection.pool, stopping=stopping)
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
            # #2788: none unless set, and never one the API reads (README).
            downloads_dir=config.download_cache_dir,
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
        geometry=GeometryIndex(projection.pool),
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
        stored_solid_concurrency=stored_solid_concurrency(store.source),
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
        geometry=GeometryIndex(state.refs.pool),
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
        openscad_version=state.openscad_version or "",
        stored_solid_concurrency=stored_solid_concurrency(state.store.source),
    )


DrainOutcome = Literal["drained", "current", "timed_out", "stopped"]


async def _drain(
    still_current: Callable[[], Awaitable[bool]],
    is_drained: Callable[[], Awaitable[bool]],
    *,
    timeout: float,
    poll: float,
    grace: float,
    stop_now: asyncio.Event | None = None,
) -> DrainOutcome:
    """Poll until no run is pinned to this build (``drained``) or ``timeout`` passes
    (``timed_out``). A build that is still, or again, current with another worker of it
    polling (``still_current``, ``current``) ends it too, because that worker serves its
    pinned runs (#874, #4869); but only after ``grace``, during which this worker keeps
    serving them itself, in case the pod that replaces it is slow to come, or never
    comes. ``stop_now`` (a second signal, #605) ends it at once (``stopped``)."""
    loop = asyncio.get_running_loop()
    trust_current_at = loop.time() + grace

    async def polled() -> DrainOutcome:
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

    polling = asyncio.create_task(polled())
    stopping = asyncio.create_task((stop_now or asyncio.Event()).wait())
    try:
        await asyncio.wait({polling, stopping}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        polling.cancel()
        stopping.cancel()
        await asyncio.wait({polling, stopping})
    return polling.result() if not polling.cancelled() else "stopped"


def deployment_writes(
    client: Client, build_id: str, deployment_name: str
) -> tuple[Callable[[], Awaitable[None]], Callable[[], Awaitable[str | None]]]:
    """``make_current_until_polled``'s write and read for this build. Each write carries
    the conflict token of the last read that succeeded (#2538), so a rollout that lands
    between the read and the write makes Temporal refuse it rather than be overwritten;
    the next read then sees that rollout's build and the retry stops."""
    token = b""

    async def read() -> str | None:
        nonlocal token
        build, token = await current_routing(
            client, namespace=client.namespace, deployment_name=deployment_name
        )
        return build

    async def write() -> None:
        await make_current(
            client,
            namespace=client.namespace,
            build_id=build_id,
            deployment_name=deployment_name,
            conflict_token=token,
        )

    return write, read


async def make_current_until_polled(
    set_current: Callable[[], Awaitable[None]],
    *,
    build_id: str,
    backoff: Sequence[float],
    every: float,
    error_after: float,
    error_every: float | None = None,
    current: Callable[[], Awaitable[str | None]] | None = None,
) -> None:
    """Make the build current, retrying an `RPCError` until it takes; cancel it to
    stop. Failures past ``error_after`` seconds log at error level, and are retried
    every ``error_every`` (default ``every``). Any other error propagates.

    With ``current`` (the deployment's current build), it stops once a build other than
    this one and the one current when it began is made current (#2538): a newer
    rollout's worker has taken over, and a late write here would hand the deployment
    back to this old build, whose stop then skips its drain because it is current.
    (``deployment_writes`` closes the gap between the read and the write with the
    read's conflict token.)

    The build current when it began is what it never takes over from. When that first
    read fails, the first read that succeeds is taken instead: a newer rollout made
    current in between is then indistinguishable from the old build, and this one may
    still take the deployment back. Accepted: it needs a failed read and a rollout in
    the same few seconds, and the next rollout's worker makes its own build current."""
    loop = asyncio.get_running_loop()
    escalate = loop.time() + error_after
    unread = object()
    before: object = unread

    async def read() -> object:
        assert current is not None
        try:
            return await current()
        except RPCError:
            logger.warning("could not read the deployment's current build", exc_info=True)
            return unread

    if current is not None:
        before = await read()
    attempt = 0
    while True:
        attempt += 1
        if current is not None and attempt > 1:
            now = await read()
            if before is unread:
                before = now
            elif now not in (unread, None, build_id, before):
                logger.info(
                    "another build was made current; this one no longer tries to be",
                    extra={"build_id": build_id, "current": now},
                )
                return
        try:
            await set_current()
        except RPCError as error:
            late = loop.time() >= escalate
            delay = backoff[attempt - 1] if attempt <= len(backoff) else every
            if late and error_every is not None:
                delay = max(delay, error_every)
            logger.log(
                logging.ERROR if late else logging.WARNING,
                "could not make this build current yet",
                extra={"build_id": build_id, "attempt": attempt, "error": str(error)},
            )
            await asyncio.sleep(delay)
        else:
            logger.info("made this build current", extra={"build_id": build_id})
            return


async def wait_until_routed(
    client: Client,
    task_queue: str,
    *,
    deployment_name: str,
    build_id: str,
    timeout: float | None = None,
    poll: float | None = None,
) -> bool:
    """Ask `routes_to` until ``task_queue`` routes to this build (True), or ``timeout``
    (`ROUTED_WAIT`) passes (False, logged as an error: Temporal took the build but its
    queue still sends new work elsewhere). A version is current a moment before its
    queues hear of it (#4090). An `RPCError` is a no, asked again."""
    timeout = ROUTED_WAIT if timeout is None else timeout
    poll = ROUTED_POLL if poll is None else poll
    loop = asyncio.get_running_loop()
    until = loop.time() + timeout
    while loop.time() < until:
        with contextlib.suppress(RPCError):
            if await routes_to(
                client, task_queue, deployment_name=deployment_name, build_id=build_id
            ):
                return True
        await asyncio.sleep(poll)
    logger.error(
        "the queue does not route to this build yet; reporting ready anyway",
        extra={"build_id": build_id, "task_queue": task_queue},
    )
    return False


async def _poll(
    settings: Settings,
    deps: WorkerDeps,
    client: Client,
    stop: asyncio.Event,
    *,
    drain: bool,
    stop_now: asyncio.Event | None = None,
    routed: asyncio.Event | None = None,
) -> None:
    """``routed`` is set once the render queue routes to this build, for `/readyz`
    (#4869): a rollout that stops the old pods sooner finds their build still current."""
    config = deps.config
    build_id = settings.revision
    # What every output's record names (§8.4): this image and its openscad. The
    # in-process worker has the API's probe already; running openscad again here, in a
    # task beside the API's first requests, would only race them.
    deps.revision = settings.revision
    if not deps.openscad_version:
        deps.openscad_version = await probe_openscad_version(config) or ""
    worker = render_worker(
        client,
        settings.temporal_task_queue_render,
        RenderActivities(deps),
        pipeline=PipelineActivities(deps),
        build_id=build_id,
        max_concurrent_activities=config.render_concurrency,
        graceful_shutdown_timeout=timedelta(
            seconds=config.render_timeout + ACTIVITY_TIMEOUT_MARGIN
        ),
    )
    await _serve_versioned(
        client,
        [worker],
        stop,
        build_id=build_id,
        deployment_name=DEPLOYMENT_NAME,
        task_queue=settings.temporal_task_queue_render,
        drain_timeout=2 * config.activity_timeout + 120 if drain else None,
        stop_now=stop_now,
        ready=(settings.temporal_task_queue_render, routed) if routed is not None else None,
    )


async def _until_written(write: Callable[[], Awaitable[None]], failed: str) -> None:
    """Retry ``write`` on an `RPCError` (`MAKE_CURRENT_BACKOFF`, then every
    `MAKE_CURRENT_EVERY`) until it takes; cancel it to stop. A build version exists only
    once a worker of it has polled, so a metadata write just after start may be refused."""
    attempt = 0
    while True:
        try:
            await write()
        except RPCError as error:
            logger.warning(failed, extra={"attempt": attempt + 1, "error": str(error)})
            delay = (
                MAKE_CURRENT_BACKOFF[attempt]
                if attempt < len(MAKE_CURRENT_BACKOFF)
                else MAKE_CURRENT_EVERY
            )
            attempt += 1
            await asyncio.sleep(delay)
        else:
            return


async def _serve_versioned(
    client: Client,
    workers: Sequence[Worker],
    stop: asyncio.Event,
    *,
    build_id: str,
    deployment_name: str,
    task_queue: str,
    drain_timeout: float | None,
    stop_now: asyncio.Event | None = None,
    ready: tuple[str, asyncio.Event] | None = None,
) -> None:
    """Run ``workers`` (one deployment version) until ``stop``, making the build current
    beside them; then, unless ``drain_timeout`` is None, drain the build's pinned runs,
    which only another worker of the build polling ``task_queue`` ends early (#4869),
    and not one marked draining itself (#4948). ``stop_now`` cuts the drain short (#605).
    With ``ready`` (a task queue and an event), the event is set once that queue routes to
    this build (`wait_until_routed`), for `/readyz` (#4146)."""

    async def is_drained() -> bool:
        try:
            return await drained(
                client,
                namespace=client.namespace,
                build_id=build_id,
                deployment_name=deployment_name,
            )
        except RPCError:
            logger.warning("could not count this build's running workflows", exc_info=True)
            return False

    # This worker's own polls, told apart from another pod's of the same build.
    identities = {worker.config().get("identity") or client.identity for worker in workers}

    # Set once this worker's drain mark is written (#4948): until then a stopping peer
    # cannot tell this worker from one that serves, so this one trusts no peer either.
    marked = asyncio.Event()
    # `mark` and `still_current` read `drain_began` and `marked_within`, which are set
    # when the drain begins, before either runs.

    async def mark() -> None:
        await mark_draining(
            client,
            deployment_name=deployment_name,
            build_id=build_id,
            identities=identities,
            since=drain_began,
            prune_within=marked_within,
        )
        marked.set()

    async def still_current() -> bool:
        if not marked.is_set():
            return False
        try:
            if not await is_current(
                client,
                namespace=client.namespace,
                build_id=build_id,
                deployment_name=deployment_name,
            ):
                return False
            # The pollers first: with no other poller of this build, the usual rollout,
            # the poll ends here and reads no version metadata (#4998).
            others = await pollers_elsewhere(
                client,
                task_queue,
                deployment_name=deployment_name,
                build_id=build_id,
                identities=identities,
            )
            if not others:
                return False
            # One read of the marks serves both checks below (#4996), so they never
            # disagree. The start's clear, cancelled at stop, may still land after the
            # mark and remove it; a stopping peer would then count this worker. So trust
            # only a mark that is still there, and write it again when it is gone. That
            # happens only on a poll that finds a peer (#4998): with none, no worker can
            # count this one, and a peer that comes later is seen by a later poll. This
            # narrows the race but does not close it: a cancelled clear can still land
            # after this read and before the peer's own check. That residual race is
            # accepted: it needs a clear held in flight past the grace, and its worst case
            # is the one #4948 fixed, never a worse one.
            marks = await draining_marks(client, deployment_name=deployment_name, build_id=build_id)
            if not all(identity in marks for identity in identities):
                logger.warning("this worker's drain mark is gone; writing it again")
                await mark()
                return False
            return any_serving(others, marks, marked_within=marked_within)
        except RPCError:
            logger.warning(
                "could not read the deployment's current build, the drain marks or the"
                " queue's pollers",
                exc_info=True,
            )
            return False

    async with contextlib.AsyncExitStack() as running:
        for worker in workers:
            await running.enter_async_context(worker)
        # Phase 1 runs one replica: the newest worker is current. Entering the worker
        # started its polling, so the retry runs beside it; stop cancels the retry.
        set_current, read_current = deployment_writes(client, build_id, deployment_name)

        async def make_current_then_routed() -> None:
            await make_current_until_polled(
                set_current,
                build_id=build_id,
                backoff=MAKE_CURRENT_BACKOFF,
                every=MAKE_CURRENT_EVERY,
                error_after=MAKE_CURRENT_ERROR_AFTER,
                error_every=MAKE_CURRENT_ERROR_EVERY,
                current=read_current,
            )
            if ready is not None:
                queue, routed = ready
                await wait_until_routed(
                    client, queue, deployment_name=deployment_name, build_id=build_id
                )
                routed.set()

        current = asyncio.create_task(make_current_then_routed())
        stopped = asyncio.create_task(stop.wait())
        # A container restarted in its pod keeps its identity: its last life's drain mark
        # must not make a stopping peer skip it (#4948). Only a worker that drains marks.
        clearing = asyncio.create_task(
            _until_written(
                lambda: clear_draining(
                    client,
                    deployment_name=deployment_name,
                    build_id=build_id,
                    identities=identities,
                ),
                "could not clear this worker's drain mark yet",
            )
            if drain_timeout is not None
            else asyncio.sleep(0)
        )
        try:
            done, _ = await asyncio.wait({current, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if current in done:
                current.result()  # an error other than RPCError stops the worker, as before
                await stopped
        finally:
            current.cancel()
            stopped.cancel()
            clearing.cancel()
            await asyncio.wait({current, stopped, clearing})
        if drain_timeout is None:
            return

        # A workflow is PINNED to the build that started it: one waiting between two
        # activities is served by no other build, so keep polling until none is left,
        # unless this build is still current and another worker of it polls: a restart
        # of the same build (a manifest change, a node drain) leaves its runs to the next
        # pod of that build. Current alone is not enough: in a rollout the new pods run
        # the next build, not yet current, and serve none of these runs (#4869). Nor is a
        # poll enough: a draining worker keeps polling, so this one marks itself first,
        # and two pods of the build that stop together each skip the other (#4948).
        logger.info(
            "stopping: draining this build's workflows",
            extra={"build_id": build_id, "timeout_s": drain_timeout},
        )
        drain_began = datetime.now(UTC)
        # A peer's mark younger than this may be a drain still going, and an older one is
        # pruned: a pod of one build drains as long as this one.
        marked_within = drain_timeout + POLLER_SEEN_WITHIN + DRAIN_MARK_SLACK
        marking = asyncio.create_task(
            _until_written(mark, "could not mark this worker draining yet")
        )
        try:
            outcome = await _drain(
                still_current,
                is_drained,
                timeout=drain_timeout,
                poll=DRAIN_POLL,
                grace=min(DRAIN_CURRENT_GRACE, drain_timeout),
                stop_now=stop_now,
            )
        finally:
            marking.cancel()
            await asyncio.wait({marking})
        if outcome == "drained":
            logger.info("drained", extra={"build_id": build_id})
        elif outcome == "current":
            logger.warning(
                "stopping without draining: this build is still current and another"
                " worker of it polls, so its pinned workflows are left to that worker",
                extra={"build_id": build_id},
            )
        elif outcome == "stopped":
            logger.warning(
                "stopping now on a second signal; exiting with workflows still running"
                " on this build",
                extra={"build_id": build_id},
            )
        else:
            logger.warning(
                "drain timed out; exiting with workflows still running on this build",
                extra={"build_id": build_id, "timeout_s": drain_timeout},
            )


class _HealthServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, sock: socket.socket | None = None) -> None:
        super().__init__(config)
        self._sock = sock

    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        await super().serve(sockets if sockets is not None or self._sock is None else [self._sock])

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        # `main` owns SIGTERM/SIGINT: they set `stop`, which stops this server too.
        yield


def _allocated_bytes(root: Path) -> int:
    """Bytes allocated to everything under ``root``, directories included and a file
    with several links once, as ``du`` (and so kubelet, measuring an emptyDir) counts
    them. A symlink to a directory counts its own blocks, not its target's, as ``du``
    does without ``-L``. An entry removed while it is walked is skipped."""
    seen: set[tuple[int, int]] = set()
    total = 0
    for directory, dirs, files in os.walk(root):
        # `os.walk` lists a symlinked directory in `dirs` but never walks it.
        links = (name for name in dirs if os.path.islink(os.path.join(directory, name)))
        names = (*files, *links)
        for path in (directory, *(os.path.join(directory, name) for name in names)):
            with contextlib.suppress(FileNotFoundError):
                stat = os.lstat(path)
                if stat.st_nlink > 1:
                    if (stat.st_dev, stat.st_ino) in seen:
                        continue
                    seen.add((stat.st_dev, stat.st_ino))
                total += stat.st_blocks * 512
    return total


#: Seconds one walk of the data directory answers the scraper for (#2020). The walk
#: covers libraries, fonts and the piece cache, so a 15-30 s scrape would otherwise
#: walk all of it each time; the API keeps its store walk as long (`STORE_USAGE_MAX_AGE`).
#: The gauge feeds an eviction alert that fires over minutes, so a minute stale is fine.
DATA_USAGE_MAX_AGE = 60.0


@dataclass
class _DataUsage:
    """``_allocated_bytes(root)``, walked again only once ``max_age`` has passed."""

    root: Path
    max_age: float = DATA_USAGE_MAX_AGE
    clock: Callable[[], float] = time.monotonic
    _value: int | None = field(default=None, init=False)
    _read_at: float = field(default=0.0, init=False)

    async def read(self) -> int:
        now = self.clock()
        if self._value is None or now - self._read_at >= self.max_age:
            self._value = await asyncio.to_thread(_allocated_bytes, self.root)
            self._read_at = now
        return self._value


async def _refresh_store_metrics(metrics: Metrics, store: StoreBundle, data: _DataUsage) -> None:
    """The store gauges this process owns: its whole data directory, which is an
    emptyDir evicted past its sizeLimit (#1785), first, since it needs no database; then
    its piece cache and the key it holds. The store's usage is the API's to export (one
    database, one set of numbers)."""
    metrics.render_data_bytes.set(await data.read())
    health = await store_health(store)
    metrics.store_render_key_fallback.set(1 if health.render_key_fallback else 0)
    if isinstance(store.blobs, CachedBlobStore):
        metrics.worker_cache_bytes.set(await asyncio.to_thread(store.blobs.cached_bytes))


def _health_app(
    settings: Settings,
    metrics: Metrics,
    store: StoreBundle | None,
    task_queue: str,
    sdk_port: int | None = None,
    ready: asyncio.Event | None = None,
) -> Starlette:
    """``store`` is the render worker's; the print worker holds none (#1060). With
    ``sdk_port``, ``/metrics`` carries the Temporal SDK's own series too (#3788).

    ``/readyz`` (#4146) answers 503 until ``ready`` is set: for a rolling update's
    readiness probe, so the old pod is stopped only once the queue routes to this build.
    Without ``ready`` it answers as ``/healthz``. ``/healthz`` never waits on it: a
    liveness probe must not kill a worker whose Temporal is slow to take its build."""
    data = _DataUsage(settings.data_dir)

    def _body() -> dict[str, Any]:
        return {"ok": True, "build_id": settings.revision, "task_queue": task_queue}

    async def healthz(_: Request) -> JSONResponse:
        body = _body()
        if store is not None:
            body["store"] = (await store_health(store)).model_dump()
        return JSONResponse(body)

    async def readyz(_: Request) -> JSONResponse:
        if ready is not None and not ready.is_set():
            return JSONResponse(
                {**_body(), "ok": False, "reason": "the queue does not route to this build yet"},
                status_code=503,
            )
        return JSONResponse(_body())

    async def exposition(_: Request) -> Response:
        if store is not None:
            try:
                await _refresh_store_metrics(metrics, store, data)
            except Exception:
                # Like the API's: keep the last values, never fail the scrape.
                logger.exception("could not read the store's gauges")
        body = generate_latest(metrics.registry)
        if sdk_port is not None and sdk_reader is not None:
            body += await sdk_exposition(sdk_port, sdk_reader)
        return Response(body, media_type=CONTENT_TYPE_LATEST)

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        if sdk_reader is None:
            yield
            return
        async with sdk_reader:
            yield

    # One connection to the SDK's exporter for every scrape, closed with the server;
    # none when the worker serves no SDK series.
    sdk_reader = httpx.AsyncClient(timeout=SDK_READ_TIMEOUT) if sdk_port is not None else None
    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/readyz", readyz),
            Route("/metrics", exposition),
        ],
        lifespan=lifespan,
    )


#: Where a worker serves /healthz and /metrics: a port to bind, or a socket already
#: bound (#3807: a test holds its port from picking it to serving, so nothing else on
#: the runner can take it in between).
HealthListen = int | socket.socket


def _sdk_runtime(settings: Settings) -> tuple[Runtime | None, int | None]:
    """The Runtime a worker connects with when it connects for itself, and the port
    its /metrics reads the SDK's series from (#3788); neither when the port is 0, or
    when the exporter cannot bind it: the series are optional, the worker is not."""
    port = settings.temporal_sdk_metrics_port
    if not port:
        return None, None
    try:
        return sdk_runtime(port), port
    except ValueError as err:
        # The SDK raises ValueError when its exporter's port is taken (two workers in
        # one network namespace, as in a compose or dev run), and nothing narrower:
        # temporalio 1.34 maps the core's bind error to a plain ValueError.
        logger.warning("serving no Temporal SDK metrics on port %d: %s", port, err)
        return None, None


async def _own_client(settings: Settings, client: Client | None) -> tuple[Client, int | None]:
    """``client``, or one this worker connects for itself on the SDK metrics Runtime,
    and the port its /metrics reads them from: none for a client it was handed, whose
    Runtime it did not choose."""
    if client is not None:
        return client, None
    runtime, sdk_port = _sdk_runtime(settings)
    return (
        await connect(settings.temporal_address, settings.temporal_namespace, runtime=runtime),
        sdk_port,
    )


def _health_server(
    settings: Settings,
    metrics: Metrics,
    store: StoreBundle | None,
    listen: HealthListen,
    task_queue: str,
    sdk_port: int | None = None,
    ready: asyncio.Event | None = None,
) -> _HealthServer:
    app = _health_app(settings, metrics, store, task_queue, sdk_port, ready)
    if isinstance(listen, socket.socket):
        config = uvicorn.Config(app, log_config=None, access_log=False)
        return _HealthServer(config, listen)
    return _HealthServer(
        uvicorn.Config(app, host="0.0.0.0", port=listen, log_config=None, access_log=False)
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
    stop_now: asyncio.Event | None = None,
    health_port: HealthListen | None = HEALTH_PORT,
    client: Client | None = None,
) -> None:
    stop = stop or asyncio.Event()
    # Off the loop, as the API's boot seeds its libraries: the seed copies trees, and
    # the rest opens the projection's pool, waits for the API's schema and grants
    # (#601), and reads the store settings. A stop meanwhile ends the wait.
    try:
        deps, store = await asyncio.to_thread(build_worker_deps, settings, stopping=stop.is_set)
    except WorkerStoppedError:
        logger.info("stopped before the database was ready for this worker")
        return
    assert deps.metrics is not None and deps.thumbnail_executor is not None
    evicting = _start_housekeeping(deps, deps.config.asset_sweep_interval)
    try:
        client, sdk_port = await _own_client(settings, client)
        routed = asyncio.Event()
        server = (
            _health_server(
                settings,
                deps.metrics,
                store,
                health_port,
                settings.temporal_task_queue_render,
                sdk_port,
                routed,
            )
            if health_port is not None
            else None
        )
        serving = asyncio.create_task(server.serve()) if server is not None else None
        try:
            async with LoopWatch(deps.metrics):
                await _poll(
                    settings, deps, client, stop, drain=True, stop_now=stop_now, routed=routed
                )
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


@dataclass
class PrintWorkerDeps:
    """What ``--queue bambuddy`` holds (#1060, spec 2026-10-01 §5.5): Postgres, the
    event bus, the Bambuddy key (in the stored settings) and the API's internal URL. It
    mounts no data volume and runs no template code."""

    settings_store: SettingsStore
    events: PgNotifyEventBus
    outputs: RemoteOutputs
    rack: RackUsageStore
    metrics: Metrics
    print_runs: PrintRunStore
    operations: OperationStore
    activities: list[Callable[..., Any]]
    follow_print: Callable[..., Any]
    #: Native library files, read through the API's internal routes (#3827).
    library: LibraryReads | None = None
    #: The printer registry (#3823): each LAN printer's watch, on the follow queue. The
    #: KEK opens a printer's access code; without it the watches end, saying why.
    printers: PrinterStore | None = None
    reports: PrinterReportStore | None = None
    kek: Kek | None = None
    printer_watch: Callable[..., Any] | None = None

    async def aclose(self) -> None:
        await self.outputs.aclose()
        if isinstance(self.library, RemoteLibrary):
            await self.library.aclose()
        await self.events.aclose()
        await asyncio.to_thread(self.rack.close)
        await asyncio.to_thread(self.settings_store.close)


class ApiUrlMissingError(ValueError):
    pass


async def _given(library: RemoteLibrary) -> RemoteLibrary | None:
    return library


def build_print_deps(settings: Settings) -> PrintWorkerDeps:
    """The print worker's stores, over the API's database and its internal routes. The
    Bambuddy settings, key included, are read from the database on every use."""
    if settings.api_internal_url is None:
        raise ApiUrlMissingError(
            "SCADBUDDY_API_INTERNAL_URL is required for --queue bambuddy: the print worker"
            " reads outputs through the API's cluster-internal Service, e.g."
            " http://scadbuddy:8080"
        )
    metrics = Metrics()
    metrics.build_info.labels(settings.version, settings.revision).set(1)
    events = PgNotifyEventBus(
        settings.database_url, listener=PgListener(settings.database_url), metrics=metrics
    )
    settings_store = SettingsStore(settings, events=events)
    settings_store.open()
    pool = settings_store.pool
    uploads = BambuddyUploadStore(pool)
    links = PrintLinkStore(pool)
    outputs = RemoteOutputs(settings.api_internal_url)
    # Asked of the API whichever library serves: on ``bambuddy`` it answers every native
    # id with the 404 of a file not found. A native run's copy is written through it too
    # (#3818 tier A Task 4).
    library = RemoteLibrary(settings.api_internal_url, httpx.AsyncClient(timeout=REMOTE_TIMEOUT))
    rack = RackUsageStore(settings.database_url)
    observer = ProgressObserver(events)

    activity = ActivityIndex(pool, events=events, metrics=metrics)
    print_runs = PrintRunStore(pool, events=events, activity=activity)
    operations = OperationStore(pool, events=events, activity=activity)
    kek = settings_kek(settings.secret_key_file)
    printers = PrinterStore(pool, kek, previous_kek=settings_kek(settings.secret_key_previous_file))
    reports = PrinterReportStore(pool)
    # A LAN print's progress is its printer's report (plan 2026-10-16 Task 9).
    # The follow's newest-failure read shares its newest-run query (#4540 review).
    recent_runs = RecentRuns(print_runs)
    # A subject's ``print.run`` drops its kept run (#4541).
    events.add_listener(recent_runs.heard)
    lan = LanProgress(recent_runs.latest, printers, reports)

    async def read_progress(meta: OutputMeta) -> PrintProgress | None:
        # As the API's: the follow links archives too (#306), which the rack's settle
        # hook needs (#836). A LAN print's follow builds no client (plan 2026-10-18 D3).
        async def bambuddy() -> PrintProgress | None:
            async with client_for(settings_store.load()) as client:
                return await progress_for(client, meta, uploads=uploads, links=links)

        return await lan_first(lan, meta.id, bambuddy)

    async def read_library(subject: PrintSubject) -> PrintProgress | None:
        async def bambuddy() -> PrintProgress | None:
            async with client_for(settings_store.load()) as client:
                return await library_progress(client, subject, links, uploads=uploads)

        return await lan_first(lan, subject.run_subject, bambuddy)

    follower = Follower(
        outputs=outputs,
        observer=observer,
        read=read_progress,
        read_library=read_library,
        events=events,
        on_settled=[settle_hook(rack, links, settings_store.load)],
        # A LAN print's last print and rack picks follow its reads (#4644, #4543).
        on_read=[lan_last_print(OutputPrintStore(pool)), lan_settle_hook(rack)],
        newest_failed=newest_failed_from(
            print_runs, settings_store.load, latest=recent_runs.latest
        ),
        record_outcome=PrintOutcomeStore(pool, events).record,
    )
    # The image's profiles, read once for the dialog's starts and the runs' reuse keys,
    # with the kept copy of Bambuddy's local presets (#4645).
    native = native_slice_starter(
        settings, Profiles.installed(settings.slicer_profiles_dir, held=HeldPresetStore(pool))
    )
    kinds = bambuddy_kinds_over(
        settings_store=settings_store,
        outputs=outputs,
        uploads=uploads,
        links=links,
        archive_cache=ArchiveCache(),
        library=partial(_given, library),
        native_slices=NativeSliceStore(pool),
        native=native,
        # A native printer's background slice is decided on its registry row, as the
        # run is (plan 2026-10-18 D2).
        printers=printers,
        reports=reports,
    )
    printing = PrintActivities(
        PrintDeps(
            settings_store=settings_store,
            outputs=outputs,
            prints=OutputPrintStore(pool),
            uploads=uploads,
            store=print_runs,
            observer=observer,
            rack=rack,
            links=links,
            library=library,
            native=native,
            printers=printers,
            reports=reports,
            # A native run's plan lays the providers' records over its trays (#4551):
            # the same providers the API discovers, over this worker's settings.
            spools=Spools(
                store=SpoolStore(pool),
                providers=discover_providers(SettingsOnly(settings_store), None),
            ),
        )
    )
    # A LAN print's sliced blob, through the API's internal route with a token signed
    # under the key (plan 2026-10-16 Task 9); a LAN print needs the key anyway.
    blobs = RemoteBlobs(settings.api_internal_url, kek) if kek is not None else None
    lan_printing = LanActivities(
        LanDeps(
            printers=printers,
            settings_store=settings_store,
            library=library,
            blobs=blobs,
            rack=rack,
            # A run's next plate copy waits on the printer's report (#4541).
            reports=reports,
        )
    )
    return PrintWorkerDeps(
        settings_store=settings_store,
        events=events,
        outputs=outputs,
        rack=rack,
        metrics=metrics,
        print_runs=print_runs,
        operations=operations,
        activities=[
            *printing.all(),
            *lan_printing.all(),
            *operation_activities(
                operations,
                settings_store,
                {kind.name: kind for kind in kinds},
            ),
        ],
        follow_print=FollowActivities(follower, running=metrics.print_follows_running).follow_print,
        library=library,
        printers=printers,
        reports=reports,
        kek=kek,
        printer_watch=WatchActivities(printers, reports, events).printer_watch,
    )


async def run_print_worker(
    settings: Settings,
    *,
    stop: asyncio.Event | None = None,
    stop_now: asyncio.Event | None = None,
    health_port: HealthListen | None = HEALTH_PORT,
    client: Client | None = None,
) -> None:
    """``python -m scadbuddy.worker --queue bambuddy``: the ``scadbuddy-print`` worker
    (#1060), versioned like the render worker but not drained: its workflows are
    AUTO_UPGRADE (`print_deployment`, #2066), so the next build takes them over."""
    stop = stop or asyncio.Event()
    deps = await asyncio.to_thread(build_print_deps, settings)
    # The image's preset catalogue, built behind the start for the first native plan_run
    # (#3818 tier A); nothing waits on it.
    warming = warm_catalogue(
        await asyncio.to_thread(Profiles.installed, settings.slicer_profiles_dir),
        slices_native(deps.settings_store),
    )
    try:
        await deps.events.start()
        client, sdk_port = await _own_client(settings, client)
        queue = settings.temporal_task_queue_bambuddy
        build_id = settings.revision
        workers = [
            bambuddy_worker(client, queue, deps.activities, build_id=build_id),
            follow_worker(
                client,
                queue,
                deps.follow_print,
                printer_watch=deps.printer_watch,
                build_id=build_id,
            ),
        ]
        routed = asyncio.Event()
        server = (
            _health_server(settings, deps.metrics, None, health_port, queue, sdk_port, routed)
            if health_port is not None
            else None
        )
        serving = asyncio.create_task(server.serve()) if server is not None else None
        try:
            async with LoopWatch(deps.metrics):
                await _serve_versioned(
                    client,
                    workers,
                    stop,
                    build_id=build_id,
                    deployment_name=PRINT_DEPLOYMENT_NAME,
                    task_queue=queue,
                    drain_timeout=None,
                    stop_now=stop_now,
                    ready=(queue, routed),
                )
        finally:
            if server is not None and serving is not None:
                server.should_exit = True
                await serving
    finally:
        if warming is not None:
            warming.cancel()
            await asyncio.gather(warming, return_exceptions=True)
        await deps.aclose()


class ProjectsKeyMissingError(ValueError):
    pass


async def run_projects_worker(
    settings: Settings,
    *,
    stop: asyncio.Event | None = None,
    health_port: HealthListen | None = HEALTH_PORT,
) -> None:
    """``python -m scadbuddy.worker --queue projects``: flow runs (#1057, spec 2026-10-01
    §4.3). Postgres and the KEK only: a flow's payloads are sealed per run, and it never
    runs unsealed. Versioned (#4146): the Worker Deployment ``scadbuddy-projects`` at
    ``SCADBUDDY_REVISION``, its build made current once at start and `/readyz` ready once
    the queue routes to it, as the print worker. Every workflow on it is AUTO_UPGRADE
    (`client.projects_deployment`), never pinned (plan 2026-10-09 Ruling 13), so it stops
    without a drain; a run in flight moves to the current build at its next task."""
    stop = stop or asyncio.Event()
    keys = payload_keys(settings)
    if keys is None:
        raise ProjectsKeyMissingError(
            "SCADBUDDY_SECRET_KEY_FILE is required for --queue projects: a flow's payloads"
            " are sealed under keys the KEK protects (spec 2026-10-01 §6.5)"
        )
    if settings.api_internal_url is None:
        raise ApiUrlMissingError(
            "SCADBUDDY_API_INTERNAL_URL is required for --queue projects: a flow's render,"
            " print and arrange call the API's cluster-internal Service, e.g."
            " http://scadbuddy:8080"
        )
    metrics = Metrics()
    metrics.build_info.labels(settings.version, settings.revision).set(1)
    events = PgNotifyEventBus(
        settings.database_url, listener=PgListener(settings.database_url), metrics=metrics
    )
    settings_store = SettingsStore(settings, events=events)
    await asyncio.to_thread(settings_store.open)
    try:
        await events.start()
        runtime, sdk_port = _sdk_runtime(settings)
        client = await connect_flows(
            settings.temporal_address, settings.temporal_namespace, keys[0], runtime=runtime
        )
        store = FlowStore(settings_store.pool, events=events)
        queue = settings.temporal_task_queue_projects
        decisions = operation_activities(
            OperationStore(settings_store.pool, events=events),
            settings_store,
            {
                kind.name: kind
                for kind in flow_kinds(
                    store,
                    keys[0],
                    approval_key(settings),
                    api=lambda: api_client(settings.api_internal_url),
                )
            },
        )
        api = api_client(settings.api_internal_url)
        routes = FlowRoutes(api)
        grants = FlowGrants(store, api, approval_key(settings))
        build_id = settings.revision
        worker = projects_worker(
            client,
            queue,
            [*FlowActivities(store).all(), *routes.all(), *grants.all(), *decisions],
            deployment_config=projects_deployment(build_id),
        )
        routed = asyncio.Event()
        server = (
            _health_server(settings, metrics, None, health_port, queue, sdk_port, routed)
            if health_port is not None
            else None
        )
        serving = asyncio.create_task(server.serve()) if server is not None else None
        try:
            async with LoopWatch(metrics):
                await _serve_versioned(
                    client,
                    [worker],
                    stop,
                    build_id=build_id,
                    deployment_name=PROJECTS_DEPLOYMENT_NAME,
                    task_queue=queue,
                    drain_timeout=None,
                    ready=(queue, routed),
                )
        finally:
            if server is not None and serving is not None:
                server.should_exit = True
                await serving
            await routes.aclose()
    finally:
        await events.aclose()
        await asyncio.to_thread(settings_store.close)


Queue = Literal["render", "bambuddy", "projects"]

#: Each worker's ``service.name`` (#2292): the Deployment it runs as, so the render and
#: print workers are told apart in Tempo rather than all reporting as one service.
SERVICE_NAMES: dict[Queue, str] = {
    "render": "scadbuddy-render",
    "bambuddy": "scadbuddy-print",
    "projects": "scadbuddy-projects",
}


def _on_signal(stop: asyncio.Event, stop_now: asyncio.Event) -> Callable[[], None]:
    """The first SIGTERM/SIGINT stops and drains; a second cuts the drain short (#605)."""

    def handle() -> None:
        if stop.is_set():
            stop_now.set()
        else:
            stop.set()

    return handle


async def _main(settings: Settings, queue: Queue = "render") -> None:
    stop, stop_now = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal(stop, stop_now))
    if queue == "bambuddy":
        await run_print_worker(settings, stop=stop, stop_now=stop_now)
    elif queue == "projects":
        await run_projects_worker(settings, stop=stop)
    else:
        await run_worker(settings, stop=stop, stop_now=stop_now)


def parse_queue(argv: Sequence[str] | None = None) -> Queue:
    parser = argparse.ArgumentParser(prog="python -m scadbuddy.worker")
    parser.add_argument(
        "--queue",
        choices=("render", "bambuddy", "projects"),
        default="render",
        help="render: the render worker (scadbuddy-render); bambuddy: the print worker"
        " (scadbuddy-print, #1060); projects: flow runs (scadbuddy-projects, #1057)",
    )
    queue: Queue = parser.parse_args(argv).queue
    return queue


def main() -> None:
    queue = parse_queue()
    settings = Settings()
    configure_logging(settings.log_level)
    # parse_queue returns a Queue, and SERVICE_NAMES must name every one (#2894).
    if queue not in SERVICE_NAMES:
        # Not an assert, which python -O strips (#3020 review).
        raise SystemExit(f"queue {queue!r} has no service name")
    configure_tracing(SERVICE_NAMES[queue], version=settings.version, revision=settings.revision)
    # `kill -USR1 1` in the pod writes every thread's stack to stderr (#3788): the
    # loop watch logs a stall by itself, this is for a hang it does not see.
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    asyncio.run(_main(settings, queue))


if __name__ == "__main__":
    main()
