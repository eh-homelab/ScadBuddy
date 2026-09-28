"""What the printer physically has, read off Bambuddy (spec 2026-09-27 §3, §4.4).

Pure helpers shared by the choices route and the run: the rack's nozzles, the plate the
last print used, and the warnings that compare a choice against either. Kept apart from
``choices.py`` because that module imports ``print_run`` and the run in ``print_run``
needs these, which would otherwise be an import cycle.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from pydantic import BaseModel

from scadbuddy.bambuddy.filaments import FilamentWarning
from scadbuddy.bambuddy.models import Archive, PrinterStatus
from scadbuddy.bambuddy.resolver import FlowType

_RAN = {"completed", "cancelled", "failed"}
_OLDEST = datetime.min.replace(tzinfo=UTC)


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


def last_bed_type(archives: list[Archive], *, printer_id: int) -> str | None:
    """The plate of ``printer_id``'s newest print that actually ran. The archive read is
    already filtered by printer; checking the id again keeps a row from another printer
    from ever naming this one's plate."""
    ran = [row for row in archives if row.printer_id == printer_id and row.status in _RAN]

    def when(row: Archive) -> datetime:
        # Aware throughout, or ``max`` raises on a naive/aware pair (PR #335 review 3):
        # a row with no timestamp at all is the oldest, and a naive one is read as UTC,
        # which is what the recorded archives' offset-less times are.
        stamp = row.started_at or row.completed_at or row.created_at
        if stamp is None:
            return _OLDEST
        return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)

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
