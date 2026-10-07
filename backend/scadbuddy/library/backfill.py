"""#902: an output saved before manifests gets one by re-rendering it.

`POST /outputs/{id}/backfill` queues an ordinary render of the output's recorded inputs
at its recorded revision and leaves `backfill.json` naming the job in the output's
directory. Each finished re-render is attached to its output: the Parts are held first
(as `create_output` holds them before it writes), then `manifest.json` is written and
the marker removed. :func:`follow_backfills` attaches on the job's settling event, in
every API process; :func:`attach_backfills` is the housekeeping Schedule's backstop for
an event no process heard. Each output is claimed under a Postgres advisory lock, so
those racing attach it once; every step can run again, so a crash anywhere is finished
by the next attach.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterator

from scadbuddy.core.events import Event, EventBus, JobEvent
from scadbuddy.library.outputs import (
    BackfillState,
    OutputNotFoundError,
    OutputStore,
    hold_parts,
    release_parts,
)
from scadbuddy.render.job_models import Job, JobNotFoundError, OutputRecord, PipelineOutput
from scadbuddy.store.refs import BlobRefs

logger = logging.getLogger(__name__)


def choose_output(job: Job, record: OutputRecord | None) -> PipelineOutput | None:
    """The re-render's output that is this output: the job's only one, else the one
    built from the same Parts. A Part's key is its slug, revision, file and params, so
    the same inputs at the same revision give the same keys."""
    if len(job.outputs) == 1:
        return job.outputs[0]
    if record is None:
        return None
    wanted = set(record.parts)
    return next((out for out in job.outputs if set(out.record.parts) == wanted), None)


#: The events that settle a job: its re-render is attached, or marked with why not.
SETTLED = ("job.done", "job.failed", "job.superseded")


def attach_backfills(outputs: OutputStore, refs: BlobRefs, read_job: Callable[[str], Job]) -> int:
    """Attach every finished re-render to its output. Returns how many it attached; one
    that failed is marked with why, and is not tried again. Each output is on its own:
    one that cannot be attached costs only itself, and a transient failure (the
    database, the disk) is left for the next pass."""
    return _attach_all(outputs, refs, read_job, outputs.pending_backfills())


def attach_job_backfills(
    outputs: OutputStore, refs: BlobRefs, read_job: Callable[[str], Job], job_id: str
) -> int:
    """`attach_backfills` for the outputs waiting on ``job_id`` only."""
    pending = [(oid, state) for oid, state in outputs.pending_backfills() if state.job_id == job_id]
    return _attach_all(outputs, refs, read_job, pending)


def follow_backfills(events: EventBus, attach: Callable[[str], int]) -> Callable[[], None]:
    """Run ``attach(job_id)`` in a thread for every settled job heard, off the
    listener's thread. A failure is logged and left for the backstop. Returns the
    remover."""
    loop = asyncio.get_running_loop()
    pending: set[asyncio.Task[None]] = set()

    async def run(job_id: str) -> None:
        try:
            await asyncio.to_thread(attach, job_id)
        except Exception:
            logger.exception(
                "could not attach a finished re-render; the housekeeping sweep retries",
                extra={"job_id": job_id},
            )

    def schedule(job_id: str) -> None:
        task = loop.create_task(run(job_id))
        pending.add(task)
        task.add_done_callback(pending.discard)

    def on_event(event: Event) -> None:
        if isinstance(event, JobEvent) and event.kind in SETTLED:
            # The loop may have closed first, at shutdown.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(schedule, event.job_id)

    remove: Callable[[], None] = events.add_listener(on_event)
    return remove


@contextlib.contextmanager
def _claimed(refs: BlobRefs, output_id: str) -> Iterator[bool]:
    """Whether this caller holds the output's attach: a transaction-scoped advisory
    lock, so another process, thread or the backstop attaching it skips it."""
    with refs.pool.connection() as conn, conn.transaction():
        row = conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS mine",
            (f"scadbuddy-backfill:{output_id}",),
        ).fetchone()
        yield bool(row and row["mine"])


def _attach_all(
    outputs: OutputStore,
    refs: BlobRefs,
    read_job: Callable[[str], Job],
    pending: list[tuple[str, BackfillState]],
) -> int:
    attached = 0
    for output_id, state in pending:
        try:
            with _claimed(refs, output_id) as mine:
                if not mine:
                    continue  # another attach holds it
                current = outputs.backfill(output_id)
                if current is None or current != state:
                    continue  # attached, failed or re-queued since it was listed
                if _attach(outputs, refs, read_job, output_id, state.job_id):
                    attached += 1
        except ValueError as error:  # a corrupt record.json or manifest: not worth retrying
            logger.exception("could not attach a re-render", extra={"id": output_id})
            try:
                outputs.fail_backfill(output_id, state.job_id, f"the output's record: {error}")
            except Exception:
                logger.exception("could not mark a backfill failed", extra={"id": output_id})
        except Exception:
            logger.exception("could not attach a re-render; retrying", extra={"id": output_id})
    return attached


def _attach(
    outputs: OutputStore,
    refs: BlobRefs,
    read_job: Callable[[str], Job],
    output_id: str,
    job_id: str,
) -> bool:
    if outputs.manifest(output_id):
        # Attached by a pass that crashed before removing the marker.
        outputs.clear_backfill(output_id)
        return False
    try:
        job = read_job(job_id)
    except JobNotFoundError:
        outputs.fail_backfill(output_id, job_id, "the re-render is gone; try again")
        return False
    if job.state in ("pending", "running"):
        return False
    if job.state != "done":
        reason = job.error if job.state == "failed" and job.error else None
        outputs.fail_backfill(output_id, job.id, reason or f"the re-render was {job.state}")
        return False
    try:
        chosen = choose_output(job, outputs.record(output_id))
    except OutputNotFoundError:
        return False  # deleted while it was re-rendering: nothing to attach to
    if chosen is None or not chosen.manifest:
        outputs.fail_backfill(
            output_id, job.id, "the re-render wrote no output with this output's objects"
        )
        return False
    hold_parts(refs, output_id, chosen.manifest)
    try:
        outputs.attach_backfill(output_id, chosen)
    except (OutputNotFoundError, FileNotFoundError):
        # Deleted after it was read: the delete's release may have run before the hold
        # above, so drop the holds here, or nothing ever does.
        release_parts(refs, output_id)
        return False
    logger.info("attached a re-render to an output", extra={"id": output_id, "job_id": job.id})
    return True
