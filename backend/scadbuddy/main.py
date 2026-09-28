from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel

from scadbuddy import __version__
from scadbuddy.api import (
    assets,
    fonts,
    health,
    jobs,
    libraries,
    lsp,
    metrics,
    models,
    outputs,
    plates,
    presets,
    printing,
    settings,
    upstream,
    versions,
)
from scadbuddy.api.deps import STATE_ATTR, AppState, build_state, probe_openscad_version
from scadbuddy.api.limits import BODY_LIMITS, BodySizeGate
from scadbuddy.api.static import SPAStaticFiles
from scadbuddy.core.logging import configure_logging
from scadbuddy.core.metrics import HttpMetrics
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.problems import install_problem_handlers
from scadbuddy.core.settings import Settings
from scadbuddy.library.assets import referenced_asset_ids
from scadbuddy.library.history import GitError
from scadbuddy.library.libraries import migrate_lockfile

API_PREFIX = "/api/v1"

logger = logging.getLogger(__name__)

DESCRIPTION = "Self-hosted OpenSCAD customizer for Bambuddy."


def _api_router() -> APIRouter:
    router = APIRouter(prefix=API_PREFIX)
    router.include_router(models.router)
    router.include_router(upstream.router)
    router.include_router(versions.router)
    router.include_router(presets.router)
    router.include_router(jobs.router)
    router.include_router(assets.router)
    router.include_router(outputs.router)
    router.include_router(printing.router)
    router.include_router(settings.router)
    router.include_router(fonts.router)
    router.include_router(plates.router)
    router.include_router(libraries.router)
    router.include_router(lsp.router)
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
    output, preset and template (`referenced_asset_ids`) -- and any failure to read
    them raises before anything is removed. What is referenced after that is kept by
    its last use, which the sweep re-checks under the store's lock per asset.
    """
    jobs = state.queue.store.list_jobs()
    referenced = referenced_asset_ids(state.paths, [job.params for job in jobs])
    removed = state.assets.sweep(referenced, grace=state.config.asset_sweep_grace)
    state.metrics.assets_swept.inc(len(removed))
    if removed:
        logger.info("removed unused uploads", extra={"count": len(removed)})
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    state: AppState = getattr(app.state, STATE_ATTR)
    state.paths.ensure()
    # Before the built-in sync: an existing models directory becomes revision 1,
    # so what a newer image changes in a built-in is a commit on top of it rather
    # than an unversioned overwrite.
    await asyncio.to_thread(state.history.ensure_repo)
    # Before anything shells out to openscad or fc-list: it is what points
    # fontconfig at the fonts on the data volume.
    state.fonts.prepare()
    state.openscad_version = await probe_openscad_version(state.config)
    seed_dir = state.settings.resolve_seed_models_dir()
    if seed_dir is not None:
        await asyncio.to_thread(state.catalogue.sync_builtins, seed_dir)
    # After the sync, so the built-ins exist: a model the old seed copied in
    # becomes a duplicate of its built-in (#158). Contains its own failures.
    await asyncio.to_thread(state.catalogue.link_seeded)
    # A delete that died between its rename and its rmtree left a tombstone.
    # Best effort, as it is after a delete: leftovers must not stop the boot.
    try:
        await asyncio.to_thread(state.catalogue.sweep_tombstones)
    except OSError:
        logger.exception("could not sweep tombstones")
    # A duplicate the process died in the middle of left its staging copy. Nothing
    # is duplicating yet: no request has been served.
    await _sweep_duplicate_staging_logged(state)
    # Derived files a failed or raced delete left keyed to a slug that is gone.
    # It logs and skips whatever it cannot read, so it never stops the boot.
    await asyncio.to_thread(state.catalogue.sweep_orphans)
    # Pins from before they moved into each model (#93): once, then the shared
    # lockfile is gone. It logs what it cannot record, so it never stops the boot.
    try:
        slugs = [record.slug for record in await asyncio.to_thread(state.catalogue.list_models)]
        await asyncio.to_thread(migrate_lockfile, state.paths, state.history, slugs)
    except (OSError, ValueError, GitError):
        logger.exception("could not migrate the library lockfile")
    # A library clone the process died in the middle of. Nothing is cloning yet:
    # no request has been served.
    try:
        await asyncio.to_thread(state.libraries.sweep_staging)
    except OSError:
        logger.exception("could not sweep library staging clones")
    # The upload store's running total, recounted once (#390): uploads and sweeps
    # keep it from here, but a file added or removed while the process was down is
    # only counted by a scan.
    try:
        await asyncio.to_thread(state.assets.rebuild_usage)
    except OSError:
        logger.exception("could not recount the upload store")
    # RenderQueue.start() fails unfinished jobs and prunes expired ones before it
    # spawns its workers, so a restart never leaves a job stuck "running".
    await state.queue.start()
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

    # After the queue has opened its store: the jobs in it are references too.
    sweeper: asyncio.Task[None] | None = None
    if state.config.asset_sweep_interval > 0:
        await _sweep_assets_logged(state)
        sweeper = asyncio.create_task(_asset_sweeper(state))
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
    try:
        yield
    finally:
        if sweeper is not None:
            sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await sweeper
        await state.queue.aclose()
        await state.events.aclose()


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
    models.install_model_handlers(app)
    # Outside everything that reads a body, so an oversized one is refused on its
    # headers rather than buffered.
    app.add_middleware(BodySizeGate, limits=BODY_LIMITS)
    # Outermost of all (added last): the gate answers a 413 itself without calling
    # inward, so a counter inside it would never see the requests most worth
    # counting. It reads no body, so wrapping the gate costs the gate nothing.
    app.add_middleware(HttpMetrics, metrics=state.metrics)

    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(_api_router())
    _name_in_openapi(app, models.PastedSource)

    # Last, so every API route above wins the match; unknown paths fall back to index.html.
    frontend = app_settings.resolve_frontend_dir()
    if frontend is not None:
        app.mount("/", SPAStaticFiles(frontend), name="frontend")
    else:
        logger.info("no frontend bundle found; serving the API only")
    return app


app = create_app()
