from __future__ import annotations

import asyncio
import importlib
import logging
import pkgutil
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from typing import Any

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel

import scadbuddy.api
from scadbuddy import __version__
from scadbuddy.api import assets, health, libraries, media, metrics, models
from scadbuddy.api.agent_actor import AgentActorGate, postgres_grants
from scadbuddy.api.deps import STATE_ATTR, AppState, build_state, probe_openscad_version
from scadbuddy.api.limits import BODY_LIMITS, MEDIA_UPLOAD_PATH, BodySizeGate, RouteLimit
from scadbuddy.api.static import SPAStaticFiles
from scadbuddy.core.logging import configure_logging
from scadbuddy.core.metrics import HttpMetrics
from scadbuddy.core.paths import BUILTIN_DIR, MODEL_META_NAME
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.problems import install_problem_handlers
from scadbuddy.core.settings import Settings
from scadbuddy.library.assets import referenced_asset_ids
from scadbuddy.library.history import GitError
from scadbuddy.library.library_seed import seed_libraries, seeded_checkouts
from scadbuddy.library.previews import sweep_work_dirs
from scadbuddy.render.previews import TIMEOUT_FACTOR as PREVIEW_TIMEOUT_FACTOR

API_PREFIX = "/api/v1"

logger = logging.getLogger(__name__)

DESCRIPTION = "Self-hosted OpenSCAD customizer for Bambuddy."


#: The `scadbuddy.api` modules whose router sits at the root rather than under
#: :data:`API_PREFIX`.
ROOT_ROUTE_MODULES = frozenset({"health", "metrics"})


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
    """Close the queue and the bus after a failed start, logging (not raising) what
    fails, so the start's own error is the one that propagates."""
    for close in (state.events.aclose, state.queue.aclose):
        try:
            await close()
        except Exception:
            logger.exception("could not release what a failed start opened")


def sweep_assets(state: AppState) -> list[str]:
    """Remove the uploads nothing references or has used for the grace (#296).

    The references are read first -- every job in the queue's store, then every
    saved preset, output and template (`referenced_asset_ids`) -- and any failure to read
    them raises before anything is removed. What is referenced after that is kept by
    its last use, which the sweep re-checks under the store's lock per asset.
    """
    jobs = state.queue.store.list_jobs()
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


async def _sweep_assets_logged(state: AppState) -> None:
    # Best effort, like the boot's other sweeps: a store or volume error skips this
    # sweep (removing nothing it could not prove unused) and the next one retries.
    try:
        await asyncio.to_thread(sweep_assets, state)
    except Exception:
        logger.exception("could not sweep unused uploads")


async def _sweep_duplicate_staging_logged(state: AppState) -> None:
    try:
        await asyncio.to_thread(state.catalogue.sweep_duplicate_staging)
    except OSError:
        logger.exception("could not sweep duplicate staging folders")


async def _asset_sweeper(state: AppState) -> None:
    while True:
        await asyncio.sleep(state.config.asset_sweep_interval)
        await _sweep_assets_logged(state)
        # The periodic housekeeping pass: a crashed duplicate's staging otherwise
        # waits for the next boot or duplicate (#397).
        await _sweep_duplicate_staging_logged(state)


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
    # gone: the database, and its migrations, as the render queue's store opens it.
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    state: AppState = getattr(app.state, STATE_ATTR)
    # First: without its database ScadBuddy has no settings, so it does not start.
    # It also brings the schema up to date, before the queue's store opens.
    await asyncio.to_thread(state.settings_store.open)
    state.paths.ensure()
    # Before the built-in sync: an existing models directory becomes revision 1,
    # so what a newer image changes in a built-in is a commit on top of it rather
    # than an unversioned overwrite.
    await asyncio.to_thread(state.history.ensure_repo)
    # Before anything shells out to openscad or fc-list: it is what points
    # fontconfig at the fonts on the data volume.
    state.fonts.prepare()
    state.openscad_version = await probe_openscad_version(state.config)
    # Before the first catalogue listing: that reads the previews, which live in the
    # database when there is one. Closed again if the boot fails before the queue
    # has started and taken it over.
    await state.queue.open_store()
    try:
        await _prepare_catalogue(state)
        # RenderQueue.start() fails unfinished jobs and prunes expired ones before
        # it spawns its workers, so a restart never leaves a job stuck "running".
        await state.queue.start()
    except BaseException:
        await state.queue.close_store()
        raise
    # After the queue, whose store migrated the database: the bus writes the event
    # log, and `start` refuses (EventLogMissingError) if it is not there yet. What
    # was published before now (the built-in sync's commits) waited.
    if isinstance(state.events, PgNotifyEventBus):
        try:
            await state.events.start()
        except BaseException:
            # Before the `try` below, so its `finally` never runs: release the
            # queue that did start (workers, reaper, listener, pool) here, as
            # `RenderQueue.start` releases its store when it fails.
            await _close_quietly(state)
            raise

    # Everything from here holds the queue's resources (the Postgres pool, its
    # workers), so it runs inside the `try` whose `finally` releases them: a
    # failure while starting up closes the queue as a shutdown does, rather than
    # leaking it -- the failure `RenderQueue.start` guards against for its own steps.
    sweeper: asyncio.Task[None] | None = None
    components = AsyncExitStack()
    try:
        # Every component's `run` (`core/components.py`), now that the database and
        # the bus are up. One that fails exits those already running and fails the
        # boot; closed below, before the queue it may be using.
        await components.enter_async_context(state.components.running())
        # Follows the prints a previous process was following (#268).
        await state.print_watcher.start()
        # After the queue has opened its store: the jobs in it are references too.
        if state.config.asset_sweep_interval > 0:
            await _sweep_assets_logged(state)
            sweeper = asyncio.create_task(_asset_sweeper(state))
        if state.previews is not None:
            state.previews.start()
            # Every model without a thumbnail gets its default render, one at a time
            # and behind any render someone asks for; one already made from the
            # current source is left alone, so after the first boot this renders
            # nothing. Best effort, like the migration above: a listing that fails
            # costs the backfill, never the boot.
            try:
                records = await asyncio.to_thread(state.catalogue.list_models)
            except (OSError, ValueError, GitError):
                logger.exception("could not list the models to render their previews")
            else:
                state.previews.request_all(record.slug for record in records)
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
        if state.previews is not None:
            await state.previews.aclose()
        if sweeper is not None:
            sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await sweeper
        # Before the watcher (a run starts one) and the queue (its pool records the
        # runs this process leaves unfinished as failed).
        await state.print_runs.aclose()
        await state.print_watcher.aclose()
        await components.aclose()
        await state.queue.aclose()
        if state.decisions is not None:
            await asyncio.to_thread(state.decisions.close)
        await asyncio.to_thread(state.presets.close)
        await state.events.aclose()
        grants = getattr(app.state, "agent_grants", None)
        if grants is not None:
            await grants.aclose()
        await asyncio.to_thread(state.settings_store.close)


def create_app(settings_override: Settings | None = None) -> FastAPI:
    app_settings = settings_override or Settings()
    configure_logging(app_settings.log_level)

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
                app_settings.media_upload_max_bytes,
                "a media upload",
                "SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES",
            )
        ],
    )
    # Outermost of all (added last): the gate answers a 413 itself without calling
    # inward, so a counter inside it would never see the requests most worth
    # counting. It reads no body, so wrapping the gate costs the gate nothing.
    app.add_middleware(HttpMetrics, metrics=state.metrics)

    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(_api_router())
    _name_in_openapi(app, models.PastedSource, media.MediaUpload)

    # Last, so every API route above wins the match; unknown paths fall back to index.html.
    frontend = app_settings.resolve_frontend_dir()
    if frontend is not None:
        app.mount("/", SPAStaticFiles(frontend), name="frontend")
    else:
        logger.info("no frontend bundle found; serving the API only")
    return app
