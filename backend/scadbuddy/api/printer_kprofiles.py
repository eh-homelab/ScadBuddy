"""A registry printer's K-profiles (pressure advance) through Bambuddy (#7154, plan
``docs/superpowers/plans/2026-11-23-printer-kprofiles.md``).

The printer is SB's registry row, linked to a Bambuddy printer (R-7154-2); the reads ask
Bambuddy (which asks the printer over MQTT), and every write is an operation on the
``bambuddy`` queue with an ``Idempotency-Key`` (R-7154-4). Bambuddy denies its K-profile
writes to every API key while its auth is on: then a write is the scope problem naming
``K-profiles (admin only)`` (R-7154-3).
"""

from __future__ import annotations

import asyncio
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Path, Query, Response
from fastapi.responses import JSONResponse

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    IdempotencyKey,
    operation_answer,
    run_operation,
)
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.kprofiles import (
    NOTE_KEY_MAX,
    KProfileBatch,
    KProfileNoteSet,
    KProfileRemove,
    KProfileSet,
    NoteKey,
    NozzleDiameter,
    linked_bambuddy_id,
)
from scadbuddy.bambuddy.models import KProfileNotes, KProfileTable, KProfileWriteResult
from scadbuddy.operations.component import OperationsDep
from scadbuddy.printers.component import PrintersDep

router = APIRouter(prefix="/printers", tags=["printers"])

PATH = "/{printer_id}/kprofiles"
WRITES = (
    " An operation on the `bambuddy` queue: send an `Idempotency-Key`. Bambuddy denies"
    " K-profile writes to every API key while its auth is on: then it is the scope problem"
    " naming `K-profiles (admin only)`. 404 for an unknown printer; 409 for a Bambuddy"
    " virtual printer or a printer not linked to Bambuddy."
)
ONCE = (
    " Sent once: Bambuddy dedupes no K-profile write, so check the list before sending one"
    " again whose answer was lost. `confirmed` is false when the printer did not acknowledge"
    " it in time."
)


def _subject(printer_id: str) -> str:
    return f"printer:{printer_id}:kprofiles"


@router.get(
    PATH,
    response_model=KProfileTable,
    summary="A printer's K-profiles",
    description=(
        "Bambuddy's `GET /printers/{id}/kprofiles/` for the Bambuddy printer the registry"
        " row links: the K table the printer holds for one nozzle diameter. Bambuddy asks"
        " the printer over MQTT; a printer it is not connected to (its 400), or one that"
        " does not answer (its 504), is a 502 with Bambuddy's reason. 404 for an unknown"
        " printer; 409 for a Bambuddy virtual printer"
        " or a printer not linked to Bambuddy."
    ),
)
async def get_printer_kprofiles(
    printers: PrintersDep,
    store: SettingsStoreDep,
    printer_id: str,
    nozzle_diameter: Annotated[NozzleDiameter, Query()] = "0.4",
) -> KProfileTable:
    bambuddy_id = await linked_bambuddy_id(printers.store, printer_id)
    async with client_for(await asyncio.to_thread(store.load)) as client:
        rows = await client.k_profiles(bambuddy_id, nozzle_diameter)
    return KProfileTable(profiles=rows, nozzle_diameter=nozzle_diameter)


@router.get(
    f"{PATH}/notes",
    response_model=KProfileNotes,
    summary="The notes on a printer's K-profiles",
    description=(
        "Bambuddy keeps them, not the printer: each note by its key (the profile's"
        " `setting_id`, else `slot_<slot>_<filament>_<extruder>`, else"
        " `name_<name>_<filament>`, as Bambuddy's page makes them)."
    ),
)
async def get_printer_kprofile_notes(
    printers: PrintersDep, store: SettingsStoreDep, printer_id: str
) -> KProfileNotes:
    bambuddy_id = await linked_bambuddy_id(printers.store, printer_id)
    async with client_for(await asyncio.to_thread(store.load)) as client:
        return await client.k_profile_notes(bambuddy_id)


@router.post(
    PATH,
    response_model=KProfileWriteResult,
    responses=OPERATION_RESPONSES,
    summary="Add or edit one of a printer's K-profiles",
    description=(
        "A `slot_id` of 0 adds a profile; above 0 edits the profile in that slot. `k_value`"
        " is sent with six decimal places." + ONCE + WRITES
    ),
)
async def set_printer_kprofile(
    response: Response,
    ops: OperationsDep,
    printer_id: str,
    body: KProfileSet,
    idempotency_key: IdempotencyKey = None,
) -> KProfileWriteResult | JSONResponse:
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["printer_kprofile_set"],
        subject=_subject(printer_id),
        request={"printer_id": printer_id, "row": body.model_dump(mode="json")},
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, KProfileWriteResult)


@router.post(
    f"{PATH}/batch",
    response_model=KProfileWriteResult,
    responses=OPERATION_RESPONSES,
    summary="Add several K-profiles to a printer in one command",
    description=(
        "1 to 16 profiles of one nozzle diameter, which Bambuddy sends to the printer in one"
        " command: a new profile on both extruders of a dual-nozzle printer." + ONCE + WRITES
    ),
)
async def set_printer_kprofiles_batch(
    response: Response,
    ops: OperationsDep,
    printer_id: str,
    body: KProfileBatch,
    idempotency_key: IdempotencyKey = None,
) -> KProfileWriteResult | JSONResponse:
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["printer_kprofile_batch"],
        subject=_subject(printer_id),
        request={
            "printer_id": printer_id,
            "rows": [row.model_dump(mode="json") for row in body.profiles],
        },
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, KProfileWriteResult)


@router.post(
    f"{PATH}/delete",
    response_model=KProfileWriteResult,
    responses=OPERATION_RESPONSES,
    summary="Delete one of a printer's K-profiles",
    description=(
        "Bambuddy's `DELETE /printers/{id}/kprofiles/` with this body: the profile's slot and"
        " what names it, as the list answers them." + ONCE + WRITES
    ),
)
async def delete_printer_kprofile(
    response: Response,
    ops: OperationsDep,
    printer_id: str,
    body: KProfileRemove,
    idempotency_key: IdempotencyKey = None,
) -> KProfileWriteResult | JSONResponse:
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["printer_kprofile_delete"],
        subject=_subject(printer_id),
        request={"printer_id": printer_id, "row": body.model_dump(mode="json")},
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, KProfileWriteResult)


@router.put(
    f"{PATH}/notes",
    response_model=KProfileWriteResult,
    responses=OPERATION_RESPONSES,
    summary="Set the note on one of a printer's K-profiles",
    description=(
        "At most 2,000 characters; a blank note deletes it. `setting_id` is the note's key"
        f" (1 to {NOTE_KEY_MAX} printable characters, no `/`)." + WRITES
    ),
)
async def set_printer_kprofile_note(
    response: Response,
    ops: OperationsDep,
    printer_id: str,
    body: KProfileNoteSet,
    idempotency_key: IdempotencyKey = None,
) -> KProfileWriteResult | JSONResponse:
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["printer_kprofile_note_set"],
        subject=_subject(printer_id),
        request={"printer_id": printer_id, "key": body.setting_id, "note": body.note},
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, KProfileWriteResult)


@router.delete(
    f"{PATH}/notes/{{setting_id}}",
    response_model=KProfileWriteResult,
    responses=OPERATION_RESPONSES,
    summary="Delete the note on one of a printer's K-profiles",
    description="A note that is not there is deleted all the same." + WRITES,
)
async def delete_printer_kprofile_note(
    response: Response,
    ops: OperationsDep,
    printer_id: str,
    setting_id: Annotated[NoteKey, Path()],
    idempotency_key: IdempotencyKey = None,
) -> KProfileWriteResult | JSONResponse:
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["printer_kprofile_note_delete"],
        subject=f"{_subject(printer_id)}:note:{quote(setting_id, safe='')}",
        request={"printer_id": printer_id, "key": setting_id},
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, KProfileWriteResult)
