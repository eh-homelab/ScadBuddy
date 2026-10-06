from __future__ import annotations

import asyncio
import importlib
import logging
import pkgutil
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from datetime import UTC, datetime
from functools import partial
from typing import Any, Final

from fastapi import APIRouter, FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import BaseModel
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

import scadbuddy.api
from scadbuddy import __version__
from scadbuddy.api import assets, health, libraries, media, metrics, models, telemetry
from scadbuddy.api.agent_actor import AgentActorGate, postgres_grants
from scadbuddy.api.cross_site import CrossSiteGate
from scadbuddy.api.deps import STATE_ATTR, AppState, build_state, probe_openscad_version
from scadbuddy.api.limits import BODY_LIMITS, MEDIA_UPLOAD_PATH, BodySizeGate, RouteLimit
from scadbuddy.api.runtime import apply_runtime, follow_changes
from scadbuddy.api.static import SPAStaticFiles
from scadbuddy.bambuddy.follow import FollowActivities
from scadbuddy.bambuddy.output_reader import LocalOutputs
from scadbuddy.bambuddy.runs import PrintRunStore
from scadbuddy.core.authorship import AgentAuthorship
from scadbuddy.core.logging import configure_logging
from scadbuddy.core.metrics import HttpMetrics
from scadbuddy.core.paths import BUILTIN_DIR, MODEL_META_NAME
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.problems import install_problem_handlers
from scadbuddy.core.settings import Settings
from scadbuddy.core.tracing import configure_tracing
from scadbuddy.library.assets import referenced_asset_ids
from scadbuddy.library.history import GitError
from scadbuddy.library.library_seed import seed_libraries, seeded_checkouts
from scadbuddy.library.previews import sweep_work_dirs
from scadbuddy.library.settings_store import load_render_store_settings
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.operations.component import OPERATIONS, OperationCommands
from scadbuddy.operations.kinds import OperationKind, Queue
from scadbuddy.operations.store import OperationStore
from scadbuddy.rack.component import RACK_USAGE
from scadbuddy.render.previews import TIMEOUT_FACTOR as PREVIEW_TIMEOUT_FACTOR
from scadbuddy.render.previews import PreviewUnrunError
from scadbuddy.store import sweep_blobs
from scadbuddy.store.assets import RemoteAssets
from scadbuddy.store.bambuddy import RenderSettingsSource
from scadbuddy.store.cache import CachedBlobStore
from scadbuddy.store.content import sweep_content
from scadbuddy.store.factory import build_store
from scadbuddy.worker import run_inprocess_worker, worker_deps_from_state
from scadbuddy.workflows.activities import WorkerDeps
from scadbuddy.workflows.client import (
    bambuddy_worker,
    connect,
    follow_worker,
    reconcile_lost_operations,
    reconcile_lost_runs,
)
from scadbuddy.workflows.follow import resume_followed
from scadbuddy.workflows.housekeeping import (
    HEARTBEAT_TIMEOUT,
    SWEEPS,
    ensure_schedules,
    library_worker,
)
from scadbuddy.workflows.operation import OperationWorkflow
from scadbuddy.workflows.operation_activities import operation_activities
from scadbuddy.workflows.previews import (
    DUE_ACTIVITY,
    REFRESH_ACTIVITY,
    UNRUN_FAILURE,
    PreviewBackfill,
    ensure_preview_schedule,
)
from scadbuddy.workflows.print_activities import PrintActivities, PrintDeps

API_PREFIX = "/api/v1"

#: Never traced (spec §6): set in code so no deployment can drop it.
#: Regexes searched against the full ``scheme://host/path`` URL, so each is anchored on
#: both ends: an unanchored ``/metrics`` would also drop ``/api/v1/models/metrics-x``.
EXCLUDED_URLS: Final = ",".join(
    f"^[a-z]+://[^/]+{path}$" for path in ("/healthz", "/metrics", "/telemetry/v1/traces")
)

logger = logging.getLogger(__name__)

DESCRIPTION = "Self-hosted OpenSCAD customizer for Bambuddy."


#: The `scadbuddy.api` modules whose router sits at the root rather than under
#: :data:`API_PREFIX`.
ROOT_ROUTE_MODULES = frozenset({"health", "metrics", "telemetry"})


def _api_router() -> APIRouter:
    """Every other module in `scadbuddy.api` that defines a ``router``, under
    :data:`API_PREFIX`: a new route module is mounted without an edit here. The order
    is by name and does not matter, because no two routes match the same request
    (`tests/api/test_routes.py`)."""
    router = APIRouter(prefix=API_PREFIX)
    for info in sorted(pkgutil.iter_modules(scadbuddy.api.__path__), key=lambda i: i.name):
        if info.name in ROOT_ROUTE_MODULES:
            continue
        module = importlib.import_module(f"scadbuddy.api.{info.name}")
        module_router = getattr(module, "router", None)
        if isinstance(module_router, APIRouter):
            router.include_router(module_router)
    return router


def _name_in_openapi(app: FastAPI, *extra: type[BaseModel]) -> None:
    """Add models no route declares to the OpenAPI `components`.

    A route that parses its own body, as `POST /models` does for its three content
    types, can only describe it through `openapi_extra`, which FastAPI copies in
    verbatim: the model would be inlined and never named, and the generated client
    would have no type for it. Named here, `openapi_extra` points at it by `$ref`.
    """
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = generate()
        named = schema.setdefault("components", {}).setdefault("schemas", {})
        for model in extra:
            named[model.__name__] = model.model_json_schema(
                ref_template="#/components/schemas/{model}"
            )
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


async def _close_quietly(state: AppState) -> None:
    """Close the render service and the bus after a failed start, logging (not
    raising) what fails, so the start's own error is the one that propagates."""
    closes: list[Callable[[], Awaitable[object]]] = [
        state.events.aclose,
        state.render.aclose,
        partial(asyncio.to_thread, state.projection.close),
    ]
    store = getattr(state, "store", None)  # unset when the start failed before it
    if store is not None:
        closes.append(store.aclose)
    for close in closes:
        try:
            await close()
        except Exception:
            logger.exception("could not release what a failed start opened")


def sweep_assets(state: AppState) -> list[str]:
    """Remove the uploads nothing references or has used for the grace (#296).

    The references are read first -- every job in the projection, then every
    saved preset, output and template (`referenced_asset_ids`) -- and any failure to read
    them raises before anything is removed. What is referenced after that is kept by
    its last use, which the sweep re-checks under the store's lock per asset.
    """
    jobs = state.render.store.list_jobs()
    params = [job.params for job in jobs] + state.presets.saved_params()
    referenced = referenced_asset_ids(state.paths, params)
    removed = state.assets.sweep(referenced, grace=state.config.asset_sweep_grace)
    state.metrics.assets_swept.inc(len(removed))
    if removed:
        logger.info("removed unused uploads", extra={"count": len(removed)})
    return removed


def _sweep_checkouts(state: AppState) -> list[str]:
    """The thread half of :func:`sweep_library_checkouts`."""
    # Every id any revision of any model.json -- live or deleted model, mine or a
    # built-in -- ever held: ONE `git log -p`. A restore
    # puts a revision's pins back, so each of them is still a pin. Glob pathspecs, so
    # `*` stops at `/`: a model's own model.json, a built-in's one level deeper, and
    # no file of that name inside a model's folder.
    named = state.history.object_ids_in(
        f":(glob)*/{MODEL_META_NAME}",
        f":(glob){BUILTIN_DIR}/*/{MODEL_META_NAME}",
    )
    # The image's seed (#169) is kept pinned or not: the boot would copy it back.
    seed_dir = state.settings.resolve_seed_libraries_dir()
    seeded = set(seeded_checkouts(seed_dir)) if seed_dir is not None else set()

    def keep(name: str, commit: str) -> bool:
        return (
            commit in named
            or (name, commit) in seeded
            or bool(state.checkouts.leased(state.paths.libraries / name / commit))
            # The live pins as a removal counts them: uncommitted edits, and an
            # entry with no commit or an unreadable model.json keeps every checkout.
            or bool(state.catalogue.library_users(name, commit))
        )

    return state.libraries.sweep_checkouts(keep)


async def sweep_library_checkouts(state: AppState) -> list[str]:
    """Remove the library checkouts that nothing pins (#271): no live model, and no
    revision of any model in the history -- so restoring any revision never needs a
    checkout this removed.

    Under the checkout gate alone, as a removal: no pin or render in this process
    runs meanwhile. Another replica sharing ``/data`` is kept apart by the age
    guard in :meth:`LibraryStore.sweep_checkouts`. Without a repository there is
    no history to read, so nothing is swept.
    """
    if not state.history.available:
        return []
    async with state.checkouts.removing():
        removed = await asyncio.to_thread(_sweep_checkouts, state)
    if removed:
        logger.info("removed unpinned library checkouts", extra={"checkouts": removed})
    return removed


def _remote_assets(state: AppState) -> RemoteAssets | None:
    return state.store.remote_assets


async def drop_swept_assets(state: AppState, removed: list[str], *, cutoff: datetime) -> None:
    """The store's copies of what `sweep_assets` just removed from the volume; only
    those not stored again since ``cutoff``, the sweep's start."""
    remote = _remote_assets(state)
    if remote is not None and removed:
        await remote.drop(removed, cutoff=cutoff)


async def _sweep_assets_logged(state: AppState) -> None:
    # Best effort: a store or volume error skips this sweep (removing nothing it could
    # not prove unused) and the next one retries. Only the Schedule's sweep runs it, so
    # a failure is logged and fails its activity, which reports it.
    try:
        remote = _remote_assets(state)
        cutoff = await remote.clock() if remote is not None else None
        removed = await asyncio.to_thread(sweep_assets, state)
        if remote is not None and cutoff is not None:
            await drop_swept_assets(state, removed, cutoff=cutoff)
            # What an earlier drop failed to remove, and what the store lost (a copy an
            # `ensure` dropped, a race with a delete): both converge here, per sweep.
            await remote.reconcile(state.assets, cutoff=cutoff)
            await remote.backfill(state.assets)
    except Exception:
        logger.exception("could not sweep unused uploads")
        raise


async def _sweep_duplicate_staging_logged(state: AppState, *, reraise: bool = False) -> None:
    """The boot skips a volume error; the Schedule's sweep (``reraise``) logs any error,
    then fails its activity (review #1095b 4)."""
    try:
        await asyncio.to_thread(state.catalogue.sweep_duplicate_staging)
    except Exception as error:
        if not reraise and not isinstance(error, OSError):
            raise
        logger.exception("could not sweep duplicate staging folders")
        if reraise:
            raise


async def _sweep_blobs_logged(state: AppState) -> None:
    """The Temporal path's blob store: the pieces no job references any more. Only
    the Schedule's sweep runs it, so a failure is logged and fails its activity."""
    try:
        if state.store.content is None:
            removed = await asyncio.to_thread(
                sweep_blobs, state.blobs, state.refs, grace=state.config.job_ttl
            )
        else:
            removed = await sweep_content(
                state.store.content, state.refs, grace=state.config.job_ttl
            )
            if isinstance(state.store.blobs, CachedBlobStore):
                await asyncio.to_thread(state.store.blobs.evict)
    except Exception:
        logger.exception("could not sweep unreferenced blobs")
        raise
    if removed:
        logger.info("removed unreferenced blobs", extra={"count": len(removed)})


async def _backfill_store_logged(state: AppState, *, uploads: bool, fonts: bool = True) -> None:
    """The boot's mirror of what predates the store, in the background and best effort:
    an unreachable Bambuddy never holds up or fails the start. Uploads only when the
    housekeeping Schedule's sweep does not backfill them: `SCADBUDDY_ASSET_SWEEP_INTERVAL`
    0, or the Schedule paused (`_run_library_worker`, review #1095 2). Fonts on every
    boot: nothing else mirrors a family installed before."""
    try:
        if uploads and state.store.remote_assets is not None:
            mirrored = await state.store.remote_assets.backfill(state.assets)
            logger.info("mirrored uploads", extra={"count": mirrored})
        if fonts and state.store.fonts is not None:
            logger.info("mirrored fonts", extra={"count": await state.store.fonts.backfill()})
    except Exception:
        logger.exception("could not mirror what predates the blob store; the next boot retries")


#: How often a long sweep heartbeats: well inside its `HEARTBEAT_TIMEOUT`.
HEARTBEAT_EVERY = HEARTBEAT_TIMEOUT.total_seconds() / 4


async def _heartbeating[T](work: Coroutine[Any, Any, T]) -> T:
    """Run ``work`` in the current activity, heartbeating until it ends (review #1095
    2): a worker lost mid-sweep is then noticed within the heartbeat timeout."""
    running = asyncio.create_task(work)
    try:
        while not (await asyncio.wait({running}, timeout=HEARTBEAT_EVERY))[0]:
            activity.heartbeat()
    finally:
        running.cancel()
    return await running


def _housekeeping_activities(state: AppState) -> list[Callable[..., Any]]:
    """The Schedule's sweeps (#1054), in `SWEEPS` order, each the loop's best-effort
    call: it logs a failure, then fails the activity (review #1095 1), so the run
    reports it; the other sweeps still run and the next tick tries again."""

    @activity.defn(name=SWEEPS[0])
    async def prune_jobs() -> None:
        try:
            await state.render.prune()
        except Exception:
            logger.exception("could not prune settled render jobs")
            raise

    @activity.defn(name=SWEEPS[1])
    async def sweep_assets() -> None:
        await _heartbeating(_sweep_assets_logged(state))

    @activity.defn(name=SWEEPS[2])
    async def sweep_blobs() -> None:
        await _heartbeating(_sweep_blobs_logged(state))

    @activity.defn(name=SWEEPS[3])
    async def sweep_staging() -> None:
        # A crashed duplicate's staging otherwise waits for the next boot (#397).
        await _heartbeating(_sweep_duplicate_staging_logged(state, reraise=True))

    @activity.defn(name=SWEEPS[4])
    async def sweep_claims() -> None:
        try:
            await asyncio.to_thread(ClaimStore(state.paths.claims).sweep)
        except Exception:
            logger.exception("could not sweep operation claims")
            raise

    return [prune_jobs, sweep_assets, sweep_blobs, sweep_staging, sweep_claims]


def _preview_activities(state: AppState) -> list[Callable[..., Any]]:
    """The preview backfill's steps (#1054): what the boot's pass over every model did,
    on its Schedule. With previews off (a run left open from before), both do nothing."""

    @activity.defn(name=DUE_ACTIVITY)
    async def previews_due() -> list[str]:
        previews = state.previews
        if previews is None:
            return []
        try:
            return await asyncio.to_thread(previews.due)
        except Exception:
            logger.exception("could not list the models to render their previews")
            raise

    @activity.defn(name=REFRESH_ACTIVITY)
    async def preview_refresh(slug: str) -> bool:
        previews = state.previews
        if previews is None:
            return False
        try:
            return await _heartbeating(previews.refresh(slug, raise_unrun=True))
        except PreviewUnrunError as error:
            raise ApplicationError(str(error), type=UNRUN_FAILURE) from error
        except Exception:
            logger.exception("could not refresh a model's preview", extra={"slug": slug})
            raise

    return [previews_due, preview_refresh]


async def _prepare_catalogue(state: AppState) -> None:
    """The boot's passes over the catalogue, run before the render queue starts."""
    seed_dir = state.settings.resolve_seed_models_dir()
    if seed_dir is not None:
        await asyncio.to_thread(state.catalogue.sync_builtins, seed_dir)
    # After the sync, so the built-ins exist: a model the old seed copied in
    # becomes a duplicate of its built-in (#158). Contains its own failures.
    await asyncio.to_thread(state.catalogue.link_seeded)
    # A create or duplicate that died between claiming its slug and writing it left
    # an empty directory; it becomes a tombstone for the sweep below. Logs and skips
    # whatever it cannot read or move, so it never stops the boot.
    await asyncio.to_thread(state.catalogue.sweep_stranded_claims)
    # A delete that died between its rename and its rmtree left a tombstone.
    # Best effort, as it is after a delete: leftovers must not stop the boot.
    try:
        await asyncio.to_thread(state.catalogue.sweep_tombstones)
    except OSError:
        logger.exception("could not sweep tombstones")
    # A duplicate the process died in the middle of left its staging copy. Nothing
    # is duplicating yet: no request has been served.
    await _sweep_duplicate_staging_logged(state)
    # Before the orphan sweep, which forgets the saved presets of templates that are
    # gone: the database, and its migrations, as the projection opens it.
    await asyncio.to_thread(state.presets.open)
    # Derived files a failed or raced delete left keyed to a slug that is gone.
    # It logs and skips whatever it cannot read, so it never stops the boot.
    await asyncio.to_thread(state.catalogue.sweep_orphans)
    # And their previews, which are rows in the database rather than files.
    try:
        await asyncio.to_thread(state.catalogue.sweep_orphan_previews)
    except Exception:
        logger.exception("could not sweep orphaned previews")
    # A default render the process died in left its scratch directory, which the
    # orphan sweep never reads: no slug names it. Only one older than any render may
    # run goes, since another replica may be rendering into it. Whether or not
    # previews are on: one may be left from when they were.
    try:
        await asyncio.to_thread(
            sweep_work_dirs, state.paths, state.config.render_timeout * PREVIEW_TIMEOUT_FACTOR
        )
    except OSError:
        logger.exception("could not sweep preview scratch directories")
    # A library clone the process died in the middle of. Nothing is cloning yet:
    # no request has been served.
    try:
        await asyncio.to_thread(state.libraries.sweep_staging)
    except OSError:
        logger.exception("could not sweep library staging clones")
    # The curated libraries baked into the image (#169), so a fresh volume renders
    # a BOSL2 model offline. Before the queue starts: the first render finds them.
    seed_libraries_dir = state.settings.resolve_seed_libraries_dir()
    if seed_libraries_dir is not None:
        try:
            await asyncio.to_thread(seed_libraries, state.paths, seed_libraries_dir)
        except OSError:
            logger.exception("could not seed library checkouts from the image")
    # It logs and keeps what it cannot remove; one that cannot read the history removes nothing.
    try:
        await sweep_library_checkouts(state)
    except (OSError, GitError):
        logger.exception("could not sweep library checkouts")


async def _start_render(state: AppState) -> None:
    """Open (and migrate) the projection, prepare the catalogue, fail what a legacy
    queue left running, connect the in-process worker's client, prune, and start the
    service (which settles the pending rows no workflow will run, then prunes). A
    failure leaves the projection to the lifespan's guard, which closes everything a
    failed start opened, each once."""
    projection, service, settings = state.projection, state.render, state.settings
    await asyncio.to_thread(projection.open)
    await _prepare_catalogue(state)
    # What a pre-Temporal release was running, nothing
    # will finish (#546).
    failed = await asyncio.to_thread(projection.fail_legacy_running)
    if failed:
        logger.warning(
            "failed the renders a pre-Temporal release left running",
            extra={"job_ids": [job.id for job in failed]},
        )
    if settings.temporal_worker_inprocess:
        # Eager: a worker cannot run on the API's lazy client (dev and tests).
        state.temporal = await connect(settings.temporal_address, settings.temporal_namespace)
    await service.prune()
    await service.start()


def _worker_exited(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
    """An in-process worker that ends before shutdown asked it to leaves renders
    pending: say so when it happens, not at shutdown."""
    if stop.is_set() or task.cancelled():
        return
    logger.error("the in-process render worker exited early", exc_info=task.exception())


async def _stop_worker(state: AppState, worker: asyncio.Task[None], deps: WorkerDeps) -> None:
    """Let the in-process worker drain for up to one activity, then cancel it."""
    try:
        await asyncio.wait_for(worker, state.config.activity_timeout)
    except TimeoutError:
        logger.warning("the in-process render worker did not drain in time; cancelled it")
    except Exception:
        logger.exception("the in-process render worker failed")
    if deps.thumbnail_executor is not None:
        deps.thumbnail_executor.shutdown(wait=False, cancel_futures=True)


#: How long the print worker waits before connecting again to a Temporal that is down.
PRINT_WORKER_RECONNECT = 5.0
#: How often the print worker task looks for runs whose execution is gone.
LOST_RUN_INTERVAL = 300.0


async def _connect_until(state: AppState, stop: asyncio.Event, name: str) -> Client | None:
    """The app's client, or one connected eagerly (a worker cannot run on the lazy
    client), retrying while Temporal is down; None once ``stop`` is set. A connect to a
    Temporal that never answers retries for a long time, so it is raced against
    ``stop``: the app's shutdown never waits it out."""
    settings = state.settings
    if state.temporal is not None:
        return state.temporal
    while not stop.is_set():
        connecting = asyncio.create_task(
            connect(settings.temporal_address, settings.temporal_namespace)
        )
        stopping = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({connecting, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
        if not connecting.done():
            connecting.cancel()
            return None
        error = connecting.exception()
        if error is None:
            return connecting.result()
        logger.warning("the %s worker cannot reach Temporal yet; retrying", name, exc_info=error)
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), PRINT_WORKER_RECONNECT)
    return None


async def _run_print_worker(state: AppState, stop: asyncio.Event) -> None:
    """Serve the ``bambuddy`` queue in this process until ``stop`` (#1052): only with
    SCADBUDDY_TEMPORAL_WORKER_INPROCESS or SCADBUDDY_TEMPORAL_PRINT_WORKER_INPROCESS;
    otherwise the ``scadbuddy-print`` worker serves it (#1060). Unversioned, on this
    process's stores and volume. It connects eagerly (a worker cannot run on the lazy
    client), retrying while Temporal is down, so the API still boots without it; print
    routes answer 503 meanwhile."""
    client = await _connect_until(state, stop, "print")
    if client is None:
        return
    settings = state.settings
    deps = PrintDeps(
        settings_store=state.settings_store,
        outputs=LocalOutputs(state.outputs, state.catalogue),
        prints=state.outputs.prints,
        uploads=state.uploads,
        store=state.print_runs.store,
        observer=state.print_progress,
        rack=state.components.get(RACK_USAGE),
    )
    ops = state.components.get(OPERATIONS)
    activities = [
        *PrintActivities(deps).all(),
        *operation_activities(ops.store, state.settings_store, _kinds_on(ops, "bambuddy")),
    ]
    while not stop.is_set():
        # A worker that fails is said at once and started again: until then every
        # print run waits on a queue nothing polls.
        queue = settings.temporal_task_queue_bambuddy
        workers = [
            bambuddy_worker(client, queue, activities),
            follow_worker(
                client,
                queue,
                FollowActivities(
                    state.print_follower, running=state.metrics.print_follows_running
                ).follow_print,
            ),
        ]
        if not await _serve_until(workers, stop):
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), PRINT_WORKER_RECONNECT)


async def _hand_off_watches(state: AppState, client: Client) -> None:
    """Follow on Temporal the prints the old in-process watcher recorded (#268)."""
    try:
        resumed = await resume_followed(
            state.projection.pool,
            client,
            state.settings.temporal_task_queue_bambuddy,
            datetime.now(UTC),
        )
        if resumed:
            logger.info(
                "following on Temporal the prints the old watcher followed",
                extra={"output_ids": resumed},
            )
    except Exception:
        logger.exception("could not hand the old watcher's prints to FollowPrint")


def serves_print_queue(settings: Settings) -> bool:
    """Whether the API process serves the ``bambuddy`` queue itself (#1060)."""
    return settings.temporal_worker_inprocess or settings.temporal_print_worker_inprocess


async def _print_upkeep(state: AppState, stop: asyncio.Event) -> None:
    """What the API does for print runs whoever serves their queue (#1060): hand the old
    watcher's prints to ``FollowPrint`` once and, beside it, end the runs and operations
    whose execution closed without ending them, until ``stop``. Postgres and a client only."""
    client = await _connect_until(state, stop, "print upkeep")
    if client is None:
        return
    ops = state.components.get(OPERATIONS)
    # Beside the reconciler (review #1091 4): each follow it starts may wait out an RPC
    # timeout on a slow Temporal.
    handoff = asyncio.create_task(_hand_off_watches(state, client))
    try:
        await _end_lost_runs_until(client, state.print_runs.store, ops.store, stop)
    finally:
        # A row whose follow did not start stays for the next boot.
        handoff.cancel()
        with suppress(asyncio.CancelledError):
            await handoff


async def _run_library_worker(state: AppState, stop: asyncio.Event) -> None:
    """Serve the ``library`` queue until ``stop`` (#1054): the housekeeping Schedule's
    sweeps and the preview backfill need the data volume this process holds. Once
    connected it sets the Schedules up; Temporal down at boot only delays that."""
    client = await _connect_until(state, stop, "library")
    if client is None:
        return
    settings = state.settings
    queue = settings.temporal_task_queue_library

    async def set_up() -> None:
        paused = await _set_up_housekeeping(
            client,
            queue,
            state.config.asset_sweep_interval,
            state.previews.timeout if state.previews is not None else None,
            stop,
        )
        if paused and state.store.content is not None:
            # Its sweep would backfill the uploads (review #1095 2); paused, the start does.
            await _backfill_store_logged(state, uploads=True, fonts=False)

    schedules = asyncio.create_task(set_up())
    ops = state.components.get(OPERATIONS)
    activities = [
        *_housekeeping_activities(state),
        *_preview_activities(state),
        *operation_activities(ops.store, state.settings_store, _kinds_on(ops, "library")),
    ]
    try:
        while not stop.is_set():
            worker = library_worker(
                client, queue, activities, workflows=[PreviewBackfill, OperationWorkflow]
            )
            if not await _serve_until([worker], stop, name="library"):
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), PRINT_WORKER_RECONNECT)
    finally:
        schedules.cancel()


def _kinds_on(ops: OperationCommands, queue: Queue) -> dict[str, OperationKind]:
    """The kinds one worker serves (§4.3): only those whose effect it holds."""
    return {name: kind for name, kind in ops.kinds.items() if kind.queue == queue}


async def _set_up_housekeeping(
    client: Client, queue: str, interval: float, previews: float | None, stop: asyncio.Event
) -> bool:
    """The housekeeping Schedules and the preview backfill's (``previews``: the
    scheduler's render bound; None, previews off: none), retried until Temporal takes
    them: a frontend can answer the connect before it can create one. Each set-up
    triggers its Schedule, so the preview one comes last: a retry after the
    housekeeping ones failed does not run the backfill twice. True when the sweeps'
    one is paused."""
    while not stop.is_set():
        try:
            paused = await ensure_schedules(client, queue, interval)
            await ensure_preview_schedule(client, queue, previews)
            return paused
        except Exception:
            logger.warning("could not set up the library Schedules; retrying", exc_info=True)
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), PRINT_WORKER_RECONNECT)
    return False


async def _serve_until(
    workers: Sequence[Worker],
    stop: asyncio.Event,
    alongside: Coroutine[Any, Any, None] | None = None,
    *,
    name: str = "print",
) -> bool:
    """Run ``workers`` (and ``alongside``) until ``stop``: True. A worker that ends
    first, failed or not, is said at once (review #1061: a poller that dies while running
    would otherwise leave the queue unpolled until the pod restarts), and the others
    are shut down so all start again together: False."""
    running = [asyncio.create_task(worker.run()) for worker in workers]
    beside = asyncio.create_task(alongside) if alongside is not None else None
    stopping = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({*running, stopping}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        if beside is not None:
            beside.cancel()
        stopping.cancel()
    ended = [task for task in running if task.done()]
    for task in ended:
        error = task.exception()
        logger.error(
            "the %s worker failed; starting it again",
            name,
            exc_info=error if error is not None else RuntimeError("the worker stopped"),
        )
    for worker, task in zip(workers, running, strict=True):
        if not task.done():
            await worker.shutdown()
            with suppress(Exception):
                await task
    return not ended


async def _end_lost_runs_until(
    client: Client, store: PrintRunStore, operations: OperationStore, stop: asyncio.Event
) -> None:
    """Every ``LOST_RUN_INTERVAL`` until ``stop``, end the print runs (review #1061) and
    the operations (review #1063) whose execution closed without ending them: one
    terminated in the Temporal UI."""
    while not stop.is_set():
        try:
            ended = await reconcile_lost_runs(client, store)
            if ended:
                logger.warning("ended print runs whose execution was gone", extra={"count": ended})
        except Exception:
            logger.exception("could not check print runs for lost executions")
        try:
            ended = await reconcile_lost_operations(client, operations)
            if ended:
                logger.warning("ended operations whose execution was gone", extra={"count": ended})
        except Exception:
            logger.exception("could not check operations for lost executions")
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), LOST_RUN_INTERVAL)


async def _stop_queue_worker(task: asyncio.Task[None] | None, name: str) -> None:
    """Wait for the ``name`` queue's worker task (print, library) to stop."""
    if task is None:
        return
    try:
        await asyncio.wait_for(task, PRINT_WORKER_STOP_TIMEOUT)
    except TimeoutError:
        logger.warning("the %s worker did not stop in time; cancelled it", name)
    except Exception:
        logger.exception("the %s worker failed", name)


#: The worker's own graceful shutdown (30 s) and a margin.
PRINT_WORKER_STOP_TIMEOUT = 40.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    state: AppState = getattr(app.state, STATE_ATTR)
    # First: without its database ScadBuddy has no settings, so it does not start.
    # It also brings the schema up to date, before the projection opens.
    await asyncio.to_thread(state.settings_store.open)
    # Then what the UI saved (#322), before anything below is sized or started from it.
    snapshot = await asyncio.to_thread(state.settings_store.snapshot)
    apply_runtime(state, snapshot.runtime, booting=True)
    # The blob store (#426): its index and the render settings live in the settings
    # pool, open only from here on.
    pool = state.settings_store.pool
    source = RenderSettingsSource(pool, state.settings)
    try:
        current = await asyncio.to_thread(load_render_store_settings, pool, state.settings)
        source.seed(current)
        state.store = build_store(
            backend=current.store_backend,
            current=current,
            config=state.config,
            paths=state.paths,
            pool=pool,
            source=source,
            history=state.history,
            fonts=state.fonts,
            metrics=state.metrics,
        )
    except BaseException:
        # A refused start (an unready store) closes the pool it opened, then propagates.
        await asyncio.to_thread(state.settings_store.close)
        raise
    state.blobs = state.store.blobs
    state.render.snapshots = state.store.snapshots
    # From the store's construction to the `try` below, whose `finally` owns shutdown:
    # a failure anywhere here (a refused OpenSCAD probe, a render service or bus that
    # will not start) releases what is open, the store and its pool included, rather
    # than leaking it for this failed boot.
    try:
        state.paths.ensure()
        # Before the built-in sync: an existing models directory becomes revision 1,
        # so what a newer image changes in a built-in is a commit on top of it rather
        # than an unversioned overwrite.
        await asyncio.to_thread(state.history.ensure_repo)
        # Before anything shells out to openscad or fc-list: it is what points
        # fontconfig at the fonts on the data volume.
        state.fonts.prepare()
        state.openscad_version = await probe_openscad_version(state.config)
        await _start_render(state)
        # After the projection, which migrated the database: the bus writes the event
        # log, and `start` refuses (EventLogMissingError) if it is not there yet. What
        # was published before now (the built-in sync's commits) waited.
        if isinstance(state.events, PgNotifyEventBus):
            await state.events.start()
    except BaseException:
        await _close_quietly(state)
        await asyncio.to_thread(state.settings_store.close)
        raise

    # Everything from here holds the render service's resources (the Postgres pool,
    # its pruner), so it runs inside the `try` whose `finally` releases them: a
    # failure while starting up closes them as a shutdown does, rather than leaking.
    library: asyncio.Task[None] | None = None
    stop_library = asyncio.Event()
    backfill: asyncio.Task[None] | None = None
    # A change saved on any replica, this one's included, applies its live fields here.
    unfollow = follow_changes(state)
    components = AsyncExitStack()
    worker: tuple[asyncio.Task[None], WorkerDeps] | None = None
    stop = asyncio.Event()
    stop_printing = asyncio.Event()
    printing: asyncio.Task[None] | None = None
    upkeep: asyncio.Task[None] | None = None
    try:
        # Every component's `run` (`core/components.py`), now that the database and
        # the bus are up. One that fails exits those already running and fails the
        # boot; closed below, before the queue it may be using.
        await components.enter_async_context(state.components.running())
        if state.temporal is not None:
            deps = worker_deps_from_state(state)
            task = asyncio.create_task(
                run_inprocess_worker(state.settings, deps, state.temporal, stop)
            )
            task.add_done_callback(partial(_worker_exited, stop))
            worker = (task, deps)
        # Print runs (#1052): the `scadbuddy-print` worker serves the `bambuddy` queue,
        # or this process does when told to (#1060); the upkeep is this process's.
        if serves_print_queue(state.settings):
            printing = asyncio.create_task(_run_print_worker(state, stop_printing))
        upkeep = asyncio.create_task(_print_upkeep(state, stop_printing))
        # Housekeeping (#1054): a Schedule on the `library` queue this process serves,
        # run once at once (the boot's sweep of the uploads, converging with the store;
        # review #1095 1: the boot no longer walks them itself first), then every interval.
        library = asyncio.create_task(_run_library_worker(state, stop_library))
        if state.store.content is not None:
            backfill = asyncio.create_task(
                _backfill_store_logged(state, uploads=state.config.asset_sweep_interval == 0)
            )
        # The per-change requests. The pass over every model is the preview
        # backfill's Schedule (#1054), triggered once the library worker connects.
        if state.previews is not None:
            state.previews.start()
        logger.info(
            "scadbuddy started",
            extra={
                # The deploy provenance stamped into the image (what /healthz
                # reports), not the package version, which is not bumped per deploy.
                "version": state.settings.version,
                "revision": state.settings.revision,
                "data_dir": str(state.paths.root),
                "openscad_version": state.openscad_version,
            },
        )
        yield
    finally:
        unfollow()
        if state.previews is not None:
            await state.previews.aclose()
        stop_library.set()
        await _stop_queue_worker(library, "library")
        for background in (backfill,):
            if background is not None:
                background.cancel()
                with suppress(asyncio.CancelledError):
                    await background
        stop_printing.set()
        await _stop_queue_worker(printing, "print")
        await _stop_queue_worker(upkeep, "print upkeep")
        if worker is not None:
            stop.set()
            await _stop_worker(state, *worker)
        await components.aclose()
        await state.render.aclose()
        await asyncio.to_thread(state.projection.close)
        await asyncio.to_thread(state.presets.close)
        await state.events.aclose()
        grants = getattr(app.state, "agent_grants", None)
        if grants is not None:
            await grants.aclose()
        await state.store.aclose()
        await asyncio.to_thread(state.settings_store.close)


def create_app(settings_override: Settings | None = None) -> FastAPI:
    app_settings = settings_override or Settings()
    configure_logging(app_settings.log_level)
    configure_tracing(
        "scadbuddy-api",
        version=app_settings.version,
        revision=app_settings.revision,
        inprocess_worker=app_settings.temporal_worker_inprocess,
    )

    app = FastAPI(
        title="ScadBuddy",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
    )
    state = build_state(app_settings)
    setattr(app.state, STATE_ATTR, state)
    install_problem_handlers(app)
    libraries.install_library_handlers(app)
    assets.install_asset_handlers(app)
    models.install_model_handlers(app)
    # The agent's headless browser may not make outward requests (#349, AI spec §5.3):
    # refused on the method, path and marker header alone, before any body is read.
    grants = postgres_grants(app_settings.database_url) if app_settings.database_url else None
    # Closed by the lifespan, after everything else has stopped.
    app.state.agent_grants = grants
    # Inside the gate: a commit made for the agent is authored as the agent (#252).
    app.add_middleware(AgentAuthorship)
    app.add_middleware(AgentActorGate, grants=grants)
    # Outside everything that reads a body, so an oversized one is refused on its
    # headers rather than buffered.
    app.add_middleware(
        BodySizeGate,
        limits=BODY_LIMITS,
        routes=[
            RouteLimit(
                "POST",
                MEDIA_UPLOAD_PATH,
                # The value in effect, which Settings can change while running (#322).
                lambda: state.settings.media_upload_max_bytes,
                "a media upload",
                "the upload limit in Settings, seeded by SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES",
            ),
            # The browser trace relay's 256 KiB (spec 2026-10-01 §5.2).
            telemetry.RELAY_ROUTE_LIMIT,
        ],
    )
    # A write from a page on another origin is refused before its body is read (#962).
    app.add_middleware(
        CrossSiteGate,
        public_url=lambda: state.settings_store.load().public_url,
        allowed_origins=lambda: state.settings.allowed_origin_list,
    )
    # Outermost of all (added last): the gate answers a 413 itself without calling
    # inward, so a counter inside it would never see the requests most worth
    # counting. It reads no body, so wrapping the gate costs the gate nothing.
    app.add_middleware(HttpMetrics, metrics=state.metrics)

    app.include_router(health.router)
    app.include_router(metrics.router)
    # The browser trace relay: at the root like the two above, before the SPA's mount.
    app.include_router(telemetry.router)
    app.include_router(_api_router())
    _name_in_openapi(app, models.PastedSource, media.MediaUpload)

    # Last, so every API route above wins the match; unknown paths fall back to index.html.
    frontend = app_settings.resolve_frontend_dir()
    if frontend is not None:
        app.mount("/", SPAStaticFiles(frontend), name="frontend")
    else:
        logger.info("no frontend bundle found; serving the API only")
    # Outermost, so the server span covers every middleware, the body gate included.
    FastAPIInstrumentor.instrument_app(
        app, excluded_urls=EXCLUDED_URLS, exclude_spans=["receive", "send"]
    )
    return app
