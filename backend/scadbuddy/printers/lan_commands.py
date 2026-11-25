"""The MQTT payloads of a printer's K-profiles and AMS slots, sent over ScadBuddy's own LAN
link (#7276, plan 2026-11-25, Task 1). Pure functions: no I/O.

Every builder ports one method of Bambuddy's ``bambu_mqtt.py`` at the pinned commit
``d3be761f3bd0b6bac78045dc53599331d64c3672`` (tag v1.3.1, R-7276-2) and names its lines in
its docstring; the field names are OpenBambuAPI's ``mqtt.md``
(https://github.com/Doridian/OpenBambuAPI/blob/main/mqtt.md). Two things differ from
Bambuddy on purpose: every payload takes the caller's fresh ``sequence_id``, where Bambuddy
sends ``"0"`` for ``ams_filament_setting`` and ``extrusion_cali_sel`` (R-7276-10), and only
the regular AMS (0-3, and 4-15 as Bambuddy treats them), the AMS-HT (128-135) and the
external holders (255) are addressed: ScadBuddy knows no A2L AMS-Lite or AMS 3H layout.

The H2C specifics (its two external holders, an HT unit's ids, whether it echoes the
``sequence_id`` of ``ams_filament_setting`` and ``extrusion_cali_sel``) are unverified until
the live check, Task 14 (R-7276-5)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

#: The external holders' unit id, and the global tray of Ext-L (or the single holder).
EXTERNAL = 255
EXTERNAL_TRAY = 254
#: The AMS-HT units: one tray each, the unit id is its global tray.
AMS_HT_IDS = range(128, 136)

#: An unload's nozzle temperature: below the floor (a cold nozzle) the default is sent.
UNLOAD_FLOOR = 180
UNLOAD_DEFAULT = 210
#: A load of Ext-R sends the right nozzle's temperature, or this below ``UNLOAD_FLOOR``.
RIGHT_DEFAULT = 215

_FAILED = "printer reported failure"


class UnsupportedUnitError(ValueError):
    """A unit whose wire id no source gives (P22)."""


@dataclass(frozen=True)
class KProfileRow:
    """One ``filaments`` entry of ``extrusion_cali_set``. ``ams_id`` and ``tray_id`` are
    always 0 and ``n_coef`` ``"0.000000"`` (P2), so they are not fields here."""

    cali_idx: int
    extruder_id: int
    nozzle_id: str
    nozzle_diameter: str
    filament_id: str
    name: str
    k_value: str
    setting_id: str


@dataclass(frozen=True)
class WireSlot:
    """The local ids ``ams_filament_setting`` takes (and a reset, which is one)."""

    ams_id: int
    tray_id: int
    slot_id: int


class SlotParams(BaseModel):
    """What configures a slot: the values of ``ams_filament_setting``, then the K
    selection (``extrusion_cali_sel``, or ``extrusion_cali_set_slot`` with a K value and no
    profile). The slot and the K selection take different setting ids (P3): the preset's
    (``preset_setting_id``) and the K profile's (``k_setting_id``), as Bambuddy's route
    does (``routes/printers.py`` L3207-L3225, L3291 at the pinned commit). ``tray_color``
    is ``RRGGBBAA`` without ``#``."""

    tray_info_idx: str
    tray_type: str
    tray_sub_brands: str
    tray_color: str
    nozzle_temp_min: int
    nozzle_temp_max: int
    nozzle_diameter: str
    cali_idx: int | None = None
    preset_setting_id: str | None = None
    k_setting_id: str | None = None
    k_value: float | None = None

    def digest(self) -> str:
        body = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()


def _is_ht(ams_id: int) -> bool:
    return ams_id in AMS_HT_IDS


def wire_slot(ams_id: int, tray_id: int, *, external_count: int) -> WireSlot:
    """The local ids of ``ams_filament_setting`` for unit ``ams_id`` slot ``tray_id``, as
    ``ams_set_filament_setting`` computes them:
    https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/services/bambu_mqtt.py#L8706-L8752

    ``external_count`` is ``len(PrinterStatus.vt_tray)``, which prefers the report's
    ``vir_slot`` (P1). With more than one holder each is its own unit (254 Ext-L, 255
    Ext-R) at tray 0, which Bambuddy marks as not yet captured from Studio (L8724-L8735);
    with one, the global tray 254 of unit 255. A regular AMS 0-3 sends its local slot;
    anything else Bambuddy addresses as a one-slot unit (slot 0, L8740-L8752)."""
    if ams_id == EXTERNAL:
        if external_count > 1:
            return WireSlot(EXTERNAL_TRAY + tray_id, 0, 0)
        return WireSlot(EXTERNAL, EXTERNAL_TRAY, 0)
    if 0 <= ams_id <= 3:
        return WireSlot(ams_id, tray_id, tray_id)
    return WireSlot(ams_id, tray_id, 0)


def cali_set_tray(ams_id: int, tray_id: int) -> int:
    """The flat tray index of ``extrusion_cali_set_slot`` (P4): an AMS 0-3 ``ams*4+tray``, an
    AMS-HT ``(ams-128)*4+tray``, an external holder its raw ``tray_id`` (0 or 1), as
    Bambuddy's configure route computes it:
    https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/api/routes/printers.py#L3280-L3287
    with ``legacy_tray_index``:
    https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/utils/tray_ids.py#L132-L138

    Any other unit, an AMS 4-15 among them, raises ``UnsupportedUnitError`` (P22): Bambuddy
    sends it its raw ``tray_id``, which is AMS 0's index, and no source gives a right one."""
    if _is_ht(ams_id):
        return (ams_id - AMS_HT_IDS.start) * 4 + tray_id
    if ams_id == EXTERNAL:
        return tray_id
    if 0 <= ams_id <= 3:
        return ams_id * 4 + tray_id
    raise UnsupportedUnitError(f"no known K set tray index for unit {ams_id}")


def extrusion_cali_get(nozzle_diameter: str, seq: str) -> dict[str, Any]:
    """The printer's K-profiles for one nozzle diameter (``get_kprofiles``; the payload is
    L7585-L7592):
    https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/services/bambu_mqtt.py#L7542-L7619"""
    return {
        "print": {
            "command": "extrusion_cali_get",
            "filament_id": "",
            "nozzle_diameter": nozzle_diameter,
            "sequence_id": seq,
        }
    }


def extrusion_cali_set(
    rows: Sequence[KProfileRow], nozzle_diameter: str, seq: str
) -> dict[str, Any]:
    """Make or edit K-profiles, one ``filaments`` entry each (``set_kprofile`` and
    ``set_kprofiles_batch``):
    https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/services/bambu_mqtt.py#L7666-L7825

    ``tray_id`` and ``ams_id`` are 0 and ``n_coef`` ``"0.000000"`` (L7718-L7736,
    L7794-L7809): single-nozzle firmware answers ``tray_id`` -1 with ``fail``, "invalid
    tray_id", while it applies the write (#2718 there, P2). A new profile's
    ``setting_id`` is the caller's: Bambuddy makes one (``PF`` and 17 random digits,
    L7710-L7716), which a pure builder cannot."""
    return {
        "print": {
            "command": "extrusion_cali_set",
            "filaments": [
                {
                    "ams_id": 0,
                    "cali_idx": row.cali_idx,
                    "extruder_id": row.extruder_id,
                    "filament_id": row.filament_id,
                    "k_value": row.k_value,
                    "n_coef": "0.000000",
                    "name": row.name,
                    "nozzle_diameter": row.nozzle_diameter,
                    "nozzle_id": row.nozzle_id,
                    "setting_id": row.setting_id,
                    "tray_id": 0,
                }
                for row in rows
            ],
            "nozzle_diameter": nozzle_diameter,
            "sequence_id": seq,
        }
    }


def extrusion_cali_del(
    *,
    cali_idx: int,
    filament_id: str,
    nozzle_id: str,
    nozzle_diameter: str,
    extruder_id: int,
    setting_id: str | None,
    dual_nozzle: bool,
    seq: str,
) -> dict[str, Any]:
    """Delete a K-profile (``delete_kprofile``):
    https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/services/bambu_mqtt.py#L7827-L7906

    A dual-nozzle printer takes the extruder form with no ``setting_id`` (L7868-L7878); a
    single-nozzle one the form with every field of a set (L7882-L7893)."""
    if dual_nozzle:
        body: dict[str, Any] = {
            "command": "extrusion_cali_del",
            "sequence_id": seq,
            "extruder_id": extruder_id,
            "nozzle_id": nozzle_id,
            "filament_id": filament_id,
            "cali_idx": cali_idx,
            "nozzle_diameter": nozzle_diameter,
        }
    else:
        body = {
            "command": "extrusion_cali_del",
            "sequence_id": seq,
            "filament_id": filament_id,
            "cali_idx": cali_idx,
            "setting_id": setting_id or "",
            "nozzle_diameter": nozzle_diameter,
            "nozzle_id": nozzle_id,
            "extruder_id": extruder_id,
        }
    return {"print": body}


def _filament_setting(
    slot: WireSlot,
    *,
    tray_info_idx: str,
    tray_type: str,
    tray_sub_brands: str,
    tray_color: str,
    nozzle_temp_min: int,
    nozzle_temp_max: int,
    seq: str,
) -> dict[str, Any]:
    return {
        "command": "ams_filament_setting",
        "ams_id": slot.ams_id,
        "tray_id": slot.tray_id,
        "slot_id": slot.slot_id,
        "tray_info_idx": tray_info_idx,
        "tray_type": tray_type,
        "tray_sub_brands": tray_sub_brands,
        "tray_color": tray_color,
        "nozzle_temp_min": nozzle_temp_min,
        "nozzle_temp_max": nozzle_temp_max,
        "sequence_id": seq,
    }


def ams_filament_setting(
    slot: WireSlot,
    *,
    tray_info_idx: str,
    tray_type: str,
    tray_sub_brands: str,
    tray_color: str,
    nozzle_temp_min: int,
    nozzle_temp_max: int,
    setting_id: str | None,
    seq: str,
) -> dict[str, Any]:
    """Set a slot's filament (``ams_set_filament_setting``), at the ids of ``wire_slot``:
    https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/services/bambu_mqtt.py#L8680-L8785

    ``tray_color`` goes out upper case with no ``#`` (``wire_tray_color``, L186-L211;
    L8764-L8766: P1S firmware reads lower-case hex as zeros and answers success, #2987
    there). ``setting_id`` only when given (L8773-L8775)."""
    body = _filament_setting(
        slot,
        tray_info_idx=tray_info_idx,
        tray_type=tray_type,
        tray_sub_brands=tray_sub_brands,
        tray_color=tray_color.strip().lstrip("#").upper(),
        nozzle_temp_min=nozzle_temp_min,
        nozzle_temp_max=nozzle_temp_max,
        seq=seq,
    )
    if setting_id:
        body["setting_id"] = setting_id
    return {"print": body}


def ams_reset(slot: WireSlot, seq: str) -> dict[str, Any]:
    """Clear a slot (``reset_ams_slot``): an ``ams_filament_setting`` with empty values,
    ``tray_color`` ``"00000000"`` and temperatures 0 (L8831-L8845):
    https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/services/bambu_mqtt.py#L8786-L8852"""
    return {
        "print": _filament_setting(
            slot,
            tray_info_idx="",
            tray_type="",
            tray_sub_brands="",
            tray_color="00000000",
            nozzle_temp_min=0,
            nozzle_temp_max=0,
            seq=seq,
        )
    }


def extrusion_cali_sel(
    ams_id: int,
    tray_id: int,
    *,
    external_count: int,
    cali_idx: int,
    filament_id: str,
    nozzle_diameter: str,
    seq: str,
) -> dict[str, Any]:
    """Select a K-profile for a slot (``extrusion_cali_sel``):
    https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/services/bambu_mqtt.py#L8854-L8942

    Its ``tray_id`` is the global one: ``ams*4+slot`` on an AMS 0-3 (L8905-L8908); an
    external holder's 254 or 255 in both ids with more than one holder, 254 in both with
    one (L8887-L8900); anything else its local tray at slot 0 (L8914-L8920). No
    ``setting_id``: Studio never sends one and firmware mislinks the profile with it
    (L8867-L8868). ``external_count`` as in ``wire_slot``."""
    if ams_id == EXTERNAL:
        if external_count > 1:
            ids = (EXTERNAL_TRAY + tray_id, EXTERNAL_TRAY + tray_id, 0)
        else:
            ids = (EXTERNAL_TRAY, EXTERNAL_TRAY, 0)
    elif 0 <= ams_id <= 3:
        ids = (ams_id, ams_id * 4 + tray_id, tray_id)
    else:
        ids = (ams_id, tray_id, 0)
    return {
        "print": {
            "command": "extrusion_cali_sel",
            "cali_idx": cali_idx,
            "filament_id": filament_id,
            "nozzle_diameter": nozzle_diameter,
            "ams_id": ids[0],
            "tray_id": ids[1],
            "slot_id": ids[2],
            "sequence_id": seq,
        }
    }


def extrusion_cali_set_slot(
    ams_id: int,
    tray_id: int,
    *,
    k_value: float,
    nozzle_diameter: str,
    filament_id: str,
    setting_id: str,
    name: str,
    seq: str,
) -> dict[str, Any]:
    """Set a slot's K value directly, with no profile selected (Bambuddy's
    ``extrusion_cali_set`` method):
    https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/services/bambu_mqtt.py#L8944-L9020
    which its configure route sends only when ``cali_idx`` < 0 (L3280 of
    ``routes/printers.py``): ``cali_idx`` -1, ``n_coef`` ``"1.400000"``, ``extruder_id``
    and ``ams_id`` 0, ``nozzle_id`` ``HS00-<diameter>`` (L8981, L8993-L9005), the tray
    index of ``cali_set_tray`` (P4). ``setting_id`` is the K profile's
    (``SlotParams.k_setting_id``, P3)."""
    return {
        "print": {
            "command": "extrusion_cali_set",
            "filaments": [
                {
                    "ams_id": 0,
                    "cali_idx": -1,
                    "extruder_id": 0,
                    "filament_id": filament_id,
                    "k_value": f"{k_value:.6f}",
                    "n_coef": "1.400000",
                    "name": name,
                    "nozzle_diameter": nozzle_diameter,
                    "nozzle_id": f"HS00-{nozzle_diameter}",
                    "setting_id": setting_id,
                    "tray_id": cali_set_tray(ams_id, tray_id),
                }
            ],
            "nozzle_diameter": nozzle_diameter,
            "sequence_id": seq,
        }
    }


def ams_get_rfid(ams_id: int, slot_id: int, seq: str) -> dict[str, Any]:
    """Read a slot's tag again (``ams_refresh_tray``; the payload L8615-L8617 with the
    ``sequence_id`` of ``_send_rfid_command``, L8661):
    https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/services/bambu_mqtt.py#L8567-L8648

    No ``M620 R`` fallback (R-7276-11)."""
    return {
        "print": {
            "command": "ams_get_rfid",
            "ams_id": ams_id,
            "slot_id": slot_id,
            "sequence_id": seq,
        }
    }


def ams_change_filament_load(
    global_tray: int, *, extruder_id: int | None, right_temp: int, seq: str
) -> dict[str, Any]:
    """Load a slot's filament (``ams_load_filament``):
    https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/services/bambu_mqtt.py#L8337-L8429

    Ext-R (255) is unit 255 slot 0 at the right nozzle's temperature, ``RIGHT_DEFAULT``
    below ``UNLOAD_FLOOR`` (L8384-L8391); the external holder 254 is unit 255 slot 254, -1
    temperatures (L8392-L8396); an AMS-HT is its unit id with slot 0 and itself as target,
    an AMS ``(tray // 4, tray % 4)`` with the global tray as target
    (``change_filament_wire_ids``, L239-L254). ``extruder_id`` only when given
    (L8415-L8416)."""
    if global_tray == EXTERNAL:
        temp = right_temp if right_temp >= UNLOAD_FLOOR else RIGHT_DEFAULT
        ams_id, slot_id, curr, tar = EXTERNAL, 0, temp, temp
    elif global_tray == EXTERNAL_TRAY:
        ams_id, slot_id, curr, tar = EXTERNAL, EXTERNAL_TRAY, -1, -1
    elif _is_ht(global_tray):
        ams_id, slot_id, curr, tar = global_tray, 0, -1, -1
    else:
        ams_id, slot_id = divmod(global_tray, 4)
        curr, tar = -1, -1
    body: dict[str, Any] = {
        "command": "ams_change_filament",
        "sequence_id": seq,
        "ams_id": ams_id,
        "slot_id": slot_id,
        "target": global_tray,
        "curr_temp": curr,
        "tar_temp": tar,
    }
    if extruder_id is not None:
        body["extruder_id"] = extruder_id
    return {"print": body}


def ams_change_filament_unload(source_tray: int, *, nozzle_temp: int, seq: str) -> dict[str, Any]:
    """Unload (``ams_unload_filament``):
    https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/services/bambu_mqtt.py#L8431-L8543

    Addressed to the source's unit (255 for an external holder, an AMS-HT's own id, an
    AMS ``tray // 4``; L8467-L8471), slot and target 255, at the nozzle's temperature or
    ``UNLOAD_DEFAULT`` below ``UNLOAD_FLOOR`` (L8516-L8518)."""
    if source_tray in (EXTERNAL, EXTERNAL_TRAY):
        ams_id = EXTERNAL
    elif _is_ht(source_tray):
        ams_id = source_tray
    else:
        ams_id = source_tray // 4
    temp = nozzle_temp if nozzle_temp >= UNLOAD_FLOOR else UNLOAD_DEFAULT
    return {
        "print": {
            "command": "ams_change_filament",
            "sequence_id": seq,
            "ams_id": ams_id,
            "slot_id": 255,
            "target": 255,
            "curr_temp": temp,
            "tar_temp": temp,
        }
    }


def matches(reply: Mapping[str, Any], command: str, seq: str) -> bool:
    """Whether a report's ``print`` object answers the command sent under ``seq``: equal
    ``command`` and ``sequence_id`` (R-7276-10)."""
    return (
        reply.get("command") == command
        and "sequence_id" in reply
        and str(reply["sequence_id"]) == seq
    )


def refusal(reply: Mapping[str, Any]) -> str | None:
    """The printer's reason when its answer's ``result`` is ``fail`` in any case, else
    ``None``, as ``await_cali_ack`` (L7655-L7659) and ``_send_rfid_command``
    (L8669-L8670) read it:
    https://github.com/maziggy/bambuddy/blob/d3be761f3bd0b6bac78045dc53599331d64c3672/backend/app/services/bambu_mqtt.py#L7636-L7664"""
    if str(reply.get("result", "")).lower() != "fail":
        return None
    return str(reply.get("reason", "") or "") or _FAILED


#: Every payload builder, each citing its source.
BUILDERS: tuple[Callable[..., dict[str, Any]], ...] = (
    extrusion_cali_get,
    extrusion_cali_set,
    extrusion_cali_del,
    ams_filament_setting,
    ams_reset,
    extrusion_cali_sel,
    extrusion_cali_set_slot,
    ams_get_rfid,
    ams_change_filament_load,
    ams_change_filament_unload,
)
