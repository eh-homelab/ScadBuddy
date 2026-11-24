"""The profile-fetch worker's CRUD routes (#6625, plan 2026-11-18 Decision 7): under
``/api/v1/internal/crud/profile-fetch/``, for the ``profile-fetch`` credential only
(`crud_router`). Area ``runs`` (`filament/profile_fetch.py`):

- ``PUT …/runs/{run_id}/files/{sha256}?printer_model=&nozzle=``: one target's preset file,
  the raw bytes in the body, named by their sha256; 204;
- ``POST …/runs/{run_id}/import`` (``Idempotency-Key``, 428 without): the run's files
  kept as the type's ScadBuddy presets and made its presets, then mirrored to Bambuddy
  after the commit while ``filament_preset_mirror`` is on (#7176); the `ImportOutcome`;
- ``POST …/runs/{run_id}/failed``: the workflow gave up; 204.

Each answers 404 ``run_not_found`` for no such run, and the file and ``failed`` routes 409
``not_running`` for a run that is not ``running`` (the import answers an ended run's
outcome instead). So the worker can only add files to a fetch the API opened.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Path, Query, Request, status
from pydantic import BaseModel

from scadbuddy.api.deps import StateDep
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router
from scadbuddy.crud.idempotency import MaybeIdempotencyKey, required_key
from scadbuddy.filament.profile_fetch import import_run, mark_failed, store_file
from scadbuddy.profile_fetch.wire import MAX_FILE_BYTES, ExportFailure, FailedAsk, ImportOutcome

router = crud_router("profile-fetch")

Sha256 = Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")]
TOO_LARGE = "a preset file is at most 512 KiB"


class ImportAsk(BaseModel):
    """``POST …/runs/{run_id}/import``: the targets the worker could not export."""

    failures: list[ExportFailure]


@router.put(
    "/runs/{run_id}/files/{sha256}",
    status_code=status.HTTP_204_NO_CONTENT,
    name="crud_profile_fetch_file_put",
)
async def file_put(
    run_id: UUID,
    sha256: Sha256,
    request: Request,
    state: StateDep,
    printer_model: Annotated[str, Query(min_length=1, max_length=64)],
    nozzle: Annotated[str, Query(min_length=1, max_length=8)],
) -> None:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_FILE_BYTES:
        raise ApiError(status.HTTP_413_CONTENT_TOO_LARGE, TOO_LARGE)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_FILE_BYTES:
            raise ApiError(status.HTTP_413_CONTENT_TOO_LARGE, TOO_LARGE)
    await store_file(state, run_id, sha256, printer_model, nozzle, bytes(data))


@router.post("/runs/{run_id}/import", name="crud_profile_fetch_import")
async def run_import(
    run_id: UUID,
    body: ImportAsk,
    state: StateDep,
    idempotency_key: MaybeIdempotencyKey = None,
) -> ImportOutcome:
    return await import_run(state, run_id, required_key(idempotency_key), body.failures)


@router.post(
    "/runs/{run_id}/failed",
    status_code=status.HTTP_204_NO_CONTENT,
    name="crud_profile_fetch_failed",
)
async def run_failed(run_id: UUID, body: FailedAsk, state: StateDep) -> None:
    await mark_failed(state, run_id, body.reason)
