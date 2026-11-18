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


def _links(conn: Conn, rows: Sequence[SpoolUsage]) -> dict[int, _Link]:
    """The SB spool linked to each BB spool ``rows`` name, by BB spool id: whether it is
    archived, and the time of its first `ledger.BB_SPOOL` event."""
    found = conn.execute(
        "SELECT l.bb_spool_id, l.spool_id, s.archived_at IS NOT NULL AS archived,"
        " (SELECT min(e.at) FROM filament_spool_events e"
        " WHERE e.spool_id = l.spool_id AND e.ref_kind = %s) AS linked_at"
        " FROM filament_bb_links l JOIN filament_spools s ON s.id = l.spool_id"
        " WHERE l.bb_spool_id = ANY(%s)",
        (ledger.BB_SPOOL, sorted({row.spool_id for row in rows})),
    ).fetchall()
    return {r["bb_spool_id"]: _Link(r["spool_id"], r["archived"], r["linked_at"]) for r in found}


def _books(row: SpoolUsage, link: _Link | None) -> bool:
    """Whether ``row`` is one to book: it took grams, its BB spool is linked to an SB
    spool that is not archived (the pull never writes to one), and it is later than the
    link's reading."""
    if row.weight_used <= 0 or link is None or link.archived:
        return False
    return link.linked_at is None or row.created_at > link.linked_at


def unbooked(conn: Conn, rows: Sequence[SpoolUsage]) -> set[int]:
    """The ids of the ``rows`` that `book_usage` would book and no `ledger.BB_USAGE`
    event names yet."""
    links = _links(conn, rows)
    ids = {row.id for row in rows if _books(row, links.get(row.spool_id))}
    if not ids:
        return set()
    booked = conn.execute(
        "SELECT DISTINCT ref_id FROM filament_spool_events"
        " WHERE ref_kind = %s AND ref_id = ANY(%s)",
        (ledger.BB_USAGE, [str(i) for i in ids]),
    ).fetchall()
    return ids - {int(row["ref_id"]) for row in booked}


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


def book_usage(
    conn: Conn, rows: Sequence[SpoolUsage], archives: Sequence[Archive], *, actor: str
) -> UsageReport:
    """Book ``rows`` on ``conn``'s transaction, as the module says (`_books`). A row of
    a BB spool with no link is counted ``unlinked``; a row of an archived SB spool is
    counted neither way. Each row is booked in its own savepoint: one that fails is
    logged and skipped, and the rest are booked. The spools it books are locked first, in
    id order, as `capture.lock` does for a report write, so the two cannot deadlock."""
    report = UsageReport()
    links = _links(conn, rows)
    spools = {
        link.spool_id
        for row in rows
        if (link := links.get(row.spool_id)) is not None and _books(row, link)
    }
    if spools:
        capture.lock(conn, sorted(spools))
    events: list[tuple[SpoolEvent, UUID]] = []
    for row in rows:
        link = links.get(row.spool_id)
        if row.weight_used > 0 and link is None:
            report.unlinked += 1
        if link is None or not _books(row, link):
            continue
        try:
            with conn.transaction():
                booked = _book(conn, row, link.spool_id, archives, actor)
                if booked is not None:
                    events.append((booked, link.spool_id))
        except Exception as error:
            logger.warning(
                "could not book a Bambuddy usage row; skipped",
                extra={"bb_usage_id": row.id, "error": type(error).__name__},
                exc_info=True,
            )
            continue
        if booked is not None:
            report.booked += 1
    # After every row is booked, so a job's other unpaired rows of this batch count.
    for event, spool_id in events:
        try:
            with conn.transaction():
                _cover(conn, event, spool_id, actor)
        except Exception as error:
            logger.warning(
                "could not cover an SB estimate for a Bambuddy usage row; skipped",
                extra={"bb_usage_id": event.data.get("bb_usage_id"), "error": type(error).__name__},
                exc_info=True,
            )
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


def _cover(conn: Conn, event: SpoolEvent, spool_id: UUID, actor: str) -> None:
    """Pair a usage row with an SB estimate on another spool (#6598). Bambuddy's tray
    assignment can book a plate on a different SB spool than the one SB's estimate names;
    the pair is then on two spools, and the plate counts on both. When the row's spool has
    no SB estimate of the job, and on the row's printer exactly one other spool has an
    open estimate of it, and no other usage row of the job is unpaired, book on that
    spool a ``print_ended`` of source ``print_actual`` and 0 g with the estimate's ref and
    ``data.covered_by`` the row's event: the estimate drops by `ledger.basis`'s own rule,
    which stays per spool. Any other shape changes nothing and is logged as
    ``filament.usage.unpaired`` when an estimate was a candidate. A retried booking books
    nothing twice: the covering event's key is the estimate's."""
    job = event.data.get("job")
    if not isinstance(job, str) or event.printer_id is None:
        return
    if _has_estimate(conn, spool_id, job):
        return
    printer = str(event.printer_id)
    candidates = [u for u in prints.open_for_job(conn, printer, job) if u.spool_id != spool_id]
    if not candidates:
        return
    others = conn.execute(
        "SELECT e.spool_id FROM filament_spool_events e WHERE e.ref_kind = %s AND e.kind = %s"
        " AND e.printer_id = %s AND e.data ->> 'job' = %s AND e.id <> %s AND NOT EXISTS"
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
    if len(candidates) != 1 or others:
        logger.warning(
            "filament.usage.unpaired",
            extra={
                "printer": printer,
                "job": job,
                "spool_ids": sorted(
                    {str(spool_id), *(str(u.spool_id) for u in candidates)}
                    | {str(r["spool_id"]) for r in others}
                ),
            },
        )
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
