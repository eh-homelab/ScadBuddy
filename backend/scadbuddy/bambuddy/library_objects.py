"""A plain library file's objects for Arrange (#1863): a Bambuddy library file no
ScadBuddy output stands behind has no manifest, so its objects are read from the 3MF
the library print path fetches (:meth:`LibrarySource.fetch_3mf`, #1882: the file's
bytes, an STL wrapped in ScadBuddy's own 3MF) and each is written into the blob store
as a piece, the shape a render's Part has, so `write_output` places it like any other.
The file is downloaded whole whatever its size, spooled to the cache volume
(:func:`~scadbuddy.bambuddy.library_view.spooled_3mf`): its read budget, not its size,
says whether it can be read.

A piece's key is the file's sha256 and the object's place in it: the same bytes are
the same pieces, whichever arrange reads them, and a changed file is new ones. A sliced
file, an STL with no mesh, and one whose objects cannot be read faithfully
(:mod:`scadbuddy.render.objects3mf`) are :class:`NotArrangeableError`, saying why.

A file is read once per hash (#1973): reading took 13.5 s for a 13 MB file idle and
~52 s under the dialog's parallel requests. Its pieces are stored as it is read, and its
object list, or why it cannot be arranged, is kept under ``cache/library-objects/`` by
the file's SHA-256 as Bambuddy states it (as :mod:`~scadbuddy.bambuddy.library_view`
keeps a preview), so the dialog's next listing and the arrange after it download and
parse nothing. A kept list whose pieces were swept since is read again; a file Bambuddy
states no hash for is read each time.

Each read runs within a :class:`~scadbuddy.render.read_budget.ReadBudget` (#2087). A kept
list answers any budget, since keeping it costs nothing to read; a kept refusal answers
only a budget its own covers, so a request with a larger one reads the file again.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from pydantic import BaseModel, ValidationError

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.library_view import _HASH, _store, spooled_3mf
from scadbuddy.bambuddy.models import LibraryFile
from scadbuddy.bambuddy.print_source import (
    SLICED_TYPE,
    LibrarySource,
    ReadOutOfMemoryError,
    SpoolFullError,
    printable,
)
from scadbuddy.render.glb import footprint_box
from scadbuddy.render.job_models import ManifestObject
from scadbuddy.render.jobs import LAYOUT_NAME
from scadbuddy.render.objects3mf import (
    ReadObject,
    UnreadableObjectsError,
    read_objects,
    write_piece,
)
from scadbuddy.render.read_budget import ReadBudget
from scadbuddy.store import BlobStore
from scadbuddy.store.bambuddy import SHARED_TITLE
from scadbuddy.store.cache import StaleBlobError
from scadbuddy.store.content_models import BlobScope

#: A library piece's key prefix; the number moves when the reader changes what a file's
#: objects are, so an older reading is never taken for the new one. It prefixes a kept
#: object list's name too, for the same reason.
LIBRARY_PIECE_PREFIX = "lib3"
#: Where the object lists are kept, under the data volume's cache.
CACHE_DIRNAME = "library-objects"
#: How many files' object lists are kept, newest first: each is a few hundred bytes.
MAX_CACHED = 512
#: The chunk a spooled file is hashed in.
_CHUNK = 1024 * 1024
#: Library pieces belong to no template.
LIBRARY_SCOPE = BlobScope(slug=None, title=SHARED_TITLE)


class NotArrangeableError(Exception):
    """A library file whose objects cannot be read, and why."""

    def __init__(self, file_id: int, filename: str, reason: str) -> None:
        super().__init__(f"{filename or f'library file {file_id}'}: {reason}")
        self.file_id = file_id
        self.filename = filename
        self.reason = reason


@dataclass(frozen=True)
class LibraryObjects:
    """A library file's objects as manifest entries (``part`` their piece key, stored
    already; ``library_file_id`` the file; ``slug`` empty until the arrange files the
    result)."""

    file_id: int
    filename: str
    objects: list[ManifestObject]


#: Moves when what a kept read holds changes. 1: each object's colours' materials.
KEPT_VERSION = 1


class _Kept(BaseModel):
    """What one file hash reads as: its objects, or why it cannot be arranged."""

    objects: list[ManifestObject] = []
    refused: str | None = None
    #: The budget a refusal was read within: one kept before #2087 had the defaults.
    budget: ReadBudget = ReadBudget()
    #: What the objects were read with: one kept before :data:`KEPT_VERSION` lacks
    #: what that version added (each colour's material), so it is read again.
    version: int = 0


def _digest(payload: BinaryIO) -> str:
    """``payload``'s SHA-256, read from its start, left open at its start again."""
    payload.seek(0)
    digest = hashlib.sha256()
    while chunk := payload.read(_CHUNK):
        digest.update(chunk)
    payload.seek(0)
    return digest.hexdigest()


def piece_key(digest: str, index: int) -> str:
    return f"{LIBRARY_PIECE_PREFIX}-{digest[:48]}-{index}"


def _kept_at(cache: Path, file: LibraryFile) -> Path | None:
    digest = (file.file_hash or "").lower()
    if not _HASH.fullmatch(digest):
        return None
    return cache / CACHE_DIRNAME / f"{LIBRARY_PIECE_PREFIX}-{digest}.json"


def _load(path: Path) -> _Kept | None:
    try:
        return _Kept.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError):
        return None


async def _keep(path: Path | None, kept: _Kept) -> None:
    if path is not None:
        data = (kept.model_dump_json() + "\n").encode("utf-8")
        await asyncio.to_thread(_store, path, data, MAX_CACHED)


async def _stored(blobs: BlobStore, key: str) -> bool:
    directory = await asyncio.to_thread(blobs.dir_for, key)
    return await blobs.fetch(key) and await asyncio.to_thread((directory / LAYOUT_NAME).is_file)


async def read_library_objects(
    client: BambuddyClient,
    file_id: int,
    *,
    blobs: BlobStore,
    cache: Path,
    budget: ReadBudget | None = None,
) -> LibraryObjects:
    """``file_id``'s objects, their pieces in ``blobs``: kept in ``cache`` by the file's
    hash, else downloaded and read within ``budget`` (the defaults when None). A file
    deleted in Bambuddy is the client's 404; one that cannot be arranged is
    :class:`NotArrangeableError`."""
    spend = budget or ReadBudget()
    file = await client.library_file(file_id)
    kind = (file.file_type or "").lower()
    if kind == SLICED_TYPE:
        raise NotArrangeableError(
            file_id, file.filename, "it is sliced already, so its objects cannot be laid out again"
        )
    if not printable(kind):
        raise NotArrangeableError(
            file_id,
            file.filename,
            f"Arrange reads only 3MF and STL files, and it is a {kind or 'file of unknown type'}",
        )
    path = _kept_at(cache, file)
    kept = None if path is None else await asyncio.to_thread(_load, path)
    if kept is not None and kept.refused is not None and kept.budget.covers(spend):
        raise NotArrangeableError(file_id, file.filename, kept.refused)
    if (
        kept is not None
        and kept.refused is None
        and kept.version >= KEPT_VERSION
        and all([await _stored(blobs, obj.part) for obj in kept.objects])
    ):
        # Another library file with the same bytes kept them: these are this file's.
        objects = [obj.model_copy(update={"library_file_id": file_id}) for obj in kept.objects]
        return LibraryObjects(file_id, file.filename, objects)
    source = LibrarySource(
        file_id=file_id, colours=[], plates=[], filename=file.filename, file_type=kind
    )
    try:
        async with spooled_3mf(client, source, cache, spend) as payload:
            if payload is None:
                reason = "it holds no mesh ScadBuddy can read"
                # No budget decides this one, so no larger budget would read it: kept as
                # the ceilings', which cover every budget.
                await _keep(path, _Kept(refused=reason, budget=ReadBudget.widest()))
                raise NotArrangeableError(file_id, file.filename, reason)
            digest = await asyncio.to_thread(_digest, payload)
            try:
                read = await asyncio.to_thread(read_objects, payload, spend)
            except MemoryError:
                raise ReadOutOfMemoryError from None
    except UnreadableObjectsError as error:
        # A budget refusal of an STL, or a 3MF that cannot be read.
        await _keep(path, _Kept(refused=str(error), budget=spend))
        raise NotArrangeableError(file_id, file.filename, str(error)) from None
    except (SpoolFullError, ReadOutOfMemoryError) as error:
        # The volume's or the process's, not the file's: never kept, so the next try
        # (with room, memory or a triangle limit) reads it again.
        raise NotArrangeableError(file_id, file.filename, str(error)) from None
    objects = []
    for index, obj in enumerate(read):
        # Packed by its negative parts' reach too: the slicer cuts them out of the whole
        # plate, so no neighbour may sit inside one (`footprint_box`).
        box = footprint_box(obj.parts)
        objects.append(
            ManifestObject(
                part=piece_key(digest, index),
                file=obj.name,
                slug="",
                revision=None,
                bbox=box,
                footprint=(box.size[0], box.size[1]),
                colours=list(
                    dict.fromkeys(
                        colour
                        for part in obj.parts
                        if not part.negative
                        for colour in [
                            part.colour,
                            *(part.paint.used() if part.paint is not None else []),
                        ]
                    )
                ),
                materials=dict(obj.materials),
                count=obj.count,
                library_file_id=file_id,
                notes=list(obj.notes),
            )
        )
    await publish_library_pieces(blobs, objects, read)
    await _keep(path, _Kept(objects=objects, version=KEPT_VERSION))
    return LibraryObjects(file_id, file.filename, objects)


async def publish_library_pieces(
    blobs: BlobStore, objects: list[ManifestObject], read: list[ReadObject]
) -> None:
    """Write each object as its piece and store it, unless the store holds it already
    (the same bytes read before). Two reads of one file at once write the same
    bytes, so the one that loses the race keeps the winner's."""
    for entry, obj in zip(objects, read, strict=True):
        key = entry.part
        if await _stored(blobs, key):
            continue
        directory = await asyncio.to_thread(blobs.dir_for, key)
        expected = await blobs.checkout_fresh(key)
        await asyncio.to_thread(write_piece, directory, obj)
        try:
            await blobs.publish_fresh(key, scope=LIBRARY_SCOPE, expected=expected)
        except StaleBlobError:
            continue
