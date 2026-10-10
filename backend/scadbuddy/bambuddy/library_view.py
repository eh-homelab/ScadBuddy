"""Viewing a library file as an output is viewed (#1749, #1753): its preview GLB and the
mesh analysis the print checks read, both from the 3MF the print path fetches
(:meth:`~scadbuddy.bambuddy.print_source.LibrarySource.fetch_3mf`: the file's bytes, an
STL wrapped in ScadBuddy's own 3MF). Only how that 3MF is obtained differs from an
output's, whose preview and analysis are read from its ``model.3mf``.

Each plate is read where the file places it (:func:`read_plate_parts`). Both are cached
under ``cache/library-views/`` by the file's SHA-256 as Bambuddy states it and the plate,
the newest :data:`MAX_CACHED` files kept: the same bytes are the same view, and a changed
file is a new one. A file Bambuddy states no hash for is read each time.

Each read spends a :class:`~scadbuddy.render.read_budget.ReadBudget` (#2087). Only a
view is kept, never a refusal, so a larger budget always reads a refused file again.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
import uuid
from pathlib import Path

from pydantic import ValidationError

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import LibraryFile
from scadbuddy.bambuddy.print_source import SLICED_TYPE, LibrarySource, printable
from scadbuddy.render.geometry import ANALYSIS_VERSION, GeometryAnalysis, analyze_geometry
from scadbuddy.render.glb import write_glb
from scadbuddy.render.objects3mf import PlateRead, UnreadableObjectsError, read_plate_parts
from scadbuddy.render.read_budget import ReadBudget

#: Where the views are kept, under the data volume's cache.
CACHE_DIRNAME = "library-views"
#: How many cached files are kept, newest first: a preview and an analysis each count.
MAX_CACHED = 64
#: Moves when what a view of the same bytes is changes, so an older one is not served.
VIEW_VERSION = 1
_HASH = re.compile(r"[0-9a-f]{64}")


class NotViewableError(Exception):
    """A library file that cannot be viewed, and why."""

    def __init__(self, file: LibraryFile, reason: str) -> None:
        super().__init__(f"{file.filename or f'library file {file.id}'}: {reason}")
        self.reason = reason


async def _read(
    client: BambuddyClient, file: LibraryFile, plate: int, budget: ReadBudget | None
) -> PlateRead:
    kind = (file.file_type or "").lower()
    if kind == SLICED_TYPE:
        raise NotViewableError(file, "it is sliced already, so ScadBuddy cannot read its mesh")
    if not printable(kind):
        raise NotViewableError(
            file,
            f"ScadBuddy reads only 3MF and STL files, and it is a {kind or 'file of unknown type'}",
        )
    source = LibrarySource(
        file_id=file.id, colours=[], plates=[], filename=file.filename, file_type=kind
    )
    payload = await source.fetch_3mf(client)
    if payload is None:
        raise NotViewableError(file, "it is too large to read, or holds no mesh ScadBuddy can read")
    try:
        return await asyncio.to_thread(read_plate_parts, payload, plate, budget)
    except UnreadableObjectsError as error:
        raise NotViewableError(file, str(error)) from None


def _cached(cache: Path, file: LibraryFile, name: str) -> Path | None:
    digest = (file.file_hash or "").lower()
    if not _HASH.fullmatch(digest):
        return None
    return cache / CACHE_DIRNAME / f"v{VIEW_VERSION}-{digest}-{name}"


def _store(path: Path, data: bytes, keep: int) -> None:
    """Write ``path`` aside and rename it in, then drop all but the ``keep`` newest files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    partial.write_bytes(data)
    partial.replace(path)
    kept = sorted(
        (entry for entry in path.parent.iterdir() if not entry.name.startswith(".")),
        key=lambda entry: entry.stat().st_mtime_ns,
        reverse=True,
    )
    for stale in kept[keep:]:
        stale.unlink(missing_ok=True)


def _glb(read: PlateRead) -> bytes:
    with tempfile.TemporaryDirectory() as scratch:
        out = Path(scratch) / "preview.glb"
        write_glb(read.parts, out)
        return out.read_bytes()


def _analysis(read: PlateRead, plate: int) -> GeometryAnalysis:
    analysis = analyze_geometry(read.parts, extruders=[part.material_index for part in read.parts])
    return analysis.model_copy(update={"plate": plate, "plates": read.plates})


async def library_preview(
    client: BambuddyClient,
    cache: Path,
    file_id: int,
    plate: int = 1,
    budget: ReadBudget | None = None,
) -> bytes:
    """Plate ``plate`` of library file ``file_id`` as a preview GLB, its parts in their
    colours. A file deleted in Bambuddy is the client's 404; one that cannot be read is
    :class:`NotViewableError`; a plate it lacks is
    :class:`~scadbuddy.render.geometry.NoSuchPlateError`."""
    file = await client.library_file(file_id)
    path = _cached(cache, file, f"plate-{plate}.glb")
    if path is not None and await asyncio.to_thread(path.is_file):
        return await asyncio.to_thread(path.read_bytes)
    data = await asyncio.to_thread(_glb, await _read(client, file, plate, budget))
    if path is not None:
        await asyncio.to_thread(_store, path, data, MAX_CACHED)
    return data


async def library_geometry(
    client: BambuddyClient,
    cache: Path,
    file_id: int,
    plate: int = 1,
    budget: ReadBudget | None = None,
) -> GeometryAnalysis:
    """The mesh analysis of plate ``plate`` of library file ``file_id``, as
    ``OutputStore.geometry`` measures an output's; refused as :func:`library_preview`."""
    file = await client.library_file(file_id)
    path = _cached(cache, file, f"plate-{plate}.geometry.json")
    if path is not None:
        try:
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
            cached = GeometryAnalysis.model_validate_json(text)
        except (OSError, ValidationError):
            cached = None
        if cached is not None and cached.version == ANALYSIS_VERSION:
            return cached
    read = await _read(client, file, plate, budget)
    analysis = await asyncio.to_thread(_analysis, read, plate)
    if path is not None:
        data = (analysis.model_dump_json(indent=2) + "\n").encode("utf-8")
        await asyncio.to_thread(_store, path, data, MAX_CACHED)
    return analysis
