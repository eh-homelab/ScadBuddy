"""``POST /telemetry/v1/traces``: the browser's spans, relayed to the collector
(spec 2026-10-01 §5.2).

A transport, not a versioned operation: mounted at the root beside ``/healthz`` and
``/metrics`` (``main.ROOT_ROUTE_MODULES``) and left out of the OpenAPI schema, so
nothing ``_api_router()`` attaches applies to it, and it needs no agent tool and no
``coverage.ts`` entry. Same origin as the page, so it works inside Bambuddy's iframe.

Every refusal is an RFC 9457 problem (`core/problems.py`), and its ``detail`` names the
rule, never the request's own values. Every other method on the path, and every other
path under ``/telemetry``, is answered here too: registered before the SPA's static
mount is not enough, because a ``GET`` the ``POST`` route does not take would fall
through to the mount and be served ``index.html``.
"""

from __future__ import annotations

import asyncio
import re
from typing import Final

from fastapi import APIRouter, FastAPI, Request, Response

from scadbuddy.api.limits import RouteLimit
from scadbuddy.core.problems import ApiError
from scadbuddy.telemetry.admission import check_content_type, check_origin, relay_client
from scadbuddy.telemetry.component import TraceRelayDep
from scadbuddy.telemetry.payload import BatchTooLargeError, PayloadError, RouteMatcher, prepare
from scadbuddy.telemetry.routes import route_matcher

RELAY_PATH: Final = "/telemetry/v1/traces"
#: On the 204 when tracing is off (no endpoint, or ``OTEL_SDK_DISABLED``): the page's
#: exporter stops for the rest of its life (spec §5.3).
TRACING_HEADER: Final = "X-ScadBuddy-Tracing"
#: The browser never comes near it (it sends at most 48 KiB a request, spec §5.3); it
#: bounds other callers. Without it the ``application/json`` default, 8 MiB, would apply.
MAX_BODY_BYTES: Final = 256 * 1024

#: For ``main.py``'s `BodySizeGate`: refused on the headers, or as the body streams.
RELAY_ROUTE_LIMIT: Final = RouteLimit(
    "POST",
    re.compile(rf"^{re.escape(RELAY_PATH)}$"),
    MAX_BODY_BYTES,
    "a trace batch",
    "fixed, not a setting",
)

_NOT_POST: Final = ["GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"]

router = APIRouter(tags=["telemetry"])

_MATCHER_ATTR: Final = "trace_relay_route_matcher"


def _route_matcher(app: FastAPI) -> RouteMatcher:
    """The app's routes as a `RouteMatcher`, built on the first batch, when every route
    is registered, and kept on the app: `prepare` runs it off the event loop."""
    matcher: RouteMatcher | None = getattr(app.state, _MATCHER_ATTR, None)
    if matcher is None:
        matcher = route_matcher(app.routes)
        setattr(app.state, _MATCHER_ATTR, matcher)
    return matcher


@router.post(RELAY_PATH, include_in_schema=False, status_code=204)
async def relay_traces(request: Request, relay: TraceRelayDep) -> Response:
    # ``async def`` on purpose: `RelayLimits` is not thread-safe, so its buckets must be
    # touched on the event loop, never from the threadpool a plain ``def`` runs in.
    settings = relay.settings()
    check_origin(request.headers, settings)
    if relay.forwarder.off:
        return Response(status_code=204, headers={TRACING_HEADER: "off"})
    if relay.forwarder.closing:
        raise ApiError(503, "the relay is shutting down")
    check_content_type(request.headers)
    peer = request.client.host if request.client is not None else None
    relay.limits.take(relay_client(request.headers, peer, settings))
    try:
        batch = await asyncio.to_thread(prepare, await request.body(), _route_matcher(request.app))
    except BatchTooLargeError as error:
        raise ApiError(413, str(error)) from error
    except PayloadError as error:
        raise ApiError(400, "the body is not an OTLP/JSON trace export") from error
    if batch is not None:
        relay.forwarder.offer(batch)
    return Response(status_code=204)


@router.api_route(RELAY_PATH, methods=_NOT_POST, include_in_schema=False)
async def relay_other_methods() -> Response:
    raise ApiError(405, "the relay accepts POST only", headers={"Allow": "POST"})


@router.api_route("/telemetry", methods=[*_NOT_POST, "POST"], include_in_schema=False)
@router.api_route("/telemetry/{rest:path}", methods=[*_NOT_POST, "POST"], include_in_schema=False)
async def no_such_telemetry_route() -> Response:
    raise ApiError(404, "the only telemetry route is POST /telemetry/v1/traces")
