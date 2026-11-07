"""The gate's per-route check (#4975 phase 3, spec 2026-11-01 §8, §9.1), an app-wide
dependency: it runs once the route is matched, for HTTP and WebSocket routes alike, so it
knows the route's class. ``AuthGate`` has already put the caller on the scope.

In ``observe`` what ``enforce`` would refuse is served, counted as
``scadbuddy_auth_would_refuse_total{class, reason}`` and logged once per class and
reason a minute; in ``off`` nothing is looked at. ``enforce`` (plan phase 8) refuses the
same requests (§8.2): a missing or dead credential is 401 ``…/auth/signed-out``, with
``WWW-Authenticate: Bearer`` where a bearer is what is wanted, and a live one without the
class is 403 ``…/auth/forbidden``. A socket is closed with 1008 before it is accepted
(§5.4).
"""

from __future__ import annotations

from fastapi import WebSocketException, status
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.requests import HTTPConnection

from scadbuddy.api.deps import get_state
from scadbuddy.auth.component import AUTH
from scadbuddy.auth.gate import caller_of
from scadbuddy.auth.routes import (
    REQUEST_HEADER,
    Reason,
    RouteClass,
    route_class,
    scriptless,
    would_refuse,
)
from scadbuddy.core.authorship import AUTHOR_REFUSAL_KEY
from scadbuddy.core.problems import ApiError

PROBLEMS = "https://scadbuddy.dev/problems/auth/"
SIGNED_OUT = f"{PROBLEMS}signed-out"
FORBIDDEN = f"{PROBLEMS}forbidden"
#: The reasons that mean no live credential at all: 401, so a page signs in again.
UNAUTHENTICATED: frozenset[Reason] = frozenset({"signed_out", "no_credential"})
#: What each refusal says, safely: never the credential or who it belongs to.
_DETAILS: dict[Reason, str] = {
    "signed_out": "Sign in to ScadBuddy: this request carries no live session.",
    "no_credential": "This route needs a service credential with its scope.",
    "not_a_person": "Only a person signed in themselves may do this.",
    "out_of_scope": "This credential's scope does not cover this request.",
    "wrong_caller": "These routes belong to another service.",
    "no_request_header": "A signed-in request from the page carries X-ScadBuddy-Request: 1.",
    "author_header": "This caller may not name an author.",
    "wrong_author": "This caller may not name that author.",
}


def refusal(route_cls: RouteClass, reason: Reason, *, bearer: bool) -> ApiError:
    """``enforce``'s answer to a request :func:`would_refuse` refuses (§8.2)."""
    if reason in UNAUTHENTICATED:
        service_only = route_cls.startswith(("internal.", "crud."))
        wants_bearer = bearer or service_only
        return ApiError(
            401,
            _DETAILS[reason],
            type_=SIGNED_OUT,
            headers={"WWW-Authenticate": 'Bearer realm="scadbuddy"'} if wants_bearer else None,
            reason=reason,
        )
    return ApiError(403, _DETAILS[reason], type_=FORBIDDEN, reason=reason)


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
        if state.settings.auth_mode == "enforce":
            if websocket:
                # Before it is accepted, as the origin refusal closes one (§5.4).
                raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
            raise refusal(
                route_cls, reason, bearer=connection.headers.get("authorization") is not None
            )
        state.components.get(AUTH).would_refuse.note(route_cls, reason, route.path)
    # What `AgentAuthorship` made of the author headers (#4975 phase 5), counted here
    # where the route's class and template are known.
    author_refusal = connection.scope.get(AUTHOR_REFUSAL_KEY)
    if author_refusal is not None:
        state.components.get(AUTH).would_refuse.note(route_cls, author_refusal, route.path)
