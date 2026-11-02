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
import time
from collections.abc import Callable
from typing import Protocol

import psycopg
from starlette.datastructures import Headers
from starlette.requests import cookie_parser
from starlette.types import ASGIApp, Receive, Scope, Send

from scadbuddy.auth.models import ANONYMOUS, AuthSession, Caller, Person, Service

logger = logging.getLogger(__name__)

#: The session cookie (§5.2). ``__Host-``: ``Secure``, ``Path=/`` and no ``Domain``.
SESSION_COOKIE = "__Host-scadbuddy_session"
#: Where the caller sits on the scope (an ASGI extension key of ours).
CALLER_SCOPE_KEY = "scadbuddy.caller"
#: A failed lookup is logged at most this often, so a Postgres outage does not write a
#: line for every request that carries a cookie.
FAILURE_LOG_INTERVAL = 60.0


class SessionLookup(Protocol):
    def resolve(self, token: str) -> AuthSession | None: ...


class CredentialLookup(Protocol):
    def resolve(self, token: str) -> Service | None: ...


#: A service credential's prefix (`auth/service_credentials.py`), in the bearer header.
SERVICE_PREFIX = "sbsvc_"


class AuthGate:
    def __init__(
        self,
        app: ASGIApp,
        *,
        sessions: Callable[[], SessionLookup],
        credentials: Callable[[], CredentialLookup] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.sessions = sessions
        self.credentials = credentials
        self._clock = clock
        self._logged_at: float | None = None
        self._unlogged = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            scope[CALLER_SCOPE_KEY] = await self._caller(scope)
        await self.app(scope, receive, send)

    async def _caller(self, scope: Scope) -> Caller:
        headers = Headers(scope=scope)
        scheme, _, bearer = (headers.get("authorization") or "").partition(" ")
        if (
            self.credentials is not None
            and scheme.lower() == "bearer"
            and bearer.startswith(SERVICE_PREFIX)
        ):
            # A service credential (§7.1); a cookie beside it is not looked at.
            try:
                service = await asyncio.to_thread(self.credentials().resolve, bearer.strip())
            except psycopg.Error as error:
                self._log_failure(error)
                return ANONYMOUS
            return ANONYMOUS if service is None else service
        header = headers.get("cookie")
        token = cookie_parser(header).get(SESSION_COOKIE) if header else None
        if not token:
            return ANONYMOUS
        try:
            session = await asyncio.to_thread(self.sessions().resolve, token)
        except psycopg.Error as error:
            self._log_failure(error)
            return ANONYMOUS
        return ANONYMOUS if session is None else Person(session)

    def _log_failure(self, error: psycopg.Error) -> None:
        now = self._clock()
        if self._logged_at is not None and now - self._logged_at < FAILURE_LOG_INTERVAL:
            self._unlogged += 1
            return
        # The type only: a driver's message can carry the connection's host and user.
        logger.warning(
            "session lookup failed (%s); serving the request as anonymous",
            type(error).__name__,
            extra={"failures_since_last_logged": self._unlogged},
        )
        self._logged_at = now
        self._unlogged = 0


def caller_of(scope: Scope) -> Caller:
    """The caller ``AuthGate`` resolved, or anonymous where it did not run."""
    caller: Caller = scope.get(CALLER_SCOPE_KEY, ANONYMOUS)
    return caller
