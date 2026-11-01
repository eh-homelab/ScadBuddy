"""The spool refresh's ``seen`` half (#4191): every LAN printer's trays as its latest
report in the printer registry's report store shows them (`printers/reports.py`,
``printer_reports``), a `TraySource` (`spools/providers.py`).

A report is read as the print dialog reads it (`printers/status.py`
`status_from_report`, then `bambuddy/trays.py` `seen_trays`): the AMS units'
``ams.ams[].tray[]`` and the external holders (``vt_tray``, or ``vir_slot`` on an
H2-series printer, which wins there), filled trays only, each with its cleaned RFID keys,
material, colour and ``tray_info_idx``; its fingerprint is `spools.models.tray_fingerprint`,
never the tray's ``state``. A report older than `printers.status.STALE_AFTER` is left
out: `SpoolStore.seen` stamps ``last_seen_at`` with now, and a printer not heard from
for that long shows nothing now.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from scadbuddy.bambuddy.models import PrinterStatus
from scadbuddy.bambuddy.trays import seen_trays
from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.status import is_fresh, status_from_report
from scadbuddy.printers.store import PrinterStore
from scadbuddy.spools.models import SeenTray


def report_status(
    row: PrinterRow, report: tuple[dict[str, Any], datetime], *, now: datetime
) -> PrinterStatus:
    """``row``'s status from its stored report (`PrinterReportStore.get`)."""
    data, seen_at = report
    return status_from_report(
        row, LanReport(serial=row.serial, received_at=seen_at, print=data), now=now
    )


class Registry(Protocol):
    """What :class:`ReportTrays` reads of the printer registry (`printers.component.Printers`,
    whose pool opens on first use, so building the source reads nothing)."""

    @property
    def store(self) -> PrinterStore: ...

    @property
    def reports(self) -> PrinterReportStore: ...


def _now() -> datetime:
    return datetime.now(UTC)


class ReportTrays:
    def __init__(self, registry: Registry, *, now: Callable[[], datetime] = _now) -> None:
        self.registry = registry
        self._now = now

    async def latest(self) -> list[SeenTray]:
        return await asyncio.to_thread(self._latest)

    def _latest(self) -> list[SeenTray]:
        now = self._now()
        rows = {row.id: row for row in self.registry.store.list()}
        found: list[SeenTray] = []
        for printer_id, report in self.registry.reports.all().items():
            row = rows.get(printer_id)
            if row is None or not is_fresh(report[1], now):
                continue
            found += seen_trays(report_status(row, report, now=now))
        return found
