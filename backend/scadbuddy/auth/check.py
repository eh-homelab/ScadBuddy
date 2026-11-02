"""The gate's per-route check (#4975 phase 3, spec 2026-11-01 §8, §9.1), an app-wide
dependency: it runs once the route is matched, for HTTP and WebSocket routes alike, so it
knows the route's class. ``AuthGate`` has already put the caller on the scope.

In ``observe`` what ``enforce`` would refuse is served, counted as
``scadbuddy_auth_would_refuse_total{class, reason}`` and logged once per class and
reason a minute; in ``off`` nothing is looked at. Refusing is ``enforce`` (plan phase 8).
"""

from __future__ import annotations

from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.requests import HTTPConnection

from scadbuddy.api.deps import get_state
from scadbuddy.auth.component import AUTH
from scadbuddy.auth.gate import caller_of
from scadbuddy.auth.routes import (
    REQUEST_HEADER,
    route_class,
    scriptless,
    would_refuse,
)
from scadbuddy.core.authorship import AUTHOR_REFUSAL_KEY


async def auth_check(connection: HTTPConnection) -> None:
    state = get_state(connection)
    if state.settings.auth_mode == "off":
        return
    route = connection.scope.get("route")
    if not isinstance(route, APIRoute | APIWebSocketRoute):
        return
    route_cls = route_class(route)
    websocket = connection.scope["type"] == "websocket"
    reason = would_refuse(
        route_cls,
        caller_of(connection.scope),
        request_header=connection.headers.get(REQUEST_HEADER) == "1",
        # A browser cannot add a header to a socket's handshake; `Origin` covers it.
        header_needed=not websocket and not scriptless(route),
        method=connection.scope.get("method", "GET"),
    )
    if reason is not None:
        state.components.get(AUTH).would_refuse.note(route_cls, reason, route.path)
    # What `AgentAuthorship` made of the author headers (#4975 phase 5), counted here
    # where the route's class and template are known.
    refusal = connection.scope.get(AUTHOR_REFUSAL_KEY)
    if refusal is not None:
        state.components.get(AUTH).would_refuse.note(route_cls, refusal, route.path)
