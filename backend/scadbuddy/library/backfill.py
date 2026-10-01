"""#902: an output saved before manifests gets one by re-rendering it.

`POST /outputs/{id}/backfill` queues an ordinary render of the output's recorded inputs
at its recorded revision and leaves `backfill.json` naming the job in the output's
directory. :func:`attach_backfills`, run by the API on every reconcile pass, attaches
each finished re-render to its output: the Parts are held first (as `create_output`
holds them before it writes), then `manifest.json` is written and the marker removed.
Every step can run again, so a crash anywhere is finished by the next pass.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from scadbuddy.library.outputs import OutputNotFoundError, OutputStore, hold_parts, release_parts
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


def attach_backfills(outputs: OutputStore, refs: BlobRefs, read_job: Callable[[str], Job]) -> int:
    """Attach every finished re-render to its output. Returns how many it attached; one
    that failed is marked with why, and is not tried again. Each output is on its own:
    one that cannot be attached costs only itself, and a transient failure (the
    database, the disk) is left for the next pass."""
    attached = 0
    for output_id, state in outputs.pending_backfills():
        try:
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
