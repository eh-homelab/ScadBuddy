"""The edge's FastAPI app (#5282). It mounts no `/api/v1` route and never proxies one.

Its lifespan waits until the CRUD service serves every area the edge calls
(`EDGE_AREAS`), then serves; a SIGTERM or SIGINT ends that wait, and the edge then
closes its client and never serves. A CRUD route's refusal
with a public storefront problem (`wire.py` `public_problem`, a render limit's 429 among
them) is answered with its status, problem and ``Retry-After``; any other refusal is
logged and answered 502 with no detail, since it names the edge's own fault (a
credential, a header). A CRUD service out of reach past the client's budget is a 503.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from types import FrameType

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
from scadbuddy.storefront.wire import public_problem

logger = logging.getLogger(__name__)

STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


@contextmanager
def _stopped_by_signals(stop: asyncio.Event) -> Iterator[None]:
    """Sets ``stop`` on a SIGTERM or SIGINT, then runs the handler it replaced (uvicorn's,
    which ends the server), or the default action. Only on the main thread, where Python
    takes signals; elsewhere (a test client's thread) it does nothing."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    loop = asyncio.get_running_loop()
    previous = {signum: signal.getsignal(signum) for signum in STOP_SIGNALS}

    def stopped(signum: int, frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(stop.set)
        handler = previous[signal.Signals(signum)]
        if callable(handler):
            handler(signum, frame)
        else:
            signal.signal(signum, handler)
            signal.raise_signal(signum)

    for signum in STOP_SIGNALS:
        signal.signal(signum, stopped)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def create_app(settings: StorefrontSettings, *, crud: StorefrontCrud | None = None) -> FastAPI:
    client = crud if crud is not None else StorefrontCrud.from_settings(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            # During a roll the API pod may be older than the edge: serve only once it
            # serves every area the edge calls. A stop signal ends the wait; the edge
            # then closes its client and never serves.
            stop = asyncio.Event()
            with _stopped_by_signals(stop):
                if not await client.wait_for_areas(stop):
                    logger.info("stopped before the CRUD service served the storefront's areas")
                    return
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
        if not public_problem(error.problem.get("type")):
            logger.error(
                "the CRUD service refused the edge: %s %s",
                error.status,
                error.problem,
                extra={"path": request.url.path},
            )
            return problem_response(request, 502, "The shop could not answer this request.")
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
