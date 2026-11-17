"""Restate a written 3MF in the colours of the spools a print chose (#476).

A 3MF is written at render time in the model's own colours, before anyone has picked
a spool. Bambuddy's slicer takes the filament colours of the presets it is handed, but
it keeps the cover images the file carries — so without this, the queue shows a plate
thumbnail in the model's colours while the printer lays down the spools'. ScadBuddy's
own slicer, the Bambu Studio CLI, keeps them too: run headless, its GLFW (built for
Wayland only) finds no display, so it draws no cover and copies the input's (#6170).
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Callable, Sequence
from typing import BinaryIO

from scadbuddy.render.bambu3mf import (
    PROJECT_SETTINGS_NAME,
    cover_names,
    laid_out_plates,
    rezipped,
)
from scadbuddy.render.geometry import parts_from_3mf
from scadbuddy.render.split import ColourPart, normalise_colour
from scadbuddy.render.thumbnail import render_plate_thumbnails

#: Reads the parts of the plates named from a 3MF, by plate index.
PlatesReader = Callable[[BinaryIO, Sequence[int]], dict[int, list[ColourPart]]]


def own_plates(archive: BinaryIO, plates: Sequence[int]) -> dict[int, list[ColourPart]]:
    """The plates of a ScadBuddy 3MF (:func:`~scadbuddy.render.geometry.parts_from_3mf`)."""
    return {index: parts_from_3mf(archive, index) for index in plates}


def recolour_3mf(
    payload: bytes, colours: Sequence[str], *, read_plates: PlatesReader = own_plates
) -> bytes:
    """Return ``payload`` with ``filament_colour`` set to ``colours`` and every plate's
    cover images redrawn in them.

    ``colours`` is one per filament of the file, in filament order; a different count
    raises :class:`ValueError`, since the extruder numbers of every part index into it.
    A file written without covers gets none. Written in the writer's policy, every entry
    it does not change copied as its compressed bytes (:func:`rezipped`, #3679). An
    author's ``plate_no_light_<n>.png`` is replaced by the lit cover.

    ``read_plates`` reads the covered plates' parts from the recoloured file. The default
    reads ScadBuddy's own files. An author's file needs
    :func:`~scadbuddy.render.objects3mf.read_plates_parts`, which reads build items,
    multi-part objects and object-level extruders, within a read budget (#6170). A plate
    with no parts keeps its covers.
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
        for index, parts in read_plates(recoloured, covered).items():
            if not parts:
                continue
            drawn = render_plate_thumbnails(parts)
            changed.update(
                zip(
                    cover_names(index),
                    (drawn.plate, drawn.plate_small, drawn.top, drawn.pick),
                    strict=True,
                )
            )
            # Bambu Studio saves an unlit copy of the cover too, which its CLI keeps as
            # it keeps the others (#6170); this side draws no unlit view.
            if (unlit := f"Metadata/plate_no_light_{index}.png") in names:
                changed[unlit] = drawn.plate
    return rezipped(payload, changed)
