"""Refuse a write that a page on another origin made through a LAN user's browser (#962).

A ``<form>`` POST with no body, or a ``text/plain``, urlencoded or multipart one, needs
no CORS preflight, so the route would run before the browser ever looks at the
response. Browsers send ``Origin`` on every such request, so the rule is the realtime
socket's (`origin_allowed`, ``api/realtime.py``): a present ``Origin`` must be the
public URL's, one in ``SCADBUDDY_ALLOWED_ORIGINS``, or loopback. ``Origin: null`` (a
sandboxed frame, a ``data:`` page) is not any of them. A request with no ``Origin`` is
not a browser page (curl, the agent's server-side calls) and passes, as it does on the
socket. Safe methods are not checked: they change nothing, and a cross-site page cannot
read their answers (the API sends no CORS headers).

UNCONFIGURED. While neither a public URL (Settings, seeded by ``SCADBUDDY_PUBLIC_URL``)
nor ``SCADBUDDY_ALLOWED_ORIGINS`` is set, a write is also accepted when its ``Origin``
is the request's own origin (its scheme and ``Host``), so a fresh install opened at
``http://<host>:8080`` can save the settings that configure it. That still refuses a
page on another site, which is what #962 is about, but it gives up DNS-rebinding
protection: a rebound name arrives with the attacker's name in both ``Origin`` and
``Host``. Configuring either one turns this off, and then ``Origin == Host`` alone is
refused, as on the realtime socket.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from scadbuddy.api.realtime import _origin, origin_allowed
from scadbuddy.core.problems import PROBLEM_MEDIA_TYPE

logger = logging.getLogger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: How much of a refused ``Origin`` is logged: the header is the caller's to make huge.
LOGGED_ORIGIN_CHARS = 200
REFUSED = (
    "writes must come from the ScadBuddy UI at its public URL; if this is the UI, set "
    "SCADBUDDY_PUBLIC_URL to the URL it is opened at, or list this origin in "
    "SCADBUDDY_ALLOWED_ORIGINS"
)


def _same_host(origin: str | None, scope: Scope, headers: Headers) -> bool:
    """Whether ``origin`` is the request's own ``scheme://host[:port]``."""
    host = headers.get("host")
    if origin is None or not host:
        return False
    own = _origin(f"{scope['scheme']}://{host}")
    return own is not None and _origin(origin) == own


class CrossSiteGate:
    def __init__(
        self,
        app: ASGIApp,
        *,
        public_url: Callable[[], str | None],
        allowed_origins: Callable[[], Sequence[str]],
    ) -> None:
        self.app = app
        # Callables: the public URL is a stored setting that can change while running.
        self.public_url = public_url
        self.allowed_origins = allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in SAFE_METHODS:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        allowed = self.allowed_origins()
        # The stored public URL is read only when the cheap answer is no.
        if origin_allowed(origin, None, allowed):
            await self.app(scope, receive, send)
            return
        public_url = await asyncio.to_thread(self.public_url)
        unconfigured = not public_url and not allowed
        if origin_allowed(origin, public_url, allowed) or (
            unconfigured and _same_host(origin, scope, headers)
        ):
            await self.app(scope, receive, send)
            return
        logger.warning(
            "refused a %s %s from origin %r: not the public URL's origin (%r), not in "
            "SCADBUDDY_ALLOWED_ORIGINS and not loopback; if this is the UI, set "
            "SCADBUDDY_PUBLIC_URL or SCADBUDDY_ALLOWED_ORIGINS",
            scope["method"],
            scope["path"],
            (origin or "")[:LOGGED_ORIGIN_CHARS],
            public_url,
        )
        response = JSONResponse(
            {
                "type": "about:blank",
                "title": "Forbidden",
                "status": 403,
                "detail": REFUSED,
                "instance": scope["path"],
            },
            status_code=403,
            media_type=PROBLEM_MEDIA_TYPE,
        )
        await response(scope, receive, send)
