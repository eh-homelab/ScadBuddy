"""The render worker's CRUD routes (#5298): the store settings it renders with (inventory
R2), for `crud/render.py` `RemoteRenderSettingsSource`, and its jobs' rows (R3-R10), for
`RemoteJobProjection`.

The settings answer holds secrets in plaintext, released only to ``crud.render`` (plan D8): the
render key (or the full key while no render key is saved) and the S3 keys. The CRUD
router answers ``Cache-Control: no-store``, and each answer is logged with the secrets it
released and the credential it released them to, never their values.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import Request, status
from psycopg import Connection
from psycopg.rows import DictRow
from pydantic import BaseModel

from scadbuddy.api.deps import StateDep
from scadbuddy.auth.gate import caller_of
from scadbuddy.auth.models import Service
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router
from scadbuddy.crud.idempotency import IdempotencyKey, Replay, idempotent_write
from scadbuddy.library.settings_store import RenderStoreSettings, load_render_store_settings
from scadbuddy.render.inputs import legacy_inputs
from scadbuddy.render.job_models import Job, JobNotFoundError, QueueFullError, StepInfo
from scadbuddy.render.projection import PROJECT_POOL_TIMEOUT

logger = logging.getLogger(__name__)

router = crud_router("render")
#: A full queue's refusal of an accept: `crud/render.py` `QUEUE_FULL`.
QUEUE_FULL = "queue-full"
#: The finish's name in the idempotency ledger.
FINISH_ROUTE = "POST /jobs/{job_id}/finish"


def released(current: RenderStoreSettings) -> list[str]:
    """The names of the secrets in ``current``, for the log."""
    names = []
    if current.api_key is not None:
        names.append("bambuddy_api_key" if current.key_is_fallback else "bambuddy_render_api_key")
    if current.s3 is not None:
        names += ["store_s3_access_key_id", "store_s3_secret_access_key"]
    return names


@router.get("/settings", name="crud_render_settings")
async def render_settings(request: Request, state: StateDep) -> RenderStoreSettings:
    current = await asyncio.to_thread(
        load_render_store_settings, state.settings_store.pool, state.settings
    )
    # The guard admitted only a render `Service`.
    principal = caller_of(request.scope)
    assert isinstance(principal, Service)
    logger.info(
        "released the render settings to service credential %s, secrets: %s",
        principal.credential_id,
        ", ".join(released(current)) or "none",
    )
    return current


# -- the jobs' rows (inventory R3-R10) -------------------------------------------
#
# Each route is the API's own `JobProjection` method: a write that changes a row's state
# commits its activity row and its ``job.*`` event in the same transaction, the event
# log's lock taken first (`core/pg_events.py`, "Lock order"), as the worker's SQL did. So
# an older worker still writing over SQL during a roll takes the locks in the same order.
# Every write but the finish is natural (plan D5): sent again, it answers as it did the
# first time. The finish takes an Idempotency-Key, for its ``moved``.


class JobAccept(BaseModel):
    job: Job
    key: str
    workflow_id: str
    run_id: str
    max_pending: int = 0
    supersedes: str | None = None


class Claims(BaseModel):
    claims: int


class Settled(BaseModel):
    settled: bool
    moved: bool


def _no_such() -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, "No such render job.")


@router.post("/jobs/accept", name="crud_render_accept_job")
async def accept_job(body: JobAccept, state: StateDep) -> Job:
    """R3: the execution's row, inserted or found by its workflow ids. A full queue is a
    409 ``queue-full`` with nothing written."""
    job = body.job
    try:
        held = await asyncio.to_thread(
            state.projection.accept,
            job,
            body.key,
            workflow_id=body.workflow_id,
            run_id=body.run_id,
            max_pending=body.max_pending,
            supersedes=body.supersedes,
        )
    except QueueFullError as error:
        raise ApiError(
            status.HTTP_409_CONFLICT, str(error), reason=QUEUE_FULL, depth=error.depth
        ) from None
    # A resend finds the row its first send inserted; its request is the same. Not the
    # job id: a retried activity mints a new one. Any other row, found by naming another
    # execution's workflow ids, is answered as a missing one.
    asked = (job.slug, job.kind, job.model_version, job.inputs or legacy_inputs(job.params))
    if (held.slug, held.kind, held.model_version, held.inputs) != asked:
        raise _no_such()
    return held


@router.put("/jobs/{job_id}/claims", name="crud_render_job_claims")
async def set_claims(job_id: str, body: Claims, state: StateDep) -> None:
    """R4: the workflow's claim count, while the row is open."""
    await asyncio.to_thread(state.projection.set_claims, job_id, body.claims)


@router.post("/jobs/{job_id}/started", name="crud_render_job_started")
async def mark_started(job_id: str, state: StateDep) -> Job | None:
    """R5: the row moved from pending to running, or null when it was not pending."""
    return await asyncio.to_thread(state.projection.mark_started, job_id)


@router.put("/jobs/{job_id}/steps", name="crud_render_job_steps")
async def set_steps(job_id: str, body: list[StepInfo], state: StateDep) -> None:
    """R6: the pipeline's steps, while the row is open."""
    await asyncio.to_thread(state.projection.set_steps, job_id, body)


@router.post("/jobs/{job_id}/finish", name="crud_render_finish_job")
async def finish_job(job_id: str, body: Job, key: IdempotencyKey, state: StateDep) -> Settled:
    """R7. ``settled``: whether the row holds this settle, true again for a resend that
    finds the row already holding this terminal state and ``finished_at``. ``moved``:
    whether this write changed the row, the only one that announces it and that the
    worker counts. The write takes an ``Idempotency-Key``, one per call of the worker's
    `finish`: a resend inside that call replays its ``moved``; a later call, a retried
    activity's, gets false."""
    if body.id != job_id:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "The job is not this route's.")
    if body.state not in ("done", "failed", "cancelled"):
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"A job does not finish as {body.state}."
        )
    projection = state.projection
    wire = body.model_dump(mode="json")

    def write(conn: Connection[DictRow]) -> Replay:
        moved = projection.finish_in(conn, body)
        settled = moved
        if not moved:
            try:
                held = projection.read_in(conn, job_id)
            except JobNotFoundError:
                held = None
            settled = held is not None and (held.state, held.finished_at) == (
                body.state,
                body.finished_at,
            )
        return Replay(200, {"settled": settled, "moved": moved})

    def run() -> Replay:
        with projection.pool.connection(timeout=PROJECT_POOL_TIMEOUT) as conn:
            return idempotent_write(conn, "render", key, FINISH_ROUTE, wire, lambda: write(conn))

    reply = await asyncio.to_thread(run)
    return Settled.model_validate(reply.body)


@router.get("/jobs/{job_id}", name="crud_render_get_job")
async def read_job(job_id: str, state: StateDep) -> Job:
    """R8."""
    try:
        return await asyncio.to_thread(state.projection.read, job_id)
    except JobNotFoundError:
        raise _no_such() from None
