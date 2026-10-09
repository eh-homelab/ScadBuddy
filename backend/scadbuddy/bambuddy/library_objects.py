"""A plain library file's objects for Arrange (#1863): a Bambuddy library file no
ScadBuddy output stands behind has no manifest, so its objects are read from the 3MF
the library print path fetches (:meth:`LibrarySource.fetch_3mf`, #1882: the file's
bytes, an STL wrapped in ScadBuddy's own 3MF) and each is written into the blob store
as a piece, the shape a render's Part has, so `write_output` places it like any other.

A piece's key is the file's sha256 and the object's place in it: the same bytes are
the same pieces, whichever arrange reads them, and a changed file is new ones. A sliced
file, one past the download cap, and one whose objects cannot be read faithfully
(:mod:`scadbuddy.render.objects3mf`) are :class:`NotArrangeableError`, saying why.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.print_source import SLICED_TYPE, LibrarySource, printable
from scadbuddy.render.glb import bounding_box
from scadbuddy.render.job_models import ManifestObject
from scadbuddy.render.jobs import LAYOUT_NAME
from scadbuddy.render.objects3mf import (
    ReadObject,
    UnreadableObjectsError,
    read_objects,
    write_piece,
)
from scadbuddy.store import BlobStore
from scadbuddy.store.bambuddy import SHARED_TITLE
from scadbuddy.store.cache import StaleBlobError
from scadbuddy.store.content_models import BlobScope

#: A library piece's key prefix; the number moves when the reader changes what a file's
#: objects are, so an older reading is never taken for the new one.
LIBRARY_PIECE_PREFIX = "lib1"
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
    """A library file's objects: as manifest entries (``part`` their piece key,
    ``library_file_id`` the file, ``slug`` empty until the arrange files the result) and
    as read, to write into the store."""

    file_id: int
    filename: str
    objects: list[ManifestObject]
    read: list[ReadObject]


def piece_key(digest: str, index: int) -> str:
    return f"{LIBRARY_PIECE_PREFIX}-{digest[:48]}-{index}"


async def read_library_objects(client: BambuddyClient, file_id: int) -> LibraryObjects:
    """Download ``file_id`` and read its objects. A file deleted in Bambuddy is the
    client's 404; one that cannot be arranged is :class:`NotArrangeableError`."""
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
    source = LibrarySource(
        file_id=file_id, colours=[], plates=[], filename=file.filename, file_type=kind
    )
    payload = await source.fetch_3mf(client)
    if payload is None:
        raise NotArrangeableError(
            file_id, file.filename, "it is too large to read, or holds no mesh ScadBuddy can read"
        )
    digest = hashlib.sha256(payload).hexdigest()
    try:
        read = await asyncio.to_thread(read_objects, payload)
    except UnreadableObjectsError as error:
        raise NotArrangeableError(file_id, file.filename, str(error)) from None
    objects = []
    for index, obj in enumerate(read):
        box = bounding_box(obj.parts)
        objects.append(
            ManifestObject(
                part=piece_key(digest, index),
                file=obj.name,
                slug="",
                revision=None,
                bbox=box,
                footprint=(box.size[0], box.size[1]),
                colours=[part.colour for part in obj.parts],
                count=obj.count,
                library_file_id=file_id,
                notes=list(obj.notes),
            )
        )
    return LibraryObjects(file_id, file.filename, objects, read)


async def publish_library_pieces(blobs: BlobStore, found: LibraryObjects) -> None:
    """Write each object as its piece and store it, unless the store holds it already
    (the same bytes read before). Two arranges of one file at once write the same
    bytes, so the one that loses the race keeps the winner's."""
    for entry, obj in zip(found.objects, found.read, strict=True):
        key = entry.part
        directory = await asyncio.to_thread(blobs.dir_for, key)
        if await blobs.fetch(key) and await asyncio.to_thread((directory / LAYOUT_NAME).is_file):
            continue
        expected = await blobs.checkout_fresh(key)
        await asyncio.to_thread(write_piece, directory, obj)
        try:
            await blobs.publish_fresh(key, scope=LIBRARY_SCOPE, expected=expected)
        except StaleBlobError:
            continue
