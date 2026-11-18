"""ScadBuddy's own print-hour counter for a LAN-linked printer (#5774, spec
2026-11-17-machine-rate-actuals §3.6): the seconds the printer's reports say
``gcode_state`` `COUNTED_STATE`, kept in ``printer_run_hours``, one row per printer.

The CRUD service moves it inside the two writes the LAN watch already sends
(`reports.py` `PrinterReportStore.advance_in` and `touch`), in their transaction, after
the report row's lock, so the counter needs no new route, grant or database client
(#5295). Each step is `accrue`, a pure function of the row, the write's ``seen_at`` and
the report (none for a touch).

A gap over `GAP_LIMIT` means the watch or its link was down. It counts only when the
report after it shows the same print still running (the printer printed on); otherwise
ScadBuddy cannot know when the state changed, so the gap goes to ``unseen_seconds`` and
the counter is low, never high. A touch after such a gap changes nothing, so the next
report decides it. A watch clock that ran ahead and then stepped back loses at most the
stretch it repeats: the first report back within `FUTURE_TOLERANCE` of the database's
clock moves ``counted_to`` back and counts nothing, and counting goes on from there. Any
other write before ``counted_to`` (a late write, on a clock that is ahead or not) changes
nothing, so no stretch is counted twice (`accrue`).

An unlink keeps the row but clears its ``state`` (`store.py` `PrinterStore.unlink_lan`),
so no gap spans it, and a merge keeps the kept printer's row (`PRINTER_REFERENCES`)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow

from scadbuddy.printers.status import FUTURE_TOLERANCE, STALE_AFTER

#: The one ``gcode_state`` counted, as Bambuddy's own counter counts (spec §2.2), not
#: `status.RUNNING`, which also holds ``PREPARE``, ``SLICING`` and ``PAUSE``.
COUNTED_STATE = "RUNNING"

#: The longest gap between two writes that counts on the row's state alone. The watch
#: writes at least every 10 s while its link is up (`watch.HEARD_EVERY`).
GAP_LIMIT = STALE_AFTER

_TENTH = Decimal("0.1")
_MICROS = Decimal(1_000_000)
_MICROSECOND = timedelta(microseconds=1)


@dataclass(frozen=True)
class RunHours:
    """One printer's ``printer_run_hours`` row, as `accrue` reads and writes it."""

    run_seconds: Decimal
    unseen_seconds: Decimal
    counted_to: datetime
    state: str | None
    subtask: str | None


def _same_print_running(row: RunHours, report: dict[str, Any]) -> bool:
    named = report.get("subtask_name")
    return report.get("gcode_state") == COUNTED_STATE and bool(named) and named == row.subtask


def accrue(row: RunHours, t: datetime, report: dict[str, Any] | None, *, now: datetime) -> RunHours:
    """``row`` after a write seen at ``t``: a report (``report``) or a touch (None);
        ``now`` is the database's clock. A write at ``counted_to`` (a resend) or before it
        counts nothing. Only a report from a clock that was corrected moves ``counted_to``
        back, to ``t``, so the next report counts its own gap: ``counted_to`` is past ``now``
        by more than `FUTURE_TOLERANCE` and ``t`` is not. Any other write before it changes
    nothing, its state included (the row already holds a newer one).
        A late write (a print worker's request still running on the API after the worker
        gave its answer up) gets past `reports.stale` while the stored ``seen_at`` is ahead,
        and moving back on it would count the stretch to ``counted_to`` twice. A touch is
        stamped `watch.LINK_PROOF` (20 s) before it is sent, so after a report it is the usual
        case."""
    after = row
    gap = t - row.counted_to
    ahead = now + FUTURE_TOLERANCE
    if gap < timedelta(0):
        if report is None or not row.counted_to > ahead >= t:
            return row
        after = replace(after, counted_to=t)
    elif gap > timedelta(0):
        seconds = Decimal(gap // _MICROSECOND) / _MICROS
        if row.state == COUNTED_STATE:
            if gap <= GAP_LIMIT or (report is not None and _same_print_running(row, report)):
                after = replace(after, run_seconds=row.run_seconds + seconds)
            elif report is None:
                return row  # a touch after a long silence: the next report decides
            else:
                after = replace(after, unseen_seconds=row.unseen_seconds + seconds)
        after = replace(after, counted_to=t)
    if report is not None:
        state, subtask = report.get("gcode_state"), report.get("subtask_name")
        if state is not None:
            after = replace(after, state=state)
        if subtask is not None:
            after = replace(after, subtask=subtask)
    return after


def hours(run_seconds: Decimal) -> Decimal:
    """The reading: ``run_seconds`` in hours, rounded half up to a tenth."""
    return (run_seconds / 3600).quantize(_TENTH, rounding=ROUND_HALF_UP)


_COLUMNS = "run_seconds, unseen_seconds, counted_to, state, subtask"


def _row(data: DictRow) -> RunHours:
    return RunHours(
        run_seconds=data["run_seconds"],
        unseen_seconds=data["unseen_seconds"],
        counted_to=data["counted_to"],
        state=data["state"],
        subtask=data["subtask"],
    )


def read(conn: Connection[DictRow], printer_id: str) -> RunHours | None:
    """The printer's counter, a plain read; None when it was never LAN-linked."""
    data = conn.execute(
        f"SELECT {_COLUMNS} FROM printer_run_hours WHERE printer = %s", (printer_id,)
    ).fetchone()
    return None if data is None else _row(data)


def advance_in(
    conn: Connection[DictRow], printer_id: str, t: datetime, report: dict[str, Any] | None
) -> None:
    """Move the printer's counter by one write, in the caller's transaction, after it
    locked the printer's report row. A report makes the row on the printer's first
    (``counted_to`` its ``seen_at``, nothing counted); a touch makes none."""
    if report is not None:
        conn.execute(
            "INSERT INTO printer_run_hours (printer, counted_to) VALUES (%s, %s)"
            " ON CONFLICT (printer) DO NOTHING",
            (printer_id, t),
        )
    data = conn.execute(
        f"SELECT {_COLUMNS}, now() AS now FROM printer_run_hours WHERE printer = %s FOR UPDATE",
        (printer_id,),
    ).fetchone()
    if data is None:
        return
    row = _row(data)
    after = accrue(row, t, report, now=data["now"])
    if after == row:
        return
    conn.execute(
        "UPDATE printer_run_hours SET run_seconds = %s, unseen_seconds = %s,"
        " counted_to = %s, state = %s, subtask = %s WHERE printer = %s",
        (
            after.run_seconds,
            after.unseen_seconds,
            after.counted_to,
            after.state,
            after.subtask,
            printer_id,
        ),
    )
