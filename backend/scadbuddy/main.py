from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel

from scadbuddy import __version__
from scadbuddy.api import fonts, health, jobs, models, outputs, printing, settings, versions
from scadbuddy.api.deps import STATE_ATTR, AppState, build_state, probe_openscad_version
from scadbuddy.api.limits import BODY_LIMITS, BodySizeGate
from scadbuddy.api.static import SPAStaticFiles
from scadbuddy.core.logging import configure_logging
from scadbuddy.core.problems import install_problem_handlers
from scadbuddy.core.settings import Settings

API_PREFIX = "/api/v1"

logger = logging.getLogger(__name__)

DESCRIPTION = "Self-hosted OpenSCAD customizer for Bambuddy."


def _api_router() -> APIRouter:
    router = APIRouter(prefix=API_PREFIX)
    router.include_router(models.router)
    router.include_router(versions.router)
    router.include_router(jobs.router)
    router.include_router(outputs.router)
    router.include_router(printing.router)
    router.include_router(settings.router)
    router.include_router(fonts.router)
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    state: AppState = getattr(app.state, STATE_ATTR)
    state.paths.ensure()
    # Before the seed: an existing models directory becomes revision 1, so a
    # re-seed on an image upgrade is a commit on top of it rather than an
    # unversioned overwrite.
    await asyncio.to_thread(state.history.ensure_repo)
    # Before anything shells out to openscad or fc-list: it is what points
    # fontconfig at the fonts on the data volume.
    state.fonts.prepare()
    state.openscad_version = await probe_openscad_version(state.config)
    seed_dir = state.settings.resolve_seed_models_dir()
    if seed_dir is not None:
        await asyncio.to_thread(state.catalogue.seed, seed_dir)
    # A delete that died between its rename and its rmtree left a tombstone.
    # Best effort, as it is after a delete: leftovers must not stop the boot.
    try:
        await asyncio.to_thread(state.catalogue.sweep_tombstones)
    except OSError:
        logger.exception("could not sweep tombstones")
    # RenderQueue.start() fails unfinished jobs and prunes expired ones before it
    # spawns its workers, so a restart never leaves a job stuck "running".
    await state.queue.start()
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
        await state.queue.aclose()


def create_app(settings_override: Settings | None = None) -> FastAPI:
    app_settings = settings_override or Settings()
    configure_logging(app_settings.log_level)

    app = FastAPI(
        title="ScadBuddy",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
    )
    setattr(app.state, STATE_ATTR, build_state(app_settings))
    install_problem_handlers(app)
    # Outermost, so an oversized body is refused on its headers rather than buffered.
    app.add_middleware(BodySizeGate, limits=BODY_LIMITS)

    app.include_router(health.router)
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
