"""What each printer is doing now, whoever started it (#977).

ScadBuddy is an overlay on Bambuddy, so ``/prints`` shows every active printer's live
state from ``GET /printers/{id}/status``, not only the archives ScadBuddy linked: a job
started from Bambuddy, or one paused on an HMS error, is as visible as one ScadBuddy
queued. A job whose archive ScadBuddy linked points at its print; any other at
Bambuddy's archives.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import HmsError, Printer, PrinterStatus
from scadbuddy.bambuddy.print_links import PrintLinkStore
from scadbuddy.core.problems import ApiError

logger = logging.getLogger(__name__)

#: Bambuddy's archives page, where a job ScadBuddy did not start is found. Bambuddy has
#: no page per archive (``App.tsx`` at 14da007, as ``api/print_history.py`` records),
#: so the card names the archive beside this link rather than in it.
ARCHIVES_PAGE = "/archives"

Activity = Literal["printing", "paused", "idle", "finished", "failed", "offline", "unknown"]

#: Bambu's ``gcode_state`` values, as Bambuddy passes them through as ``state``.
_ACTIVITY: dict[str, Activity] = {
    "RUNNING": "printing",
    "PREPARE": "printing",
    "SLICING": "printing",
    "PAUSE": "paused",
    "IDLE": "idle",
    "FINISH": "finished",
    "FAILED": "failed",
}


class HmsNote(BaseModel):
    code: str
    #: Bambuddy's text for the code, or ``None`` when it has none.
    description: str | None = None
    severity: int | None = None


class PrinterNow(BaseModel):
    printer_id: int
    name: str
    activity: Activity
    #: Bambu's own word (``RUNNING``, ``PAUSE`` …), for anything ``activity`` folds.
    state: str | None = None
    job_name: str | None = None
    #: Percent, 0-100.
    progress: float | None = None
    layer: int | None = None
    total_layers: int | None = None
    remaining_minutes: int | None = None
    hms: list[HmsNote] = Field(default_factory=list)
    #: The finished plate must be cleared before the next job starts. ``None`` when
    #: that is not known (#3632): the status read failed, or Bambuddy's flag was not a
    #: bool. The card then asks for the plate to be checked.
    awaiting_plate_clear: bool | None = False
    archive_id: int | None = None
    #: ScadBuddy linked ``archive_id``: its print is ``/prints/{archive_id}``.
    linked: bool = False
    #: Where Bambuddy shows the archive, when ScadBuddy did not link it.
    bambuddy_url: str | None = None
    #: The status read failed; the rest is unknown.
    error: str | None = None


class PrintersNow(BaseModel):
    printers: list[PrinterNow] = Field(default_factory=list)


def _activity(status: PrinterStatus) -> Activity:
    if not status.connected:
        return "offline"
    return _ACTIVITY.get((status.state or "").upper(), "unknown")


def _hms(errors: list[HmsError]) -> list[HmsNote]:
    return [
        HmsNote(
            code=error.full_code or error.code,
            description=error.description,
            severity=error.severity,
        )
        for error in errors
    ]


async def printer_now(
    client: BambuddyClient, links: PrintLinkStore, printer: Printer
) -> PrinterNow:
    """One printer's card; a failed status read is a card that says so, never a failed
    page."""
    try:
        status = await client.printer_status(printer.id)
    except ApiError as error:
        logger.info("printer status unreadable", extra={"printer_id": printer.id})
        return PrinterNow(
            printer_id=printer.id,
            name=printer.name,
            activity="unknown",
            awaiting_plate_clear=None,
            error=error.detail,
        )
    except (ValidationError, ValueError):
        # A shape the model cannot take, or a body that is not JSON at all
        # (``JSONDecodeError`` is a ``ValueError``), is this printer's card, not the
        # strip's 500.
        logger.warning("printer status malformed", extra={"printer_id": printer.id})
        return PrinterNow(
            printer_id=printer.id,
            name=printer.name,
            activity="unknown",
            awaiting_plate_clear=None,
            error="Bambuddy's status for this printer is in a shape ScadBuddy can't read.",
        )
    activity = _activity(status)
    working = activity in ("printing", "paused")
    archive_id = status.current_archive_id if working else None
    linked = archive_id is not None and await links.linked(archive_id) is not None
    return PrinterNow(
        printer_id=printer.id,
        name=status.name or printer.name,
        activity=activity,
        state=status.state,
        job_name=(status.subtask_name or status.current_print) if working else None,
        progress=status.progress if working else None,
        layer=status.layer_num if working else None,
        total_layers=status.total_layers if working else None,
        remaining_minutes=status.remaining_time if working else None,
        hms=_hms(status.hms_errors),
        awaiting_plate_clear=status.awaiting_plate_clear,
        archive_id=archive_id,
        linked=linked,
        bambuddy_url=(
            client.config.web_url(ARCHIVES_PAGE) if archive_id is not None and not linked else None
        ),
    )


async def printers_now(client: BambuddyClient, links: PrintLinkStore) -> PrintersNow:
    """Every active printer's card, read together, in Bambuddy's order."""
    printers = [printer for printer in await client.printers() if printer.is_active]
    cards = await asyncio.gather(*(printer_now(client, links, row) for row in printers))
    return PrintersNow(printers=list(cards))
