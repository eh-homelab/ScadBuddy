"""Reading a sliced 3MF (#3822): a plate's time, grams per slot and filament changes."""

from __future__ import annotations

import io
import json
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import dataclass
from typing import BinaryIO

from pydantic import BaseModel

from scadbuddy.bambuddy.extruders import Side, side_of
from scadbuddy.bambuddy.filaments3mf import (
    SLICE_INFO_NAME,
    UnreadableFilamentsError,
    read_filaments,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.render.bambu3mf import PROJECT_SETTINGS_NAME

logger = logging.getLogger(__name__)

#: A plate's G-code, unzipped, past this is not read.
MAX_GCODE_BYTES = 256 * 1024 * 1024
#: Bambu's ``change_filament_gcode`` loads each filament with ``M620 S<n>A``; 255 is
#: "no filament" (an unload).
_LOAD = re.compile(rb"^M620 S(\d+)A", re.MULTILINE)
#: The G-code header's list of the 1-based filaments the plate prints.
_HEADER_FILAMENTS = re.compile(rb"^; filament: ([\d,]+)\s*$", re.MULTILINE)
#: How far into the G-code the header is looked for when no end marker is found.
_HEADER_BYTES = 64 * 1024
#: The CLI's per-plate filament order, and the most of it that is read.
_SEQUENCE = "Metadata/filament_sequence.json"
_MAX_SEQUENCE_BYTES = 16 * 1024 * 1024
#: What reading one corrupt or unsupported member raises (a bad CRC, a truncated or
#: corrupt deflate stream, an unknown compression, an encrypted entry), as
#: `printers/nozzle_mapping.py` `read_rack_plan` lists them: the header and the sequence
#: read as saying nothing then, and never cost a plate its G-code or its rack plan.
_MEMBER_ERRORS = (
    zipfile.BadZipFile,
    OSError,
    EOFError,
    NotImplementedError,
    RuntimeError,
    zlib.error,
)


def _file(payload: bytes | BinaryIO) -> BinaryIO:
    """A file over ``payload``, at its start; a file is read from its start, never
    copied into memory (Task 12 review M4)."""
    if isinstance(payload, bytes):
        return io.BytesIO(payload)
    payload.seek(0)
    return payload


def _gcode_name(archive: zipfile.ZipFile, plate_id: int) -> str | None:
    """``Metadata/plate_<plate_id>.gcode``, else the only plate's; ``None`` for neither."""
    names = [
        name for name in archive.namelist() if re.fullmatch(r"Metadata/plate_\d+\.gcode", name)
    ]
    wanted = f"Metadata/plate_{plate_id}.gcode"
    return wanted if wanted in names else (names[0] if len(names) == 1 else None)


def _gcode(archive: zipfile.ZipFile, plate_id: int) -> bytes | None:
    """:func:`plate_gcode` in an open archive."""
    name = _gcode_name(archive, plate_id)
    if name is None:
        return None
    # Read no further than the bound, whatever the zip's header claims.
    with archive.open(name) as member:
        gcode = member.read(MAX_GCODE_BYTES + 1)
    if len(gcode) > MAX_GCODE_BYTES:
        logger.info("the sliced plate's G-code is past its bound")
        return None
    return gcode


def plate_gcode(payload: bytes | BinaryIO, plate_id: int) -> bytes | None:
    """The sliced plate's G-code (``Metadata/plate_N.gcode``, else the only plate's);
    None when there is none, the file is not a zip, or it is past :data:`MAX_GCODE_BYTES`.
    ``payload`` is the file's bytes or the file itself."""
    return _plate_files(payload, plate_id)[0]


def _plate_files(
    payload: bytes | BinaryIO, plate_id: int
) -> tuple[bytes | None, PrintedFilaments, dict[int, Side]]:
    """The plate's G-code (:func:`plate_gcode`), the filaments it prints
    (:func:`printed_filaments`) and their sides (:func:`filament_sides`), read in one
    pass over the archive."""
    try:
        with zipfile.ZipFile(_file(payload)) as archive:
            try:
                gcode = _gcode(archive, plate_id)
            except _MEMBER_ERRORS as error:
                # Only the G-code is lost: the filaments are read from the rest.
                logger.info(
                    "the sliced plate's G-code could not be read",
                    extra={"error": type(error).__name__},
                )
                gcode = None
            return gcode, printed_filaments(archive, plate_id), filament_sides(archive, plate_id)
    except (*_MEMBER_ERRORS, KeyError) as error:
        logger.info("the sliced file could not be read", extra={"error": type(error).__name__})
        return None, PrintedFilaments(), {}


def filament_changes(gcode: bytes) -> int:
    """How many times the G-code changes filament: its loads (255 is an unload), minus
    the first."""
    loads = [int(match) for match in _LOAD.findall(gcode) if int(match) != 255]
    return max(len(loads) - 1, 0)


def _sequence(archive: zipfile.ZipFile, index: int) -> list[int]:
    """The 1-based filaments plate ``index`` prints, in the order it prints them
    (``Metadata/filament_sequence.json``'s ``plate_<index>.sequence``); empty when the
    file has none or it cannot be read."""
    if _SEQUENCE not in archive.namelist():
        return []
    try:
        # Its own failure never costs the plate its G-code, read from the same archive.
        with archive.open(_SEQUENCE) as member:
            data = member.read(_MAX_SEQUENCE_BYTES + 1)
        if len(data) > _MAX_SEQUENCE_BYTES:
            return []
        doc = json.loads(data)
    except (*_MEMBER_ERRORS, ValueError):
        return []
    plate = doc.get(f"plate_{index}") if isinstance(doc, dict) else None
    sequence = plate.get("sequence") if isinstance(plate, dict) else None
    if not isinstance(sequence, list):
        return []
    return [n for n in sequence if isinstance(n, int) and not isinstance(n, bool) and n >= 1]


def _header_filaments(archive: zipfile.ZipFile, plate_id: int) -> list[int]:
    """The 1-based filaments the plate's G-code header names (``; filament: 2,3``), read
    from the head of the G-code only; empty when it names none or cannot be read."""
    name = _gcode_name(archive, plate_id)
    if name is None:
        return []
    try:
        with archive.open(name) as member:
            head = member.read(_HEADER_BYTES)
    except _MEMBER_ERRORS:
        return []
    end = head.find(b"; HEADER_BLOCK_END")
    found = _HEADER_FILAMENTS.search(head[: end if end >= 0 else len(head)])
    if found is None:
        return []
    return [int(n) for n in found.group(1).split(b",") if n.strip().isdigit() and int(n) >= 1]


@dataclass(frozen=True)
class PrintedFilaments:
    """The filaments a sliced plate prints, and the one it prints first, as the G-code
    and ``filament_sequence.json`` say: what ``slice_info.config``'s filament ids are
    read against (#4711, #4712). Bambu Studio's CLI counts what a plate extrudes before
    its first filament change as filament 1's, a filament the plate may never load: a
    plate of one filament is all ``id="1"``, whatever it prints (measured on
    v02.08.02.61, ``tests/slicing/test_slice_info.py``)."""

    printed: frozenset[int] = frozenset()
    #: ``None`` when nothing says which is first: the ids are then read as written.
    first: int | None = None

    def slot(self, written: int) -> int:
        """The slot ``slice_info.config``'s filament ``id=written`` is: itself when the
        plate prints it (or nothing says what it prints), else the plate's first."""
        if not self.printed or self.first is None or written in self.printed:
            return written
        return self.first


def printed_filaments(archive: zipfile.ZipFile, plate_id: int) -> PrintedFilaments:
    """Plate ``plate_id``'s :class:`PrintedFilaments`: the header's filaments, else the
    sequence's; first the sequence's first, else the header's one filament when it names
    only one, and none when that is not among the filaments printed."""
    sequence = _sequence(archive, plate_id)
    printed = frozenset(_header_filaments(archive, plate_id)) or frozenset(sequence)
    first = sequence[0] if sequence else (next(iter(printed)) if len(printed) == 1 else None)
    if first not in printed:
        # The sequence and the header disagree: nothing says which filament is first.
        first = None
    return PrintedFilaments(printed=printed, first=first)


def _bounded(archive: zipfile.ZipFile, name: str) -> bytes | None:
    """Member ``name``, or ``None`` when it is missing or past `_MAX_SEQUENCE_BYTES`."""
    if name not in archive.namelist():
        return None
    with archive.open(name) as member:
        data = member.read(_MAX_SEQUENCE_BYTES + 1)
    return None if len(data) > _MAX_SEQUENCE_BYTES else data


def _physical_sides(archive: zipfile.ZipFile) -> list[Side] | None:
    """Each slicer extruder's physical side, by ``physical_extruder_map`` (physical 0 is
    the right, `bambuddy.extruders`); ``None`` unless it names one left and one right."""
    data = _bounded(archive, PROJECT_SETTINGS_NAME)
    settings = json.loads(data) if data is not None else None
    physical = settings.get("physical_extruder_map") if isinstance(settings, dict) else None
    if not isinstance(physical, list):
        return None
    sides = [side_of(int(value)) if str(value).isdigit() else None for value in physical]
    if sorted(side or "" for side in sides) != ["L", "R"]:
        return None
    return [side for side in sides if side is not None]


def filament_sides(archive: zipfile.ZipFile, plate_index: int) -> dict[int, Side]:
    """#4232: the side each filament ``slice_info.config`` names on plate ``plate_index``
    was sliced for, by its written id: its ``group_id`` names a ``<nozzle>``, whose
    1-based ``extruder_id`` is a slicer extruder, which ``physical_extruder_map`` makes a
    physical one (as `printers/nozzle_mapping.py` reads the rack's). Measured on
    v02.08.02.61's H2D and H2C slices (``tests/slicing/test_slice_info.py``). Empty on a
    single-nozzle printer, and wherever the file cannot say."""
    try:
        sides = _physical_sides(archive)
        data = _bounded(archive, SLICE_INFO_NAME) if sides is not None else None
        root = ET.fromstring(data) if data is not None else None
    except (*_MEMBER_ERRORS, ValueError, ET.ParseError):
        return {}
    if sides is None or root is None:
        return {}
    plates = root.findall("plate")
    plate = next(
        (
            each
            for each in plates
            if any(
                meta.get("key") == "index" and meta.get("value") == str(plate_index)
                for meta in each.findall("metadata")
            )
        ),
        plates[0] if len(plates) == 1 else None,
    )
    if plate is None:
        return {}
    extruders = {nozzle.get("id"): nozzle.get("extruder_id") for nozzle in plate.findall("nozzle")}
    found: dict[int, Side] = {}
    for filament in plate.findall("filament"):
        written, extruder = filament.get("id") or "", extruders.get(filament.get("group_id"))
        number = int(extruder) if extruder is not None and extruder.isdigit() else 0
        if written.isdigit() and 1 <= number <= len(sides):
            found[int(written)] = sides[number - 1]
    return found


def _sides_on_printed(sides: dict[int, Side], printed: PrintedFilaments) -> dict[int, Side]:
    """``sides`` on the filaments the plate prints, as `_on_printed` moves the grams; a
    slot written on two sides has none."""
    moved: dict[int, Side | None] = {}
    for written, side in sides.items():
        slot = printed.slot(written)
        moved[slot] = side if moved.get(slot, side) == side else None
    return {slot: side for slot, side in sorted(moved.items()) if side is not None}


def _on_printed(grams: dict[int, float], printed: PrintedFilaments) -> dict[int, float]:
    """``grams`` on the filaments the plate prints (#4711): grams on a filament it does
    not print are its first filament's."""
    if all(printed.slot(slot) == slot for slot in grams):
        return grams
    moved: dict[int, float] = {}
    for slot, used in grams.items():
        key = printed.slot(slot)
        moved[key] = round(moved.get(key, 0.0) + used, 2)
    return dict(sorted(moved.items()))


class NativeSliceRead(BaseModel):
    """What the native slicer's 3MF says about one plate. Not ``SlicedPlate``:
    ``filaments3mf.py`` has one."""

    plate_id: int
    print_time_seconds: int | None = None
    grams_by_slot: dict[int, float] = {}
    filament_used_g: float | None = None
    filament_changes: int | None = None
    #: The side each slot was sliced for (#4232), as the Bambuddy path's ``side``.
    side_by_slot: dict[int, Side] = {}


def plate_grams(payload: bytes | BinaryIO, plate_id: int) -> dict[int, float]:
    """``plate_id``'s grams per slot as :func:`read_sliced` reads them, without reading
    the G-code's body: what a LAN start's spool use is (#4643). Empty for a file or a
    plate that cannot be read."""
    try:
        read = read_filaments(_file(payload), sliced=True)
    except UnreadableFilamentsError:
        return {}
    profile = read.profile
    if profile is None or not profile.plates:
        return {}
    plate = profile.plates[0] if len(profile.plates) == 1 else profile.plate(plate_id)
    if plate is None:
        return {}
    grams = {slot.slot_id: slot.used_grams for slot in read.sliced.get(plate.index, [])}
    try:
        with zipfile.ZipFile(_file(payload)) as archive:
            printed = printed_filaments(archive, plate.index)
    except (*_MEMBER_ERRORS, KeyError):
        printed = PrintedFilaments()
    return _on_printed(grams, printed)


def read_sliced(payload: bytes | BinaryIO, plate_id: int) -> NativeSliceRead:
    """``plate_id``'s time, grams and filament changes. The only plate of a file of one
    is read whatever its number; a plate a file of several lacks is a 404; a file that
    cannot be read reads as every field unknown."""
    try:
        read = read_filaments(_file(payload), sliced=True)
    except UnreadableFilamentsError:
        return NativeSliceRead(plate_id=plate_id)
    profile = read.profile
    if profile is None or not profile.plates:
        return NativeSliceRead(plate_id=plate_id)
    if len(profile.plates) == 1:
        plate = profile.plates[0]
        if plate_id not in (0, plate.index):
            logger.info("the sliced file's only plate is not the one asked for")
    else:
        found = profile.plate(plate_id)
        if found is None:
            raise ApiError(404, f"the sliced file has no plate {plate_id}")
        plate = found
    index = plate.index
    grams_by_slot = {slot.slot_id: slot.used_grams for slot in read.sliced.get(index, [])}
    gcode, printed, sides = _plate_files(payload, index)
    grams_by_slot = _on_printed(grams_by_slot, printed)
    return NativeSliceRead(
        plate_id=plate_id,
        print_time_seconds=plate.print_seconds,
        grams_by_slot=grams_by_slot,
        filament_used_g=round(sum(grams_by_slot.values()), 2) if grams_by_slot else None,
        filament_changes=None if gcode is None else filament_changes(gcode),
        side_by_slot=_sides_on_printed(sides, printed),
    )
