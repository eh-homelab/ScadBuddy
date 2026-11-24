"""Which SB spool is in a registry printer's tray (#3818 tier A, #6262 Task 4).

The native path's manual link: a person says which SB spool is in an untagged tray, and
ScadBuddy places the spool at the tray (its location is the tray's place) with the
tray's fingerprint (`FilamentInventory.link`), so a changed tray drops it. It replaces,
on the native path, the print dialog's question that writes Bambuddy's assignment
(``POST /print/printers/{id}/trays/.../spool``, `api/printing.py`): nothing here reaches
Bambuddy or any other provider. The tray is read from the printer's latest LAN report,
so a printer that has never reported, or a tray it does not show filled, is a 409.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from scadbuddy.api.filament import actor, nudge_after_write
from scadbuddy.auth.component import CallerDep
from scadbuddy.bambuddy.trays import EXTERNAL, seen_trays, tray_label
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.idempotency import (
    MaybeIdempotencyKey,
    Replay,
    idempotent_write,
    required_key,
    stored,
)
from scadbuddy.filament import models as m
from scadbuddy.filament.catalog import CatalogKeyEmptyError, CatalogRefError
from scadbuddy.filament.component import FilamentDep
from scadbuddy.filament.inventory import TrayColourError
from scadbuddy.printers.component import PrintersDep
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.reports import report_status
from scadbuddy.printers.store import printer_uuid
from scadbuddy.spools.models import SeenTray

router = APIRouter(prefix="/printers", tags=["printers"], dependencies=[Depends(nudge_after_write)])

TRAY_PATH = "/{printer_id}/trays/{ams_id}/{tray_id}/spool"
NEW_SPOOL_PATH = "/{printer_id}/trays/{ams_id}/{tray_id}/new-spool"
NEW_SPOOL_ROUTE = f"POST /printers{NEW_SPOOL_PATH}"
AmsId = Annotated[int, Path(ge=0, le=255)]
TrayId = Annotated[int, Path(ge=0, le=15)]


class TraySpoolLink(BaseModel):
    """The SB spool in the tray."""

    model_config = ConfigDict(extra="forbid")

    spool_id: UUID


def _no_printer(printer_id: str) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, f"no printer {printer_id}")


def _known_slot(ams_id: int, tray_id: int) -> None:
    """422 for a unit or slot no printer has (#4336 review M1): an AMS 0-15 with slots
    0-3, an AMS-HT 128-135 with slot 0, the external holders 255 with 0-1, the spaces
    `trays.unit_label` names."""
    if 0 <= ams_id <= 15:
        known = tray_id <= 3
    elif 128 <= ams_id <= 135:
        known = tray_id == 0
    else:
        known = ams_id == EXTERNAL and tray_id <= 1
    if not known:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"no printer has unit {ams_id} slot {tray_id}: an AMS is 0-15 with slots 0-3, an"
            " AMS-HT 128-135 with slot 0, and the external holders 255 with slots 0-1",
        )


async def _row(printers: PrintersDep, printer_id: str) -> PrinterRow:
    row = None
    if printer_uuid(printer_id) is not None:
        row = await asyncio.to_thread(printers.store.get, printer_id)
    if row is None:
        raise _no_printer(printer_id)
    if row.kind != "physical":
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"{row.name} is a Bambuddy virtual printer; it has no trays of its own",
        )
    return row


async def _tray(printers: PrintersDep, row: PrinterRow, ams_id: int, tray_id: int) -> SeenTray:
    report = await asyncio.to_thread(printers.reports.get, row.id)
    if report is None:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"{row.name} has not reported its trays over the LAN yet",
        )
    found = seen_trays(report_status(row, report, now=datetime.now(UTC)))
    tray = next((t for t in found if (t.ams_id, t.tray_id) == (ams_id, tray_id)), None)
    if tray is None:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"{row.name}'s last report does not show {tray_label(ams_id, tray_id)} filled",
        )
    return tray


def _written[T](printer_id: str, write: Callable[[], T]) -> T:
    """The inventory's refusals as problem documents. A printer deleted after it was read
    is its 404, not a 500 (the #4186 review's handoff): the link's own printer read, or
    the tray place's foreign key (`CatalogRefError`)."""
    try:
        return write()
    except CatalogRefError:
        raise _no_printer(printer_id) from None
    except LookupError as error:
        raise ApiError(status.HTTP_404_NOT_FOUND, str(error)) from None
    except ValueError as error:
        raise ApiError(status.HTTP_409_CONFLICT, str(error)) from None


@router.put(
    TRAY_PATH,
    response_model=m.Spool,
    summary="Say which spool is in a tray",
    description=(
        "The native path's manual link (#3818, #6262): the SB spool (`spool_id`, from "
        "`GET /filament/spools`) in an untagged tray. The spool moves to the tray's place, "
        "and the tray's previous spool to no place, each with a `moved` ledger event. It "
        "holds the tray until the tray's material or colour (alpha included) changes. An "
        "RFID tray's spool is the one its tag names; a tagged tray's tag is bound to the "
        "spool (one on no spool, or on an archived one, is taken), and a spool that held "
        "another tag is rebound to the tray's. 404 for an unknown printer or spool; 409 "
        "when the printer has not reported the tray filled, the tray's tag is on another "
        "spool, or its two keys name two different tags. Writes ScadBuddy; while "
        "`filament_sync.tags` pushes (`sb_to_bb` or `two_way`), the spool refresh then pushes "
        "the tag change to Bambuddy."
    ),
)
async def put_tray_spool(
    printers: PrintersDep,
    filament: FilamentDep,
    caller: CallerDep,
    printer_id: str,
    ams_id: AmsId,
    tray_id: TrayId,
    body: TraySpoolLink,
) -> m.Spool:
    _known_slot(ams_id, tray_id)
    row = await _row(printers, printer_id)
    tray = await _tray(printers, row, ams_id, tray_id)
    who = actor(caller)
    return await asyncio.to_thread(
        _written,
        printer_id,
        lambda: filament.inventory.link(
            UUID(row.id), ams_id, tray_id, body.spool_id, tray, actor=who
        ),
    )


@router.delete(
    TRAY_PATH,
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Forget which spool is in a tray",
    description=(
        "The tray's spool moves to no place, with a `moved` ledger event; the spool "
        "itself stays. 404 for an unknown printer."
    ),
)
async def delete_tray_spool(
    printers: PrintersDep,
    filament: FilamentDep,
    caller: CallerDep,
    printer_id: str,
    ams_id: AmsId,
    tray_id: TrayId,
) -> Response:
    _known_slot(ams_id, tray_id)
    row = await _row(printers, printer_id)
    await asyncio.to_thread(
        filament.inventory.unlink, UUID(row.id), ams_id, tray_id, actor=actor(caller)
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    NEW_SPOOL_PATH,
    response_model=m.Spool,
    status_code=status.HTTP_201_CREATED,
    summary="Make a spool from what a tray reports",
    description=(
        "Create from slot (#7150): a new SB spool from the printer's latest LAN report of the "
        "tray, placed in it. Its material is the tray's (matched by name or alias, and added "
        "to the catalog when none has it), its colour the tray's `#RRGGBBAA`, and a tagged "
        "tray's tag is bound to it. It has no type, label weight or grams yet: it needs a "
        "type, as a spool the type proposals group. The tray's previous spool moves to no "
        "place, with a `moved` ledger event. ScadBuddy builds it from its own report; "
        "nothing reaches Bambuddy. Needs an `Idempotency-Key` (428 without one): a retry "
        "with the same key answers the first spool and writes nothing. 404 for an unknown "
        "printer; 409 for a Bambuddy virtual printer, a printer that has not reported the "
        "tray filled, a tray whose tag is already on a spool (that spool is the tray's) or "
        "whose two keys name two different tags; 422 for a unit or slot no printer has, "
        "a material name with no letter or digit, or a colour that is not `#RRGGBBAA`."
    ),
)
async def new_tray_spool(
    printers: PrintersDep,
    filament: FilamentDep,
    caller: CallerDep,
    printer_id: str,
    ams_id: AmsId,
    tray_id: TrayId,
    idempotency_key: MaybeIdempotencyKey = None,
) -> JSONResponse:
    who = actor(caller)
    key = f"{who}\x1f{required_key(idempotency_key)}"
    _known_slot(ams_id, tray_id)
    asked = {"printer_id": printer_id, "ams_id": ams_id, "tray_id": tray_id}

    def first() -> Replay | None:
        with filament.spools.transaction() as conn:
            return stored(conn, "filament", key, NEW_SPOOL_ROUTE, asked)

    # A retry gets the first answer, even once the tray it read has changed.
    replay = await asyncio.to_thread(first)
    if replay is not None:
        return JSONResponse(replay.body, status_code=replay.status)
    row = await _row(printers, printer_id)
    tray = await _tray(printers, row, ams_id, tray_id)
    printer = UUID(row.id)

    def write() -> Replay:
        place = filament.inventory.tray_place(printer, ams_id, tray_id, tray)
        with filament.spools.transaction() as conn:

            def run() -> Replay:
                try:
                    made = filament.inventory.create_in_tray_on(
                        conn, place, printer, ams_id, tray_id, tray, actor=who
                    )
                except (CatalogKeyEmptyError, TrayColourError) as error:
                    raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
                return Replay(status.HTTP_201_CREATED, made.model_dump(mode="json"))

            return idempotent_write(conn, "filament", key, NEW_SPOOL_ROUTE, asked, run)

    done = await asyncio.to_thread(_written, printer_id, write)
    return JSONResponse(done.body, status_code=done.status)
