"""A native rack print's ``nozzle_mapping`` (#3818, hot-path plan 2026-10-18 Task 8): which
physical nozzle each filament slot prints from, sent in ``print.project_file``.

Measured on the live H2C (firmware 01.02.00.00) on 2026-10-24, and from the completed
prints Bambuddy dispatched to it over the LAN (its ``print_queue`` items 367, 370 and 374;
`tests/printers/fixtures/rack-project-file.json`):

- The field is `RACK_WIRE_SLOTS` (32) entries, one per filament slot (index 0 is slot 1),
  each a **physical** nozzle id: `FIXED_NOZZLE_ID` (1) the fixed hotend, ``15 +
  position`` (16-21) a rack position 1-6, ``-1`` a slot the plate does not print. A plate
  that prints nothing from the rack sends no field at all. Bambuddy's ``bambu_mqtt.py``
  (``_RACK_WIRE_SLOTS``, ``_FIXED_NOZZLE_ID``, ``rack_position_to_nozzle_id``, read in
  the deployed image the same day) says the same, with Bambu Studio's own captures.
- Extruder indices are another namespace: on the wire extruder 0 is the rack's carriage
  and 1 the fixed hotend. In ``slice_info.config`` a filament names its group
  (``<filament group_id>``) and the group its slicer extruder (``<nozzle id=<group>
  extruder_id=<1-based>>``); the H2C preset's ``extruder_max_nozzle_count`` ["1", "6"]
  says the slicer's second extruder is the rack, and ``physical_extruder_map`` ["1", "0"]
  makes it the wire's 0 (`bambuddy.extruders.RACK_SIDE`). Item 370's plate
  (`fixtures/rack-slice-info-h2c.config`, groups ``[2, 4, 3, 1, 0, 1]`` by slot) went out
  as ``[21, 18, 20, 17, 1, 17, -1 x26]`` and printed.

A wrong id makes the printer level with one nozzle and print with another, a first layer
in mid-air. So whatever cannot be resolved with confidence, here or in the file, sends no
field and lets the firmware pick, with a warning logged: never a guess.
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, field
from typing import IO, Any

from pydantic import BaseModel

from scadbuddy.bambuddy.extruders import RACK_SIDE
from scadbuddy.bambuddy.filaments import FilamentWarning, SpoolOption
from scadbuddy.bambuddy.models import (
    FilamentGroup,
    FilamentRequirement,
    PrinterStatus,
    RackAlgorithm,
)
from scadbuddy.rack.rank import Pick, Usage, diameter, manual_for, rack_groups, rank_rack
from scadbuddy.rack.rank import rack_warnings as rank_warnings
from scadbuddy.slicing.slice_info import PrintedFilaments, printed_filaments

logger = logging.getLogger(__name__)

#: Entries on the wire, whatever the plate's slot count.
RACK_WIRE_SLOTS = 32
#: The fixed hotend's physical id (not its extruder index, which is 1 too).
FIXED_NOZZLE_ID = 1
#: A rack position's physical id is this plus the position.
RACK_POSITION_BASE = 15
RACK_POSITIONS = range(1, 7)
#: The wire's extruder for the fixed hotend; `RACK_SIDE` (0) is the rack's carriage.
FIXED_SIDE = 1 - RACK_SIDE
#: The most of a config file read.
MAX_CONFIG_BYTES = 16 * 1024 * 1024


def rack_nozzle_id(position: int) -> int | None:
    """The physical nozzle id of rack position 1-6, else ``None``."""
    if isinstance(position, bool) or position not in RACK_POSITIONS:
        return None
    return RACK_POSITION_BASE + position


def nozzle_mapping(
    slot_groups: Sequence[int], fixed_groups: Set[int], picks: Mapping[int, int]
) -> list[int] | None:
    """One physical nozzle id per slot: `FIXED_NOZZLE_ID` for a group on the fixed hotend,
    the picked position's id for a rack group, ``-1`` for a slot not printed; padded to
    `RACK_WIRE_SLOTS`. ``None`` when no slot prints from the rack (no field is sent), and
    ``None`` with a warning when a rack group has no pick, a pick is no rack position, or
    the plate has more slots than the wire."""
    if len(slot_groups) > RACK_WIRE_SLOTS:
        logger.warning(
            "nozzle_mapping not sent: the plate has more slots than the wire carries",
            extra={"slots": len(slot_groups)},
        )
        return None
    wire = [-1] * RACK_WIRE_SLOTS
    on_rack = False
    for index, group_id in enumerate(slot_groups):
        if group_id < 0:
            continue
        if group_id in fixed_groups:
            wire[index] = FIXED_NOZZLE_ID
            continue
        position = picks.get(group_id)
        if position is None:
            logger.warning(
                "nozzle_mapping not sent: a rack group has no rack position",
                extra={"group_id": group_id},
            )
            return None
        nozzle = rack_nozzle_id(position)
        if nozzle is None:
            logger.warning(
                "nozzle_mapping not sent: a pick is no rack position",
                extra={"group_id": group_id, "position": position},
            )
            return None
        wire[index] = nozzle
        on_rack = True
    return wire if on_rack else None


class SlicedRack(BaseModel):
    """What a sliced plate needs of a rack, read from its 3MF (`read_rack_plan`): each
    slot's group (index 0 is slot 1, ``-1`` not printed) and each printed filament with
    its group, in `rack.rank.rack_groups`' shape. No serial: it crosses the history."""

    slot_groups: list[int]
    filaments: list[FilamentRequirement]

    @property
    def fixed_groups(self) -> set[int]:
        return {
            f.group_id
            for f in self.filaments
            if f.group_id is not None and f.group is not None and not f.group.on_rack
        }

    @property
    def on_rack(self) -> bool:
        return any(f.group is not None and f.group.on_rack for f in self.filaments)


class _UnresolvedError(ValueError):
    """The file names a rack plan that cannot be read with confidence."""


def _read(archive: zipfile.ZipFile, name: str) -> bytes:
    with archive.open(name) as member:
        data = member.read(MAX_CONFIG_BYTES + 1)
    if len(data) > MAX_CONFIG_BYTES:
        raise _UnresolvedError(f"{name} is past its bound")
    return data


def _rack_index(settings: dict[str, Any]) -> int | None:
    """The slicer extruder (0-based) that is the rack, or ``None`` on a printer with
    none. The rack is the extruder that holds more than one nozzle; the file's
    ``physical_extruder_map`` must make it the wire's `RACK_SIDE` and every other one the
    fixed hotend's, or this translation does not model the machine."""
    counts = settings.get("extruder_max_nozzle_count")
    if not isinstance(counts, list):
        return None
    try:
        rack = [index for index, count in enumerate(counts) if int(count) > 1]
    except (TypeError, ValueError):
        raise _UnresolvedError("extruder_max_nozzle_count is not a list of numbers") from None
    if not rack:
        return None
    if len(rack) != 1:
        raise _UnresolvedError("more than one extruder holds several nozzles")
    physical = settings.get("physical_extruder_map")
    try:
        sides = [int(value) for value in physical] if isinstance(physical, list) else None
    except (TypeError, ValueError):
        sides = None
    if sides is None or len(sides) != len(counts):
        raise _UnresolvedError("physical_extruder_map does not name every extruder")
    expected = [RACK_SIDE if index == rack[0] else FIXED_SIDE for index in range(len(sides))]
    if sides != expected:
        raise _UnresolvedError("physical_extruder_map is not the H2C's")
    return rack[0]


def _plate(root: ET.Element, plate_id: int | None) -> ET.Element:
    plates = root.findall("plate")
    if len(plates) == 1:
        return plates[0]
    for plate in plates:
        for meta in plate.findall("metadata"):
            if meta.get("key") == "index" and meta.get("value") == str(plate_id):
                return plate
    raise _UnresolvedError("slice_info.config has no such plate")


def _int(value: str | None) -> int:
    try:
        return int(value or "")
    except ValueError:
        raise _UnresolvedError("a number in slice_info.config is not one") from None


def _shape(diameter_text: str | None, volume_type: str | None) -> tuple[float | None, str]:
    return diameter(diameter_text), (volume_type or "").strip().lower()


def _plan(archive: zipfile.ZipFile, plate_id: int | None) -> SlicedRack | None:
    names = archive.namelist()
    if "Metadata/project_settings.config" not in names:
        return None
    try:
        settings = json.loads(_read(archive, "Metadata/project_settings.config"))
    except ValueError:
        raise _UnresolvedError("project_settings.config is not JSON") from None
    if not isinstance(settings, dict) or (rack := _rack_index(settings)) is None:
        return None  # not a rack printer's file: nothing to send
    if "Metadata/slice_info.config" not in names:
        raise _UnresolvedError("the file has no slice_info.config")
    try:
        root = ET.fromstring(_read(archive, "Metadata/slice_info.config"))
    except ET.ParseError:
        raise _UnresolvedError("slice_info.config is not XML") from None
    plate = _plate(root, plate_id)
    # Each group's slicer extruder and hotend, from the plate's nozzle table.
    table: dict[int, tuple[int, tuple[float | None, str]]] = {}
    for nozzle in plate.findall("nozzle"):
        group_id, extruder = _int(nozzle.get("id")), _int(nozzle.get("extruder_id")) - 1
        entry = (extruder, _shape(nozzle.get("nozzle_diameter"), nozzle.get("volume_type")))
        if extruder < 0 or table.setdefault(group_id, entry) != entry:
            raise _UnresolvedError("the nozzle table names a group twice, or no extruder")
    slots: dict[int, int] = {}
    shapes: dict[int, tuple[float | None, str]] = {}
    filaments: list[FilamentRequirement] = []
    for node in plate.findall("filament"):
        slot, group_id = _int(node.get("id")), _int(node.get("group_id"))
        placed = table.get(group_id)
        if placed is None:
            raise _UnresolvedError("a filament's group is not in the nozzle table")
        extruder, hotend = placed
        shape = _shape(node.get("nozzle_diameter"), node.get("volume_type"))
        # The filament, the table and the group's other filaments agree on its hotend.
        if shape != hotend or shapes.setdefault(group_id, shape) != shape or shape[0] is None:
            raise _UnresolvedError("a group's filaments and nozzle disagree about its hotend")
        if slot < 1 or slot in slots:
            raise _UnresolvedError("a slot is named twice, or is no slot")
        slots[slot] = group_id
        try:
            grams = float(node.get("used_g") or 0)
        except ValueError:
            grams = 0.0
        filaments.append(
            FilamentRequirement(
                slot_id=slot,
                type=node.get("type") or None,
                color=node.get("color") or None,
                used_grams=round(grams, 2),
                group_id=group_id,
                group=FilamentGroup(
                    on_rack=extruder == rack,
                    nozzle_diameter=node.get("nozzle_diameter") or "",
                    volume_type=node.get("volume_type") or "",
                    filament_color=node.get("color") or "",
                ),
            )
        )
    filaments = _on_printed(
        filaments, printed_filaments(archive, _plate_index(plate, plate_id)), settings
    )
    slots = {f.slot_id: f.group_id for f in filaments if f.group_id is not None}
    if not slots:
        raise _UnresolvedError("the plate names no filament")
    if max(slots) > RACK_WIRE_SLOTS:
        raise _UnresolvedError("the plate has more slots than the wire carries")
    return SlicedRack(
        slot_groups=[slots.get(slot, -1) for slot in range(1, max(slots) + 1)],
        filaments=sorted(filaments, key=lambda f: f.slot_id),
    )


def _plate_index(plate: ET.Element, plate_id: int | None) -> int:
    """The plate's own ``index``, else the one asked for (1 when none was)."""
    for meta in plate.findall("metadata"):
        if meta.get("key") == "index":
            return _int(meta.get("value"))
    return plate_id or 1


def _project_value(settings: dict[str, Any], key: str, slot: int) -> str:
    """Project filament ``slot``'s ``key`` (``filament_type``, ``filament_colour``)."""
    values = settings.get(key)
    if not isinstance(values, list) or not 1 <= slot <= len(values):
        raise _UnresolvedError(f"project_settings.config names no {key} for slot {slot}")
    return str(values[slot - 1])


def _on_printed(
    filaments: list[FilamentRequirement],
    printed: PrintedFilaments,
    settings: dict[str, Any],
) -> list[FilamentRequirement]:
    """``filaments`` on the slots the plate prints (#4712), as `read_sliced` reads its
    grams (#4711): a filament ``slice_info.config`` names that the plate does not print
    is the plate's first filament. It joins that filament when the plate names it too,
    in the same group; one in another group cannot be placed with confidence. One the
    plate does not name otherwise (a plate of one filament, all ``id="1"``) takes the
    first filament's slot, and its type and colour from the project's, since
    ``slice_info.config`` wrote filament 1's."""
    if (
        printed.printed
        and printed.first is None
        and any(f.slot_id not in printed.printed for f in filaments)
    ):
        # The plate names a filament it does not print, and nothing says which it prints
        # first (a header of several filaments, no sequence): no slot can take it.
        raise _UnresolvedError("a filament the plate does not print has no first to join")
    named = {f.slot_id: f for f in filaments if printed.slot(f.slot_id) == f.slot_id}
    for moved in (f for f in filaments if printed.slot(f.slot_id) != f.slot_id):
        slot = printed.slot(moved.slot_id)
        known = named.get(slot)
        if known is None:
            colour = _project_value(settings, "filament_colour", slot)
            # Only the filament's own fields are filament 1's: its group, nozzle diameter
            # and volume type are the plate's real hotend, which the nozzle table has
            # checked against the group above. Of filament 1's own, only the type and
            # colour are read (`FilamentRequirement` carries no ``tray_info_idx``), and
            # both are replaced; the grams are the plate's, all of them this filament's.
            group = moved.group
            named[slot] = moved.model_copy(
                update={
                    "slot_id": slot,
                    "type": _project_value(settings, "filament_type", slot) or None,
                    "color": colour or None,
                    "group": None
                    if group is None
                    else group.model_copy(update={"filament_color": colour}),
                }
            )
        elif known.group_id != moved.group_id:
            raise _UnresolvedError(
                "a filament the plate does not print is in another group than its first"
            )
        else:
            named[slot] = known.model_copy(
                update={"used_grams": round(known.used_grams + moved.used_grams, 2)}
            )
    return list(named.values())


def read_rack_plan(file: IO[bytes], plate_id: int | None) -> SlicedRack | None:
    """``plate_id``'s rack plan from the sliced 3MF, or ``None``: on a file for a printer
    with no rack, and (with a warning) on one whose plan cannot be read with confidence.
    The only plate of a file of one is read whatever its number (or with none). It never
    raises: a file it cannot read only sends no field (an upload never fails on it)."""
    try:
        with zipfile.ZipFile(file) as archive:
            return _plan(archive, plate_id)
    except _UnresolvedError as error:
        logger.warning("nozzle_mapping not sent: %s", error)
        return None
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        OSError,
        KeyError,
        EOFError,
        NotImplementedError,
        RuntimeError,
        ValueError,
        zlib.error,
    ) as error:
        # A corrupt or unsupported member (a bad CRC, a truncated stream, an
        # unknown compression or an encrypted entry): unreadable, so no field.
        logger.warning(
            "nozzle_mapping not sent: the sliced file is unreadable",
            extra={"error": type(error).__name__},
        )
        return None


@dataclass
class NozzleChoice:
    """What `choose_nozzles` picked: the wire (``None`` sends none), the picks behind it
    (with serials: never into a history), and the rack's warnings."""

    mapping: list[int] | None
    picks: list[Pick] = field(default_factory=list)
    warnings: list[FilamentWarning] = field(default_factory=list)
    #: The "bambuddy" ranking left the rack groups to the firmware on purpose.
    declined: bool = False


def choose_nozzles(
    plan: SlicedRack,
    status: PrinterStatus,
    *,
    algorithm: RackAlgorithm,
    manual_position: int | None,
    usage: Mapping[str, Usage],
    spools: Mapping[int, SpoolOption],
) -> NozzleChoice:
    """Rank the printer's rack for the plate's rack groups, as the Bambuddy path does
    (`rack.rank.rank_rack`, the dialog's hand-picked position through ``manual_for``),
    and write the picks as the wire. ``status`` is the printer's LAN report's. The
    "bambuddy" ranking picks nothing itself, so its rack groups are left to the
    firmware: no field, as before Task 8."""
    groups = rack_groups(plan.filaments, spools)
    if not groups:
        return NozzleChoice(mapping=None)
    manual, notes = manual_for(groups, manual_position, algorithm, picker="the printer")
    picks = rank_rack(groups, status.nozzle_rack, algorithm, usage, manual)
    found = rank_warnings(
        groups, status.nozzle_rack, algorithm, picks, manual, picker="the printer"
    )
    if algorithm == "bambuddy" and len(picks) < len(groups):
        # Nothing is sent, the hand-picked position included, so ``manual_for``'s "group
        # N got position P" does not hold (#4545).
        if manual and len(groups) > 1:
            notes = [
                FilamentWarning(
                    kind="rack-manual-partial",
                    message=f"Rack position {manual_position} was not sent: the slice put "
                    f"the rack side in {len(groups)} nozzle groups, and over the LAN the "
                    "printer picks for all of them or none.",
                )
            ]
            # Said once: rack_warnings' "does not fit …, so it was not used" is this.
            # Dropping every rack-manual-partial relies on ``manual_for`` assigning the
            # position to one group only; a second manual group would lose its own.
            found = [note for note in found if note.kind != "rack-manual-partial"]
        return NozzleChoice(mapping=None, warnings=[*notes, *found], declined=True)
    mapping = nozzle_mapping(
        plan.slot_groups,
        plan.fixed_groups,
        {group_id: pick.position for group_id, pick in picks.items()},
    )
    ordered = [picks[group_id] for group_id in sorted(picks)] if mapping is not None else []
    return NozzleChoice(mapping=mapping, picks=ordered, warnings=[*notes, *found])
