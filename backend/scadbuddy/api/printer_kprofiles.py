"""A registry printer's K-profiles (pressure advance) over ScadBuddy's own LAN link (#7276,
plan ``docs/superpowers/plans/2026-11-25-filament-hardware-off-bambuddy.md`` Task 7; the
routes are #7154's).

The reads and writes are MQTT commands to the printer (`printers/lan_commands.py`), run
by the ``LanCommand`` workflow on the ``bambuddy`` queue. Each command route takes the
LAN row first (409 ``not_lan_linked``), refuses a printer whose stored report is not fresh
(409 ``printer_offline``) unless its ``Idempotency-Key`` already started the command (P38),
runs its commands and answers through `command_response`: 200 with the command's view
(`native.COMMAND_VIEWS`), or 202 with the run's workflow id when the printer is slow (P27;
follow it at ``GET /printers/{id}/commands/{command_id}``, which answers the same view,
P37). A write that may have reached the printer is
``confirmed: false``, never a failure (P30). The notes are ScadBuddy's own
(`printers/command_data.py`); no route here calls Bambuddy.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Response, status

from scadbuddy.api.operations import KEY_REQUIRED_DETAIL, KEY_REQUIRED_PROBLEM, IdempotencyKey
from scadbuddy.bambuddy.models import (
    KProfileNotes,
    KProfileTable,
    KProfileWriteResult,
    PrinterStatus,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.printers.component import LanCommandsDep, PrintersDep
from scadbuddy.printers.kprofiles import (
    NOTE_KEY_MAX,
    KProfileBatch,
    KProfileNoteSet,
    KProfileRemove,
    KProfileSet,
    NoteKey,
    NozzleDiameter,
    set_rows,
)
from scadbuddy.printers.lan_command_models import (
    LanCommandInput,
    LanCommandPending,
    LanCommandRun,
)
from scadbuddy.printers.lan_commands import (
    extrusion_cali_del,
    extrusion_cali_get,
    extrusion_cali_set,
)
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.native import LanCommands, command_response, lan_row, live_status
from scadbuddy.printers.store import printer_uuid
from scadbuddy.workflows.print_models import LAN_REPLY_WAIT_READ, LAN_REPLY_WAIT_WRITE

router = APIRouter(prefix="/printers", tags=["printers"])

PATH = "/{printer_id}/kprofiles"
#: The placeholder ``sequence_id`` of a builder's payload: ``lan_command`` replaces it.
SEQ = "0"
FEATURE = "K-profiles"
COMMAND = (
    " ScadBuddy reaches the printer over its own LAN link, so the printer must be linked over"
    " the LAN (409 `not_lan_linked`) and heard from lately (409 `printer_offline`). 404 for an"
    " unknown printer. A command the printer is slow to answer is a 202 with a `workflow_id`:"
    " follow it at `GET /printers/{id}/commands/{command_id}`. 409 `printer_refused` when the"
    " printer refuses it, `printer_cert_changed` when it presents another certificate; 502"
    " `printer_unreachable`; 503 `lan_unavailable`."
)
WRITES = (
    " Send an `Idempotency-Key`: a key sent again joins the run it started or reads its"
    " outcome, and the command is sent once, even when the printer has not been heard from"
    " since." + COMMAND
)
IGNORED = (
    " `n_coef`, `ams_id` and `tray_id` are accepted and ignored: the printer gets"
    ' `"0.000000"`, 0 and 0, as Bambuddy sends them (single-nozzle firmware refuses a'
    " `tray_id` of -1 while it applies the write)."
)
ONCE = (
    " Sent once: check the list before sending one again whose answer was lost. `confirmed` is"
    " false when the printer did not acknowledge it in time."
)
ACCEPTED: dict[int | str, dict[str, Any]] = {status.HTTP_202_ACCEPTED: {"model": LanCommandPending}}


def _key(idempotency_key: str | None) -> str:
    if not idempotency_key:
        raise ApiError(
            status.HTTP_428_PRECONDITION_REQUIRED, KEY_REQUIRED_DETAIL, type_=KEY_REQUIRED_PROBLEM
        )
    return idempotency_key


async def _reach(printers: PrintersDep, printer_id: str) -> tuple[PrinterRow, PrinterStatus]:
    """The LAN row (404, 409 ``not_lan_linked``) and its status from the stored report."""
    row = await lan_row(printers.store, printer_id, feature=FEATURE)
    return row, await live_status(printers.reports, row, now=datetime.now(UTC))


async def _send(
    commands: LanCommands,
    row: PrinterRow,
    seen: PrinterStatus,
    run: LanCommandRun,
    key: str | None = None,
) -> Response:
    """Run ``run`` once the report is fresh (P36), and answer its view. A key that already
    started it skips the freshness check (P38): the resend reads that run, never sends."""
    if not seen.connected and not await commands.exists(row.id, run, key):
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"{row.name} has not been heard from lately, so its {FEATURE} cannot be reached.",
            code="printer_offline",
        )
    return command_response(await commands.run(row.id, run, key=key), run)


def _write(row: PrinterRow, name: str, payload: dict[str, object]) -> LanCommandRun:
    return LanCommandRun(
        commands=[
            LanCommandInput(
                printer_id=row.id,
                name=name,
                payload=payload,
                wait_s=LAN_REPLY_WAIT_WRITE,
                kind="write",
            )
        ]
    )


@router.get(
    PATH,
    response_model=KProfileTable,
    responses=ACCEPTED,
    summary="A printer's K-profiles",
    description=(
        "The K table the printer holds for one nozzle diameter, read over ScadBuddy's LAN link"
        " (`extrusion_cali_get`, tried three times). 502 `printer_no_answer` when the printer"
        " does not answer." + COMMAND
    ),
)
async def get_printer_kprofiles(
    printers: PrintersDep,
    commands: LanCommandsDep,
    printer_id: str,
    nozzle_diameter: Annotated[NozzleDiameter, Query()] = "0.4",
) -> Response:
    row, seen = await _reach(printers, printer_id)
    run = LanCommandRun(
        commands=[
            LanCommandInput(
                printer_id=row.id,
                name="extrusion_cali_get",
                payload=extrusion_cali_get(nozzle_diameter, SEQ),
                wait_s=LAN_REPLY_WAIT_READ,
                kind="read",
            )
        ]
    )
    return await _send(commands, row, seen, run)


async def _notes_owner(printers: PrintersDep, printer_id: str) -> str:
    """The printer's canonical id: 404 for an unknown one. Notes are ScadBuddy's own, so
    the printer needs no LAN link."""
    key = printer_uuid(printer_id)
    row = None
    if key is not None:
        row = await asyncio.to_thread(printers.store.get, printer_id)
    if row is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no printer {printer_id}")
    return row.id


@router.get(
    f"{PATH}/notes",
    response_model=KProfileNotes,
    summary="The notes on a printer's K-profiles",
    description=(
        "ScadBuddy keeps them, not the printer: each note by its key (the profile's"
        " `setting_id`, else `slot_<slot>_<filament>_<extruder>`, else"
        " `name_<name>_<filament>`). 404 for an unknown printer."
    ),
)
async def get_printer_kprofile_notes(printers: PrintersDep, printer_id: str) -> KProfileNotes:
    owner = await _notes_owner(printers, printer_id)
    notes = await asyncio.to_thread(printers.command_data.notes, printer_uuid(owner))  # type: ignore[arg-type]
    return KProfileNotes(notes=notes)


@router.post(
    PATH,
    response_model=KProfileWriteResult,
    responses=ACCEPTED,
    summary="Add or edit one of a printer's K-profiles",
    description=(
        "A `slot_id` of 0 adds a profile; above 0 edits the profile in that slot. `k_value`"
        " is sent with six decimal places." + IGNORED + ONCE + WRITES
    ),
)
async def set_printer_kprofile(
    printers: PrintersDep,
    commands: LanCommandsDep,
    printer_id: str,
    body: KProfileSet,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    key = _key(idempotency_key)
    row, seen = await _reach(printers, printer_id)
    payload = extrusion_cali_set(set_rows([body]), body.nozzle_diameter, SEQ)
    return await _send(commands, row, seen, _write(row, "extrusion_cali_set", payload), key)


@router.post(
    f"{PATH}/batch",
    response_model=KProfileWriteResult,
    responses=ACCEPTED,
    summary="Add several K-profiles to a printer in one command",
    description=(
        "1 to 16 profiles of one nozzle diameter, sent to the printer in one command: a new"
        " profile on both extruders of a dual-nozzle printer; each new profile gets its own"
        " `setting_id`, as Bambuddy's batch makes them." + IGNORED + ONCE + WRITES
    ),
)
async def set_printer_kprofiles_batch(
    printers: PrintersDep,
    commands: LanCommandsDep,
    printer_id: str,
    body: KProfileBatch,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    key = _key(idempotency_key)
    row, seen = await _reach(printers, printer_id)
    payload = extrusion_cali_set(set_rows(body.profiles), body.profiles[0].nozzle_diameter, SEQ)
    return await _send(commands, row, seen, _write(row, "extrusion_cali_set", payload), key)


@router.post(
    f"{PATH}/delete",
    response_model=KProfileWriteResult,
    responses=ACCEPTED,
    summary="Delete one of a printer's K-profiles",
    description=(
        "`extrusion_cali_del` for the profile's slot and what names it, as the list answers"
        " them; a dual-nozzle printer takes the extruder form." + ONCE + WRITES
    ),
)
async def delete_printer_kprofile(
    printers: PrintersDep,
    commands: LanCommandsDep,
    printer_id: str,
    body: KProfileRemove,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    key = _key(idempotency_key)
    row, seen = await _reach(printers, printer_id)
    payload = extrusion_cali_del(
        cali_idx=body.slot_id,
        filament_id=body.filament_id,
        nozzle_id=body.nozzle_id,
        nozzle_diameter=body.nozzle_diameter,
        extruder_id=body.extruder_id,
        setting_id=body.setting_id,
        dual_nozzle=len(seen.nozzles) > 1,
        seq=SEQ,
    )
    return await _send(commands, row, seen, _write(row, "extrusion_cali_del", payload), key)


@router.put(
    f"{PATH}/notes",
    response_model=KProfileWriteResult,
    summary="Set the note on one of a printer's K-profiles",
    description=(
        "At most 2,000 characters; a blank note deletes it. `setting_id` is the note's key"
        f" (1 to {NOTE_KEY_MAX} printable characters, no `/`, not `.` or `..`). An upsert:"
        " an `Idempotency-Key` is accepted and ignored. 404 for an unknown printer."
    ),
)
async def set_printer_kprofile_note(
    printers: PrintersDep,
    printer_id: str,
    body: KProfileNoteSet,
    idempotency_key: IdempotencyKey = None,
) -> KProfileWriteResult:
    owner = await _notes_owner(printers, printer_id)
    await asyncio.to_thread(
        printers.command_data.set_note,
        printer_uuid(owner),  # type: ignore[arg-type]
        body.setting_id,
        body.note,
    )
    return KProfileWriteResult(success=True)


@router.delete(
    f"{PATH}/notes/{{setting_id}}",
    response_model=KProfileWriteResult,
    summary="Delete the note on one of a printer's K-profiles",
    description=(
        "A note that is not there is deleted all the same. 422 for a key no note can have"
        " (`.`, `..`, or longer than the PUT allows). An `Idempotency-Key` is accepted and"
        " ignored. 404 for an unknown printer."
    ),
)
async def delete_printer_kprofile_note(
    printers: PrintersDep,
    printer_id: str,
    setting_id: Annotated[NoteKey, Path()],
    idempotency_key: IdempotencyKey = None,
) -> KProfileWriteResult:
    owner = await _notes_owner(printers, printer_id)
    await asyncio.to_thread(
        printers.command_data.delete_note,
        printer_uuid(owner),  # type: ignore[arg-type]
        setting_id,
    )
    return KProfileWriteResult(success=True)
