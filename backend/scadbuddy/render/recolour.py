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
from collections.abc import Sequence

from scadbuddy.render.bambu3mf import (
    PROJECT_SETTINGS_NAME,
    cover_names,
    laid_out_plates,
    rezipped,
)
from scadbuddy.render.geometry import MeshBudget, parts_from_3mf
from scadbuddy.render.split import normalise_colour
from scadbuddy.render.thumbnail import render_plate_thumbnails


def recolour_3mf(
    payload: bytes, colours: Sequence[str], *, max_triangles: int | None = None
) -> bytes:
    """Return ``payload`` with ``filament_colour`` set to ``colours`` and every plate's
    cover images redrawn in them.

    ``colours`` is one per filament of the file, in filament order; a different count
    raises :class:`ValueError`, since the extruder numbers of every part index into it.
    A file written without covers gets none. Written in the writer's policy, every entry
    it does not change copied as its compressed bytes (:func:`rezipped`, #3679). An
    author's ``plate_no_light_<n>.png`` is replaced by the lit cover. With
    ``max_triangles``, the meshes read for the covers, every plate's together, may hold
    at most that many triangles, and three vertices for each, counted as parsed (one
    :class:`~scadbuddy.render.geometry.MeshBudget`): past it,
    :class:`TooManyTrianglesError`, raised as the parse reaches it, so an untrusted file
    cannot make the read unbounded.
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
        budget = None if max_triangles is None else MeshBudget(max_triangles)
        for index in covered:
            parts = parts_from_3mf(recoloured, index, budget=budget)
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
