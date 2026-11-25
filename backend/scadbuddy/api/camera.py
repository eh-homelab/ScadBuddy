"""The browser's camera routes (camera live view, phase 3), under ``/api/v1/printers``:
the feed state of a printer's camera, and a request to restart its pull.

The relay and the recorder write the feed state through the CRUD routes
(`api/crud_camera.py`); here the browser only reads it. A reconnect emits
``camera.reconnect``, which the relay follows."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, status
from pydantic import BaseModel

from scadbuddy.api.deps import StateDep
from scadbuddy.camera.models import CameraFeed
from scadbuddy.camera.store import CameraStore
from scadbuddy.core.events import CameraReconnectEvent, emit
from scadbuddy.core.problems import ApiError
from scadbuddy.printers.component import PRINTERS
from scadbuddy.printers.store import PrinterNotFoundError

PROBLEMS = "https://scadbuddy.dev/problems/"

router = APIRouter(prefix="/printers", tags=["printers"])


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
    "/{printer_id}/camera",
    response_model=PrinterCamera,
    summary="A printer's camera feed state",
)
async def get_printer_camera_state(state: StateDep, printer_id: str) -> PrinterCamera:
    recorded, feeds = await _state(state, printer_id)
    return PrinterCamera(
        recorded=recorded, live=recorded and state.settings.camera_edge_url is not None, feeds=feeds
    )


@router.post(
    "/{printer_id}/camera/reconnect",
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
    emit(state.events, CameraReconnectEvent(printer_id=printer_id))
    return Reconnecting(printer_id=printer_id)
