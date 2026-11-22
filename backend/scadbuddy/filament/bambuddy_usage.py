"""Bambuddy's usage rows booked as print ends (#6263, spec §8.2). Read only: nothing here
writes to Bambuddy.

BB keeps one usage row per spool per print (``GET /inventory/usage``,
`scadbuddy.bambuddy.models.SpoolUsage`), SB's own LAN prints included, and it writes the
row and its spool's ``updated_at`` at one instant. Each row on a linked spool is one
``print_ended`` event with source ``print_actual``, ref `ledger.BB_USAGE` and the row's
id, at the row's ``created_at``: booked once, whatever the pull that sees it. Its
``data.job`` is the row's ``print_name``, which for SB's own plate is SB's
``subtask_name``, so the ledger pairs it with SB's events of that plate and counts the
plate once (`ledger.py`, rule 3). A row with no name is booked with ``job`` null: SB's
plates always carry a subtask name, so it is never an SB plate. A row that is not later
than the spool's first `ledger.BB_SPOOL` event lies inside the reading the link made,
and is not booked. Nor is a row of an archived SB spool: the pull never writes to one.
The one exception is a spool the pull made archived because Bambuddy had archived it
(#6707, its ``archived`` event carries ``data.bb_archived_at``): every row of it is booked,
the ones before its reading too, so its history and consumption are whole. Its reading is
BB's own value after those rows, so the anchor rule keeps what is left as BB says.

When Bambuddy books a plate on another SB spool than SB's estimate names, `_cover` books
a 0 g ``print_ended`` on the estimate's spool, with ``data.covered_by`` the row's event,
so the estimate drops (details and limits in its docstring).

The archive of the row's print is the one of the same BB printer that ended in
``[created_at - ARCHIVE_BEFORE, created_at + ARCHIVE_AFTER]``, the nearest. Measured on
2026-11-15 against the live BB (spec §8.2, "The archive window", n 379): an archive ends
0.085 s to 17.4 s after its usage rows, never before. The ruling in the plan's
"Decisions made while building (#6263)"
(``docs/superpowers/plans/2026-11-15-filament-capture.md``) sets the window to
``[created_at, created_at + 30 s]``. A row with no archive in it keeps a null
``bb_archive_id``; its grams count all the same.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from scadbuddy.bambuddy.models import Archive, SpoolUsage, as_utc
from scadbuddy.filament import capture, ledger, prints
from scadbuddy.filament.ledger import Conn
from scadbuddy.filament.models import SpoolEvent

logger = logging.getLogger(__name__)

#: How long before a usage row an archive may end: none was seen (spec §8.2).
ARCHIVE_BEFORE = timedelta(0)
#: How long after a usage row an archive may end: 17.4 s was the widest seen (spec §8.2).
ARCHIVE_AFTER = timedelta(seconds=30)


@dataclass
class UsageReport:
    """What one booking did: the rows it booked, and the rows of a BB spool no SB spool
    is linked to."""

    booked: int = 0
    unlinked: int = 0


@dataclass(frozen=True)
class _Link:
    spool_id: UUID
    archived: bool
    linked_at: datetime | None
    #: The pull made the spool archived, as Bambuddy had archived it (#6707).
    made_archived: bool = False


def _links(conn: Conn, rows: Sequence[SpoolUsage]) -> dict[int, _Link]:
    """The SB spool linked to each BB spool ``rows`` name, by BB spool id: whether it is
    archived, the time of its first `ledger.BB_SPOOL` event, and whether the pull made it
    archived (#6707)."""
    found = conn.execute(
        "SELECT l.bb_spool_id, l.spool_id, s.archived_at IS NOT NULL AS archived,"
        " (SELECT min(e.at) FROM filament_spool_events e"
        " WHERE e.spool_id = l.spool_id AND e.ref_kind = %s) AS linked_at,"
        " EXISTS (SELECT 1 FROM filament_spool_events e WHERE e.spool_id = l.spool_id"
        " AND e.kind = 'archived' AND e.data ? 'bb_archived_at') AS made_archived"
        " FROM filament_bb_links l JOIN filament_spools s ON s.id = l.spool_id"
        " WHERE l.bb_spool_id = ANY(%s)",
        (ledger.BB_SPOOL, sorted({row.spool_id for row in rows})),
    ).fetchall()
    return {
        r["bb_spool_id"]: _Link(r["spool_id"], r["archived"], r["linked_at"], r["made_archived"])
        for r in found
    }


def _books(row: SpoolUsage, link: _Link | None) -> bool:
    """Whether ``row`` is one to book: it took grams, its BB spool is linked to an SB
    spool that is not archived (the pull never writes to one), and it is later than the
    link's reading. Every row of a spool the pull made archived books (#6707)."""
    if row.weight_used <= 0 or link is None:
        return False
    if link.made_archived:
        return True
    if link.archived:
        return False
    return link.linked_at is None or row.created_at > link.linked_at


def unbooked(conn: Conn, rows: Sequence[SpoolUsage]) -> set[int]:
    """The ids of the ``rows`` that `book_usage` would book and no `ledger.BB_USAGE`
    event names yet."""
    links = _links(conn, rows)
    ids = {row.id for row in rows if _books(row, links.get(row.spool_id))}
    return ids - _booked(conn, ids)


def _booked(conn: Conn, ids: set[int]) -> set[int]:
    """The ``ids`` a `ledger.BB_USAGE` event names already."""
    if not ids:
        return set()
    booked = conn.execute(
        "SELECT DISTINCT ref_id FROM filament_spool_events"
        " WHERE ref_kind = %s AND ref_id = ANY(%s)",
        (ledger.BB_USAGE, [str(i) for i in ids]),
    ).fetchall()
    return {int(row["ref_id"]) for row in booked}


def match_archive(row: SpoolUsage, archives: Sequence[Archive]) -> int | None:
    """The id of the archive of ``row``'s print: of the same BB printer, ended in the
    module's window, the nearest; ``None`` when none is."""
    if row.printer_id is None:
        return None
    best: tuple[timedelta, int] | None = None
    for archive in archives:
        ended = as_utc(archive.completed_at)
        if archive.printer_id != row.printer_id or ended is None:
            continue
        offset = ended - row.created_at
        if not -ARCHIVE_BEFORE <= offset <= ARCHIVE_AFTER:
            continue
        if best is None or abs(offset) < best[0]:
            best = (abs(offset), archive.id)
    return None if best is None else best[1]


#: A usage row's place in Bambuddy's order: its ``created_at``, then its ``id``.
Mark = tuple[datetime, int]


def usage_mark(conn: Conn, instance: str) -> Mark | None:
    """The newest usage row a pull of ``instance`` has read and booked whole (R-7002-6);
    ``None`` when none is recorded, or it was read from another instance. It locks the
    state row ``FOR UPDATE`` until ``conn``'s transaction ends, so two bookings take turns
    and the second counts against the mark the first left (R-7002-7). Call it first in the
    transaction, before any spool lock: the state row, then the spools, as
    `bb_sync_results.apply_results` locks them."""
    row = conn.execute(
        "SELECT usage_read_instance, usage_read_at, usage_read_id FROM filament_bb_sync_state"
        " FOR UPDATE"
    ).fetchone()
    if (
        row is None
        or row["usage_read_instance"] != instance
        or row["usage_read_at"] is None
        or row["usage_read_id"] is None
    ):
        return None
    return (row["usage_read_at"], row["usage_read_id"])


def advance_mark(conn: Conn, instance: str, mark: Mark) -> None:
    """Record ``mark`` as ``instance``'s usage mark; never moves it back for one instance."""
    at, row_id = mark
    conn.execute(
        "UPDATE filament_bb_sync_state SET usage_read_instance = %s, usage_read_at = %s,"
        " usage_read_id = %s WHERE usage_read_instance IS DISTINCT FROM %s"
        " OR usage_read_at IS NULL OR (usage_read_at, usage_read_id) < (%s, %s)",
        (instance, at, row_id, instance, at, row_id),
    )


def book_usage(
    conn: Conn,
    rows: Sequence[SpoolUsage],
    archives: Sequence[Archive],
    *,
    actor: str,
    mark: Mark | None = None,
) -> UsageReport:
    """Book ``rows`` on ``conn``'s transaction, as the module says (`_books`). A row of
    a BB spool with no link is counted ``unlinked`` only when it is later than ``mark``,
    the newest row an earlier pull read and booked whole (`usage_mark`), by ``created_at``
    then ``id``; with no mark every such row counts. So a row an earlier pull read is not
    counted again (R-7002-6). A row of an archived SB spool is counted neither way. Each row is
    booked, and its estimate covered (`_cover`), in one savepoint: a row whose booking or
    cover fails is logged and skipped, nothing of it is kept, and the next pull retries
    both; the rest are booked. A row a `ledger.BB_USAGE` event names already is left out
    in one query, never tried again: the pull sends every row it read, not only the new
    ones (R-7002-3). The spools it books are locked first, in id order, as `capture.lock`
    does for a report write, so the two cannot deadlock."""
    report = UsageReport()
    links = _links(conn, rows)
    wanted = [
        (row, link)
        for row in rows
        if (link := links.get(row.spool_id)) is not None and _books(row, link)
    ]
    done = _booked(conn, {row.id for row, _ in wanted})
    todo = [(row, link) for row, link in wanted if row.id not in done]
    spools = {link.spool_id for _, link in todo}
    if spools:
        capture.lock(conn, sorted(spools))
    for row in rows:
        if (
            row.weight_used > 0
            and links.get(row.spool_id) is None
            and (mark is None or (row.created_at, row.id) > mark)
        ):
            report.unlinked += 1
    for i, (row, link) in enumerate(todo):
        try:
            with conn.transaction():
                booked = _book(conn, row, link.spool_id, archives, actor)
                if booked is not None:
                    _cover(
                        conn,
                        booked,
                        row,
                        link.spool_id,
                        actor,
                        [(r, k.spool_id) for r, k in todo[i + 1 :]],
                    )
        except Exception as error:
            logger.warning(
                "could not book a Bambuddy usage row; skipped",
                extra={"bb_usage_id": row.id, "error": type(error).__name__},
                exc_info=True,
            )
            continue
        if booked is not None:
            report.booked += 1
    return report


def _book(
    conn: Conn, row: SpoolUsage, spool_id: UUID, archives: Sequence[Archive], actor: str
) -> SpoolEvent | None:
    """One row's ``print_ended`` event; ``None`` when it is booked already."""
    event = ledger.record(
        conn,
        spool_id,
        "print_ended",
        source="print_actual",
        delta_g=-row.weight_used,
        ref_kind=ledger.BB_USAGE,
        ref_id=str(row.id),
        at=row.created_at,
        printer=_printer(conn, row.printer_id),
        actor=actor,
        data={
            "job": row.print_name,
            "basis": "bb_usage",
            "bb_usage_id": row.id,
            "bb_spool_id": row.spool_id,
            "bb_status": row.status,
            "bb_archive_id": match_archive(row, archives),
        },
    )
    return event


def _cover(
    conn: Conn,
    event: SpoolEvent,
    row: SpoolUsage,
    spool_id: UUID,
    actor: str,
    pending: Sequence[tuple[SpoolUsage, UUID]],
) -> None:
    """Pair a usage row with an SB estimate on another spool (#6598). Bambuddy's tray
    assignment can book a plate on a different SB spool than the one SB's estimate names;
    the pair is then on two spools, and the plate counts on both. When the row's spool has
    no SB estimate of the job, and on the row's printer exactly one other spool has an
    open estimate of it, and no other usage row of the job is unpaired, book on that
    spool a ``print_ended`` of source ``print_actual`` and 0 g with the estimate's ref and
    ``data.covered_by`` the row's event: the estimate drops by `ledger.basis`'s own rule,
    which stays per spool. Any other shape changes nothing and is logged as
    ``filament.usage.unpaired`` when an estimate was a candidate. A retried booking books
    nothing twice: the covering event's key is the estimate's.

    A candidate is an open estimate of the job (`prints.open_for_job`) whose own event names
    no other printer, and whose spool has no usage row of the job, booked already or still
    pending in this batch: a pair by `ledger.basis` rule 3 writes no ``print_ended``, so
    that estimate would stay open though it is paired. Candidates match on the job name
    within the printer, not on the run: a stale estimate of the same job that never ended
    can be the candidate. A candidate on an archived spool is not written to (the pull never
    writes to one) and is logged with reason ``archived``. ``others`` counts the booked
    usage rows of the job that no estimate pairs, bar one that covered an estimate (an
    unlinked BB spool's row is not booked, so not counted) and the rows of this batch not
    yet booked (``pending``), the row's own spool included: a second row of the job on one
    spool makes the job ambiguous."""
    job = event.data.get("job")
    if not isinstance(job, str) or event.printer_id is None:
        return
    if _has_estimate(conn, spool_id, job):
        return
    printer = str(event.printer_id)
    unbooked = {
        other_spool
        for other, other_spool in pending
        if other.print_name == job and other.printer_id == row.printer_id
    }
    candidates = [
        u
        for u in prints.open_for_job(conn, printer, job)
        if u.spool_id != spool_id
        and u.spool_id not in unbooked
        and _candidate(conn, u, job, event.printer_id)
    ]
    if not candidates:
        return
    others = {
        r["spool_id"]
        for r in conn.execute(
            "SELECT e.spool_id FROM filament_spool_events e WHERE e.ref_kind = %s AND e.kind = %s"
            " AND e.printer_id = %s AND e.data ->> 'job' = %s AND e.id <> %s"
            " AND NOT EXISTS (SELECT 1 FROM filament_spool_events c"
            " WHERE c.data ->> 'covered_by' = e.id::text) AND NOT EXISTS"
            " (SELECT 1 FROM filament_spool_events s WHERE s.spool_id = e.spool_id"
            " AND s.ref_kind = %s AND s.kind = %s AND s.data ->> 'job' = %s)",
            (
                ledger.BB_USAGE,
                ledger.PRINT_ENDED,
                event.printer_id,
                job,
                event.id,
                ledger.PRINT_RUN,
                ledger.PRINT_STARTED,
                job,
            ),
        ).fetchall()
    }
    others |= {
        other_spool
        for other, other_spool in pending
        if other.print_name == job
        and other.printer_id == row.printer_id
        and other.id != row.id
        and not _has_estimate(conn, other_spool, job)
    }
    archived = len(candidates) == 1 and conn.execute(
        "SELECT archived_at IS NOT NULL AS archived FROM filament_spools WHERE id = %s",
        (candidates[0].spool_id,),
    ).fetchone() == {"archived": True}
    if len(candidates) != 1 or others or archived:
        extra: dict[str, object] = {
            "printer": printer,
            "job": job,
            "spool_ids": sorted(
                {str(spool_id), *(str(u.spool_id) for u in candidates)} | {str(o) for o in others}
            ),
        }
        if archived:
            extra["reason"] = "archived"
        logger.warning("filament.usage.unpaired", extra=extra)
        return
    (use,) = candidates
    ledger.record(
        conn,
        use.spool_id,
        ledger.PRINT_ENDED,
        source=ledger.PRINT_ACTUAL,
        delta_g=0.0,
        ref_kind=ledger.PRINT_RUN,
        ref_id=use.ref_id,
        at=event.at,
        actor=actor,
        data={"basis": "covered", "job": job, "covered_by": str(event.id)},
    )


def _candidate(conn: Conn, use: prints.OpenUse, job: str, printer: UUID) -> bool:
    """Whether the open ``use`` may be covered: its ``print_started`` names no printer or
    ``printer``, and its spool has no booked usage row of ``job`` on ``printer``."""
    found = conn.execute(
        "SELECT 1 FROM filament_spool_events e WHERE e.spool_id = %s AND e.ref_kind = %s"
        " AND e.kind = %s AND e.ref_id = %s AND (e.printer_id IS NULL OR e.printer_id = %s)"
        " AND NOT EXISTS (SELECT 1 FROM filament_spool_events b WHERE b.spool_id = e.spool_id"
        " AND b.ref_kind = %s AND b.kind = %s AND b.data ->> 'job' = %s"
        " AND b.printer_id = %s) LIMIT 1",
        (
            use.spool_id,
            ledger.PRINT_RUN,
            ledger.PRINT_STARTED,
            use.ref_id,
            printer,
            ledger.BB_USAGE,
            ledger.PRINT_ENDED,
            job,
            printer,
        ),
    ).fetchone()
    return found is not None


def _has_estimate(conn: Conn, spool_id: UUID, job: str) -> bool:
    """Whether the spool has an SB ``print_started`` of the job (open or ended)."""
    found = conn.execute(
        "SELECT 1 FROM filament_spool_events WHERE spool_id = %s AND ref_kind = %s"
        " AND kind = %s AND data ->> 'job' = %s LIMIT 1",
        (spool_id, ledger.PRINT_RUN, ledger.PRINT_STARTED, job),
    ).fetchone()
    return found is not None


def _printer(conn: Conn, bb_printer_id: int | None) -> UUID | None:
    """The SB printer that links BB printer ``bb_printer_id``."""
    if bb_printer_id is None:
        return None
    row = conn.execute(
        "SELECT id FROM printers WHERE bambuddy_printer_id = %s", (bb_printer_id,)
    ).fetchone()
    return None if row is None else row["id"]
