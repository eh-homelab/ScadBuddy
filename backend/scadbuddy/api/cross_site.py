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
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from scadbuddy.api.realtime import origin_allowed
from scadbuddy.core.problems import PROBLEM_MEDIA_TYPE

logger = logging.getLogger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


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
        origin = Headers(scope=scope).get("origin")
        allowed = self.allowed_origins()
        # The stored public URL is read only when the cheap answer is no.
        if origin_allowed(origin, None, allowed) or origin_allowed(
            origin, await asyncio.to_thread(self.public_url), allowed
        ):
            await self.app(scope, receive, send)
            return
        logger.warning(
            "refused a %s %s from origin %r: not the public URL's origin, not in "
            "SCADBUDDY_ALLOWED_ORIGINS and not loopback",
            scope["method"],
            scope["path"],
            origin,
        )
        response = JSONResponse(
            {
                "type": "about:blank",
                "title": "Forbidden",
                "status": 403,
                "detail": "writes must come from the ScadBuddy UI at its public URL",
                "instance": scope["path"],
            },
            status_code=403,
            media_type=PROBLEM_MEDIA_TYPE,
        )
        await response(scope, receive, send)
