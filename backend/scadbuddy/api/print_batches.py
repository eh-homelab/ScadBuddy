"""Batch print (#2672): one stepped print dialog over several library files.

The batch, each file's choices and the step in view live in Postgres
(:mod:`scadbuddy.batches.store`), so a reload mid-batch resumes where it was left.
Queue all starts each chosen file's ordinary library run (``POST
/print/library/{file_id}/run``) in turn, so every refusal and every queue rule is the
single-file print's, and reports what happened to each.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Path, Response, status
from pydantic import BaseModel, Field

from scadbuddy.activity.component import ACTIVITY, ActivityComponent
from scadbuddy.api.components import component_dep
from scadbuddy.api.deps import PathsDep, PrintRunsDep, ReadBudgetDep, SettingsStoreDep
from scadbuddy.api.library_print import post_library_run
from scadbuddy.api.operations import MAY_HAVE_STARTED
from scadbuddy.bambuddy.runs import PrintRunStore
from scadbuddy.batches.component import PrintBatchesDep
from scadbuddy.batches.store import (
    BatchChoice,
    BatchItem,
    BatchOutcome,
    NewBatchFile,
    PrintBatch,
    PrintBatchStore,
)
from scadbuddy.core.problems import ApiError

ActivityDep = Annotated[ActivityComponent, component_dep(ACTIVITY)]

router = APIRouter(prefix="/print/batches", tags=["print"])

#: Most files one batch takes: each is a step a person walks through.
MAX_BATCH_FILES = 50

BatchIdPath = Annotated[str, Path(min_length=1, max_length=64)]
PositionPath = Annotated[int, Path(ge=0)]


class NewBatch(BaseModel):
    files: list[NewBatchFile] = Field(min_length=1, max_length=MAX_BATCH_FILES)


class BatchStep(BaseModel):
    step: int = Field(ge=0)


async def _batch(store: PrintBatchStore, batch_id: str) -> PrintBatch:
    batch = await store.get(batch_id)
    if batch is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "There is no such batch print.")
    return batch


def _item(batch: PrintBatch, position: int) -> BatchItem:
    for item in batch.items:
        if item.position == position:
            return item
    raise ApiError(status.HTTP_404_NOT_FOUND, "The batch print has no such file.")


def _settled(batch: PrintBatch) -> bool:
    """Whether Queue all has nothing left to do: every file kept has a started run."""
    return all(
        item.outcome is not None and item.outcome.status == "started"
        for item in batch.items
        if not (item.skipped or item.removed)
    )


CHANGE_REFUSED = "This file's print has started, or may have, so its choices cannot change."


def _changeable(item: BatchItem) -> None:
    """A started run is Bambuddy's now, and a pending one may be: changing its choices
    would send another request and could print the file twice."""
    if item.outcome is not None and item.outcome.status in ("started", "pending"):
        raise ApiError(status.HTTP_409_CONFLICT, CHANGE_REFUSED)


async def _read_runs(
    batches: PrintBatchStore, runs: PrintRunStore, batch: PrintBatch
) -> PrintBatch:
    """A started file whose run then failed (a slice or an upload, after the 202) is
    ``failed``: it can be edited and sent again, as a new run (#2718 review). A run that
    failed after it tried to queue (``may_have_queued``) stays started, as it may be on
    Bambuddy's queue, and sending it again could print it twice."""
    changed = False
    for item in batch.items:
        outcome = item.outcome
        if outcome is None or outcome.status != "started" or outcome.run_id is None:
            continue
        run = await runs.get(outcome.run_id)
        if run is None or run.status != "failed" or run.may_have_queued:
            continue
        failed = BatchOutcome(
            status="failed",
            run_id=outcome.run_id,
            detail=run.error.detail if run.error is not None else None,
            attempt=outcome.attempt,
        )
        changed |= await batches.record_outcome(batch.id, item.position, failed, if_unchanged=item)
    return await _batch(batches, batch.id) if changed else batch


def _open(batch: PrintBatch) -> None:
    if batch.status != "open":
        raise ApiError(
            status.HTTP_409_CONFLICT, "This batch print is queued already, so it cannot change."
        )


@router.post(
    "",
    response_model=PrintBatch,
    status_code=status.HTTP_201_CREATED,
    summary="Start a batch print of library files",
)
async def post_batch(body: NewBatch, batches: PrintBatchesDep) -> PrintBatch:
    """A batch over ``files``, in that order, opened on the first file's step."""
    ids = [file.library_file_id for file in body.files]
    if len(set(ids)) != len(ids):
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "A file is in the batch twice.")
    return await batches.create(body.files)


@router.get("/{batch_id}", response_model=PrintBatch, summary="A batch print, to resume it")
async def get_batch(
    batch_id: BatchIdPath, batches: PrintBatchesDep, runs: PrintRunsDep
) -> PrintBatch:
    return await _read_runs(batches, runs.store, await _batch(batches, batch_id))


@router.put("/{batch_id}/step", response_model=PrintBatch, summary="The step a batch print shows")
async def put_batch_step(
    batch_id: BatchIdPath, body: BatchStep, batches: PrintBatchesDep
) -> PrintBatch:
    """The step in view: a file's position, or the file count for the review. A queued
    batch's step no longer moves, as its files no longer change."""
    batch = await _batch(batches, batch_id)
    _open(batch)
    if body.step > len(batch.items):
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "The batch print has no such step.")
    updated = await batches.set_step(batch_id, body.step)
    assert updated is not None
    return updated


@router.put(
    "/{batch_id}/items/{position}",
    response_model=PrintBatch,
    summary="One file's choices in a batch print",
)
async def put_batch_item(
    batch_id: BatchIdPath,
    position: PositionPath,
    body: BatchChoice,
    batches: PrintBatchesDep,
    runs: PrintRunsDep,
) -> PrintBatch:
    """What the file's step chose (the run request it would send), or that it is skipped
    or removed, and with ``step`` the step to show next, in the same write. Nothing is
    checked or queued here: Queue all runs each file's own print."""
    batch = await _read_runs(batches, runs.store, await _batch(batches, batch_id))
    _open(batch)
    _changeable(_item(batch, position))
    if body.step is not None and body.step > len(batch.items):
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "The batch print has no such step.")
    updated = await batches.choose(batch_id, position, body)
    if updated is None:
        # Queue all marked it pending, or started it, since the read above.
        raise ApiError(status.HTTP_409_CONFLICT, CHANGE_REFUSED)
    return updated


@router.post(
    "/{batch_id}/queue",
    response_model=PrintBatch,
    summary="Queue every chosen file of a batch print",
)
async def post_batch_queue(
    batch_id: BatchIdPath,
    batches: PrintBatchesDep,
    runs: PrintRunsDep,
    activity: ActivityDep,
    store: SettingsStoreDep,
    paths: PathsDep,
    budget: ReadBudgetDep,
) -> PrintBatch:
    """Starts each file's library run, in order, and records what happened to each: the
    run it started (follow it at ``GET /print/runs/{id}``), the refusal, or that it has
    no choices yet. Skipped and removed files are left out. One file's refusal never
    stops the rest. Sent again, a file whose run started is left as it is, and one whose
    choices are unchanged re-attaches to its run rather than queueing it twice.

    The batch stays open, so it can be sent again and its refused files edited, until
    every file kept has a started run."""
    batch = await _read_runs(batches, runs.store, await _batch(batches, batch_id))
    for item in batch.items:
        if item.skipped or item.removed:
            continue
        if item.outcome is not None and item.outcome.status == "started":
            continue
        attempt = _attempt(item)
        # Pending while it is sent, so a PUT of its choices cannot land under the send:
        # the choices sent are the ones recorded (#2718 review).
        sending = BatchOutcome(status="pending", attempt=attempt)
        if not await batches.record_outcome(batch_id, item.position, sending, if_unchanged=item):
            continue  # its choices changed since the read: the next Queue all sends them
        outcome = await _queue_one(batch_id, item, attempt, runs, activity, store, paths, budget)
        await batches.record_outcome(batch_id, item.position, outcome)
    done = await _batch(batches, batch_id)
    if not _settled(done):
        return done
    await batches.mark_queued(batch_id)
    return await _batch(batches, batch_id)


def _attempt(item: BatchItem) -> int:
    """A failed run's file is sent as a new run, never re-attached to the failed one."""
    if item.outcome is None:
        return 0
    return item.outcome.attempt + (item.outcome.status == "failed")


async def _queue_one(
    batch_id: str,
    item: BatchItem,
    attempt: int,
    runs: PrintRunsDep,
    activity: ActivityDep,
    store: SettingsStoreDep,
    paths: PathsDep,
    budget: ReadBudgetDep,
) -> BatchOutcome:
    if item.request is None:
        return BatchOutcome(status="unchosen", detail="No choices were made for this file.")
    request_id = batch_request_id(batch_id, item, attempt)
    request = item.request.model_copy(update={"request_id": request_id})
    try:
        run = await post_library_run(
            item.library_file_id, request, Response(), runs, activity, store, paths, budget
        )
    except ApiError as error:
        # Not a refusal: ScadBuddy still checking (503) or Temporal down, when the run
        # may have started. The same request_id sent again follows that run.
        if error.status == status.HTTP_503_SERVICE_UNAVAILABLE or error.extensions.get(
            MAY_HAVE_STARTED
        ):
            return BatchOutcome(status="pending", detail=error.detail, attempt=attempt)
        return BatchOutcome(status="refused", detail=error.detail, attempt=attempt)
    return BatchOutcome(status="started", run_id=run.id, attempt=attempt)


def batch_request_id(batch_id: str, item: BatchItem, attempt: int = 0) -> str:
    """One press per file and choice: the same choices sent again are the same run
    (#742), while changed choices after a refusal, or any send after a failed run, are a
    new one."""
    if item.request is None:
        raise ValueError(f"batch file {item.position} has no choices to send")
    chosen = item.request.model_dump(mode="json", exclude={"request_id"}, exclude_unset=True)
    digest = hashlib.sha256(json.dumps(chosen, sort_keys=True).encode()).hexdigest()[:16]
    retry = f":{attempt}" if attempt else ""
    return f"batch:{batch_id}:{item.position}:{digest}{retry}"


@router.delete("/{batch_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Drop a batch print")
async def delete_batch(batch_id: BatchIdPath, batches: PrintBatchesDep) -> None:
    """Forgets the batch. Nothing it queued is touched: that is Bambuddy's queue."""
    if not await batches.delete(batch_id):
        raise ApiError(status.HTTP_404_NOT_FOUND, "There is no such batch print.")
