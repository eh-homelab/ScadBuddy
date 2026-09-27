"""What the printer physically has, read off Bambuddy (spec 2026-09-27 §3, §4.4).

Pure helpers shared by the choices route and the run: the rack's nozzles, the plate the
last print used, and the warnings that compare a choice against either. Kept apart from
``choices.py`` because that module imports ``pipelines`` and the run in ``pipelines``
needs these, which would otherwise be an import cycle.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from pydantic import BaseModel

from scadbuddy.bambuddy.filaments import FilamentWarning
from scadbuddy.bambuddy.models import Archive, PrinterStatus
from scadbuddy.bambuddy.resolver import FlowType

_RAN = {"completed", "cancelled", "failed"}


class InstalledNozzle(BaseModel):
    size: str
    flow: FlowType
    count: int


def installed_nozzles(status: PrinterStatus | None) -> list[InstalledNozzle]:
    """Rack slots by size and flow. The second letter of ``nozzle_type`` is the flow
    (``HH01`` high flow, ``HS01`` standard) — inferred from the codes present (spec §3)."""
    if status is None:
        return []
    counts: Counter[tuple[str, FlowType]] = Counter()
    for slot in status.nozzle_rack:
        if not slot.nozzle_diameter or len(slot.nozzle_type) < 2:
            continue
        flow: FlowType = "high_flow" if slot.nozzle_type[1] == "H" else "standard"
        counts[(slot.nozzle_diameter, flow)] += 1
    return [
        InstalledNozzle(size=size, flow=flow, count=count)
        for (size, flow), count in sorted(counts.items())
    ]


def last_bed_type(archives: list[Archive]) -> str | None:
    ran = [row for row in archives if row.printer_id is not None and row.status in _RAN]

    def when(row: Archive) -> datetime:
        return row.started_at or row.completed_at or row.created_at or datetime.min

    newest = max(ran, key=when, default=None)
    return newest.bed_type if newest else None


def plate_warning(
    chosen: str, last: str | None, printer_name: str | None
) -> FilamentWarning | None:
    if last is None or chosen == last:
        return None
    return FilamentWarning(
        kind="plate-differs",
        message=(
            f"The {printer_name or 'printer'}'s last print used {last}. "
            f"Swap to {chosen} before this starts."
        ),
    )


def nozzle_warning(size: str, installed: list[InstalledNozzle]) -> FilamentWarning | None:
    if not installed or any(nozzle.size == size for nozzle in installed):
        return None
    return FilamentWarning(
        kind="not-installed",
        message=f"No {size} mm nozzle is installed. Install one before this prints.",
    )
