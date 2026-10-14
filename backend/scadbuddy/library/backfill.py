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
import threading
from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import psycopg

from scadbuddy.core.events import Event, EventBus, JobEvent, OutputEvent, emit
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

#: The jobs whose rows did not validate when last read, so each one's trace is logged once.
_unreadable_jobs: set[str] = set()


class AmbiguousOutputError(ValueError):
    """Several of the re-render's outputs were built from the output's Parts, and the
    output does not record which one it was, or records one that is not among them."""

    def __init__(self, parts: list[str], recorded_index: int | None) -> None:
        super().__init__(parts)
        #: The index the output was saved with, or None if it was saved before outputs
        #: recorded one.
        self.recorded_index = recorded_index


def choose_output(job: Job, record: OutputRecord | None) -> PipelineOutput | None:
    """The re-render's output that is this output: the job's only one, else the one
    built from the same Parts. A Part's key is its slug, revision, file and params, so
    the same inputs at the same revision give the same keys. Two outputs of one job with
    the same Parts are told apart by the index the output recorded when it was saved;
    one saved before that was recorded, or whose index is not among them, is refused
    rather than given either (#1007 item 2).
    """
    if len(job.outputs) == 1:
        return job.outputs[0]
    if record is None:
        return None
    wanted = set(record.parts)
    same = [i for i, out in enumerate(job.outputs) if set(out.record.parts) == wanted]
    if record.output_index is not None and record.output_index in same:
        return job.outputs[record.output_index]
    if len(same) > 1:
        raise AmbiguousOutputError(record.parts, record.output_index)
    return job.outputs[same[0]] if same else None


#: The events that settle a job: its re-render is attached, or marked with why not.
SETTLED = ("job.done", "job.failed", "job.superseded")


def attach_backfills(
    outputs: OutputStore,
    refs: BlobRefs,
    read_job: Callable[[str], Job],
    events: EventBus | None = None,
    stop: threading.Event | None = None,
) -> int:
    """Attach every finished re-render to its output. Returns how many it attached; one
    that failed is marked with why, and is not tried again. Each output is on its own:
    one that cannot be attached costs only itself, and a transient failure (the
    database, the disk) is left for the next pass. Either outcome is announced as
    ``output.updated`` on ``events`` (#1970). Once ``stop`` is set it attaches no
    further output: the boot's pass runs in a thread the shutdown cannot cancel (#2128)."""
    pending = outputs.pending_backfills()
    # A job no output waits on any more (its outputs deleted, or failed another way) is
    # never read again, so nothing else would drop it. Two races only cost a log line,
    # and are left (#3574): a backfill.json caught mid-write is skipped by this listing,
    # so its job is forgotten once and its trace logged again next pass; and a job
    # attach_job_backfills adds between the listing and this line is dropped, the same.
    _unreadable_jobs.intersection_update(state.job_id for _, state in pending)
    return _attach_all(outputs, refs, read_job, pending, events, stop)


def attach_job_backfills(
    outputs: OutputStore,
    refs: BlobRefs,
    read_job: Callable[[str], Job],
    job_id: str,
    events: EventBus | None = None,
) -> int:
    """`attach_backfills` for the outputs waiting on ``job_id`` only."""
    pending = [(oid, state) for oid, state in outputs.pending_backfills() if state.job_id == job_id]
    return _attach_all(outputs, refs, read_job, pending, events)


def follow_backfills(
    events: EventBus, attach: Callable[[str], int]
) -> Callable[[], Coroutine[Any, Any, None]]:
    """Run ``attach(job_id)`` in a thread for every settled job heard, off the
    listener's thread. A failure is logged and left for the backstop. Returns the
    remover, which stops hearing events and then waits for every attach in flight: a
    thread cannot be cancelled, and one left running would outlive the pool (#1759)."""
    loop = asyncio.get_running_loop()
    pending: set[asyncio.Task[None]] = set()
    stopped = False

    async def run(job_id: str) -> None:
        try:
            await asyncio.to_thread(attach, job_id)
        except Exception:
            logger.exception(
                "could not attach a finished re-render; the housekeeping sweep retries",
                extra={"job_id": job_id},
            )

    def schedule(job_id: str) -> None:
        if stopped:
            return  # heard before the remover ran, scheduled after it
        task = loop.create_task(run(job_id))
        pending.add(task)
        task.add_done_callback(pending.discard)

    def on_event(event: Event) -> None:
        if isinstance(event, JobEvent) and event.kind in SETTLED:
            # The loop may have closed first, at shutdown.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(schedule, event.job_id)

    remove_listener = events.add_listener(on_event)

    async def remove() -> None:
        nonlocal stopped
        stopped = True
        remove_listener()
        while pending:
            await asyncio.wait(set(pending))

    return remove


#: Seconds to wait for the claim's own connection: never forever, since the lifespan's
#: shutdown waits for an attach in flight.
CLAIM_CONNECT_TIMEOUT = 30


@contextlib.contextmanager
def _claimed(refs: BlobRefs, output_id: str) -> Iterator[bool]:
    """Whether this caller holds the output's attach: a transaction-scoped advisory
    lock, so another process, thread or the backstop attaching it skips it. The lock is
    taken on a connection of its own, outside the pool: the attach takes the pool's
    connections while it holds the lock, and a pool of one would otherwise never have
    one free (#1757)."""
    conninfo = refs.pool.conninfo
    if callable(conninfo):
        conninfo = conninfo()
    with (
        psycopg.connect(conninfo, connect_timeout=CLAIM_CONNECT_TIMEOUT) as conn,
        conn.transaction(),
    ):
        row = conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"scadbuddy-backfill:{output_id}",),
        ).fetchone()
        yield bool(row and row[0])


def _attach_all(
    outputs: OutputStore,
    refs: BlobRefs,
    read_job: Callable[[str], Job],
    pending: list[tuple[str, BackfillState]],
    events: EventBus | None,
    stop: threading.Event | None = None,
) -> int:
    attached = 0
    for output_id, state in pending:
        if stop is not None and stop.is_set():
            break  # what is left waits for the next pass
        try:
            with _claimed(refs, output_id) as mine:
                if not mine:
                    continue  # another attach holds it
                current = outputs.backfill(output_id)
                if current is None or current != state:
                    continue  # attached, failed or re-queued since it was listed
                if _attach(outputs, refs, read_job, output_id, state.job_id):
                    attached += 1
                if _changed(outputs, output_id, state):
                    _announce(events, outputs, output_id)
        except ValueError as error:  # a corrupt record.json or manifest: not worth retrying
            logger.exception("could not attach a re-render", extra={"id": output_id})
            try:
                outputs.fail_backfill(output_id, state.job_id, f"the output's record: {error}")
            except Exception:
                logger.exception("could not mark a backfill failed", extra={"id": output_id})
            else:
                _announce(events, outputs, output_id)
        except Exception:
            logger.exception("could not attach a re-render; retrying", extra={"id": output_id})
    return attached


def _changed(outputs: OutputStore, output_id: str, state: BackfillState) -> bool:
    """Whether the attach settled the backfill, read under its own ``try`` (#2038): a
    re-read that fails (a half-written ``backfill.json`` from a concurrent re-queue)
    must not reach the attach's ``fail_backfill``. It counts as changed, since an
    announcement only makes a reader read the output again."""
    try:
        return outputs.backfill(output_id) != state
    except Exception:
        logger.exception("could not re-read a backfill after its attach", extra={"id": output_id})
        return True


def _announce(events: EventBus | None, outputs: OutputStore, output_id: str) -> None:
    """``output.updated``: the output's backfill was attached, cleared or marked failed,
    so a reader waiting on it reads it again rather than polling (#1970).

    Never raises: it runs after the attach is done, so a failure here must not mark
    that attach failed or stop the pass. The slug comes from the output's directory,
    as `attach_backfill` names it, not from ``meta.json`` or the database, which can
    fail where the attach did not."""
    if events is None:
        return
    try:
        slug = outputs.directory(output_id).parent.name
    except OutputNotFoundError:
        return  # deleted meanwhile: its own output.deleted said so
    except Exception:
        logger.exception("could not announce a backfill", extra={"id": output_id})
        return
    emit(events, OutputEvent(kind="output.updated", output_id=output_id, slug=slug))


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
    except ValueError:
        # The job's row did not validate: not the output's fault, and not permanent (a
        # row written by another release mid-deploy). Left for the next pass (#1007),
        # where a ValueError here would otherwise mark the backfill failed. Its trace is
        # logged once per job; a row that never validates would otherwise log one every
        # pass, for as long as it stays.
        if job_id in _unreadable_jobs:
            logger.warning(
                "a re-render's job still does not validate; retrying",
                extra={"id": output_id, "job_id": job_id},
            )
        else:
            _unreadable_jobs.add(job_id)
            logger.exception(
                "could not read a re-render's job; retrying",
                extra={"id": output_id, "job_id": job_id},
            )
        return False
    _unreadable_jobs.discard(job_id)
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
    except AmbiguousOutputError as error:
        why = (
            "it was saved before outputs recorded which one they were"
            if error.recorded_index is None
            else "the one it was saved as is not among them"
        )
        outputs.fail_backfill(
            output_id,
            job.id,
            f"the re-render wrote several outputs from this output's objects, and {why};"
            " save it again",
        )
        return False
    if chosen is None or not chosen.manifest:
        outputs.fail_backfill(
            output_id, job.id, "the re-render wrote no output with this output's objects"
        )
        return False
    hold_parts(refs, output_id, chosen.manifest, job.slug)
    try:
        outputs.attach_backfill(output_id, chosen)
    except (OutputNotFoundError, FileNotFoundError):
        # Deleted after it was read: the delete's release may have run before the hold
        # above, so drop the holds here, or nothing ever does.
        release_parts(refs, output_id)
        return False
    logger.info("attached a re-render to an output", extra={"id": output_id, "job_id": job.id})
    return True
