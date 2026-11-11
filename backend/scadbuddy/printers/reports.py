"""`printer_reports`: each LAN printer's latest merged MQTT report (#3823, plan
2026-10-16, Task 6), written by its `PrinterWatch` and read by ``/printers/now``, the
print dialog and the native follow. ``seen_at`` is when the printer was last heard
from: a report, or the connection still being up (`touch`).

The row also keeps the print its reports last showed running (``running_subtask``). A
write that shows that print ended returns the end (`advance_print`), so the watch
settles the print's rack picks as the printer ends it (#5138, #5142), whether or not a
follow still reads its run.

The end is timed to the report that shows it. A watch that was down, or a printer off
the network, past the print's end charges the print up to that report: the end lies
somewhere after the row's last ``seen_at``, and an upper bound counts the wear a lower
one would miss.

A write that shows a print running that was not running before it returns the start
(`started_print`): the print watcher's LAN nudge (`print_watch/nudge.py`, #4699 phase 3).
The row's ``running_subtask`` changes once per job, under the row's lock, so one job
gives one start, also across a reconnect's ``pushall`` and a new attempt of the watch."""

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


def advance_print(running: str | None, new: dict[str, Any]) -> tuple[str | None, PrintEnd | None]:
    """The print running after the report ``new``, given the one ``running`` before it,
    and the print ``new`` shows ended: ``running``'s own ``FINISH``, ``FAILED`` or
    ``IDLE``, or another print's name (the printer moved on).

    A report that says too little (no ``subtask_name`` or ``gcode_state``: a delta
    before a reconnect's ``pushall`` answer) keeps ``running`` and ends nothing, so the
    next whole report still ends it. Only an end seen after a run counts: a report
    naming a new ``subtask_name`` while it still says the last job's ``FINISH`` (#4935)
    is a start, and nothing was running before it.

    Any other name ends ``running``. That relies on ScadBuddy's subtask names being
    unique per run (they carry a run-id prefix, the plate and, past the first, the
    copy), so a different name is always a different job, never the same print
    renamed."""
    named, state = new.get("subtask_name"), new.get("gcode_state")
    if not named or state is None:
        return running, None
    now_running = named if state in RUNNING else None
    if running is None or (named == running and state in RUNNING):
        return now_running, None
    if named != running:
        return now_running, PrintEnd(subtask_name=running, timed=False)
    return None, PrintEnd(subtask_name=running, timed=state in ("FINISH", "FAILED"))


def started_print(before: str | None, after: str | None) -> str | None:
    """The print a write started: the one running ``after`` it, when that is not the one
    running ``before`` it."""
    return after if after is not None and after != before else None


@dataclass(frozen=True)
class JobStart:
    """A job the watch saw start (`started_print`): its ``subtask_name``, when the report
    that showed it start was received, and the plate G-code the report names
    (``gcode_file``), for the print watcher's nudge (#4699)."""

    subtask_name: str
    started_at: datetime
    gcode_file: str | None = None


@dataclass(frozen=True)
class Advance:
    """What one write showed: the print it ended (`advance_print`) and the print it
    started (`started_print`), each when there is one."""

    ended: PrintEnd | None = None
    started: str | None = None


class PrinterReportStore:
    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self.pool = pool

    def put(self, printer_id: str, report: dict[str, Any], seen_at: datetime) -> PrintEnd | None:
        """Write the report; the print it shows ended (`advance_print`), if any."""
        return self.advance(printer_id, report, seen_at).ended

    def advance(self, printer_id: str, report: dict[str, Any], seen_at: datetime) -> Advance:
        """Write the report; the print it ended and the print it started. A printer gone
        from the registry (deleted while it was watched) or no longer LAN-linked (an
        attempt still running after the unlink) writes nothing, and ends and starts
        nothing."""
        key = printer_uuid(printer_id)
        if key is None:
            return Advance()
        with self.pool.connection() as conn, conn.transaction():
            old = conn.execute(
                "SELECT running_subtask FROM printer_reports WHERE printer_id = %s FOR UPDATE",
                (printer_id,),
            ).fetchone()
            before = None if old is None else old["running_subtask"]
            running, ended = advance_print(before, report)
            written = conn.execute(
                "INSERT INTO printer_reports (printer_id, report, seen_at, running_subtask)"
                " SELECT id, %s, %s, %s FROM printers WHERE id = %s AND lan_host IS NOT NULL"
                " ON CONFLICT (printer_id) DO UPDATE SET report = EXCLUDED.report,"
                " seen_at = EXCLUDED.seen_at, running_subtask = EXCLUDED.running_subtask",
                (Jsonb(report), seen_at, running, printer_id),
            ).rowcount
        if not written:
            return Advance()
        return Advance(ended=ended, started=started_print(before, running))

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
