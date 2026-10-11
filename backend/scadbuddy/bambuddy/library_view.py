"""Viewing a library file as an output is viewed (#1749, #1753): its preview GLB and the
mesh analysis the print checks read, both from the 3MF the print path fetches
(:meth:`~scadbuddy.bambuddy.print_source.LibrarySource.fetch_3mf`: the file's bytes, an
STL wrapped in ScadBuddy's own 3MF). Only how that 3MF is obtained differs from an
output's, whose preview and analysis are read from its ``model.3mf``.

Each plate is read where the file places it (:func:`read_plate_parts`). Both are cached
under ``cache/library-views/`` by the file's SHA-256 as Bambuddy states it and the plate,
the :data:`MAX_CACHED` most recently used files kept: the same bytes are the same view, and
a changed file is a new one. A file Bambuddy states no hash for is read each time: those
are files it indexes from an external folder, edited in place there, and neither their
size nor ``updated_at`` follows such an edit, so neither can stand in for the hash (#2160).

Each read spends a :class:`~scadbuddy.render.read_budget.ReadBudget` (#2087). Only a
view is kept, never a refusal, so a larger budget always reads a refused file again.
The budget is the only bound: the file is downloaded whole whatever its size, spooled to
the cache volume rather than held in memory (:func:`spooled_3mf`).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import BinaryIO

from pydantic import ValidationError

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import LibraryFile
from scadbuddy.bambuddy.print_source import SLICED_TYPE, LibrarySource, SpoolFullError, printable
from scadbuddy.render.geometry import ANALYSIS_VERSION, GeometryAnalysis, analyze_geometry
from scadbuddy.render.glb import write_glb
from scadbuddy.render.objects3mf import PlateRead, UnreadableObjectsError, read_plate_parts
from scadbuddy.render.read_budget import ReadBudget

#: Where the views are kept, under the data volume's cache.
CACHE_DIRNAME = "library-views"
#: How many cached files are kept, most recently used first: a preview and an analysis
#: each count.
MAX_CACHED = 64
#: Moves when what a view of the same bytes is changes, so an older one is not served.
VIEW_VERSION = 1
_HASH = re.compile(r"[0-9a-f]{64}")


class NotViewableError(Exception):
    """A library file that cannot be viewed, and why."""

    def __init__(self, file: LibraryFile, reason: str) -> None:
        super().__init__(f"{file.filename or f'library file {file.id}'}: {reason}")
        self.reason = reason


#: Where a library file is spooled while it is read, under the data volume's cache.
SPOOL_DIRNAME = "library-spool"
#: What a spooled download leaves free on its volume: a file of any size is read, but
#: never by filling the volume that holds the models, outputs and their history.
SPOOL_RESERVE_BYTES = 1024**3


def _room(spool: Path) -> Callable[[], bool]:
    def check() -> bool:
        return shutil.disk_usage(spool).free > SPOOL_RESERVE_BYTES

    return check


@contextlib.asynccontextmanager
async def spooled_3mf(
    client: BambuddyClient, source: LibrarySource, cache: Path, budget: ReadBudget
) -> AsyncIterator[BinaryIO | None]:
    """``source``'s 3MF downloaded whole into an unnamed file under ``cache``, open at
    its start; ``None`` for an STL that holds no mesh. A library file of any size can be
    read this way: what the read may cost is ``budget``'s to say, not the file's size
    (an STL past it is :class:`~scadbuddy.render.objects3mf.OverBudgetError` before it
    is wrapped). It stops as :class:`SpoolFullError` before the volume's last
    :data:`SPOOL_RESERVE_BYTES`."""
    spool = cache / SPOOL_DIRNAME
    await asyncio.to_thread(spool.mkdir, parents=True, exist_ok=True)
    with await asyncio.to_thread(lambda: tempfile.TemporaryFile("w+b", dir=spool)) as out:
        if not await source.write_3mf(client, out, budget, _room(spool)):
            yield None
            return
        await asyncio.to_thread(out.seek, 0)
        yield out


async def _read(
    client: BambuddyClient,
    cache: Path,
    file: LibraryFile,
    plate: int,
    budget: ReadBudget | None,
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
    try:
        async with spooled_3mf(client, source, cache, budget or ReadBudget()) as payload:
            if payload is None:
                raise NotViewableError(file, "it holds no mesh ScadBuddy can read")
            return await asyncio.to_thread(read_plate_parts, payload, plate, budget)
    except UnreadableObjectsError as error:
        raise NotViewableError(file, str(error)) from None
    except SpoolFullError as error:
        raise NotViewableError(file, str(error)) from None


def _cached(cache: Path, file: LibraryFile, name: str) -> Path | None:
    digest = (file.file_hash or "").lower()
    if not _HASH.fullmatch(digest):
        return None
    return cache / CACHE_DIRNAME / f"v{VIEW_VERSION}-{digest}-{name}"


def _store(path: Path, data: bytes, keep: int) -> None:
    """Write ``path`` aside and rename it in, then drop all but the ``keep`` most recently
    used files (a hit touches its file: :func:`_hit`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    partial.write_bytes(data)
    partial.replace(path)
    used: list[tuple[int, Path]] = []
    for entry in path.parent.iterdir():
        if entry.name.startswith("."):
            continue
        try:
            used.append((entry.stat().st_mtime_ns, entry))
        except FileNotFoundError:
            continue  # another prune removed it between the listing and the stat
    used.sort(key=lambda item: item[0], reverse=True)
    for _, stale in used[keep:]:
        stale.unlink(missing_ok=True)


def _hit(path: Path) -> None:
    """Mark a cached file used, so the prune keeps it (#2160). One pruned meanwhile is
    simply not marked."""
    with contextlib.suppress(FileNotFoundError):
        os.utime(path)


def _glb(read: PlateRead) -> bytes:
    with tempfile.TemporaryDirectory() as scratch:
        out = Path(scratch) / "preview.glb"
        write_glb(read.parts, out)
        return out.read_bytes()


def plate_glb(payload: bytes, plate: int, budget: ReadBudget | None = None) -> bytes:
    """Plate ``plate`` of a 3MF as a preview GLB, its parts in their colours (#2169: an
    output's plate in the print dialog's pane, as a library file's is). A plate the file
    lacks is :class:`~scadbuddy.render.geometry.NoSuchPlateError`."""
    return _glb(read_plate_parts(payload, plate, budget))


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
    if path is not None:
        try:
            cached = await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            pass
        else:
            await asyncio.to_thread(_hit, path)
            return cached
    data = await asyncio.to_thread(_glb, await _read(client, cache, file, plate, budget))
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
            await asyncio.to_thread(_hit, path)
            return cached
    read = await _read(client, cache, file, plate, budget)
    analysis = await asyncio.to_thread(_analysis, read, plate)
    if path is not None:
        data = (analysis.model_dump_json(indent=2) + "\n").encode("utf-8")
        await asyncio.to_thread(_store, path, data, MAX_CACHED)
    return analysis
