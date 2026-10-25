"""How each print ended, recorded once (#2665): ``print_outcomes``.

``print.settled`` is published per process by `ProgressObserver`'s in-memory comparison
and carries no outcome, so two replicas reading one print can each publish it. A flow
trigger needs better: the follow's settled read (`Follower`, every path a print takes,
Bambuddy's queue and the LAN alike) records the outcome here against the run that
queued or started the print, and publishes ``print.outcome`` in the same transaction.
The row's key is the run, so a second reader (another replica, a poke's old attempt, a
follow started again by the progress route) records nothing and publishes nothing.

The print is the subject's newest ``succeeded`` run, and only when the settled read is
that run's: one of its queue items, its slice job, or (a LAN print) its LAN start. A
read that names none of them (a new print queued between the read and the record)
records nothing; the new print's own settle records it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.bambuddy.progress import PrintProgress
from scadbuddy.bambuddy.runs import PrintRun, TransactionalEvents
from scadbuddy.core.events import PrintOutcome, PrintOutcomeKind

logger = logging.getLogger(__name__)

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
        return await asyncio.to_thread(self._record, run_subject, slug, progress)

    def _record(self, run_subject: str, slug: str, progress: PrintProgress) -> PrintOutcome | None:
        if not progress.settled:
            return None
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT id, output_id, status, created_at, finished_at, result, error,"
                " printer::text AS printer FROM print_runs"
                " WHERE output_id = %s AND status = 'succeeded'"
                " ORDER BY created_at DESC LIMIT 1",
                (run_subject,),
            ).fetchone()
            if row is None:
                return None
            run = PrintRun.model_validate(row)
            if not is_runs_print(run, progress):
                logger.info(
                    "a settled read is not the newest run's print; nothing recorded",
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
