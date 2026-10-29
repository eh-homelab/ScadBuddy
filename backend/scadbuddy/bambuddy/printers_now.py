"""What each printer is doing now, whoever started it (#977).

ScadBuddy is an overlay on Bambuddy, so ``/prints`` shows every active printer's live
state from ``GET /printers/{id}/status``, not only the archives ScadBuddy linked: a job
started from Bambuddy, or one paused on an HMS error, is as visible as one ScadBuddy
queued. A job whose archive ScadBuddy linked points at its print; any other at
Bambuddy's archives.

Since the printer registry (#3823) a card is a registry printer's. A LAN-linked one
with a fresh report (`is_fresh`) shows that report, whether or not it is also in
Bambuddy, and costs no Bambuddy call (hot-path plan 2026-10-18 D4); otherwise a
Bambuddy-linked one shows Bambuddy's status. A Bambuddy that is configured but does not
answer costs only the cards only it could fill: each of those says so (#4186 review I1).
The serial stays off the card (I6): the registry's own ``GET /printers`` carries it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import HmsError, Printer, PrinterStatus
from scadbuddy.bambuddy.print_links import PrintLinkStore
from scadbuddy.core.problems import ApiError
from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.status import is_fresh, status_from_report
from scadbuddy.printers.store import PrinterStore

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


#: Which link answered for a card: Bambuddy's status, the printer's own LAN report, or
#: neither (a LAN printer never yet heard from).
CardPath = Literal["bambuddy", "native", "none"]


class PrinterNow(BaseModel):
    #: The registry printer (#3823): what a card is keyed by.
    id: str
    path: CardPath
    #: The Bambuddy printer's id, when the row links one: its camera and its archives.
    printer_id: int | None
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


def _card(row: PrinterRow, path: CardPath, status: PrinterStatus) -> PrinterNow:
    activity = _activity(status)
    working = activity in ("printing", "paused")
    return PrinterNow(
        id=row.id,
        path=path,
        printer_id=row.bambuddy_printer_id,
        name=status.name or row.name,
        activity=activity,
        state=status.state,
        job_name=(status.subtask_name or status.current_print) if working else None,
        progress=status.progress if working else None,
        layer=status.layer_num if working else None,
        total_layers=status.total_layers if working else None,
        remaining_minutes=status.remaining_time if working else None,
        hms=_hms(status.hms_errors),
        awaiting_plate_clear=status.awaiting_plate_clear,
    )


async def printer_now(
    client: BambuddyClient, links: PrintLinkStore, printer: Printer, row: PrinterRow
) -> PrinterNow:
    """One Bambuddy-linked printer's card; a failed status read is a card that says so,
    never a failed page."""
    try:
        status = await client.printer_status(printer.id)
    except ApiError as error:
        logger.info("printer status unreadable", extra={"printer_id": printer.id})
        return PrinterNow(
            id=row.id,
            path="bambuddy",
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
            id=row.id,
            path="bambuddy",
            printer_id=printer.id,
            name=printer.name,
            activity="unknown",
            awaiting_plate_clear=None,
            error="Bambuddy's status for this printer is in a shape ScadBuddy can't read.",
        )
    card = _card(row, "bambuddy", status.model_copy(update={"name": status.name or printer.name}))
    working = card.activity in ("printing", "paused")
    archive_id = status.current_archive_id if working else None
    linked = archive_id is not None and await links.linked(archive_id) is not None
    return card.model_copy(
        update={
            "archive_id": archive_id,
            "linked": linked,
            "bambuddy_url": (
                client.config.web_url(ARCHIVES_PAGE)
                if archive_id is not None and not linked
                else None
            ),
        }
    )


def lan_card(
    row: PrinterRow, report: tuple[dict[str, Any], datetime] | None, *, now: datetime
) -> PrinterNow:
    """A card from the printer's own LAN report (Task 6); offline when it has none."""
    if report is None:
        return PrinterNow(
            id=row.id,
            path="none",
            printer_id=row.bambuddy_printer_id,
            name=row.name,
            activity="offline",
        )
    data, seen_at = report
    status = status_from_report(
        row, LanReport(serial=row.serial, received_at=seen_at, print=data), now=now
    )
    return _card(row, "native", status)


Reports = dict[str, tuple[dict[str, Any], datetime]]


def _fresh(row: PrinterRow, latest: Reports, now: datetime) -> bool:
    """Whether the row's own LAN report answers for it (D4)."""
    report = latest.get(row.id)
    return row.lan_host is not None and report is not None and is_fresh(report[1], now)


def _unreachable(row: PrinterRow, error: str) -> PrinterNow:
    return PrinterNow(
        id=row.id,
        path="bambuddy",
        printer_id=row.bambuddy_printer_id,
        name=row.name,
        activity="unknown",
        awaiting_plate_clear=None,
        error=error,
    )


async def printers_now(
    client: BambuddyClient | None,
    links: PrintLinkStore,
    printers: PrinterStore,
    reports: PrinterReportStore,
    *,
    now: datetime,
    native: frozenset[str] = frozenset(),
) -> PrintersNow:
    """One card per registry printer (#3823). Every active Bambuddy printer first, in
    Bambuddy's order, each with its registry row (`ensure_bambuddy` makes one for a
    printer it has never seen, so the strip never loses one); then every other
    LAN-linked row, by name. Each card from the row's fresh LAN report, else Bambuddy's
    status, else offline. ``client`` is ``None`` when no Bambuddy is configured; a
    Bambuddy that fails to list its printers leaves every row it last listed as active
    a card that says so, or its LAN report (`PrinterRow.bambuddy_active`, recorded on
    each answer), so the outage hides what Bambuddy hid (#4186 re-review N4).

    ``native`` is the rows a run reaches over the LAN (`print_run.lan_printers`): each
    is its LAN report's card, never Bambuddy's status, even while that report is not
    fresh (#4551, plan 2026-10-18 D4). Only the Bambuddy-path printers ask Bambuddy:
    their status, and the listing, which is also how a printer the registry has not
    seen yet reaches the strip."""
    active: list[Printer] = []
    unreachable: str | None = None
    if client is not None:
        try:
            active = [printer for printer in await client.printers() if printer.is_active]
        except ApiError as error:
            logger.info("Bambuddy's printers unreadable; the registry answers")
            unreachable = error.detail
    listed = frozenset(printer.id for printer in active)
    if unreachable is None and client is not None:
        await asyncio.to_thread(printers.mark_bambuddy_active, listed)
    rows = [
        await asyncio.to_thread(
            printers.ensure_bambuddy,
            printer.id,
            serial=printer.serial_number,
            name=printer.name,
            model=printer.model,
            listed=listed,
        )
        for printer in active
    ]
    latest: Reports = await asyncio.to_thread(reports.all)

    async def card(printer: Printer, row: PrinterRow) -> PrinterNow:
        if row.id in native or _fresh(row, latest, now) or client is None:
            return lan_card(row, latest.get(row.id), now=now)
        return await printer_now(client, links, printer, row)

    cards = list(await asyncio.gather(*(card(p, r) for p, r in zip(active, rows, strict=True))))
    shown = {row.id for row in rows}
    for row in await asyncio.to_thread(printers.list):
        if row.id in shown or row.kind != "physical":  # a VP is not hardware (#4747)
            continue
        bambuddy_down = (
            unreachable is not None and row.bambuddy_printer_id is not None and row.bambuddy_active
        )
        if bambuddy_down and row.id not in native and not _fresh(row, latest, now):
            cards.append(_unreachable(row, unreachable or ""))
        elif row.lan_host is not None:
            cards.append(lan_card(row, latest.get(row.id), now=now))
    return PrintersNow(printers=cards)
