"""#2715: remove what a print run's lost slice start sliced, if Bambuddy took it.

A run starts a plate's slice again when ``POST /library/files/{id}/slice`` got no
answer, or a 5xx or 429 (#2081). Bambuddy may have taken the first POST all the same;
its job then slices to a ``.gcode.3mf`` that nothing queues, and the run never learnt
its id. Every slice job ScadBuddy starts is recorded (a run's in
``print_slice_starts``, a background slice's in ``print_preview_slices``), and so is
every lost start. Bambuddy numbers its jobs in sequence and keeps a finished one for 30
minutes, so the housekeeping sweep (``housekeeping_sweep_preview_slices``, every
``PRUNE_INTERVAL``) looks at the jobs numbered beside the first one recorded after a
lost start: one that slices the same copy, was created then, and that nobody recorded
is the lost start's, and the file it sliced to is removed.

A slice someone starts by hand in Bambuddy's UI on the same ScadBuddy copy, within
that window, matches the rule too and its file is removed with the lost start's
(#2911): a slice job says what it slices, and when, but not with which presets or
plate, so nothing tells the two apart. The copy is ScadBuddy's own inbox file, never
the user's, which is what keeps that case narrow.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import status

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, LostStart
from scadbuddy.core.problems import ApiError

logger = logging.getLogger(__name__)

#: How many job ids either side of the first job recorded after a lost start to read.
PROBE_SPAN = 8
#: How far Bambuddy's clock may be from ours.
CLOCK_SKEW = timedelta(minutes=2)
#: A lost start's job is created within this of it: a run starts again after a minute
#: or two (``printing.SLICE_BACKOFF``), and only jobs recorded this soon after it are
#: its neighbours.
NEIGHBOURS_WITHIN = timedelta(minutes=30)
#: Past this a lost start is finished with, whatever was found: Bambuddy has dropped a
#: job finished 30 minutes before.
GIVE_UP_AFTER = timedelta(minutes=45)
#: How long a recorded slice start is kept (#2911): well past the longest a lost start
#: is looked at (:data:`GIVE_UP_AFTER`) and the window it looks back and ahead over
#: (:data:`NEIGHBOURS_WITHIN`), so pruning never makes a recorded job look unrecorded.
STARTS_KEPT = timedelta(days=1)


async def sweep_lost_slices(
    client: BambuddyClient, uploads: BambuddyUploadStore, *, now: datetime | None = None
) -> int:
    """Remove the sliced files of jobs that lost slice starts left; how many. A lost
    start whose job may still be slicing is kept for the next sweep."""
    now = now or datetime.now(UTC)
    # Answered starts and swept lost ones are read only beside a lost start (#2911).
    await uploads.prune_slice_starts(now - STARTS_KEPT)
    removed = 0
    for lost in await uploads.lost_starts():
        try:
            settled, count = await _sweep(client, uploads, lost)
        except ApiError as error:
            logger.warning(
                "could not look for a lost slice start's job; trying again next sweep",
                extra={"library_file_id": lost.library_file_id, "status": error.status},
            )
            settled, count = False, 0
        removed += count
        if settled or now - lost.created_at > GIVE_UP_AFTER:
            await uploads.finish_lost_start(lost.id)
    return removed


async def _sweep(
    client: BambuddyClient, uploads: BambuddyUploadStore, lost: LostStart
) -> tuple[bool, int]:
    """Whether ``lost`` is settled (every candidate job read and finished), and how many
    files were removed for it."""
    after = await uploads.slice_jobs_after(lost.created_at, NEIGHBOURS_WITHIN)
    if not after:
        # Nothing started since, so nothing to look beside yet: the run is still
        # waiting to start again, or the start that answered was recorded late.
        return False, 0
    first = min(after)
    candidates = [
        job_id
        for job_id in range(max(1, first - PROBE_SPAN), first + PROBE_SPAN + 1)
        if job_id not in after
    ]
    recorded = await uploads.slice_jobs_recorded(candidates, lost.created_at - NEIGHBOURS_WITHIN)
    settled, removed = True, 0
    for job_id in candidates:
        if job_id in recorded:
            continue
        try:
            found = await _remove_if_lost(client, lost, job_id)
        except ApiError as error:
            # This job is looked at again next sweep; the files already removed for
            # this lost start still count (#2911).
            logger.warning(
                "could not look at a job beside a lost slice start; trying again next sweep",
                extra={"slice_job_id": job_id, "status": error.status},
            )
            settled = False
            if _bambuddy_failing(error):
                # The jobs after it would fail the same way: one warning per lost
                # start, not one per candidate (#3013 review).
                break
            continue
        if found is None:
            settled = False
        elif found:
            removed += 1
    return settled, removed


def _bambuddy_failing(error: ApiError) -> bool:
    """Whether ``error`` says Bambuddy itself is failing or unreachable (a 5xx, which
    is also how the client reports no answer, or a 429), not this one job."""
    return error.status >= status.HTTP_500_INTERNAL_SERVER_ERROR or (
        error.status == status.HTTP_429_TOO_MANY_REQUESTS
    )


async def _remove_if_lost(client: BambuddyClient, lost: LostStart, job_id: int) -> bool | None:
    """Whether ``job_id`` was ``lost``'s and its file removed; ``None`` while it is still
    slicing, so the lost start is looked at again."""
    try:
        job = await client.slice_job(job_id)
    except ApiError as error:
        if error.status == status.HTTP_404_NOT_FOUND:
            return False
        raise
    if job.kind != "library_file" or job.source_id != lost.library_file_id:
        return False
    created = _parsed(job.created_at)
    if (
        created is None
        or created < lost.created_at - CLOCK_SKEW
        or created > lost.created_at + NEIGHBOURS_WITHIN
    ):
        return False
    if not job.finished:
        return None
    result = job.result
    if job.status != "completed" or result is None or result.library_file_id is None:
        return False
    if not await _remove(client, result.library_file_id, result.name):
        return False
    logger.info(
        "removed the file a lost slice start sliced to",
        extra={"slice_job_id": job_id, "library_file_id": result.library_file_id},
    )
    return True


async def _remove(client: BambuddyClient, file_id: int, name: str | None) -> bool:
    """Delete ``file_id`` while it still carries the name its job gave it, so nothing
    else that took the id goes (as the background-slice sweep does)."""
    if name is None:
        return False
    try:
        made = await client.library_file(file_id)
    except ApiError as error:
        if error.status == status.HTTP_404_NOT_FOUND:
            return False
        raise
    if made.filename != name:
        return False
    return await client.delete_library_file(file_id)


def _parsed(value: str | None) -> datetime | None:
    """Bambuddy's ``created_at``: ISO 8601, UTC when it names no zone."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
