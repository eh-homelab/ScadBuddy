"""Reading a sliced 3MF (#3822): a plate's time, grams per slot and filament changes."""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from typing import BinaryIO

from pydantic import BaseModel

from scadbuddy.bambuddy.filaments3mf import UnreadableFilamentsError, read_filaments
from scadbuddy.core.problems import ApiError

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


def _file(payload: bytes | BinaryIO) -> BinaryIO:
    """A file over ``payload``, at its start; a file is read from its start, never
    copied into memory (Task 12 review M4)."""
    if isinstance(payload, bytes):
        return io.BytesIO(payload)
    payload.seek(0)
    return payload


def _gcode(archive: zipfile.ZipFile, plate_id: int) -> bytes | None:
    """:func:`plate_gcode` in an open archive."""
    names = [
        name for name in archive.namelist() if re.fullmatch(r"Metadata/plate_\d+\.gcode", name)
    ]
    wanted = f"Metadata/plate_{plate_id}.gcode"
    name = wanted if wanted in names else (names[0] if len(names) == 1 else None)
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


def _plate_files(payload: bytes | BinaryIO, plate_id: int) -> tuple[bytes | None, list[int]]:
    """The plate's G-code (:func:`plate_gcode`) and its filament sequence
    (:func:`_sequence`), read in one pass over the archive."""
    try:
        with zipfile.ZipFile(_file(payload)) as archive:
            return _gcode(archive, plate_id), _sequence(archive, plate_id)
    except (zipfile.BadZipFile, KeyError) as error:
        logger.info("the sliced file could not be read", extra={"error": type(error).__name__})
        return None, []


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
    except (zipfile.BadZipFile, ValueError):
        return []
    plate = doc.get(f"plate_{index}") if isinstance(doc, dict) else None
    sequence = plate.get("sequence") if isinstance(plate, dict) else None
    if not isinstance(sequence, list):
        return []
    return [n for n in sequence if isinstance(n, int) and not isinstance(n, bool) and n >= 1]


def _header_filaments(gcode: bytes | None) -> list[int]:
    """The 1-based filaments the G-code's header names (``; filament: 2,3``)."""
    if gcode is None:
        return []
    end = gcode.find(b"; HEADER_BLOCK_END")
    found = _HEADER_FILAMENTS.search(gcode[: end if end >= 0 else _HEADER_BYTES])
    if found is None:
        return []
    return [int(n) for n in found.group(1).split(b",") if n.strip().isdigit() and int(n) >= 1]


def _printed(gcode: bytes | None, sequence: list[int]) -> tuple[set[int], int | None]:
    """The filaments a plate prints, and the one it prints first: the sequence's first,
    else the header's one filament when it names only one; ``None`` when neither says."""
    printed = set(_header_filaments(gcode)) or set(sequence)
    first = sequence[0] if sequence else (next(iter(printed)) if len(printed) == 1 else None)
    return printed, first


def _on_printed(grams: dict[int, float], printed: set[int], first: int | None) -> dict[int, float]:
    """``grams`` on the filaments the plate prints (#4711). Bambu Studio's CLI counts
    what a plate extrudes before its first filament change as filament 1's, a filament
    the plate may never load: a plate of one filament is all ``id="1"``, whatever it
    prints (measured on v02.08.02.61, ``tests/slicing/test_slice_info.py``). Grams on a
    filament the plate does not print are its first filament's."""
    if not printed or first is None or all(slot in printed for slot in grams):
        return grams
    moved: dict[int, float] = {}
    for slot, used in grams.items():
        key = slot if slot in printed else first
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
    gcode, sequence = _plate_files(payload, index)
    grams_by_slot = _on_printed(grams_by_slot, *_printed(gcode, sequence))
    return NativeSliceRead(
        plate_id=plate_id,
        print_time_seconds=plate.print_seconds,
        grams_by_slot=grams_by_slot,
        filament_used_g=round(sum(grams_by_slot.values()), 2) if grams_by_slot else None,
        filament_changes=None if gcode is None else filament_changes(gcode),
    )
