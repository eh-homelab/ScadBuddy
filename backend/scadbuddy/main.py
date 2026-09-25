from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from scadbuddy import __version__
from scadbuddy.api import fonts, health, jobs, models, outputs, printing, settings
from scadbuddy.api.deps import STATE_ATTR, AppState, build_state, probe_openscad_version
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
    router.include_router(jobs.router)
    router.include_router(outputs.router)
    router.include_router(printing.router)
    router.include_router(settings.router)
    router.include_router(fonts.router)
    return router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    state: AppState = getattr(app.state, STATE_ATTR)
    state.paths.ensure()
    # Before anything shells out to openscad or fc-list: it is what points
    # fontconfig at the fonts on the data volume.
    state.fonts.prepare()
    state.openscad_version = await probe_openscad_version(state.config)
    seed_dir = state.settings.resolve_seed_models_dir()
    if seed_dir is not None:
        state.catalogue.seed(seed_dir)
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

    app.include_router(health.router)
    app.include_router(_api_router())

    # Last, so every API route above wins the match; unknown paths fall back to index.html.
    frontend = app_settings.resolve_frontend_dir()
    if frontend is not None:
        app.mount("/", SPAStaticFiles(frontend), name="frontend")
    else:
        logger.info("no frontend bundle found; serving the API only")
    return app


app = create_app()
