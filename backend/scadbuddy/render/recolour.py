"""Restate a written 3MF in the colours of the spools a print chose (#476).

A 3MF is written at render time in the model's own colours, before anyone has picked
a spool. Bambuddy's slicer takes the filament colours of the presets it is handed, but
it keeps the cover images the file carries — so without this, the queue shows a plate
thumbnail in the model's colours while the printer lays down the spools'.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Sequence

from scadbuddy.render.bambu3mf import (
    PROJECT_SETTINGS_NAME,
    cover_names,
    laid_out_plates,
    rezipped,
)
from scadbuddy.render.geometry import parts_from_3mf
from scadbuddy.render.split import normalise_colour
from scadbuddy.render.thumbnail import render_plate_thumbnails


def recolour_3mf(payload: bytes, colours: Sequence[str]) -> bytes:
    """Return ``payload`` with ``filament_colour`` set to ``colours`` and every plate's
    cover images redrawn in them.

    ``colours`` is one per filament of the file, in filament order; a different count
    raises :class:`ValueError`, since the extruder numbers of every part index into it.
    A file written without covers gets none. Written in the writer's policy, every entry
    it does not change copied as its compressed bytes (:func:`rezipped`, #3679).
    """
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = set(archive.namelist())
        settings = json.loads(archive.read(PROJECT_SETTINGS_NAME))
        plates = [each.index for each in laid_out_plates(archive)]
    current = settings.get("filament_colour") or []
    if len(colours) != len(current):
        raise ValueError(
            f"the 3MF has {len(current)} filament colours, but {len(colours)} were given"
        )
    settings["filament_colour"] = [normalise_colour(colour) for colour in colours]
    changed = {PROJECT_SETTINGS_NAME: (json.dumps(settings, indent=4) + "\n").encode("utf-8")}
    covered = [index for index in plates if cover_names(index)[0] in names]
    if covered:
        # Read from the recoloured archive, in memory: that is what gives each part its
        # new colour through its extruder number.
        recoloured = io.BytesIO(rezipped(payload, changed))
        for index in covered:
            drawn = render_plate_thumbnails(parts_from_3mf(recoloured, index))
            changed.update(
                zip(
                    cover_names(index),
                    (drawn.plate, drawn.plate_small, drawn.top, drawn.pick),
                    strict=True,
                )
            )
    return rezipped(payload, changed)
