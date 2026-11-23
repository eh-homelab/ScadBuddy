"""A printer's K-profiles (pressure advance) through Bambuddy (#7154, plan
``docs/superpowers/plans/2026-11-23-printer-kprofiles.md``).

The bodies SB accepts (R-7154-6), the linked Bambuddy printer a registry row names
(R-7154-2), and the writes as operations on the ``bambuddy`` queue (R-7154-4). Bambuddy's
shapes are ``backend/app/schemas/kprofile.py`` and its routes
``backend/app/api/routes/kprofiles.py`` (maziggy/bambuddy v1.3.1). Bambuddy dedupes no
K-profile write, so set, batch and delete run once; a note's set and delete are upserts
(R-7154-5). SB keeps no copy of the table or the notes (R-7154-8).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from fastapi import status
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from scadbuddy.bambuddy.client import client_for
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import SettingsReader
from scadbuddy.operations.kinds import OperationKind
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.store import PrinterStore, printer_uuid

#: The nozzles Bambu's printers take (R-7154-6).
NozzleDiameter = Literal["0.2", "0.4", "0.6", "0.8"]
#: The flow type and the diameter, as Bambu Studio's generator writes it: ``HS00-0.4``
#: is standard flow, ``HH00-0.4`` high flow (``KProfilesView.tsx``, v1.3.1). A printer may
#: report a flow code alone (``HH01``) or nothing at all (#1748 there), and a write names a
#: profile as the list answered it, so both are taken (review of #7173).
NOZZLE_ID = r"^([A-Z]{2}[0-9]{2}(-[0-9]\.[0-9])?)?$"
FILAMENT_ID = r"^[A-Za-z0-9_.+-]{1,64}$"
#: A ``setting_id`` as the list answers it, empty included.
SETTING_ID = r"^[A-Za-z0-9_.+-]{0,64}$"
N_COEF = r"^[0-9]{1,2}(\.[0-9]{1,6})?$"
#: A K value below this, with at most this many places (R-7154-6).
K_MAX = Decimal(10)
K_PLACES = 6
BATCH_MAX = 16
NOTE_MAX = 2000
NOTE_KEY_MAX = 256


def _printable(value: str) -> str:
    if any(not ch.isprintable() for ch in value):
        raise ValueError("must hold printable characters only")
    return value


def _k_value(value: str) -> str:
    """``k_value`` with six places, as Bambuddy's page sends it."""
    try:
        k = Decimal(value)
    except InvalidOperation:
        raise ValueError("must be a decimal number") from None
    exponent = k.as_tuple().exponent
    if not k.is_finite() or not isinstance(exponent, int):
        raise ValueError("must be a decimal number")
    if k < 0 or k >= K_MAX:
        raise ValueError(f"must be at least 0 and below {K_MAX}")
    if -exponent > K_PLACES:
        raise ValueError(f"has at most {K_PLACES} decimal places")
    return f"{k:.{K_PLACES}f}"


def _note_key(value: str) -> str:
    if "/" in value:
        raise ValueError("cannot hold a /")
    return _printable(value)


ProfileName = Annotated[str, Field(min_length=1, max_length=64), AfterValidator(_printable)]
KValue = Annotated[str, Field(min_length=1, max_length=16), AfterValidator(_k_value)]
NoteKey = Annotated[str, Field(min_length=1, max_length=NOTE_KEY_MAX), AfterValidator(_note_key)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KProfileSet(_Strict):
    """One profile to write: Bambuddy's ``KProfileCreate`` with SB's bounds. A
    ``slot_id`` of 0 adds a profile; above 0 edits the profile in that slot."""

    slot_id: int = Field(default=0, ge=0, le=10_000)
    extruder_id: int = Field(default=0, ge=0, le=1)
    nozzle_id: str = Field(pattern=NOZZLE_ID)
    nozzle_diameter: NozzleDiameter
    filament_id: str = Field(pattern=FILAMENT_ID)
    name: ProfileName
    k_value: KValue
    setting_id: str | None = Field(default=None, pattern=SETTING_ID)
    #: What the printer reported on the row an edit sends back; Bambuddy's defaults else.
    n_coef: str = Field(default="0.000000", pattern=N_COEF)
    ams_id: int = Field(default=0, ge=0, le=255)
    tray_id: int = Field(default=-1, ge=-1, le=15)


class KProfileBatch(_Strict):
    """Profiles Bambuddy sends in one command, all at one nozzle diameter (it sends the
    first row's for all)."""

    profiles: list[KProfileSet] = Field(min_length=1, max_length=BATCH_MAX)

    @model_validator(mode="after")
    def _one_nozzle(self) -> KProfileBatch:
        if len({row.nozzle_diameter for row in self.profiles}) > 1:
            raise ValueError("every profile of a batch has the same nozzle_diameter")
        return self


class KProfileRemove(_Strict):
    """Bambuddy's ``KProfileDelete``: the profile's slot and what names it."""

    slot_id: int = Field(ge=0, le=10_000)
    extruder_id: int = Field(default=0, ge=0, le=1)
    nozzle_id: str = Field(pattern=NOZZLE_ID)
    nozzle_diameter: NozzleDiameter
    filament_id: str = Field(pattern=FILAMENT_ID)
    setting_id: str | None = Field(default=None, pattern=SETTING_ID)


class KProfileNoteSet(_Strict):
    """A note on a profile, by the key Bambuddy's page stores it under: the profile's
    ``setting_id``, else ``slot_<slot>_<filament>_<extruder>``, else
    ``name_<name>_<filament>``. A blank note deletes it."""

    setting_id: NoteKey
    note: str = Field(max_length=NOTE_MAX)


def _no_printer(printer_id: str) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, f"no printer {printer_id}")


def bambuddy_id_of(row: PrinterRow | None, printer_id: str) -> int:
    """The Bambuddy printer a registry row links (R-7154-2): 404 for no row, 409 for a
    Bambuddy virtual printer or a row with no Bambuddy link."""
    if row is None:
        raise _no_printer(printer_id)
    if row.kind != "physical":
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"{row.name} is a Bambuddy virtual printer; it holds no K-profiles",
        )
    if row.bambuddy_printer_id is None:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"{row.name} is not linked to a Bambuddy printer, so ScadBuddy cannot reach"
            " its K-profiles",
        )
    return row.bambuddy_printer_id


async def linked_bambuddy_id(printers: PrinterStore | None, printer_id: str) -> int:
    row = None
    if printers is not None and printer_uuid(printer_id) is not None:
        row = await asyncio.to_thread(printers.get, printer_id)
    return bambuddy_id_of(row, printer_id)


WHERE = "the printer's K-profiles in Bambuddy (Settings > K-Profiles)"


def kprofile_kinds(
    *, settings_store: SettingsReader, printers: PrinterStore | None
) -> list[OperationKind]:
    """``printer_kprofile_set``, ``_batch``, ``_delete``, ``_note_set`` and
    ``_note_delete``. Each check finds the Bambuddy printer the registry row links; each
    run makes one call. The request holds SB's printer id and the validated body."""

    async def check(request: dict[str, Any]) -> dict[str, Any]:
        return {"bambuddy_printer_id": await linked_bambuddy_id(printers, request["printer_id"])}

    async def set_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        async with client_for(await asyncio.to_thread(settings_store.load)) as client:
            result = await client.set_k_profile(checked["bambuddy_printer_id"], request["row"])
        return result.model_dump(mode="json")

    async def batch_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        async with client_for(await asyncio.to_thread(settings_store.load)) as client:
            result = await client.set_k_profiles_batch(
                checked["bambuddy_printer_id"], request["rows"]
            )
        return result.model_dump(mode="json")

    async def delete_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        async with client_for(await asyncio.to_thread(settings_store.load)) as client:
            result = await client.delete_k_profile(checked["bambuddy_printer_id"], request["row"])
        return result.model_dump(mode="json")

    async def note_set_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        async with client_for(await asyncio.to_thread(settings_store.load)) as client:
            result = await client.set_k_profile_note(
                checked["bambuddy_printer_id"], request["key"], request["note"]
            )
        return result.model_dump(mode="json")

    async def note_delete_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        async with client_for(await asyncio.to_thread(settings_store.load)) as client:
            result = await client.delete_k_profile_note(
                checked["bambuddy_printer_id"], request["key"]
            )
        return result.model_dump(mode="json")

    return [
        OperationKind("printer_kprofile_set", check, set_run, where=WHERE),
        OperationKind("printer_kprofile_batch", check, batch_run, where=WHERE),
        OperationKind("printer_kprofile_delete", check, delete_run, where=WHERE),
        OperationKind(
            "printer_kprofile_note_set", check, note_set_run, run_attempts=3, where=WHERE
        ),
        OperationKind(
            "printer_kprofile_note_delete", check, note_delete_run, run_attempts=3, where=WHERE
        ),
    ]
