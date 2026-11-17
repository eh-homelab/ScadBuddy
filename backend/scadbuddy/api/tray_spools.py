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

from fastapi import APIRouter, Path, Response, status
from pydantic import BaseModel, ConfigDict

from scadbuddy.api.filament import actor
from scadbuddy.auth.component import CallerDep
from scadbuddy.bambuddy.trays import EXTERNAL, seen_trays, tray_label
from scadbuddy.core.problems import ApiError
from scadbuddy.filament import models as m
from scadbuddy.filament.catalog import CatalogRefError
from scadbuddy.filament.component import FilamentDep
from scadbuddy.printers.component import PrintersDep
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.reports import report_status
from scadbuddy.printers.store import printer_uuid
from scadbuddy.spools.models import SeenTray

router = APIRouter(prefix="/printers", tags=["printers"])

TRAY_PATH = "/{printer_id}/trays/{ams_id}/{tray_id}/spool"
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
        "spool, or its two keys name two different tags. Writes ScadBuddy only."
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
