"""The end of a plate's use of an SB spool (#6263 Task 5).

A plate's ``print_started`` estimate (`print_uses.record_uses`) is closed by one
``print_ended``. Two readers book it, and the ledger keeps one per ``ref_id``:

- the printer report that shows the plate end (`plate_ended`, from
  `printers/reports.py` `advance_in`), at the report's time: ``data.ended`` ``report``;
- the run's settle (`print_uses.settle_uses_in`), for an end no report timed (the
  printer moved on, or went ``IDLE``), at the clock: ``data.ended`` ``settle``.

The estimate carries its job in ``data.job`` (the printer's ``subtask_name``), so a
report finds it without the run id (`filament_spool_events_job`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow

from scadbuddy.filament import ledger
from scadbuddy.filament import models as m

logger = logging.getLogger(__name__)

Conn = Connection[DictRow]

#: Log message of a capture that failed and was dropped; the write it ran in goes on.
FILAMENT_CAPTURE_FAILED: Final = "filament capture failed; the report is written"

#: Who a report-booked end names.
PRINTER_WATCH: Final = "system:printer-watch"


@dataclass(frozen=True)
class OpenUse:
    """A ``print_started`` with no ``print_ended`` yet."""

    spool_id: UUID
    ref_id: str
    delta_g: float | None
    job: str
    #: The metres the start booked (#7685, ``data.length_m``); ``None`` for none.
    length_m: float | None = None


_OPEN = (
    "SELECT e.spool_id, e.ref_id, e.delta_g, e.data ->> 'job' AS job,"
    " (e.data ->> 'length_m')::float AS length_m"
    " FROM filament_spool_events e WHERE e.kind = %(started)s AND e.ref_kind = %(kind)s"
    " AND {where} AND NOT EXISTS (SELECT 1 FROM filament_spool_events d"
    " WHERE d.spool_id = e.spool_id AND d.kind = %(ended)s"
    " AND d.ref_kind = %(kind)s AND d.ref_id = e.ref_id)"
    " ORDER BY e.spool_id, e.ref_id"
)


def _open(conn: Conn, where: str, params: dict[str, Any]) -> list[OpenUse]:
    rows = conn.execute(
        _OPEN.format(where=where),
        {
            "started": ledger.PRINT_STARTED,
            "ended": ledger.PRINT_ENDED,
            "kind": ledger.PRINT_RUN,
            **params,
        },
    ).fetchall()
    return [
        OpenUse(
            spool_id=row["spool_id"],
            ref_id=row["ref_id"],
            delta_g=row["delta_g"],
            # An estimate from before #6263 and the migration's reach has no `data.job`.
            job=row["job"] or row["ref_id"].split("/", 1)[-1].rsplit("/", 1)[0],
            length_m=row["length_m"],
        )
        for row in rows
    ]


def _run_prefix(run_id: str) -> str:
    """The ``LIKE`` pattern of the run's ``ref_id``s (``run/subtask/slot``), the run id
    escaped."""
    escaped = run_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}/%"


def open_for_run(conn: Conn, run_id: str) -> list[OpenUse]:
    """The run's uses still open, by `filament_spool_events_print_ref`: a prefix match
    on ``ref_id`` (``run/subtask/slot``), the run id escaped."""
    return _open(conn, "e.ref_id LIKE %(prefix)s", {"prefix": _run_prefix(run_id)})


@dataclass(frozen=True)
class RunUse:
    """One slot's use of a LAN start of a run, as the ledger booked it (`run_uses`)."""

    #: The start's ``subtask_name``.
    subtask: str
    #: The grams its ``print_ended`` took off the spool; ``None`` while the use is open.
    grams: float | None
    #: Whether the end booked the sliced grams (``data.basis`` ``sliced``). False for an
    #: end that dropped the estimate (``not_applied``), and while the use is open.
    applied: bool
    #: When the end was booked; ``None`` while the use is open.
    ended_at: datetime | None
    #: Whether a printer report booked the end, at the report that showed the plate end
    #: (``data.ended`` ``report``), not the run's settle at the clock.
    timed: bool


def run_uses(conn: Conn, run_id: str) -> list[RunUse]:
    """Each use the LAN starts of ``run_id`` booked (one per start and slot, #8282), and
    its ``print_ended`` when it has one: what an order attempt reads as ScadBuddy's own
    record of the run's grams and plate ends (`orders/archives.py` `lan_facts`). Found by
    `filament_spool_events_print_ref`, as `open_for_run` finds them."""
    rows = conn.execute(
        "SELECT s.ref_id, d.delta_g, d.at, d.data ->> 'basis' AS basis,"
        " d.data ->> 'ended' AS ended"
        " FROM filament_spool_events s LEFT JOIN LATERAL (SELECT e.delta_g, e.at, e.data"
        "  FROM filament_spool_events e WHERE e.spool_id = s.spool_id AND e.kind = %(ended)s"
        "  AND e.ref_kind = s.ref_kind AND e.ref_id = s.ref_id ORDER BY e.at LIMIT 1) d ON true"
        " WHERE s.kind = %(started)s AND s.ref_kind = %(kind)s AND s.ref_id LIKE %(prefix)s"
        " ORDER BY s.ref_id",
        {
            "started": ledger.PRINT_STARTED,
            "ended": ledger.PRINT_ENDED,
            "kind": ledger.PRINT_RUN,
            "prefix": _run_prefix(run_id),
        },
    ).fetchall()
    skip = len(run_id) + 1
    return [
        RunUse(
            subtask=row["ref_id"][skip:].rsplit("/", 1)[0],
            grams=None if row["at"] is None else -(row["delta_g"] or 0.0),
            applied=row["basis"] == "sliced",
            ended_at=row["at"],
            timed=row["ended"] == "report",
        )
        for row in rows
    ]


def open_for_job(conn: Conn, printer_id: str, job: str) -> list[OpenUse]:
    """The uses of one job still open, by `filament_spool_events_job`, on one printer: a
    use whose run (``print_runs.printer``) names another printer is not matched, since
    two printers can run a job of the same name. A run with no printer, or no run row,
    is matched."""
    return _open(
        conn,
        "(e.data ->> 'job') = %(job)s AND NOT EXISTS (SELECT 1 FROM print_runs r"
        " WHERE r.id = split_part(e.ref_id, '/', 1)"
        " AND r.printer IS NOT NULL AND r.printer <> %(printer)s::uuid)",
        {"job": job, "printer": printer_id},
    )


def book_end(
    conn: Conn,
    use: OpenUse,
    *,
    applied: bool,
    at: datetime | None,
    actor: str,
    ended: str,
) -> m.SpoolEvent | None:
    """The ``print_ended`` of ``use``: its sliced grams as the actual when ``applied``,
    else 0, which drops the estimate, and its metres the same way as ``data.length_m``
    when the start booked them (#7685). ``None`` when the use is ended already. ``at`` is
    when the plate ended; with none the ledger books it now. ``ended`` is ``report`` or
    ``settle``."""
    data: dict[str, Any] = {
        "basis": "sliced" if applied else "not_applied",
        "job": use.job,
        "ended": ended,
    }
    if use.length_m is not None:
        data["length_m"] = use.length_m if applied else 0.0
    return ledger.record(
        conn,
        use.spool_id,
        ledger.PRINT_ENDED,
        source=ledger.PRINT_ACTUAL,
        delta_g=use.delta_g if applied else 0.0,
        ref_kind=ledger.PRINT_RUN,
        ref_id=use.ref_id,
        actor=actor,
        at=at,
        data=data,
    )


def plate_ended(conn: Conn, printer_id: str, subtask_name: str, state: str, at: datetime) -> None:
    """Book the end of every open use of ``subtask_name`` on ``printer_id``, at the report
    that shows it: its grams when the printer says ``FINISH``, else none."""
    for use in open_for_job(conn, printer_id, subtask_name):
        book_end(conn, use, applied=state == "FINISH", at=at, actor=PRINTER_WATCH, ended="report")
