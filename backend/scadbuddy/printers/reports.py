"""`printer_reports`: each LAN printer's latest merged MQTT report (#3823, plan
2026-10-16, Task 6), written by its `PrinterWatch` and read by ``/printers/now``, the
print dialog and the native follow. ``seen_at`` is when the printer was last heard
from: a report, or the connection still being up (`touch`).

The row also keeps the print its reports last showed running (``running_subtask``). A
write that shows that print ended returns the end (`advance_print`), so the watch
settles the print's rack picks as the printer ends it (#5138, #5142), whether or not a
follow still reads its run.

The end is also booked on the plate's SB spools, at that report's time
(`filament/prints.py` `plate_ended`, #6263): in a savepoint, so a failure never fails the
write. An end no report timed (the printer moved on, or went ``IDLE``) is left to the
run's settle.

The write that shows a timed plate end or a tray change books the ledger events in its
own savepoint (#6263 Tasks 5 and 6): the plate end first, then the trays
(`filament/capture.py` `reconcile`: loaded, unloaded and AMS readings), after one lock of
every spool both will touch. A failure is logged and dropped.

The end is timed to the report that shows it. A watch that was down, or a printer off
the network, past the print's end charges the print up to that report: the end lies
somewhere after the row's last ``seen_at``, and an upper bound counts the wear a lower
one would miss.

A write that shows a print running that was not running before it returns the start
(`started_print`): the print watcher's LAN nudge (`print_watch/nudge.py`, #4699 phase 3).
The row's ``running_subtask`` changes once per job, under the row's lock, so one job
gives one start, also across a reconnect's ``pushall`` and a new attempt of the watch.

A written report and a touch also move the printer's hour counter (`run_hours.py`,
#5774) in their transaction, the report row locked first, then the counter row.

A stored report reads as the printer's status through `report_status`, as the print
dialog and the SB tray resolution (`filament/inventory.py`) read it."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.bambuddy.models import PrinterStatus
from scadbuddy.filament import prints
from scadbuddy.printers import run_hours
from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.status import RUNNING as RUNNING  # re-exported (`progress`)
from scadbuddy.printers.status import status_from_report
from scadbuddy.printers.store import printer_uuid

logger = logging.getLogger(__name__)


def report_status(
    row: PrinterRow, report: tuple[dict[str, Any], datetime], *, now: datetime
) -> PrinterStatus:
    """``row``'s status from its stored report (`PrinterReportStore.get`)."""
    data, seen_at = report
    return status_from_report(
        row, LanReport(serial=row.serial, received_at=seen_at, print=data), now=now
    )


@dataclass(frozen=True)
class PrintEnd:
    """A print the printer was running and no longer is: its ``subtask_name``, and
    whether the printer said how it ended (``FINISH`` or ``FAILED``), as the follow's
    settle is timed only then (`rack.usage.lan_settle_hook`)."""

    subtask_name: str
    timed: bool


#: How far ahead of the database's clock a stored ``seen_at`` may be and still guard the
#: row (`stale`). Report times come from the clock of the host that runs the watch, which
#: may move between hosts; a row stamped by a host whose clock ran ahead must not freeze
#: every later report until real time catches up.
FUTURE_TOLERANCE = timedelta(seconds=30)


def stale(stored: datetime | None, seen_at: datetime, now: datetime) -> bool:
    """Whether a report seen at ``seen_at`` is older than the row's ``stored``: then it
    is not written (`PrinterReportStore.advance_in`). A ``stored`` time more than
    `FUTURE_TOLERANCE` past ``now`` (the database's clock) guards nothing: it came from a
    clock that ran ahead, and the next report repairs the row, as before the guard."""
    return stored is not None and seen_at < stored and stored <= now + FUTURE_TOLERANCE


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


def _capture(
    conn: Connection[DictRow],
    printer_id: str,
    ended: PrintEnd | None,
    old_report: dict[str, Any] | None,
    report: dict[str, Any],
    seen_at: datetime,
) -> None:
    """Book what the write shows in the ledger (`filament/prints.py` `plate_ended`,
    `filament/capture.py`): the timed end of a plate, then a change of trays (or a first
    report). The spools both will book on are locked first, in id order. Each step runs in
    a savepoint of its own: a failure is logged by its type and dropped, so capture never
    fails the report's write. An end no report timed is left to the run's settle."""
    from scadbuddy.filament import capture  # imports the inventory, which imports this module

    end = ended if ended is not None and ended.timed else None
    trays = old_report is None or capture.trays_changed(old_report, report)
    if end is None and not trays:
        return
    steps: list[tuple[str, Callable[[], object]]] = [
        (
            "lock",
            lambda: capture.lock(
                conn,
                capture.touched(
                    conn, printer_id, report, None if end is None else end.subtask_name, seen_at
                ),
            ),
        )
    ]
    if end is not None:
        steps.append(
            (
                "plate_end",
                lambda: prints.plate_ended(
                    conn, printer_id, end.subtask_name, report["gcode_state"], seen_at
                ),
            )
        )
    if trays:
        steps.append(
            (
                "trays",
                lambda: capture.reconcile(
                    conn, printer_id, report, seen_at, actor=capture.WATCH_ACTOR
                ),
            )
        )
    for what, step in steps:
        try:
            with conn.transaction():
                step()
        except Exception as error:
            logger.warning(
                prints.FILAMENT_CAPTURE_FAILED,
                extra={"printer_id": printer_id, "what": what, "error": type(error).__name__},
            )
            if what == "lock":
                return


def _capture_sensors(
    conn: Connection[DictRow], printer_id: str, report: dict[str, Any], seen_at: datetime
) -> None:
    """Record the AMS units' humidity and temperature (`filament/sensors.py`
    `capture_ams_on`) on every write, in a savepoint of its own, after the tray capture.
    A failure is logged by its type and dropped, as `_capture`'s steps are."""
    from scadbuddy.filament import sensors

    try:
        with conn.transaction():
            sensors.capture_ams_on(conn, printer_id, report, seen_at)
    except Exception as error:
        logger.warning(
            prints.FILAMENT_CAPTURE_FAILED,
            extra={"printer_id": printer_id, "what": "sensors", "error": type(error).__name__},
        )


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
        self._pool = pool

    @property
    def pool(self) -> ConnectionPool[Connection[DictRow]]:
        """The pool, for a CRUD route that writes in one transaction with its ledger row
        (`api/crud_print.py`)."""
        return self._pool

    def put(
        self,
        printer_id: str,
        report: dict[str, Any],
        seen_at: datetime,
        *,
        idempotency_key: str | None = None,
    ) -> PrintEnd | None:
        """Write the report; the print it shows ended (`advance_print`), if any."""
        return self.advance(printer_id, report, seen_at, idempotency_key=idempotency_key).ended

    def advance(
        self,
        printer_id: str,
        report: dict[str, Any],
        seen_at: datetime,
        *,
        idempotency_key: str | None = None,
    ) -> Advance:
        """Write the report; the print it ended and the print it started. A printer gone
        from the registry (deleted while it was watched) or no longer LAN-linked (an
        attempt still running after the unlink) writes nothing, and ends and starts
        nothing. ``idempotency_key`` is the print worker's (`crud/print.py`
        `RemoteReports.advance`), which may lose an answer; here an answer is never lost,
        so it is unused."""
        if printer_uuid(printer_id) is None:
            return Advance()
        with self.pool.connection() as conn, conn.transaction():
            return self.advance_in(conn, printer_id, report, seen_at)

    @staticmethod
    def advance_in(
        conn: Connection[DictRow], printer_id: str, report: dict[str, Any], seen_at: datetime
    ) -> Advance:
        """`advance` on ``conn``, in the caller's transaction. A report older than the row's
        ``seen_at`` writes nothing, touches nothing, and ends and starts nothing (`stale`):
        a write that lands after a newer one (a print worker's request still running on
        the API after the worker gave its answer up) never moves the row, or the print it
        shows running, back. One as old as the row's writes as before, so a resend is the
        same write. A skipped write answers no end and no start, as a written one that
        ended and started nothing does, so the watch counts it written and announces
        `printer.report` once more: the card refetches the row unchanged, which costs one
        read."""
        if printer_uuid(printer_id) is None:
            return Advance()
        old = conn.execute(
            "SELECT running_subtask, report, seen_at, now() AS now FROM printer_reports"
            " WHERE printer_id = %s FOR UPDATE",
            (printer_id,),
        ).fetchone()
        if old is not None and stale(old["seen_at"], seen_at, old["now"]):
            return Advance()
        before = None if old is None else old["running_subtask"]
        running, ended = advance_print(before, report)
        if "ipcam" not in report and old is not None and "ipcam" in (old["report"] or {}):
            # A delta before a reconnect's ``pushall`` answer has no ``ipcam``: keep the
            # camera the row shows, or the camera relay would read it as gone (#7328).
            report = {**report, "ipcam": old["report"]["ipcam"]}
        written = conn.execute(
            "INSERT INTO printer_reports (printer_id, report, seen_at, running_subtask)"
            " SELECT id, %s, %s, %s FROM printers WHERE id = %s AND lan_host IS NOT NULL"
            " ON CONFLICT (printer_id) DO UPDATE SET report = EXCLUDED.report,"
            " seen_at = EXCLUDED.seen_at, running_subtask = EXCLUDED.running_subtask",
            (Jsonb(report), seen_at, running, printer_id),
        ).rowcount
        if not written:
            return Advance()
        run_hours.advance_in(conn, printer_id, seen_at, report)
        _capture(conn, printer_id, ended, None if old is None else old["report"], report, seen_at)
        _capture_sensors(conn, printer_id, report, seen_at)
        return Advance(ended=ended, started=started_print(before, running))

    def touch(self, printer_id: str, seen_at: datetime) -> None:
        """Heard from as late as ``seen_at``, with nothing new to say; never earlier than
        what the row already says. The hour counter moves in the same transaction
        (`run_hours.advance_in`), locked after the report row, as in `advance_in`."""
        key = printer_uuid(printer_id)
        if key is None:
            return
        with self.pool.connection() as conn, conn.transaction():
            touched = conn.execute(
                "UPDATE printer_reports SET seen_at = GREATEST(seen_at, %s) WHERE printer_id = %s",
                (seen_at, printer_id),
            ).rowcount
            if touched:
                run_hours.advance_in(conn, printer_id, seen_at, None)

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

    def watch_get(self, printer_id: str) -> tuple[dict[str, Any], datetime] | None:
        """`get`, for the printer watch: the print worker's store bounds it as the watch's
        writes (`crud/print.py` `RemoteReports`)."""
        return self.get(printer_id)

    def all(self) -> dict[str, tuple[dict[str, Any], datetime]]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT printer_id, report, seen_at FROM printer_reports"
            ).fetchall()
        return {str(row["printer_id"]): (dict(row["report"]), row["seen_at"]) for row in rows}
