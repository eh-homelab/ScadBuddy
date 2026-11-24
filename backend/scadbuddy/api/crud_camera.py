"""The camera recorders' CRUD routes (plan 2026-11-23 camera recording, phase 1): under
``/api/v1/internal/crud/camera/``, for the ``camera`` credential only (`crud_router`). Areas
``printers`` and ``changes``. No route returns an access code (`api/crud_camera_relay.py` does).

- ``GET …/changes``: the change stream, only ``CAMERA_KINDS`` (`keep_camera`), with the agent
  stream's frames and ``Last-Event-ID`` replay (`api/crud_agent_changes.py`);
- ``GET …/printers`` and ``…/printers/{printer_id}``: the printers that qualify
  (`CameraStore.sources`); 404 ``not_recorded`` for one that does not;
- ``POST …/printers/{printer_id}/segments``: index uploaded segments, in at most
  `MAX_SEGMENTS` per call; 409 ``not_leader`` when the recorder holds no live lease;
- ``POST …/printers/{printer_id}/lease``: acquire, renew or release the lease;
- ``POST …/printers/{printer_id}/feed``: the recorder's feed state; 204. A change of
  state emits ``camera.feed``.

The lease and feed routes answer 404 ``printer_not_found`` for a printer that was deleted, so
the callers stop: the lease's and the feed's rows need the printer's row.

The lease and the index use the API process clock, never a time from the request.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Header, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from scadbuddy.agent_changes.hub import Change, ChangeHub
from scadbuddy.api.crud_agent_changes import HubDep, change_stream
from scadbuddy.api.deps import AppState, StateDep
from scadbuddy.camera.models import CameraSource, FeedReport, LeaseAnswer, SegmentIn
from scadbuddy.camera.store import CameraStore, NotLeaderError
from scadbuddy.core.events import PG_CHANNEL, CameraFeedEvent, emit
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router
from scadbuddy.printers.component import PRINTERS
from scadbuddy.printers.store import PrinterNotFoundError

MAX_SEGMENTS = 500
#: The ``scadbuddy_events`` kinds the camera callers' streams carry. ``camera.reconnect`` is
#: sent from phase 3; the filter names it now.
CAMERA_KINDS = frozenset({"printer.changed", "camera.reconnect"})
#: The streams both camera callers may hold together: their own budget, counted by caller
#: and not by credential, never taken from the agent's.
CAMERA_STREAMS_MAX = 8

router = crud_router("camera")


class SegmentsAsk(BaseModel):
    recorder: str = Field(min_length=1, max_length=128)
    segments: Annotated[list[SegmentIn], Field(max_length=MAX_SEGMENTS)]


class Indexed(BaseModel):
    indexed: int


class LeaseAsk(BaseModel):
    recorder: str = Field(min_length=1, max_length=128)
    action: Literal["acquire", "renew", "release"]


def keep_camera(change: Change) -> bool:
    """Only the event log's ``CAMERA_KINDS``: no other channel, no other kind."""
    if change.channel != PG_CHANNEL:
        return False
    try:
        return json.loads(change.payload).get("kind") in CAMERA_KINDS
    except (ValueError, AttributeError):
        return False


async def camera_changes(
    request: Request, state: AppState, hub: ChangeHub, last_event_id: str | None
) -> StreamingResponse:
    return await change_stream(
        request, state, hub, last_event_id, keep=keep_camera, group=("camera", CAMERA_STREAMS_MAX)
    )


def camera_store(state: StateDep) -> CameraStore:
    printers = state.components.get(PRINTERS)
    return CameraStore(printers.store.pool, printers.store)


@router.get("/changes", name="crud_camera_changes")
async def changes(
    request: Request,
    state: StateDep,
    hub: HubDep,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    return await camera_changes(request, state, hub, last_event_id)


@router.get("/printers", name="crud_camera_printers")
async def list_sources(state: StateDep) -> list[CameraSource]:
    return await asyncio.to_thread(camera_store(state).sources)


@router.get("/printers/{printer_id}", name="crud_camera_printer")
async def get_source(printer_id: str, state: StateDep) -> CameraSource:
    found = await asyncio.to_thread(camera_store(state).sources, printer_id)
    if not found:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, "This printer is not recorded.", reason="not_recorded"
        )
    return found[0]


@router.post("/printers/{printer_id}/segments", name="crud_camera_segments")
async def post_segments(printer_id: str, body: SegmentsAsk, state: StateDep) -> Indexed:
    try:
        indexed = await asyncio.to_thread(
            camera_store(state).index, printer_id, body.recorder, body.segments, datetime.now(UTC)
        )
    except NotLeaderError:
        raise ApiError(
            status.HTTP_409_CONFLICT, "This recorder does not hold the lease.", reason="not_leader"
        ) from None
    return Indexed(indexed=indexed)


@router.post("/printers/{printer_id}/lease", name="crud_camera_lease")
async def post_lease(printer_id: str, body: LeaseAsk, state: StateDep) -> LeaseAnswer:
    try:
        return await asyncio.to_thread(
            camera_store(state).lease, printer_id, body.recorder, body.action, datetime.now(UTC)
        )
    except PrinterNotFoundError:
        raise _printer_gone() from None


@router.post(
    "/printers/{printer_id}/feed",
    status_code=status.HTTP_204_NO_CONTENT,
    name="crud_camera_feed",
)
async def post_feed(printer_id: str, body: FeedReport, state: StateDep) -> None:
    await report_feed(state, printer_id, body.model_copy(update={"part": "recorder"}))


async def report_feed(state: StateDep, printer_id: str, report: FeedReport) -> None:
    try:
        changed = await asyncio.to_thread(camera_store(state).report_feed, printer_id, report)
    except PrinterNotFoundError:
        raise _printer_gone() from None
    if changed:
        emit(state.events, CameraFeedEvent(printer_id=printer_id))


def _printer_gone() -> ApiError:
    return ApiError(
        status.HTTP_404_NOT_FOUND, "There is no such printer.", reason="printer_not_found"
    )
