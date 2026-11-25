"""A printer's K-profiles (pressure advance) over ScadBuddy's own LAN link (#7276, plan
``docs/superpowers/plans/2026-11-25-filament-hardware-off-bambuddy.md`` Task 7; the bodies
are #7154's, R-7154-6).

The bodies SB accepts, and the pure steps between them and the printer's MQTT commands
(`printers/lan_commands.py`): the printer's table as the route's `KProfileTable`, the body's
rows as `KProfileRow`s, and a write's reply as the route's `KProfileWriteResult`. The
shapes Bambuddy kept are its ``backend/app/schemas/kprofile.py`` (maziggy/bambuddy v1.3.1).
SB keeps no copy of the table; it keeps the notes (`printers/command_data.py`).
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from scadbuddy.bambuddy.models import KProfile, KProfileTable, KProfileWriteResult
from scadbuddy.printers.lan_command_models import (
    LanCommandOutcome,
    LanCommandReply,
    LanCommandRun,
)
from scadbuddy.printers.lan_commands import KProfileRow

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
    # A path segment of `.` or `..` is normalised away before the DELETE's route sees it,
    # so a note under one could never be deleted (#7276 Task 10).
    if value in (".", ".."):
        raise ValueError("cannot be . or ..")
    return _printable(value)


def _storable(value: str) -> str:
    """A note Postgres can keep: no NUL, and text that encodes (a lone surrogate does
    not): else a 500 from the store, not a 422 (P35)."""
    try:
        value.encode()
    except UnicodeEncodeError:
        raise ValueError("must be valid text") from None
    if "\x00" in value:
        raise ValueError("cannot hold a NUL character")
    return value


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
    #: Accepted and ignored: every write sends ``n_coef`` "0.000000", ``ams_id`` 0 and
    #: ``tray_id`` 0, as Bambuddy does (`set_rows`, `lan_commands.extrusion_cali_set`, P2).
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
    note: Annotated[str, Field(max_length=NOTE_MAX), AfterValidator(_storable)]


def table_of(reply: dict[str, Any], nozzle_diameter: str) -> KProfileTable:
    """The route's table from an ``extrusion_cali_get`` reply: each ``filaments`` entry a
    `KProfile` (``slot_id`` is its ``cali_idx``), an entry that names another nozzle
    diameter dropped, as Bambuddy's ``_parse_kprofile_entries`` does (L7392-L7438):
    https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/services/bambu_mqtt.py#L7392-L7438
    The reply is the printer's, so an entry whose numbers do not parse, or that the model
    refuses, is dropped too, never a 500.
    """
    entries = reply.get("filaments")
    profiles: list[KProfile] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        diameter = str(entry.get("nozzle_diameter") or nozzle_diameter)
        if diameter != nozzle_diameter:
            continue
        try:
            profile = KProfile(
                slot_id=int(entry.get("cali_idx", 0)),
                extruder_id=int(entry.get("extruder_id", 0)),
                nozzle_id=str(entry.get("nozzle_id") or ""),
                nozzle_diameter=nozzle_diameter,
                filament_id=str(entry.get("filament_id") or ""),
                name=str(entry.get("name") or ""),
                k_value=str(entry.get("k_value", "")),
                setting_id=str(entry["setting_id"]) if entry.get("setting_id") else None,
                n_coef=str(entry.get("n_coef") or "0.000000"),
                ams_id=int(entry.get("ams_id", 0)),
                tray_id=int(entry.get("tray_id", -1)),
            )
        except (TypeError, ValueError):  # a pydantic ValidationError is a ValueError
            continue
        profiles.append(profile)
    return KProfileTable(profiles=profiles, nozzle_diameter=nozzle_diameter)


def new_setting_id() -> str:
    """``PF`` and 17 random digits, as Bambuddy makes a new profile's ``setting_id``
    (``set_kprofile``, L7711-L7716 at the pinned commit; P23):
    https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/services/bambu_mqtt.py#L7711-L7716
    and ``set_kprofiles_batch`` makes one for each new row of a batch, so the two rows of a
    dual-nozzle profile get two ids (L7790-L7792):
    https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/services/bambu_mqtt.py#L7790-L7792
    """
    return "PF" + "".join(secrets.choice("0123456789") for _ in range(17))


def set_rows(rows: Sequence[KProfileSet]) -> list[KProfileRow]:
    """The rows of one ``extrusion_cali_set``: ``cali_idx`` -1 for ``slot_id`` 0 (a new
    profile), else ``slot_id``; each new profile with no ``setting_id`` gets one of
    its own (`new_setting_id`, the route's, not the builder's, P23). ``n_coef``, ``ams_id`` and
    ``tray_id`` are not sent as the body has them (P2: the builder sends 0 and
    ``"0.000000"``)."""
    return [
        KProfileRow(
            cali_idx=-1 if row.slot_id == 0 else row.slot_id,
            extruder_id=row.extruder_id,
            nozzle_id=row.nozzle_id,
            filament_id=row.filament_id,
            name=row.name,
            k_value=row.k_value,
            setting_id=row.setting_id or (new_setting_id() if row.slot_id == 0 else ""),
        )
        for row in rows
    ]


def kprofile_table_view(run: LanCommandRun, outcome: LanCommandOutcome) -> KProfileTable:
    """``GET /kprofiles``'s body: the table at the nozzle diameter the command asked for,
    with the extruder count the route put in the run's ``answer`` (P54)."""
    nozzle_diameter = str(run.commands[0].payload["print"]["nozzle_diameter"])
    table = table_of(outcome.replies[0].reply or {}, nozzle_diameter)
    table.extruders = (run.answer or {}).get("extruders")
    return table


def kprofile_write_view(run: LanCommandRun, outcome: LanCommandOutcome) -> KProfileWriteResult:
    """A K-profile write's body (set, batch, delete): one command, one reply."""
    return write_result(outcome.replies[0])


def write_result(reply: LanCommandReply) -> KProfileWriteResult:
    """A write's answer: ``success`` always (a refusal was raised before this, P30);
    ``confirmed`` false when the printer sent no acknowledgement in time."""
    return KProfileWriteResult(
        success=True,
        confirmed=reply.confirmed,
        message="" if reply.confirmed else "the printer did not acknowledge it in time",
    )
