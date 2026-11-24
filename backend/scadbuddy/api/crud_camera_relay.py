"""The camera relay's CRUD routes (plan 2026-11-23 camera recording, phase 1): under
``/api/v1/internal/crud/camera-relay/``, for the ``camera-relay`` credential only
(`crud_router`). Area ``printers``. These are the only routes that return a printer's
access code: a plain string in the JSON, which the relay gives to MediaMTX and nothing
else. The code is never logged.

- ``GET …/printers`` and ``…/printers/{printer_id}``: the qualifying printers with their
  codes; 404 ``not_recorded`` for one that does not qualify;
- ``POST …/printers/{printer_id}/feed``: the relay's feed state; 204.
"""

from __future__ import annotations

import asyncio

from fastapi import status

from scadbuddy.api.crud_camera import camera_store, report_feed
from scadbuddy.api.deps import StateDep
from scadbuddy.camera.models import CameraSource, FeedReport
from scadbuddy.camera.store import CameraStore
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router

router = crud_router("camera-relay")


class RelaySource(CameraSource):
    access_code: str


def _with_codes(store: CameraStore, printer_id: str | None) -> list[RelaySource]:
    return [
        RelaySource(
            **s.model_dump(exclude={"access_code"}), access_code=s.access_code.get_secret_value()
        )
        for s in store.sources_with_codes(printer_id)
    ]


@router.get("/printers", name="crud_camera_relay_printers")
async def list_sources(state: StateDep) -> list[RelaySource]:
    return await asyncio.to_thread(_with_codes, camera_store(state), None)


@router.get("/printers/{printer_id}", name="crud_camera_relay_printer")
async def get_source(printer_id: str, state: StateDep) -> RelaySource:
    found = await asyncio.to_thread(_with_codes, camera_store(state), printer_id)
    if not found:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, "This printer is not recorded.", reason="not_recorded"
        )
    return found[0]


@router.post(
    "/printers/{printer_id}/feed",
    status_code=status.HTTP_204_NO_CONTENT,
    name="crud_camera_relay_feed",
)
async def post_feed(printer_id: str, body: FeedReport, state: StateDep) -> None:
    await report_feed(state, printer_id, body.model_copy(update={"part": "relay"}))
