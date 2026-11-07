"""How each print ended, recorded once (#2665): ``print_outcomes``.

``print.settled`` is published per process by `ProgressObserver`'s in-memory comparison
and carries no outcome, so two replicas reading one print can each publish it. A flow
trigger needs better: the follow's settled read (`Follower`, every path a print takes,
Bambuddy's queue and the LAN alike) records the outcome here against the run that
queued or started the print, and publishes ``print.outcome`` in the same transaction.
The row's key is the run, so a second reader (another replica, a poke's old attempt, a
follow started again by the progress route) records nothing and publishes nothing.

The subject's own read (`record`) is of its newest ``succeeded`` run, and records only
when the settled read is that run's: one of its queue items, its slice job, or (a LAN
print) its LAN start. A read that names none of them (a new print queued between the read
and the record) records nothing; the new print's own settle records it.

Each older run of the subject is settled on its own (#5613): two prints of one library
file at once (two order items of the file) share the subject ``library:<file id>``.
The follow asks for the subject's older open runs (`older_open`: ``succeeded``, no
outcome yet, started within `OPEN_RUN_WINDOW`), reads each by its own run, and records
each settled one against that run (`record_run`).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.bambuddy.progress import PrintProgress
from scadbuddy.bambuddy.runs import PrintRun, TransactionalEvents
from scadbuddy.core.events import PrintOutcome, PrintOutcomeKind

logger = logging.getLogger(__name__)

#: How long after it started an older run of a subject is still read for its outcome
#: (#5613). It bounds the reads: a run whose follow ended without a settled read, or one
#: from before outcomes were recorded, is not read again past it. An older print that
#: runs longer than this, overtaken by a newer print of its subject, is settled by hand.
OPEN_RUN_WINDOW = timedelta(days=3)

_RUN_COLUMNS = (
    "id, output_id, status, created_at, finished_at, result, error, printer::text AS printer"
)

_OUTCOMES: dict[str, PrintOutcomeKind] = {
    "done": "finished",
    "failed": "failed",
    "cancelled": "cancelled",
}


def outcome_of(progress: PrintProgress) -> PrintOutcomeKind:
    """A settled print's outcome: its stage, with anything but done, failed or cancelled
    (a slice job Bambuddy dropped, a LAN printer that moved on or went quiet) unknown."""
    return _OUTCOMES.get(progress.stage, "unknown")


def is_runs_print(run: PrintRun, progress: PrintProgress) -> bool:
    """Whether ``progress`` reads ``run``'s print, not an older or newer one."""
    result = run.result
    if result is None:
        return False
    if progress.route == "lan":
        return bool(result.lan)
    items = {copy.queue_entry_id for copy in progress.copies_detail}
    items.add(progress.queue_item_id)
    items.discard(None)
    if items:
        return bool(items & set(result.queue_item_ids))
    return progress.slice_job_id is not None and progress.slice_job_id == result.slice_job_id


class PrintOutcomeStore:
    def __init__(
        self,
        pool: ConnectionPool[Connection[DictRow]],
        events: TransactionalEvents | None,
    ) -> None:
        self._pool = pool
        self.events = events

    async def record(
        self, run_subject: str, slug: str, progress: PrintProgress
    ) -> PrintOutcome | None:
        """Record ``run_subject``'s settled print and publish ``print.outcome``; the event,
        or None when it was recorded already or the read is not its newest run's."""
        return await asyncio.to_thread(self._record, run_subject, slug, progress, None)

    async def record_run(
        self, run: PrintRun, slug: str, progress: PrintProgress
    ) -> PrintOutcome | None:
        """Record one run's settled print (#5613), as `record` does, but against ``run``,
        not the subject's newest run."""
        return await asyncio.to_thread(self._record, run.output_id, slug, progress, run.id)

    async def older_open(self, run_subject: str) -> list[PrintRun]:
        """The subject's ``succeeded`` runs with no outcome yet that started within
        `OPEN_RUN_WINDOW`, oldest first. Its newest ``succeeded`` run is not among them:
        the subject's own read records that one (`record`)."""
        return await asyncio.to_thread(self._older_open, run_subject)

    def _older_open(self, run_subject: str) -> list[PrintRun]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM print_runs r"
                " WHERE r.output_id = %s AND r.status = 'succeeded'"
                " AND r.created_at > now() - %s"
                " AND NOT EXISTS (SELECT 1 FROM print_outcomes o WHERE o.run_id = r.id)"
                " AND r.id <> (SELECT n.id FROM print_runs n"
                " WHERE n.output_id = %s AND n.status = 'succeeded'"
                " ORDER BY n.created_at DESC LIMIT 1)"
                " ORDER BY r.created_at",
                (run_subject, OPEN_RUN_WINDOW, run_subject),
            ).fetchall()
        return [PrintRun.model_validate(row) for row in rows]

    def _record(
        self, run_subject: str, slug: str, progress: PrintProgress, run_id: str | None
    ) -> PrintOutcome | None:
        if not progress.settled:
            return None
        with self._pool.connection() as conn, conn.transaction():
            if run_id is None:
                row = conn.execute(
                    f"SELECT {_RUN_COLUMNS} FROM print_runs"
                    " WHERE output_id = %s AND status = 'succeeded'"
                    " ORDER BY created_at DESC LIMIT 1",
                    (run_subject,),
                ).fetchone()
            else:
                row = conn.execute(
                    f"SELECT {_RUN_COLUMNS} FROM print_runs"
                    " WHERE id = %s AND output_id = %s AND status = 'succeeded'",
                    (run_id, run_subject),
                ).fetchone()
            if row is None:
                return None
            run = PrintRun.model_validate(row)
            if not is_runs_print(run, progress):
                logger.info(
                    "a settled read is not its run's print; nothing recorded",
                    extra={"subject": run_subject, "run_id": run.id},
                )
                return None
            outcome = outcome_of(progress)
            inserted: dict[str, Any] | None = conn.execute(
                "INSERT INTO print_outcomes (run_id, subject, slug, printer, outcome)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (run_id) DO NOTHING RETURNING run_id",
                (run.id, run_subject, slug, run.printer, outcome),
            ).fetchone()
            if inserted is None:
                return None
            event = PrintOutcome(
                output_id=run_subject,
                slug=slug,
                run_id=run.id,
                outcome=outcome,
                printer=run.printer,
            )
            if self.events is not None:
                self.events.publish_in(conn, event)
        return event
