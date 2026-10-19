"""Which spool is in a registry printer's tray (#3818 tier A, plan 2026-10-18 D1, Task 6c).

The native path's manual link: a person says which physical spool, or which spool
provider's record, is in an untagged tray, and ScadBuddy remembers it in
``tray_spool_links`` against the tray's fingerprint (`SpoolStore.link`), so a changed tray
drops it. It replaces, on the native path, the print dialog's question that writes
Bambuddy's assignment (``POST /print/printers/{id}/trays/.../spool``, `api/printing.py`):
nothing here reaches Bambuddy or any other provider. The tray is read from the printer's
latest LAN report, so a printer that has never reported, or a tray it does not show
filled, is a 409.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Annotated, Self
from uuid import UUID

from fastapi import APIRouter, Path, Response, status
from psycopg.errors import ForeignKeyViolation
from pydantic import BaseModel, ConfigDict, Field, model_validator

from scadbuddy.bambuddy.trays import EXTERNAL, seen_trays, tray_label
from scadbuddy.core.problems import ApiError
from scadbuddy.printers.component import PrintersDep
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.store import printer_uuid
from scadbuddy.spools.component import SpoolsDep
from scadbuddy.spools.models import SeenTray, TraySpool
from scadbuddy.spools.overlay import report_status

router = APIRouter(prefix="/printers", tags=["printers"])

TRAY_PATH = "/{printer_id}/trays/{ams_id}/{tray_id}/spool"
#: ``20261019T0304Z_tray_spool_links_printer_fkey.sql``: the one foreign key a deleted
#: printer breaks; the spool's would be a different failure.
PRINTER_FKEY = "tray_spool_links_printer_fkey"
AmsId = Annotated[int, Path(ge=0, le=255)]
TrayId = Annotated[int, Path(ge=0, le=15)]


class TraySpoolLink(BaseModel):
    """A physical spool, or one provider's record of a spool; exactly one of the two."""

    model_config = ConfigDict(extra="forbid")

    physical_spool_id: UUID | None = None
    provider: str | None = Field(default=None, min_length=1, max_length=64)
    record_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _one_spool(self) -> Self:
        record = self.provider is not None or self.record_id is not None
        if self.physical_spool_id is not None and record:
            raise ValueError("name a physical spool or a provider's record, not both")
        if self.physical_spool_id is None and (self.provider is None or self.record_id is None):
            raise ValueError("name a physical spool, or a provider and its record_id")
        return self


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


async def _written[T](printer_id: str, write: Awaitable[T]) -> T:
    """The store's refusals as problem documents. A printer deleted after it was read
    fails the link's insert on ``tray_spool_links``' foreign key: its 404, not a 500
    (the #4186 review's handoff to this task)."""
    try:
        return await write
    except ForeignKeyViolation as error:
        if error.diag.constraint_name != PRINTER_FKEY:
            raise
        raise _no_printer(printer_id) from None
    except LookupError as error:
        raise ApiError(status.HTTP_404_NOT_FOUND, str(error)) from None
    except ValueError as error:
        raise ApiError(status.HTTP_409_CONFLICT, str(error)) from None


@router.put(
    TRAY_PATH,
    response_model=TraySpool,
    summary="Say which spool is in a tray",
    description=(
        "The native path's manual link (#3818): the physical spool, or a spool provider's "
        "record, in an untagged tray, remembered until the tray's material or colour "
        "changes. A record moves to the tray's spool and a spool from any other tray; "
        "nothing is duplicated. An RFID tray's spool is the one its tag names. 404 for an "
        "unknown printer, spool or record; 409 when the printer has not reported the tray "
        "filled, or the tray's tag names another spool. Writes ScadBuddy only."
    ),
)
async def put_tray_spool(
    printers: PrintersDep,
    spools: SpoolsDep,
    printer_id: str,
    ams_id: AmsId,
    tray_id: TrayId,
    body: TraySpoolLink,
) -> TraySpool:
    _known_slot(ams_id, tray_id)
    row = await _row(printers, printer_id)
    tray = await _tray(printers, row, ams_id, tray_id)
    return await _written(
        printer_id,
        spools.store.link(
            UUID(row.id),
            tray,
            physical_spool_id=body.physical_spool_id,
            provider=body.provider,
            record_id=body.record_id,
        ),
    )


@router.delete(
    TRAY_PATH,
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Forget which spool is in a tray",
    description="The spool and its records stay. 404 for an unknown printer.",
)
async def delete_tray_spool(
    printers: PrintersDep,
    spools: SpoolsDep,
    printer_id: str,
    ams_id: AmsId,
    tray_id: TrayId,
) -> Response:
    _known_slot(ams_id, tray_id)
    row = await _row(printers, printer_id)
    await spools.store.unlink(UUID(row.id), ams_id, tray_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
