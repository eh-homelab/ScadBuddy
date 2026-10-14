"""Restate a written 3MF in the colours of the spools a print chose (#476).

A 3MF is written at render time in the model's own colours, before anyone has picked
a spool. Bambuddy's slicer takes the filament colours of the presets it is handed, but
it keeps the cover images the file carries — so without this, the queue shows a plate
thumbnail in the model's colours while the printer lays down the spools'.
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


def _archive(entries: Sequence[tuple[str, bytes]], *, stored: bool = False) -> bytes:
    """The writer's own policy, so a file recoloured to the colours it already had
    comes back byte for byte. ``stored`` deflates nothing: for a copy only read back
    here, where deflating an 86 MB model is seconds of CPU for nothing (#2877)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
            info.compress_type = (
                zipfile.ZIP_STORED if stored or name.endswith(".png") else zipfile.ZIP_DEFLATED
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
    covered = [index for index in plates if cover_names(index)[0] in names]
    covers: dict[str, bytes] = {}
    with tempfile.TemporaryDirectory() as scratch:
        # `parts_from_3mf` reads a path, and reading the recoloured archive is what
        # gives each part its new colour through its extruder number. Written only when
        # a cover is to be drawn from it.
        path = Path(scratch) / "model.3mf"
        if covered:
            path.write_bytes(_archive(entries, stored=True))
        for index in covered:
            cover = cover_names(index)
            drawn = render_plate_thumbnails(parts_from_3mf(path, index))
            covers.update(
                zip(cover, (drawn.plate, drawn.plate_small, drawn.top, drawn.pick), strict=True)
            )
    return _archive([(name, covers.get(name, data)) for name, data in entries])
