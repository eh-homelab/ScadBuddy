"""``AuthGate`` (spec 2026-11-01 §8.1): resolves each request's caller once and puts it on
the ASGI scope, where ``CallerDep`` reads it.

In ``SCADBUDDY_AUTH_MODE=off``, the only mode this build has (#4975 plan phase 1), it
refuses nothing: a request with no session cookie costs no lookup, and one whose lookup
fails (the database unreachable) is served as anonymous. Refusing and counting arrive with
the route classes (plan phase 3).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

import psycopg
from starlette.datastructures import Headers
from starlette.requests import cookie_parser
from starlette.types import ASGIApp, Receive, Scope, Send

from scadbuddy.auth.models import ANONYMOUS, AuthSession, Caller, Person

logger = logging.getLogger(__name__)

#: The session cookie (§5.2). ``__Host-``: ``Secure``, ``Path=/`` and no ``Domain``.
SESSION_COOKIE = "__Host-scadbuddy_session"
#: Where the caller sits on the scope (an ASGI extension key of ours).
CALLER_SCOPE_KEY = "scadbuddy.caller"


class SessionLookup(Protocol):
    def resolve(self, token: str) -> AuthSession | None: ...


class AuthGate:
    def __init__(self, app: ASGIApp, *, sessions: Callable[[], SessionLookup]) -> None:
        self.app = app
        self.sessions = sessions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            scope[CALLER_SCOPE_KEY] = await self._caller(scope)
        await self.app(scope, receive, send)

    async def _caller(self, scope: Scope) -> Caller:
        header = Headers(scope=scope).get("cookie")
        token = cookie_parser(header).get(SESSION_COOKIE) if header else None
        if not token:
            return ANONYMOUS
        try:
            session = await asyncio.to_thread(self.sessions().resolve, token)
        except psycopg.Error as error:
            # The type only: a driver's message can carry the connection's host and user.
            logger.warning(
                "session lookup failed (%s); serving the request as anonymous",
                type(error).__name__,
            )
            return ANONYMOUS
        return ANONYMOUS if session is None else Person(session)


def caller_of(scope: Scope) -> Caller:
    """The caller ``AuthGate`` resolved, or anonymous where it did not run."""
    caller: Caller = scope.get(CALLER_SCOPE_KEY, ANONYMOUS)
    return caller
