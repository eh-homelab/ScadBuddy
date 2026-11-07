"""The edge's FastAPI app (#5282). It mounts no `/api/v1` route and never proxies one.

Its lifespan waits until the CRUD service serves every area the edge calls
(`EDGE_AREAS`), then serves. A CRUD route's refusal is answered with its status, problem
and ``Retry-After``; a CRUD service out of reach past the client's budget is a 503.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from scadbuddy.core.problems import (
    PROBLEM_MEDIA_TYPE,
    install_problem_handlers,
    problem_response,
)
from scadbuddy.crud.client import CrudRefusedError, CrudUnavailableError
from scadbuddy.crud.storefront import StorefrontCrud
from scadbuddy.storefront.routes import router, visitor_router
from scadbuddy.storefront.settings import STOREFRONT_PREFIX, StorefrontSettings
from scadbuddy.storefront.visitors import VisitorCookie

logger = logging.getLogger(__name__)


def create_app(settings: StorefrontSettings, *, crud: StorefrontCrud | None = None) -> FastAPI:
    client = crud if crud is not None else StorefrontCrud.from_settings(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            # During a roll the API pod may be older than the edge: serve only once it
            # serves every area the edge calls. Nothing stops the wait but the process.
            await client.wait_for_areas(asyncio.Event())
            yield
        finally:
            await client.aclose()

    app = FastAPI(
        title="ScadBuddy storefront",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.crud = client
    install_problem_handlers(app)

    @app.exception_handler(CrudRefusedError)
    async def _refused(request: Request, error: CrudRefusedError) -> JSONResponse:
        headers = {"Retry-After": error.retry_after} if error.retry_after else None
        return JSONResponse(
            error.problem, status_code=error.status, headers=headers, media_type=PROBLEM_MEDIA_TYPE
        )

    @app.exception_handler(CrudUnavailableError)
    async def _unavailable(request: Request, error: CrudUnavailableError) -> JSONResponse:
        logger.warning("the CRUD service is unavailable (%s)", error)
        return problem_response(request, 503, "The shop is unavailable. Try again later.")

    app.include_router(router)
    app.include_router(visitor_router)
    key = settings.storefront_cookie_key_file.read_bytes()
    app.add_middleware(VisitorCookie, key=key, prefix=STOREFRONT_PREFIX)
    return app
