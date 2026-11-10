"""The edge's FastAPI app (#5282). It mounts no `/api/v1` route and never proxies one.

Every request passes three middlewares, outermost first: `RequestLimit` (the per-client
request limit, spec §5.8), `CrossSiteGate` (a write whose ``Origin`` is not the page's,
``SCADBUDDY_STOREFRONT_PUBLIC_URL``, is 403 before any CRUD call; plan point 16) and
`VisitorCookie` (the visitor id, spec §5.2). The gate refuses a write from a loopback
origin, unless the public URL is loopback itself (local development). It passes a write
with no ``Origin``: a browser sends ``Origin`` on every cross-site POST, and it does not
send the ``SameSite=Lax`` visitor cookie on one, so a page elsewhere cannot write as a
visitor. A request with no ``Origin`` is not a browser page (curl), and it has no
visitor but the one it makes up itself.

Mounted last, `static.py` `StorefrontStatic` serves the page (``storefront.html``) and
its own chunks from the built bundle (``SCADBUDDY_FRONTEND_DIR``, else the repo's or the
image's ``frontend/dist``); with none, the edge serves its routes only.

Its lifespan waits until the CRUD service serves every area the edge calls
(`EDGE_AREAS`), then serves; a SIGTERM or SIGINT ends that wait, and the edge then
closes its client and never serves. A CRUD route's refusal
with a public storefront problem (`wire.py` `public_problem`, a render limit's 429 among
them) is answered with its status, problem (its ``instance`` the edge's own path) and
``Retry-After``; any other refusal is
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

from scadbuddy.api.cross_site import CrossSiteGate
from scadbuddy.core.origins import is_loopback
from scadbuddy.core.problems import (
    PROBLEM_MEDIA_TYPE,
    install_problem_handlers,
    problem_response,
)
from scadbuddy.core.proxies import parse_cidr_list
from scadbuddy.crud.client import CrudRefusedError, CrudUnavailableError
from scadbuddy.crud.storefront import StorefrontCrud
from scadbuddy.storefront.ratelimit import RequestLimit, UploadAllowance
from scadbuddy.storefront.routes import RequestsEnabled, router, visitor_router
from scadbuddy.storefront.settings import STOREFRONT_PREFIX, StorefrontSettings
from scadbuddy.storefront.static import StorefrontStatic
from scadbuddy.storefront.turnstile import FailedChecks, SiteKey
from scadbuddy.storefront.visitors import VisitorCookie
from scadbuddy.storefront.wire import public_problem

logger = logging.getLogger(__name__)

STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
CROSS_SITE_REFUSED = "This request must come from the shop's own page."


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
    # Parsed once here, not on each request (`routes.py` `_client`).
    app.state.trusted_proxies = parse_cidr_list(settings.trusted_proxies)
    # Design requests: their own allowance per client, and "Ask for a design" as read last.
    app.state.upload_allowance = UploadAllowance()
    app.state.requests_enabled = RequestsEnabled()
    # The bot check's site key (#5283), for the page's config and its CSP.
    app.state.turnstile_site_key = SiteKey()
    app.state.turnstile_failures = FailedChecks()
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
        # The edge's own path: the CRUD route's would tell a visitor the internal one.
        shown = {**error.problem, "instance": request.url.path}
        return JSONResponse(
            shown, status_code=error.status, headers=headers, media_type=PROBLEM_MEDIA_TYPE
        )

    @app.exception_handler(CrudUnavailableError)
    async def _unavailable(request: Request, error: CrudUnavailableError) -> JSONResponse:
        logger.warning("the CRUD service is unavailable (%s)", error)
        return problem_response(request, 503, "The shop is unavailable. Try again later.")

    app.include_router(router)
    app.include_router(visitor_router)
    # Last, so every route wins the match: the page and its own chunks (`static.py`).
    frontend = settings.resolve_frontend_dir()
    if frontend is not None:

        async def turnstile_on() -> bool:
            return await app.state.turnstile_site_key.get(client) is not None

        app.mount("/", StorefrontStatic(frontend, turnstile=turnstile_on), name="storefront-page")
    else:
        logger.info("no storefront page bundle found; serving the API only")
    key = settings.storefront_cookie_key_file.read_bytes()
    # The session cookie's key too (`accounts.py`), under its own version tag.
    app.state.cookie_key = key
    # The last added runs first.
    app.add_middleware(VisitorCookie, key=key, prefix=STOREFRONT_PREFIX)
    app.add_middleware(
        CrossSiteGate,
        public_url=lambda: settings.storefront_public_url,
        allowed_origins=lambda: (),
        refused=CROSS_SITE_REFUSED,
        # A page on the visitor's own machine is not the shop's, unless the shop is
        # served there (local development).
        loopback=is_loopback(settings.storefront_public_url),
    )
    app.add_middleware(RequestLimit, prefix=STOREFRONT_PREFIX, trusted=app.state.trusted_proxies)
    return app
