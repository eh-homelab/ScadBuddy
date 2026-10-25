"""A registry printer's tray overlay, as the native print dialog reads it (#3818 tier A,
plan 2026-10-18 D1, Task 6c): the trays from the printer's latest LAN report
(`printers.reports`), each with its physical spool and the fresh provider records of it
(`SpoolStore.overlay`). Postgres reads only: nothing here reads a provider, or starts,
signals or waits on the refresh (`workflows/spool_refresh.py`; Review Focus 8)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from scadbuddy.bambuddy.models import PrinterStatus
from scadbuddy.bambuddy.trays import seen_trays
from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.status import status_from_report
from scadbuddy.spools.component import Spools
from scadbuddy.spools.models import Overlay


def report_status(
    row: PrinterRow, report: tuple[dict[str, Any], datetime], *, now: datetime
) -> PrinterStatus:
    """``row``'s status from its stored report (`PrinterReportStore.get`)."""
    data, seen_at = report
    return status_from_report(
        row, LanReport(serial=row.serial, received_at=seen_at, print=data), now=now
    )


def located(spools: Spools, row: PrinterRow) -> dict[str, str]:
    """``row`` in each provider's own terms, for the providers' own assignments of
    untagged trays (`SpoolStore.overlay`'s ``located``); a provider that does not know
    the printer is left out."""
    return {
        provider.name: key
        for provider in spools.providers
        if (key := provider.printer_key(row)) is not None
    }


def labels(spools: Spools) -> dict[str, str]:
    """Each provider's name as a person reads it, for the overlay's notes."""
    return {provider.name: provider.label for provider in spools.providers}


async def status_overlay(
    spools: Spools, row: PrinterRow, status: PrinterStatus, *, now: datetime
) -> Overlay:
    """The overlay of the trays ``status`` shows filled. A provider not configured now
    (Bambuddy with no ``bambuddy_url``) is never stale: the dialog's note would name a
    service that is not there (#4601). Its records, while fresh, still show."""
    overlay = await spools.store.overlay(
        UUID(row.id),
        seen_trays(status),
        now=now,
        providers=[provider.name for provider in spools.providers],
        located=located(spools, row),
    )
    absent = {
        provider.name
        for provider in spools.providers
        if provider.name in overlay.stale and not await provider.configured()
    }
    if not absent:
        return overlay
    stale = {name: at for name, at in overlay.stale.items() if name not in absent}
    return overlay.model_copy(update={"stale": stale})


async def printer_overlay(
    spools: Spools,
    row: PrinterRow,
    report: tuple[dict[str, Any], datetime],
    *,
    now: datetime,
) -> Overlay:
    """:func:`status_overlay` of ``row``'s stored report."""
    return await status_overlay(spools, row, report_status(row, report, now=now), now=now)
