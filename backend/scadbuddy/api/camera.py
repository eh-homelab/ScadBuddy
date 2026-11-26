"""The browser's camera routes (camera live view, phase 3): the feed state of a printer's
camera, a request to restart its pull, and the live view, proxied to the live edge.

The relay and the recorder write the feed state through the CRUD routes
(`api/crud_camera.py`); here the browser only reads it. A reconnect emits
``camera.reconnect``, which the relay follows.

The live view (spec §6.7) reaches the browser only on ScadBuddy's origin, under
``/api/v1/camera/live/{printer_id}/``: WHEP (WebRTC) and LL-HLS, each forwarded to the edge's
MediaMTX (`deploy/camera/mediamtx-edge.yml`) with the edge read user's password. No header of
the browser's reaches the edge, and no cookie of the edge's reaches the browser. MediaMTX
v1.21.4 carries an HLS session in the query key ``session`` when the client sends no cookie
(measured 2026-11-25, `backend/tests/api/test_camera_live.py`), so that key passes."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, suppress
from typing import Any

import httpx
from fastapi import APIRouter, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from scadbuddy.api.deps import StateDep
from scadbuddy.api.operations import PROBLEM_SCHEMA
from scadbuddy.api.streaming import ClosingStreamingResponse
from scadbuddy.camera.models import CameraFeed
from scadbuddy.camera.store import CameraStore
from scadbuddy.core.events import CameraReconnectEvent, emit
from scadbuddy.core.problems import PROBLEM_MEDIA_TYPE, ApiError
from scadbuddy.printers.component import PRINTERS
from scadbuddy.printers.store import PrinterNotFoundError, printer_uuid

logger = logging.getLogger(__name__)

PROBLEMS = "https://scadbuddy.dev/problems/"

router = APIRouter()


class PrinterCamera(BaseModel):
    #: The printer qualifies for recording: a LAN link, an RTSPS camera, recording on.
    recorded: bool
    #: A live view is on offer: recorded, and the live edge is set up.
    live: bool
    feeds: list[CameraFeed]


class Reconnecting(BaseModel):
    printer_id: str


async def _state(state: StateDep, printer_id: str) -> tuple[bool, list[CameraFeed]]:
    printers = state.components.get(PRINTERS)
    store = CameraStore(printers.store.pool, printers.store)
    try:
        return await asyncio.to_thread(store.camera_state, printer_id)
    except PrinterNotFoundError:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            f"no printer {printer_id}",
            type_=f"{PROBLEMS}printer-not-found",
        ) from None


@router.get(
    "/printers/{printer_id}/camera",
    tags=["printers"],
    response_model=PrinterCamera,
    summary="A printer's camera feed state",
)
async def get_printer_camera_state(state: StateDep, printer_id: str) -> PrinterCamera:
    recorded, feeds = await _state(state, printer_id)
    return PrinterCamera(
        recorded=recorded, live=recorded and state.settings.camera_edge_ready(), feeds=feeds
    )


@router.post(
    "/printers/{printer_id}/camera/reconnect",
    tags=["printers"],
    status_code=status.HTTP_202_ACCEPTED,
    response_model=Reconnecting,
    summary="Restart the camera pull of a printer",
    description="409 `camera-not-recorded` when the printer is not recorded.",
)
async def reconnect_printer_camera(state: StateDep, printer_id: str) -> Reconnecting:
    recorded, _ = await _state(state, printer_id)
    if not recorded:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"printer {printer_id} is not recorded",
            type_=f"{PROBLEMS}camera-not-recorded",
        )
    # The canonical spelling, so the relay never puts a second path for one printer.
    emit(state.events, CameraReconnectEvent(printer_id=str(printer_uuid(printer_id))))
    return Reconnecting(printer_id=printer_id)


# The live view: a proxy to the live edge.

LIVE = "/camera/live/{printer_id}"
#: The prefix the browser sees the live routes under (`main.API_PREFIX`).
LIVE_PREFIX = "/api/v1/camera/live"
#: An HLS file name: MediaMTX v1.21.4 serves `index.m3u8`, `video1_stream.m3u8`,
#: `<hex>_video1_init.mp4`, `<hex>_video1_seg<N>.mp4`, `<hex>_video1_part<N>.mp4` and
#: `gap.mp4` (measured 2026-11-25). No `/`, no `.` but the extension's.
HLS_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}\.(?:m3u8|mp4|m4s)")
#: A WHEP session: MediaMTX names it by a uuid.
WHEP_SESSION = re.compile(r"[A-Za-z0-9-]{1,64}")
#: The query keys an HLS request passes to the edge: LL-HLS's blocking reload and skip, and
#: MediaMTX's session and cookie check. A value of another shape is dropped.
HLS_QUERY = ("_HLS_msn", "_HLS_part", "_HLS_skip", "session", "cookieCheck")
HLS_QUERY_VALUE = re.compile(r"[A-Za-z0-9_-]{1,64}")
#: A redirect or playlist target the edge names: a file name and an optional query.
HLS_TARGET = re.compile(rf"{HLS_NAME.pattern}(?:\?[A-Za-z0-9_=&-]{{1,512}})?")
#: The largest WHEP offer or ICE fragment passed on.
WHEP_BODY_MAX = 64 * 1024
SDP = "application/sdp"
SDP_FRAG = "application/trickle-ice-sdpfrag"
#: A blocking playlist reload waits for its part, so an HLS read waits up to 20 s
#: (LL-HLS stalls below 15 s). A WHEP offer of an idle path waits for the edge's
#: on-demand pull, which MediaMTX gives 10 s (`sourceOnDemandStartTimeout`).
HLS_TIMEOUT = httpx.Timeout(5.0, read=20.0)
WHEP_TIMEOUT = httpx.Timeout(5.0, read=15.0)
#: The edge's answer headers a WHEP answer passes on; nothing else of the edge's does.
WHEP_HEADERS = ("content-type", "etag", "link", "accept-patch")
NO_STORE = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}
EDGE_UNSET = f"{PROBLEMS}camera-edge-unset"
EDGE_UNREACHABLE = f"{PROBLEMS}camera-edge-unreachable"
EDGE_REFUSED = f"{PROBLEMS}camera-edge-refused"
NOT_RECORDED = f"{PROBLEMS}camera-not-recorded"
SESSION_ENDED = f"{PROBLEMS}camera-live-session-ended"

#: The edge's transport: the default, or a test's ``httpx.MockTransport``.
_edge_transport: httpx.AsyncBaseTransport | None = None

_PROBLEM = {"content": {PROBLEM_MEDIA_TYPE: {"schema": PROBLEM_SCHEMA}}}
LIVE_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {
        **_PROBLEM,
        "description": "`camera-not-recorded`, or a path of another shape;"
        " `camera-live-session-ended`: the edge's HLS session has ended.",
    },
    502: {
        **_PROBLEM,
        "description": "`camera-edge-unreachable`: the edge did not answer;"
        " `camera-edge-refused`: it refused the API's read user, or redirected outside"
        " the printer's path.",
    },
    503: {**_PROBLEM, "description": "`camera-edge-unset`: no live edge is set up."},
}


def _not_found(detail: str) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, detail, type_=NOT_RECORDED)


def _edge(state: StateDep, *, hls: bool) -> tuple[str, httpx.BasicAuth]:
    """The edge's base URL for WHEP or HLS, and its read user; 503 when unset."""
    settings = state.settings
    base = settings.camera_edge_hls_url if hls else settings.camera_edge_whep_url
    user, password = settings.camera_edge_read_user, settings.camera_edge_read_pass
    if base is None or user is None or password is None:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "No live camera edge is set up.",
            type_=EDGE_UNSET,
        )
    return base.rstrip("/"), httpx.BasicAuth(user, password.get_secret_value())


async def _recorded(state: StateDep, printer_id: str) -> str:
    """The printer's uuid as the edge names its path; 404 unless it is recorded."""
    key = printer_uuid(printer_id)
    if key is None:
        raise _not_found(f"no recorded printer {printer_id}")
    printers = state.components.get(PRINTERS)
    store = CameraStore(printers.store.pool, printers.store)
    if not await asyncio.to_thread(store.recorded, str(key)):
        raise _not_found(f"printer {printer_id} is not recorded")
    return str(key)


def _client(base: str, auth: httpx.BasicAuth, timeout: httpx.Timeout) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base, auth=auth, timeout=timeout, transport=_edge_transport)


def _unreachable(printer_id: str, error: BaseException) -> ApiError:
    # The error's type only: its text names the edge's URL.
    logger.warning(
        "the live camera edge did not answer",
        extra={"printer_id": printer_id, "error": type(error).__name__},
    )
    return ApiError(
        status.HTTP_502_BAD_GATEWAY, "The live camera edge did not answer.", type_=EDGE_UNREACHABLE
    )


async def _check_refused(printer_id: str, response: httpx.Response) -> None:
    """A 401 or 403 from the edge. MediaMTX v1.21.4 answers 401 `session not found` for an
    HLS session that has ended (measured 2026-11-25): a 404, so the player starts again at
    the index. Any other refuses the API's read user, a deployment fault: a 502, so the
    browser neither sees the edge's Basic challenge nor takes it for a sign-out."""
    if response.status_code not in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN):
        return
    await response.aread()
    with suppress(ValueError):
        answer = response.json()
        if isinstance(answer, dict) and answer.get("error") == "session not found":
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                "The live session has ended; start again at index.m3u8.",
                type_=SESSION_ENDED,
            )
    logger.warning(
        "the live camera edge refused the API's read user",
        extra={"printer_id": printer_id, "status": response.status_code},
    )
    raise ApiError(
        status.HTTP_502_BAD_GATEWAY,
        "The live camera edge refused the API.",
        type_=EDGE_REFUSED,
    )


def _media_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";")[0].strip().lower()


async def _body(request: Request, media_type: str) -> bytes:
    """The request's body, of ``media_type`` and at most `WHEP_BODY_MAX` bytes."""
    if _media_type(request) != media_type:
        raise ApiError(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"The body must be {media_type}.")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > WHEP_BODY_MAX:
            raise ApiError(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"The body is over {WHEP_BODY_MAX} bytes.",
            )
    return bytes(body)


async def _whep(
    state: StateDep,
    printer_id: str,
    method: str,
    session: str | None,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    if session is not None and not WHEP_SESSION.fullmatch(session):
        raise _not_found(f"no WHEP session {session}")
    base, auth = _edge(state, hls=False)
    printer = await _recorded(state, printer_id)
    path = f"/{printer}/whep" + ("" if session is None else f"/{session}")
    try:
        async with _client(base, auth, WHEP_TIMEOUT) as http:
            response = await http.request(method, path, content=content, headers=headers)
    except httpx.HTTPError as error:
        raise _unreachable(printer, error) from None
    await _check_refused(printer, response)
    passed = {k: v for k, v in response.headers.items() if k.lower() in WHEP_HEADERS}
    location = response.headers.get("location")
    if location is not None and (
        found := re.fullmatch(rf"/{printer}/whep/({WHEP_SESSION.pattern})", location)
    ):
        passed["Location"] = f"{LIVE_PREFIX}/{printer}/whep/{found.group(1)}"
    return Response(
        response.content, status_code=response.status_code, headers={**passed, **NO_STORE}
    )


@router.post(
    f"{LIVE}/whep",
    tags=["camera"],
    response_class=Response,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"content": {SDP: {}}, "description": "The edge's SDP answer"},
        **LIVE_RESPONSES,
    },
    summary="Start watching a printer's camera over WebRTC (WHEP)",
    description=(
        "The body is the browser's SDP offer (`application/sdp`, at most 64 KiB), passed to"
        " the live edge. The answer's `Location` is the session's URL under this route."
    ),
)
async def offer_live_camera(state: StateDep, printer_id: str, request: Request) -> Response:
    content = await _body(request, SDP)
    return await _whep(
        state, printer_id, "POST", None, content=content, headers={"Content-Type": SDP}
    )


@router.patch(
    f"{LIVE}/whep/{{session}}",
    tags=["camera"],
    response_class=Response,
    status_code=status.HTTP_204_NO_CONTENT,
    responses=LIVE_RESPONSES,
    summary="Send ICE candidates to a WHEP session",
)
async def patch_live_camera(
    state: StateDep, printer_id: str, session: str, request: Request
) -> Response:
    content = await _body(request, SDP_FRAG)
    headers = {"Content-Type": SDP_FRAG}
    if_match = request.headers.get("if-match")
    if if_match is not None and len(if_match) <= 128:
        headers["If-Match"] = if_match
    return await _whep(state, printer_id, "PATCH", session, content=content, headers=headers)


@router.delete(
    f"{LIVE}/whep/{{session}}",
    tags=["camera"],
    response_class=Response,
    responses=LIVE_RESPONSES,
    summary="End a WHEP session",
)
async def end_live_camera(state: StateDep, printer_id: str, session: str) -> Response:
    return await _whep(state, printer_id, "DELETE", session)


def _hls_location(location: str, printer: str) -> str | None:
    """The edge's redirect as a URL under this route; ``None`` for one elsewhere."""
    if HLS_TARGET.fullmatch(location):
        return location
    rest = location.removeprefix(f"/{printer}/")
    if rest != location and HLS_TARGET.fullmatch(rest):
        return f"{LIVE_PREFIX}/{printer}/hls/{rest}"
    return None


@router.get(
    f"{LIVE}/hls/{{name}}",
    tags=["camera"],
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"application/vnd.apple.mpegurl": {}, "video/mp4": {}},
            "description": "A playlist or a media file of the edge's LL-HLS",
        },
        **LIVE_RESPONSES,
    },
    summary="Watch a printer's camera over LL-HLS",
    description=(
        "A playlist or a media file of the live edge's LL-HLS, streamed. Start at"
        " `index.m3u8`; the playlists name every other file relative to it. Only the query"
        f" keys {', '.join(f'`{key}`' for key in HLS_QUERY)} are passed on."
    ),
)
async def live_camera_hls(
    state: StateDep, printer_id: str, name: str, request: Request
) -> Response:
    if not HLS_NAME.fullmatch(name):
        raise _not_found(f"no live file {name}")
    base, auth = _edge(state, hls=True)
    printer = await _recorded(state, printer_id)
    params: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key in HLS_QUERY and key not in params and HLS_QUERY_VALUE.fullmatch(value):
            params[key] = value
    stack = AsyncExitStack()
    try:
        http = await stack.enter_async_context(_client(base, auth, HLS_TIMEOUT))
        response = await stack.enter_async_context(
            http.stream("GET", f"/{printer}/{name}", params=params)
        )
        await _check_refused(printer, response)
    except httpx.HTTPError as error:
        await stack.aclose()
        raise _unreachable(printer, error) from None
    except BaseException:
        await stack.aclose()
        raise
    headers = {**NO_STORE, "X-Accel-Buffering": "no"}
    if 300 <= response.status_code < 400:
        location = response.headers.get("location")
        rewritten = None if location is None else _hls_location(location, printer)
        if rewritten is None:
            await stack.aclose()
            # A redirect elsewhere is never passed, and a bare 3xx would leave the player stuck.
            logger.warning(
                "the live camera edge redirected outside the printer's path",
                extra={"printer_id": printer, "status": response.status_code},
            )
            raise ApiError(
                status.HTTP_502_BAD_GATEWAY,
                "The live camera edge answered with a redirect elsewhere.",
                type_=EDGE_REFUSED,
            )
        headers["Location"] = rewritten

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        except httpx.HTTPError as error:
            # The edge stalled or dropped mid-file; the player asks again.
            logger.info(
                "live camera file ended",
                extra={"printer_id": printer, "error": type(error).__name__},
            )
        finally:
            await stack.aclose()

    return ClosingStreamingResponse(
        body(),
        status_code=response.status_code,
        media_type=response.headers.get("content-type"),
        headers=headers,
        closing=stack,
    )
