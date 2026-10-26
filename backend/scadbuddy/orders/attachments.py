"""An order's files (#4251, spec 2026-10-18 §6.2): what a file is, the name it keeps,
and how its bytes become a blob of ScadBuddy's own store (Ruling 8).

Any file may be attached. What it *is* comes from its bytes, never its name or the
browser's content type (`sniff`): a PNG, JPEG, WebP, GIF, AVIF or HEIC photo is an
``image``, an MP4, QuickTime or WebM video a ``video``, a PDF a ``document``, and
anything else ``other``. Only the sniffed types are ever served as themselves
(``models.INLINE``); every other file is served as ``application/octet-stream`` with
``Content-Disposition: attachment``, so an uploaded HTML or SVG page never runs on
ScadBuddy's origin.

The upload is streamed to the claim store as a template's media is
(``api/media.py`` ``receive_upload``); the ``order_attachment_add`` operation's run
moves it into the blob ``attachment-<sha256>`` (one file, ``file``), publishes it, and
writes the row with its ``blob_refs`` hold in one transaction (``store.py``
``insert_attachment``). Identical bytes share one blob; each row holds it.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import mimetypes
import os
import re
import shutil
import uuid
from pathlib import Path, PurePosixPath

from scadbuddy.operations.claims import ClaimStore
from scadbuddy.orders.models import AttachmentKind
from scadbuddy.store import BlobStore
from scadbuddy.store.content_models import BlobScope

#: How many leading bytes `sniff` reads.
SNIFF_BYTES = 16
#: The one file in an attachment's blob directory.
BLOB_FILE = "file"
#: The longest kept file name, in characters (the column's own bound).
MAX_FILENAME = 255

#: The ISO base media brands of a still image (HEIF and AVIF share the container
#: with MP4: https://www.iana.org/assignments/media-types/image/heic).
_HEIF_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"}
_AVIF_BRANDS = {b"avif", b"avis"}


def sniff(head: bytes, filename: str) -> tuple[AttachmentKind, str]:
    """``(kind, media type)`` of a file from its first `SNIFF_BYTES` bytes; for
    ``other``, the type its name suggests (for display only: it is never served as it)."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image", "image/gif"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _AVIF_BRANDS:
            return "image", "image/avif"
        if brand in _HEIF_BRANDS:
            return "image", "image/heic"
        if brand == b"qt  ":
            return "video", "video/quicktime"
        if brand.startswith(b"M4A") or brand.startswith(b"M4B"):
            return "other", "audio/mp4"
        return "video", "video/mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video", "video/webm"
    if head.startswith(b"%PDF-"):
        return "document", "application/pdf"
    guessed, _ = _TYPES.guess_type(filename, strict=False)
    return "other", guessed or "application/octet-stream"


def _types() -> mimetypes.MimeTypes:
    """Python's own table only, never the host's ``/etc/mime.types``, so a name reads
    the same everywhere; with the 3D-printing types it lacks (IANA's ``model/*``,
    https://www.iana.org/assignments/media-types/media-types.xhtml#model)."""
    types = mimetypes.MimeTypes(filenames=())
    for extension, media_type in (
        (".stl", "model/stl"),
        (".3mf", "model/3mf"),
        (".obj", "model/obj"),
        (".step", "model/step"),
        (".stp", "model/step"),
        (".gcode", "text/x-gcode"),
    ):
        types.add_type(media_type, extension)
    return types


_TYPES = _types()
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def clean_filename(sent: str) -> str:
    """The name to keep: the last path segment of what was sent, without control
    characters, at most `MAX_FILENAME` characters (its extension kept); ``file`` when
    nothing is left."""
    name = PurePosixPath(sent.replace("\\", "/")).name
    name = _CONTROL.sub("", name).strip()
    if name in ("", ".", ".."):
        return "file"
    if len(name) > MAX_FILENAME:
        stem, dot, extension = name.rpartition(".")
        if dot and 0 < len(extension) < 16:
            name = stem[: MAX_FILENAME - len(extension) - 1] + "." + extension
        else:
            name = name[:MAX_FILENAME]
    return name


def blob_key(sha256: str) -> str:
    return f"attachment-{sha256}"


def _place(blobs: BlobStore, key: str, claims: ClaimStore, claim: str) -> None:
    """The claim's bytes as the blob's `BLOB_FILE`: linked where the volume allows it
    (a video runs to a gigabyte), copied otherwise, and moved into place under a name
    of its own, so two runs of the same bytes never leave half a file for the other."""
    directory = blobs.dir_for(key)
    staging = directory / f".{BLOB_FILE}.{uuid.uuid4().hex}"
    try:
        try:
            claims.link(claim, staging)
        except OSError as error:
            if error.errno not in (errno.EXDEV, errno.EPERM, errno.EMLINK):
                raise
            with claims.open(claim) as source, staging.open("xb") as out:
                shutil.copyfileobj(source, out, 1 << 20)
        os.replace(staging, directory / BLOB_FILE)
    finally:
        with contextlib.suppress(FileNotFoundError):
            staging.unlink()


async def keep(blobs: BlobStore, claims: ClaimStore, claim: str, sha256: str) -> str:
    """The claim kept and published as the blob ``attachment-<sha256>``; its key.
    ``LookupError`` when the claim is gone.

    Bytes the store holds already (the same photo on a second item) are its blob as it
    is: a publish over it from a directory fetched before it existed (a cache that
    evicted it, another replica) would be refused as stale. Asking costs no download
    (a video runs to a gigabyte, #4677): `BlobStore.claim` touches the blob, so the
    sweeps keep it until the row's hold is committed, as `_place`'s `dir_for` does."""
    key = blob_key(sha256)
    if await blobs.claim(key):
        return key
    await asyncio.to_thread(_place, blobs, key, claims, claim)
    await blobs.publish(key, scope=BlobScope())
    return key


async def blob_path(blobs: BlobStore, key: str) -> Path | None:
    """The kept file on this process's volume, fetched first where the store is
    remote; None when the store holds nothing under ``key``."""
    if not await blobs.fetch(key):
        return None
    path = blobs.dir_for(key) / BLOB_FILE
    return path if path.is_file() else None
