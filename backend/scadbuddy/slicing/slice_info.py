"""Reading a sliced 3MF (#3822): a plate's time, grams per slot and filament changes."""

from __future__ import annotations

import io
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


def _file(payload: bytes | BinaryIO) -> BinaryIO:
    """A file over ``payload``, at its start; a file is read from its start, never
    copied into memory (Task 12 review M4)."""
    if isinstance(payload, bytes):
        return io.BytesIO(payload)
    payload.seek(0)
    return payload


def plate_gcode(payload: bytes | BinaryIO, plate_id: int) -> bytes | None:
    """The sliced plate's G-code (``Metadata/plate_N.gcode``, else the only plate's);
    None when there is none, the file is not a zip, or it is past :data:`MAX_GCODE_BYTES`.
    ``payload`` is the file's bytes or the file itself."""
    try:
        with zipfile.ZipFile(_file(payload)) as archive:
            names = [
                name
                for name in archive.namelist()
                if re.fullmatch(r"Metadata/plate_\d+\.gcode", name)
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
    except (zipfile.BadZipFile, KeyError) as error:
        logger.info("the sliced file could not be read", extra={"error": type(error).__name__})
        return None


def filament_changes(gcode: bytes) -> int:
    """How many times the G-code changes filament: its loads (255 is an unload), minus
    the first."""
    loads = [int(match) for match in _LOAD.findall(gcode) if int(match) != 255]
    return max(len(loads) - 1, 0)


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
    gcode = plate_gcode(payload, index)
    return NativeSliceRead(
        plate_id=plate_id,
        print_time_seconds=plate.print_seconds,
        grams_by_slot=grams_by_slot,
        filament_used_g=round(sum(grams_by_slot.values()), 2) if grams_by_slot else None,
        filament_changes=None if gcode is None else filament_changes(gcode),
    )
