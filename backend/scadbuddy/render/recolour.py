"""Restate a written 3MF in the colours of the spools a print chose (#476).

A 3MF is written at render time in the model's own colours, before anyone has picked
a spool. Bambuddy's slicer takes the filament colours of the presets it is handed, but
it keeps the cover images the file carries — so without this, the queue shows a plate
thumbnail in the model's colours while the printer lays down the spools'.

:func:`pin_extruders_3mf` is the same rewrite's other half (#469): the extruder each
filament is sliced for, which the spools' AMS wiring fixes.
"""

from __future__ import annotations

import io
import json
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path

from scadbuddy.render.bambu3mf import (
    PROJECT_SETTINGS_NAME,
    ZIP_TIMESTAMP,
    cover_names,
    laid_out_plates,
)
from scadbuddy.render.geometry import parts_from_3mf
from scadbuddy.render.split import normalise_colour
from scadbuddy.render.thumbnail import render_plate_thumbnails


def _archive(entries: Sequence[tuple[str, bytes]]) -> bytes:
    """The writer's own policy, so a file recoloured to the colours it already had
    comes back byte for byte."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
            info.compress_type = (
                zipfile.ZIP_STORED if name.endswith(".png") else zipfile.ZIP_DEFLATED
            )
            out.writestr(info, data)
    return buffer.getvalue()


def recolour_3mf(payload: bytes, colours: Sequence[str]) -> bytes:
    """Return ``payload`` with ``filament_colour`` set to ``colours`` and every plate's
    cover images redrawn in them.

    ``colours`` is one per filament of the file, in filament order; a different count
    raises :class:`ValueError`, since the extruder numbers of every part index into it.
    A file written without covers gets none.
    """
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
        plates = [each.index for each in laid_out_plates(archive)]
    settings = json.loads(dict(entries)[PROJECT_SETTINGS_NAME])
    current = settings.get("filament_colour") or []
    if len(colours) != len(current):
        raise ValueError(
            f"the 3MF has {len(current)} filament colours, but {len(colours)} were given"
        )
    settings["filament_colour"] = [normalise_colour(colour) for colour in colours]
    entries = [
        (name, (json.dumps(settings, indent=4) + "\n").encode("utf-8"))
        if name == PROJECT_SETTINGS_NAME
        else (name, data)
        for name, data in entries
    ]

    names = {name for name, _ in entries}
    covers: dict[str, bytes] = {}
    with tempfile.TemporaryDirectory() as scratch:
        # `parts_from_3mf` reads a path, and reading the recoloured archive is what
        # gives each part its new colour through its extruder number.
        path = Path(scratch) / "model.3mf"
        path.write_bytes(_archive(entries))
        for index in plates:
            cover = cover_names(index)
            if cover[0] not in names:
                continue
            drawn = render_plate_thumbnails(parts_from_3mf(path, index))
            covers.update(
                zip(cover, (drawn.plate, drawn.plate_small, drawn.top, drawn.pick), strict=True)
            )
    return _archive([(name, covers.get(name, data)) for name, data in entries])


#: The H2C printer preset's ``physical_extruder_map``: logical extruder 1 is physical 1
#: (the left one) and logical 2 is physical 0 (the right). ScadBuddy's written file
#: carries no map of its own — the slicer takes it from the printer preset — so this
#: is what applies unless the file states one.
H2C_PHYSICAL_EXTRUDER_MAP: tuple[str, ...] = ("1", "0")


def pin_extruders_3mf(payload: bytes, extruders: Sequence[int]) -> bytes:
    """Return ``payload`` with each filament pinned to a physical extruder (#469).

    ``extruders`` is one per filament, in filament order: 0 the right extruder, 1 the
    left, as the printer numbers them. They are written as BambuStudio's
    ``filament_map_mode: "Manual"`` and ``filament_map``, which holds each filament's
    1-based *logical* extruder, translated through ``physical_extruder_map``. Left at
    the default ``"Auto For Flush"``, the slicer spreads the filaments over both
    extruders whatever the AMS they are loaded in is wired to.

    Only ``project_settings.config`` changes; the covers are untouched, so this composes
    with :func:`recolour_3mf` in either order. A different count raises
    :class:`ValueError`, as there.
    """
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
    settings = json.loads(dict(entries)[PROJECT_SETTINGS_NAME])
    count = len(settings.get("filament_colour") or [])
    if len(extruders) != count:
        raise ValueError(
            f"the 3MF has {count} filaments, but {len(extruders)} extruders were given"
        )
    physical = [
        str(value) for value in settings.get("physical_extruder_map") or H2C_PHYSICAL_EXTRUDER_MAP
    ]
    settings["filament_map_mode"] = "Manual"
    settings["filament_map"] = [str(physical.index(str(extruder)) + 1) for extruder in extruders]
    return _archive(
        [
            (name, (json.dumps(settings, indent=4) + "\n").encode("utf-8"))
            if name == PROJECT_SETTINGS_NAME
            else (name, data)
            for name, data in entries
        ]
    )
