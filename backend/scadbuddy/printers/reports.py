"""`printer_reports`: each LAN printer's latest merged MQTT report (#3823, plan
2026-10-16, Task 6), written by its `PrinterWatch` and read by ``/printers/now``, the
print dialog and the native follow. ``seen_at`` is when the printer was last heard
from: a report, or the connection still being up (`touch`).

A write that shows the print the row named before has ended returns that end
(`ended_print`), so the watch settles the print's rack picks as the printer ends it
(#5138, #5142), whether or not a follow still reads its run."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.printers.store import printer_uuid

#: The states that show the printer running a plate (OpenBambuAPI ``mqtt.md``).
RUNNING = frozenset({"PREPARE", "SLICING", "RUNNING", "PAUSE"})


@dataclass(frozen=True)
class PrintEnd:
    """A print the printer was running and no longer is: its ``subtask_name``, and
    whether the printer said how it ended (``FINISH`` or ``FAILED``), as the follow's
    settle is timed only then (`rack.usage.lan_settle_hook`)."""

    subtask_name: str
    timed: bool


def ended_print(old: dict[str, Any] | None, new: dict[str, Any]) -> PrintEnd | None:
    """The print ``old`` showed running that ``new`` shows ended: its own ``FINISH``,
    ``FAILED`` or ``IDLE``, or another print's name (the printer moved on). ``None``
    when ``old`` showed none running, or ``new`` still runs it or says too little.

    Only an end seen after a run counts: a report naming a new ``subtask_name`` while it
    still says the last job's ``FINISH`` (#4935) is a start, and the row before it showed
    nothing running."""
    if old is None:
        return None
    name = old.get("subtask_name")
    if not name or old.get("gcode_state") not in RUNNING:
        return None
    named, state = new.get("subtask_name"), new.get("gcode_state")
    if named is None or state is None:
        return None
    if named != name:
        return PrintEnd(subtask_name=name, timed=False)
    if state in RUNNING:
        return None
    return PrintEnd(subtask_name=name, timed=state in ("FINISH", "FAILED"))


class PrinterReportStore:
    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self.pool = pool

    def put(self, printer_id: str, report: dict[str, Any], seen_at: datetime) -> PrintEnd | None:
        """Write the report; the print it shows ended since the row's last one
        (`ended_print`), if any. A printer gone from the registry (deleted while it was
        watched) or no longer LAN-linked (an attempt still running after the unlink)
        writes nothing and ends nothing."""
        key = printer_uuid(printer_id)
        if key is None:
            return None
        with self.pool.connection() as conn, conn.transaction():
            old = conn.execute(
                "SELECT report FROM printer_reports WHERE printer_id = %s FOR UPDATE",
                (printer_id,),
            ).fetchone()
            written = conn.execute(
                "INSERT INTO printer_reports (printer_id, report, seen_at)"
                " SELECT id, %s, %s FROM printers WHERE id = %s AND lan_host IS NOT NULL"
                " ON CONFLICT (printer_id) DO UPDATE"
                " SET report = EXCLUDED.report, seen_at = EXCLUDED.seen_at",
                (Jsonb(report), seen_at, printer_id),
            ).rowcount
        if not written:
            return None
        return ended_print(None if old is None else dict(old["report"]), report)

    def touch(self, printer_id: str, seen_at: datetime) -> None:
        """Heard from as late as ``seen_at``, with nothing new to say; never earlier than
        what the row already says."""
        key = printer_uuid(printer_id)
        if key is None:
            return
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE printer_reports SET seen_at = GREATEST(seen_at, %s) WHERE printer_id = %s",
                (seen_at, printer_id),
            )

    def get(self, printer_id: str) -> tuple[dict[str, Any], datetime] | None:
        key = printer_uuid(printer_id)
        if key is None:
            return None
        with self.pool.connection() as conn:
            data = conn.execute(
                "SELECT report, seen_at FROM printer_reports WHERE printer_id = %s",
                (printer_id,),
            ).fetchone()
        return None if data is None else (dict(data["report"]), data["seen_at"])

    def all(self) -> dict[str, tuple[dict[str, Any], datetime]]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT printer_id, report, seen_at FROM printer_reports"
            ).fetchall()
        return {str(row["printer_id"]): (dict(row["report"]), row["seen_at"]) for row in rows}
