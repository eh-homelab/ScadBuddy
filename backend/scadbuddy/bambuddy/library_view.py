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
import io
import logging
import re
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable
from functools import partial
from pathlib import Path
from typing import BinaryIO

from pydantic import ValidationError

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.filaments3mf import (
    FILAMENTS_VERSION,
    LocalFilaments,
    UnreadableFilamentsError,
    read_filaments,
)
from scadbuddy.bambuddy.models import LibraryFile
from scadbuddy.bambuddy.print_source import (
    SLICED_TYPE,
    FetchedBytes,
    LibrarySource,
    ReadOutOfMemoryError,
    SpoolFullError,
    is_sliced,
    printable,
)
from scadbuddy.print_library.reads import LibraryReads, reads_of
from scadbuddy.render.geometry import ANALYSIS_VERSION, GeometryAnalysis, analyze_geometry
from scadbuddy.render.glb import write_glb
from scadbuddy.render.objects3mf import PlateRead, UnreadableObjectsError, read_plate_parts
from scadbuddy.render.read_budget import ReadBudget
from scadbuddy.store.download_cache import (
    RESERVE_BYTES,
    DownloadCache,
    is_sha256,
    store_bytes,
    touch,
)

logger = logging.getLogger(__name__)

#: A file's filament slots (#2550), kept apart from the views so that neither crowds
#: the other out of its :data:`MAX_CACHED`.
FILAMENTS_DIRNAME = "library-filaments"
FILAMENTS_NAME = f"filaments-v{FILAMENTS_VERSION}.json"

#: Where the views are kept, under the data volume's cache.
CACHE_DIRNAME = "library-views"
#: How many cached files are kept, most recently used first: a preview and an analysis
#: each count.
MAX_CACHED = 64
#: Moves when what a view of the same bytes is changes, so an older one is not served.
#: 2: a painted part is drawn in its painting's colours (#3956).
VIEW_VERSION = 2
_HASH = re.compile(r"[0-9a-f]{64}")


class NotViewableError(Exception):
    """A library file that cannot be viewed, and why."""

    def __init__(self, file: LibraryFile, reason: str) -> None:
        super().__init__(f"{file.filename or f'library file {file.id}'}: {reason}")
        self.reason = reason


#: Where a library file is spooled while it is read, under the data volume's cache.
SPOOL_DIRNAME = "library-spool"
#: What a spooled download leaves free on its volume: a file of any size is read, but
#: not by filling the volume that holds the models, outputs and their history. Checked
#: before each 1 MiB chunk, so spools running at once may each go one chunk past it.
SPOOL_RESERVE_BYTES = RESERVE_BYTES


def _room(spool: Path) -> Callable[[], bool]:
    def check() -> bool:
        return shutil.disk_usage(spool).free > SPOOL_RESERVE_BYTES

    return check


@contextlib.asynccontextmanager
async def spooled_3mf(
    client: BambuddyClient | None,
    source: LibrarySource,
    cache: Path,
    budget: ReadBudget,
    *,
    downloads: DownloadCache | None = None,
    file_hash: str | None = None,
) -> AsyncIterator[BinaryIO | None]:
    """``source``'s 3MF downloaded whole into an unnamed file under ``cache``, open at
    its start; ``None`` for an STL that holds no mesh. A library file of any size can be
    read this way: what the read may cost is ``budget``'s to say, not the file's size
    (an STL past it is :class:`~scadbuddy.render.objects3mf.OverBudgetError` before it
    is wrapped). It stops as :class:`SpoolFullError` before the volume's last
    :data:`SPOOL_RESERVE_BYTES`.

    #2788: a 3MF whose hash Bambuddy states is read from ``downloads`` when another
    read, in any process, downloaded the same bytes; else it is downloaded into it.
    An STL is not kept: what is read is its wrapped 3MF, not the file's bytes."""
    if downloads is not None and source.file_type == "3mf" and is_sha256(file_hash):
        spool = cache / SPOOL_DIRNAME
        # Read from a checked private copy, never the shared entry itself. A fault in
        # the cache reads as a miss.
        copy = await asyncio.to_thread(
            downloads.private_copy, file_hash, spool, what="library_file"
        )
        if copy is not None:
            with copy:
                yield copy
            return
        await asyncio.to_thread(spool.mkdir, parents=True, exist_ok=True)
        with await asyncio.to_thread(lambda: tempfile.TemporaryFile("w+b", dir=spool)) as out:
            # Downloaded into this read's own file, as without a cache; a copy is kept.
            # Bytes that are not the stated hash (the file changed since) are read all
            # the same, and not kept. Keeping it is best effort: a volume that fails is
            # a later miss, never a failed read.
            await source.write_3mf(client, out, budget, _room(spool))
            try:
                await asyncio.to_thread(downloads.keep_file, file_hash, out)
            except OSError:
                logger.warning(
                    "could not keep a library file", extra={"sha256": file_hash}, exc_info=True
                )
            await asyncio.to_thread(out.seek, 0)
            yield out
        return
    spool = cache / SPOOL_DIRNAME
    await asyncio.to_thread(spool.mkdir, parents=True, exist_ok=True)
    with await asyncio.to_thread(lambda: tempfile.TemporaryFile("w+b", dir=spool)) as out:
        if not await source.write_3mf(client, out, budget, _room(spool)):
            yield None
            return
        await asyncio.to_thread(out.seek, 0)
        yield out


async def _read(
    client: BambuddyClient | None,
    cache: Path,
    file: LibraryFile,
    plate: int,
    budget: ReadBudget | None,
    downloads: DownloadCache | None,
    files: LibraryReads | None = None,
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
        file_id=file.id,
        colours=[],
        plates=[],
        filename=file.filename,
        file_type=kind,
        files=files,
    )
    try:
        async with spooled_3mf(
            client,
            source,
            cache,
            budget or ReadBudget(),
            downloads=downloads,
            file_hash=file.file_hash,
        ) as payload:
            if payload is None:
                raise NotViewableError(file, "it holds no mesh ScadBuddy can read")
            slots = _filaments_at(cache, file) if kind == "3mf" else None
            if slots is not None and not await asyncio.to_thread(slots.exists):
                # The same fetch serves the print dialog's slots (#2550).
                await _keep_filaments(cache, file, payload, budget)
            try:
                return await asyncio.to_thread(read_plate_parts, payload, plate, budget)
            except MemoryError:
                raise ReadOutOfMemoryError from None
    except UnreadableObjectsError as error:
        raise NotViewableError(file, str(error)) from None
    except (SpoolFullError, ReadOutOfMemoryError) as error:
        raise NotViewableError(file, str(error)) from None


async def _keep_filaments(
    cache: Path, file: LibraryFile, payload: BinaryIO, budget: ReadBudget | None
) -> LocalFilaments | None:
    """``payload``'s filament slots, cached for ``file`` when it has a hash; ``None``
    when they cannot be read here. Leaves ``payload`` at its start. A sliced file's are
    read with what it was sliced for (#2671)."""
    try:
        local = await asyncio.to_thread(
            partial(read_filaments, sliced=is_sliced(file.file_type)), payload, budget
        )
    except UnreadableFilamentsError as error:
        logger.info(
            "could not read the library file's filament slots; Bambuddy is asked instead",
            extra={"library_file_id": file.id, "reason": str(error)},
        )
        return None
    finally:
        await asyncio.to_thread(payload.seek, 0)
    path = _filaments_at(cache, file)
    if path is not None:
        data = (local.model_dump_json() + "\n").encode("utf-8")
        await asyncio.to_thread(_store, path, data, MAX_CACHED)
    return local


async def library_filaments(
    client: BambuddyClient | None,
    cache: Path,
    file: LibraryFile,
    budget: ReadBudget | None = None,
    downloads: DownloadCache | None = None,
    files: LibraryReads | None = None,
    *,
    fetched: FetchedBytes | None = None,
) -> LocalFilaments | None:
    """The filament slots of library file ``file``, read from the file itself (#2550):
    from the cache when its preview, or an earlier read, fetched the same bytes, else
    from ``fetched``, a download the caller makes anyway (the check's, #4495), else from
    a download of it; a read is cached either way. ``None`` for a file ScadBuddy cannot
    read them from (not a 3MF, unreadable, too large for the volume), which Bambuddy is
    then asked about."""
    file_type = (file.file_type or "").lower()
    if file_type not in ("3mf", SLICED_TYPE):
        return None
    path = _filaments_at(cache, file)
    if path is not None:
        try:
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
            cached = LocalFilaments.model_validate_json(text)
        except (OSError, ValidationError):
            cached = None
        if cached is not None:
            await asyncio.to_thread(_hit, path)
            return cached
    if fetched is not None and file_type == "3mf":
        try:
            held = await fetched()
            if held is not None:
                if downloads is not None and is_sha256(file.file_hash):
                    # As the spool keeps its download (#2788): a later preview of these
                    # bytes reads them here (#4562 review). Best effort, as there.
                    try:
                        await asyncio.to_thread(downloads.put, file.file_hash, held)
                    except OSError:
                        logger.warning(
                            "could not keep a library file",
                            extra={"sha256": file.file_hash},
                            exc_info=True,
                        )
                return await _keep_filaments(cache, file, io.BytesIO(held), budget)
        except MemoryError:
            # As the spool's below: read from disk instead (#4553 review). The caller's
            # fetch keeps its failure (`PrintPipeline._fetch`), so its own read of the
            # file fails at once too and never downloads it again (#4571 review).
            logger.warning(
                "could not hold the library file to read its filament slots",
                extra={"library_file_id": file.id},
            )
        # Not held (past MAX_DOWNLOAD_BYTES, or out of memory): read from disk.
    source = LibrarySource(
        file_id=file.id,
        colours=[],
        plates=[],
        filename=file.filename,
        file_type=file_type,
        files=files,
    )
    try:
        async with spooled_3mf(
            client,
            source,
            cache,
            budget or ReadBudget(),
            downloads=downloads,
            file_hash=file.file_hash,
        ) as payload:
            if payload is None:
                return None
            return await _keep_filaments(cache, file, payload, budget)
    except (SpoolFullError, MemoryError) as error:
        logger.warning(
            "could not download the library file to read its filament slots",
            extra={"library_file_id": file.id, "error": type(error).__name__},
        )
        return None


def _cached(cache: Path, file: LibraryFile, name: str, dirname: str = CACHE_DIRNAME) -> Path | None:
    digest = (file.file_hash or "").lower()
    if not _HASH.fullmatch(digest):
        return None
    return cache / dirname / f"v{VIEW_VERSION}-{digest}-{name}"


def _filaments_at(cache: Path, file: LibraryFile) -> Path | None:
    return _cached(cache, file, FILAMENTS_NAME, FILAMENTS_DIRNAME)


def _store(path: Path, data: bytes, keep: int) -> None:
    """Write ``path`` aside and rename it in, then drop all but the ``keep`` most recently
    used files (a hit touches its file: :func:`_hit`). The downloads' own pruning
    (#2788), to a count here."""
    store_bytes(path, data, keep=keep)


def _hit(path: Path) -> None:
    """Mark a cached file used, so the prune keeps it (#2160). One pruned meanwhile is
    simply not marked."""
    touch(path)


async def _off_thread[T](work: Callable[[], T], file: LibraryFile) -> T:
    """``work`` on a thread, a :class:`MemoryError` refused as :func:`_read` refuses one
    (#2501): the preview GLB and the analysis run after the read, and the user should
    hear the same cause either way. Nothing is cached, so the next try reads again."""
    try:
        return await asyncio.to_thread(work)
    except MemoryError:
        raise NotViewableError(file, str(ReadOutOfMemoryError())) from None


def _glb(read: PlateRead) -> bytes:
    with tempfile.TemporaryDirectory() as scratch:
        out = Path(scratch) / "preview.glb"
        write_glb(read.parts, out)
        return out.read_bytes()


def plate_glb(payload: bytes, plate: int, budget: ReadBudget | None = None) -> bytes:
    """Plate ``plate`` of a 3MF as a preview GLB, its parts in their colours (#2169: an
    output's plate in the print dialog's pane, as a library file's is). A plate the file
    lacks is :class:`~scadbuddy.render.geometry.NoSuchPlateError`; running out of memory
    reading it is :class:`ReadOutOfMemoryError`, as a library file's read is (#2767), so
    no caller maps a bare :class:`MemoryError` itself."""
    try:
        return _glb(read_plate_parts(payload, plate, budget))
    except MemoryError:
        raise ReadOutOfMemoryError from None


def _analysis(read: PlateRead, plate: int) -> GeometryAnalysis:
    analysis = analyze_geometry(read.parts, extruders=[part.material_index for part in read.parts])
    return analysis.model_copy(update={"plate": plate, "plates": read.plates})


async def library_preview(
    client: BambuddyClient | None,
    cache: Path,
    file_id: int,
    plate: int = 1,
    budget: ReadBudget | None = None,
    downloads: DownloadCache | None = None,
    files: LibraryReads | None = None,
) -> bytes:
    """Plate ``plate`` of library file ``file_id`` as a preview GLB, its parts in their
    colours. A file deleted in Bambuddy is the client's 404; one that cannot be read is
    :class:`NotViewableError`; a plate it lacks is
    :class:`~scadbuddy.render.geometry.NoSuchPlateError`."""
    file = await reads_of(files, client).library_file(file_id)
    path = _cached(cache, file, f"plate-{plate}.glb")
    if path is not None:
        try:
            cached = await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            pass
        else:
            await asyncio.to_thread(_hit, path)
            return cached
    read = await _read(client, cache, file, plate, budget, downloads, files)
    data = await _off_thread(partial(_glb, read), file)
    if path is not None:
        await asyncio.to_thread(_store, path, data, MAX_CACHED)
    return data


async def library_geometry(
    client: BambuddyClient | None,
    cache: Path,
    file_id: int,
    plate: int = 1,
    budget: ReadBudget | None = None,
    downloads: DownloadCache | None = None,
    files: LibraryReads | None = None,
) -> GeometryAnalysis:
    """The mesh analysis of plate ``plate`` of library file ``file_id``, as
    ``OutputStore.geometry`` measures an output's; refused as :func:`library_preview`."""
    file = await reads_of(files, client).library_file(file_id)
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
    read = await _read(client, cache, file, plate, budget, downloads, files)
    analysis = await _off_thread(partial(_analysis, read, plate), file)
    if path is not None:
        data = (analysis.model_dump_json(indent=2) + "\n").encode("utf-8")
        await asyncio.to_thread(_store, path, data, MAX_CACHED)
    return analysis
