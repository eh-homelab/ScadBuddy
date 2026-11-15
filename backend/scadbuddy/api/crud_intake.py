"""The intake worker's CRUD routes (#6133, spec 2026-11-13 §7): under
``/api/v1/internal/crud/intake/``, for the ``intake`` credential only (`crud_router`).
Area ``sources`` (`orders/storefront/intake.py`):

- ``POST …/sources/{id}/read``: a presigned GET (5 minutes) of the file source's staged
  file, its name, size and declared type;
- ``POST …/sources/{id}/preview``: a presigned PUT of its preview PNG, of the size asked;
- ``PUT …/sources/{id}/check`` (``Idempotency-Key``, 428 without): the verdict.

Each answers 404 ``source_not_found`` for no such file source and 409 ``not_checking`` for
one that no longer waits for a check (``…/check`` answers 200 with ``recorded`` false
instead, so a verdict that lost a race is no error). None of them reads a byte of the
file (#5295, #5296).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Path

from scadbuddy.api.deps import StateDep
from scadbuddy.crud.guard import crud_router
from scadbuddy.crud.idempotency import MaybeIdempotencyKey, required_key
from scadbuddy.intake_worker.wire import (
    CheckRecorded,
    CheckResult,
    PreviewAsk,
    PreviewSlot,
    SourceRead,
)
from scadbuddy.orders.storefront.intake import preview_slot, read_source, record_check

router = crud_router("intake")

SourceId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[0-9a-f-]+$")]


@router.post("/sources/{source_id}/read", name="crud_intake_source_read")
async def source_read(source_id: SourceId, state: StateDep) -> SourceRead:
    return await read_source(state, source_id)


@router.post("/sources/{source_id}/preview", name="crud_intake_source_preview")
async def source_preview(source_id: SourceId, body: PreviewAsk, state: StateDep) -> PreviewSlot:
    return await preview_slot(state, source_id, body)


@router.put("/sources/{source_id}/check", name="crud_intake_source_check")
async def source_check(
    source_id: SourceId,
    body: CheckResult,
    state: StateDep,
    idempotency_key: MaybeIdempotencyKey = None,
) -> CheckRecorded:
    return await record_check(state, source_id, required_key(idempotency_key), body)
