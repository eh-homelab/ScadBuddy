"""Files a viewer attaches to a render: the store behind `// file` parameters (#204).

An upload is sniffed, never trusted by its name: a PNG is decoded and re-encoded
(downscaled to `MAX_PNG_SIDE`, because `surface()` cost grows with pixel count), an
SVG is parsed and stripped of everything that is not geometry. What is stored is
the result, keyed by its SHA-256 -- so the id IS the content hash, and a render's
`params.json` names exactly the bytes it read.

An asset lives as long as something names it (#296). An output's parameters, a
saved preset, a template's shipped presets or a job still in the store keep it, so
"re-render" and "Customize this version" reproduce the output long after the upload.
One that nothing names, and that nothing has uploaded or used for the grace period
(SCADBUDDY_ASSET_SWEEP_GRACE), is removed by `AssetStore.sweep`. The bytes are files
under ``data/assets/``; the metadata, the last use and so the usage are rows of the
``assets`` table (#591). The store is capped
in total bytes and in count (SCADBUDDY_ASSET_MAX_TOTAL_BYTES / _MAX_COUNT); an upload
past either is refused, and re-uploading what is already stored never is.

Nothing here ever becomes a path OpenSCAD sees. The render stages a copy under a
name it generates (`render/jobs.py`); the original file name is display-only.

A template's own sample files are the other thing a file parameter can name: the
bare names `sample_files` lists from the model's directory. They are never copied
anywhere -- OpenSCAD reads them where they are, as it does any bundled file.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import os
import re
import secrets
import threading
import time
import unicodedata
import zipfile
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import psycopg
from lxml import etree
from PIL import Image, UnidentifiedImageError
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from scadbuddy.core.metrics import Metrics
from scadbuddy.core.paths import LEGACY_PRESETS_NAME, DataPaths
from scadbuddy.library.catalogue import THUMBNAIL_NAME
from scadbuddy.render.provenance import ROOT_MODEL
from scadbuddy.render.schema import FILE_KINDS, CustomizerSchema, ParamValue, is_bare_filename
from scadbuddy.render.solids import WRAPPER_PREFIX

logger = logging.getLogger(__name__)

AssetKind = Literal["svg", "png", "stl", "3mf", "obj"]

ASSET_ID_PATTERN = r"^[0-9a-f]{64}$"
ASSET_ID_RE = re.compile(ASSET_ID_PATTERN)
#: A stored blob's file name: the id and the kind, nothing else. A write's temporary
#: file never matches.
_BLOB_RE = re.compile(r"^([0-9a-f]{64})\.(svg|png|stl|3mf|obj)$")
#: The metadata sidecar the file-based store kept beside each blob: a leftover (#591).
_SIDECAR_RE = re.compile(r"^[0-9a-f]{64}\.json$")
#: The store's advisory lock, hashed as the preset store hashes its own: held only
#: by an upload of content not yet stored, for its quota check and insert.
ASSET_LOCK_KEY = "scadbuddy-assets"


def asset_lock_key(asset_id: str) -> str:
    """One asset's advisory lock: an upload of that content, and the sweep's removal
    of it, hold it. Never the same key as `ASSET_LOCK_KEY`."""
    return f"{ASSET_LOCK_KEY}:{asset_id}"


_LOCK_XACT = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"
_LOCK_SESSION = "SELECT pg_advisory_lock(hashtextextended(%s, 0))"
_UNLOCK_SESSION = "SELECT pg_advisory_unlock(hashtextextended(%s, 0))"
#: Every column `AssetMeta` reads.
_META_COLUMNS = (
    "id, name, kind, size, width, height, view_box, path_count, source_url, licence,"
    " triangles, bounds"
)
_SELECT_META = f"SELECT {_META_COLUMNS} FROM assets WHERE id = %s"
#: The sweep's candidates among the rows: those last used before the cutoff, read
#: through the `assets_last_used` index.
STALE_ROWS = "SELECT id FROM assets WHERE last_used_at < %s"
_ROWS_AMONG = "SELECT id FROM assets WHERE id = ANY(%s)"
_MARK_USED = f"UPDATE assets SET last_used_at = %s WHERE id = %s RETURNING {_META_COLUMNS}"
_ADOPT = (
    f"INSERT INTO assets ({_META_COLUMNS}, created_at, last_used_at)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    " ON CONFLICT (id) DO UPDATE SET last_used_at = EXCLUDED.last_used_at"
)
#: The most ids one `AssetStore.last_used` reads (the render worker's
#: ``POST …/render/assets/last-used``, #5298): `RemoteAssetStore.prune_local` asks in
#: batches of this many.
LAST_USED_MAX = 500
#: Anything in a reference source that could be an asset id. Deliberately looser than
#: "the value of a `file` parameter": a sweep that keeps an asset it need not is
#: harmless, one that misses a reference destroys an output's provenance. Matching
#: raw text rather than parsed JSON also reads a file too damaged to parse.
_ID_IN_TEXT = re.compile(rb"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")

#: The largest upload read. An SVG this size is already far past a logo or a mask.
MAX_ASSET_BYTES = 8 * 1024 * 1024
#: A PNG's long side after the downscale: `surface()` makes a vertex per pixel, so
#: a 96 px star is instant and a phone photo would outlast the render timeout.
MAX_PNG_SIDE = 256
#: Refused before decoding at all: a small file can declare an enormous image.
MAX_PNG_PIXELS = 25_000_000
#: How much of the original file name is kept for display.
MAX_NAME_CHARS = 200

#: The mesh types are IANA's (https://www.iana.org/assignments/media-types/model/).
MEDIA_TYPES: dict[AssetKind, str] = {
    "svg": "image/svg+xml",
    "png": "image/png",
    "stl": "model/stl",
    "3mf": "model/3mf",
    "obj": "model/obj",
}
#: What an `<img>` can show; a mesh's picture is its preview (`meshes.preview_png`).
IMAGE_KINDS: tuple[AssetKind, ...] = ("svg", "png")
#: The refusal of anything else, sniffed.
UNSUPPORTED = "only SVG, PNG, STL, 3MF and OBJ files can be attached"

#: Files in a model's directory that are never offered as samples: the catalogue's
#: cover image is a PNG beside every model, not a picture for its parameters.
NOT_SAMPLES = frozenset({THUMBNAIL_NAME})

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"

#: Elements that execute, fetch, embed another document, or animate an attribute
#: (which can rewrite an `href` after the fact). None of them is geometry.
_DROPPED_ELEMENTS = frozenset(
    {
        "script",
        "foreignobject",
        "image",
        "iframe",
        "embed",
        "object",
        "audio",
        "video",
        "animate",
        "animatemotion",
        "animatetransform",
        "animatecolor",
        "set",
        "handler",
        "listener",
    }
)
#: A `url(...)` that points anywhere but a fragment inside this document.
_EXTERNAL_URL = re.compile(r"url\(\s*['\"]?\s*(?!#)", re.IGNORECASE)
_UNSAFE_TEXT = re.compile(r"@import|javascript:|expression\(", re.IGNORECASE)


class AssetRejectedError(ValueError):
    """The upload is not an SVG, PNG, STL, 3MF or OBJ this store will keep."""


class AssetUnavailableError(ValueError):
    """A file parameter names a value that is neither an upload in this store nor one of
    the template's samples."""

    def __init__(self, parameter: str, value: str) -> None:
        super().__init__(
            f"parameter {parameter!r} is not an uploaded or sample file: {value[:80]!r}"
        )
        self.parameter = parameter
        self.asset_id = value


class AssetNotFoundError(KeyError):
    pass


class AssetUsage(BaseModel):
    """How much the upload store holds, against its caps (#296)."""

    #: Distinct stored files.
    count: int
    #: Their total size in bytes, as stored (after sanitising and downscaling).
    bytes: int
    #: SCADBUDDY_ASSET_MAX_COUNT; 0 is no limit.
    max_count: int
    #: SCADBUDDY_ASSET_MAX_TOTAL_BYTES; 0 is no limit.
    max_total_bytes: int


class AssetQuotaError(Exception):
    """Storing the upload would take the store past one of its caps."""

    def __init__(self, detail: str, usage: AssetUsage) -> None:
        super().__init__(detail)
        self.usage = usage


class AssetLicence(BaseModel):
    """The licence a fetch said its file is under (#959), as given: nothing checks it."""

    #: Its SPDX identifier (https://spdx.org/licenses/), e.g. ``CC-BY-4.0``.
    spdx: str | None = Field(default=None, max_length=100)
    #: Its name as the source writes it, e.g. ``CC BY 4.0``.
    name: str | None = Field(default=None, max_length=200)
    #: Where its text is.
    url: str | None = Field(default=None, max_length=2048)
    #: Whom to credit.
    author: str | None = Field(default=None, max_length=200)


class AssetMeta(BaseModel):
    #: The SHA-256 of the stored bytes (after sanitising), hex.
    id: str
    #: The uploaded file's name, for display only. Never a path.
    name: str
    kind: AssetKind
    size: int
    width: int | None = None
    height: int | None = None
    #: An SVG's ``viewBox``: min-x, min-y, width and height, in its user units (#959).
    #: ``None`` for a PNG, an SVG without one, and one that is not four numbers with a
    #: positive size.
    view_box: list[float] | None = None
    #: How many ``<path>`` elements an SVG keeps after sanitising (#959).
    path_count: int | None = None
    #: The URL a fetched asset came from, without its query (#959); ``None`` for an
    #: upload. Content stored twice keeps the first source it was given.
    source_url: str | None = None
    #: The licence its fetch named (#959).
    licence: AssetLicence | None = None
    #: A mesh's triangle count (#959).
    triangles: int | None = None
    #: A mesh's bounding box (#959): min x, y, z, then max x, y, z, in its units.
    bounds: list[float] | None = None


def _meta_values(meta: AssetMeta) -> tuple[object, ...]:
    """``meta`` in :data:`_META_COLUMNS`'s order, for an insert."""
    licence = None if meta.licence is None else Jsonb(meta.licence.model_dump())
    return (
        meta.id,
        meta.name,
        meta.kind,
        meta.size,
        meta.width,
        meta.height,
        meta.view_box,
        meta.path_count,
        meta.source_url,
        licence,
        meta.triangles,
        meta.bounds,
    )


def display_name(filename: str | None, kind: AssetKind) -> str:
    """The upload's base name without control characters, or a stand-in."""
    base = re.split(r"[\\/]", filename or "")[-1]
    cleaned = "".join(ch for ch in base if unicodedata.category(ch)[0] != "C").strip()
    return cleaned[:MAX_NAME_CHARS] or f"upload.{kind}"


def _local(name: object) -> str:
    return etree.QName(name).localname.lower() if isinstance(name, str) else ""


def sanitise_svg(data: bytes) -> bytes:
    """The SVG re-serialised with nothing that runs, fetches or embeds.

    SVG only ever reaches OpenSCAD's `import()` and an `<img>` preview, neither of
    which runs script -- but the content endpoint serves it back, and a browser
    opening that URL directly would. So it is cleaned as if it were going to one.
    """
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
        remove_pis=True,
        remove_comments=True,
    )
    try:
        tree = etree.parse(io.BytesIO(data), parser)
    except etree.XMLSyntaxError as error:
        raise AssetRejectedError(f"the SVG is not well-formed XML: {error}") from None
    # A DOCTYPE naming the SVG DTD is harmless and is dropped on output; an entity
    # declaration is where expansion attacks live, and nothing geometric needs one.
    dtd = tree.docinfo.internalDTD
    if dtd is not None and any(True for _ in dtd.iterentities()):
        raise AssetRejectedError("the SVG declares entities, which are not accepted")
    root = tree.getroot()
    if not isinstance(root.tag, str) or _local(root.tag) != "svg":
        raise AssetRejectedError("the file is XML but not an SVG")
    if etree.QName(root.tag).namespace not in (SVG_NS, None):
        raise AssetRejectedError("the file is XML but not an SVG")

    for element in list(root.iter()):
        if element is root:
            continue
        parent = element.getparent()
        if parent is None:
            continue
        tag = element.tag
        if not isinstance(tag, str):  # an entity reference left unresolved
            parent.remove(element)
            continue
        namespace = etree.QName(tag).namespace
        if _local(tag) in _DROPPED_ELEMENTS or namespace == "http://www.w3.org/1999/xhtml":
            parent.remove(element)
            continue
        if (
            _local(tag) == "style"
            and element.text
            and (_EXTERNAL_URL.search(element.text) or _UNSAFE_TEXT.search(element.text))
        ):
            parent.remove(element)

    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        for attribute in list(element.attrib):
            name = _local(attribute)
            value = element.attrib[attribute]
            namespace = etree.QName(str(attribute)).namespace
            if (
                name.startswith("on")
                or (name == "href" and not value.strip().startswith("#"))
                or (namespace == XML_NS and name == "base")
                or _EXTERNAL_URL.search(value)
                or _UNSAFE_TEXT.search(value)
            ):
                del element.attrib[attribute]

    cleaned: bytes = etree.tostring(root, xml_declaration=True, encoding="UTF-8")
    return cleaned


_VIEW_BOX_SPLIT = re.compile(r"[\s,]+")


def svg_shape(data: bytes) -> tuple[list[float] | None, int]:
    """A sanitised SVG's ``viewBox`` and its count of ``<path>`` elements (#959)."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = etree.fromstring(data, parser)
    paths = sum(1 for element in root.iter() if _local(element.tag) == "path")
    raw = root.get("viewBox") or ""
    try:
        numbers = [float(part) for part in _VIEW_BOX_SPLIT.split(raw.strip()) if part]
    except ValueError:
        return None, paths
    if len(numbers) != 4 or not all(math.isfinite(n) for n in numbers):
        return None, paths
    if numbers[2] <= 0 or numbers[3] <= 0:
        return None, paths
    return numbers, paths


#: In Pillow's ValueErrors for its PNG metadata limits, what tells them from another
#: ValueError: a text or ICC chunk over ``PngImagePlugin.MAX_TEXT_CHUNK`` once
#: decompressed (``_safe_zlib_decompress``), and text chunks over ``MAX_TEXT_MEMORY`` in
#: all (``check_text_memory``). The upload keeps none of that metadata either way.
#: Pillow names no exception type for them, so this matches its message text: a Pillow
#: bump that rewords one is caught by test_assets.py's real-decode tests (iCCP, zTXt,
#: MAX_TEXT_MEMORY), which fail rather than let those uploads read as unreadable PNGs.
PNG_METADATA_LIMITS = ("MAX_TEXT_CHUNK", "MAX_TEXT_MEMORY")


def normalise_png(data: bytes) -> tuple[bytes, int, int]:
    """The PNG decoded, capped at `MAX_PNG_SIDE`, and re-encoded without metadata."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG":
                raise AssetRejectedError("the file is not a PNG")
            width, height = image.size
            if width * height > MAX_PNG_PIXELS:
                raise AssetRejectedError(
                    f"the PNG is {width}x{height}; at most {MAX_PNG_PIXELS} pixels are read"
                )
            image.load()
            picture = image if image.mode in ("L", "LA", "RGB", "RGBA") else image.convert("RGBA")
            if max(width, height) > MAX_PNG_SIDE:
                picture.thumbnail((MAX_PNG_SIDE, MAX_PNG_SIDE), Image.Resampling.LANCZOS)
            # PNG save writes an ICC profile (iCCP) from `info` unasked, and that
            # chunk carries arbitrary bytes; only transparency is pixel data.
            picture.info = {k: v for k, v in picture.info.items() if k == "transparency"}
            out = io.BytesIO()
            picture.save(out, format="PNG")
            return out.getvalue(), picture.width, picture.height
    except Image.DecompressionBombError:
        # Pillow refuses at open what declares over twice its own pixel limit, before the
        # pixel check above can run: it is that refusal, said the same way.
        raise AssetRejectedError(
            f"the PNG declares more than {MAX_PNG_PIXELS} pixels; at most {MAX_PNG_PIXELS}"
            " pixels are read"
        ) from None
    except AssetRejectedError:
        # Its own refusals (a ValueError too) pass through as they are.
        raise
    except ValueError as error:
        logger.info("a PNG upload could not be decoded", extra={"error": str(error)})
        if any(limit in str(error) for limit in PNG_METADATA_LIMITS):
            # One of Pillow's limits on text or ICC chunks: metadata the upload keeps
            # none of anyway, so re-exported without it the PNG reads.
            raise AssetRejectedError(
                "the PNG carries metadata too large to read; re-export it without metadata"
            ) from None
        # Any other ValueError Pillow raises while decoding is an unreadable PNG.
        raise AssetRejectedError(
            "the file is not a readable PNG; re-export it as PNG and try again"
        ) from None
    except (UnidentifiedImageError, OSError) as error:
        # Pillow's text names its stream object by address (#2383): for the log, not the user.
        logger.info("a PNG upload could not be decoded", extra={"error": str(error)})
        raise AssetRejectedError(
            "the file is not a readable PNG; re-export it as PNG and try again"
        ) from None


def sniff(data: bytes) -> AssetKind:
    """What the bytes are, whatever the upload called them."""
    if data.startswith(PNG_MAGIC):
        return "png"
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"<") and b"<svg" in data[:65536]:
        return "svg"
    # Here, not at the top: the mesh readers import the render, which imports this.
    from scadbuddy.library.meshes import looks_like_mesh

    mesh = looks_like_mesh(data)
    if mesh is not None:
        return mesh
    raise AssetRejectedError(UNSUPPORTED)


class AssetStoreUnavailableError(RuntimeError):
    """The upload store's metadata lives in Postgres, and this store has no pool: only
    a template's pipeline check (`workflows/verify_pipeline.py`) builds one without."""

    def __init__(self) -> None:
        super().__init__(
            "the upload store's metadata is in the database; set SCADBUDDY_DATABASE_URL (#401)"
        )


class AssetStore:
    """``data/assets/<sha256>.<kind>`` for the bytes, an ``assets`` row for the rest (#591).

    The row is the asset: its metadata, its LAST USE (``last_used_at``, set by every
    upload of the content, a re-upload included, and by every render or preset save
    that names it, `use`) and so the store's usage, which is ``count(*)`` and
    ``sum(size)`` over the table -- no scan, no running total to go stale, and every
    replica reads the same numbers. The pool is the projection's
    (`JobProjection.pool`), opened and migrated at startup; this store opens
    nothing of its own.

    A row never exists without its blob: `put` writes the blob before its insert
    commits, and the sweep deletes the row before it removes the blob. The other way
    round -- a blob with no row, left by an upload whose insert failed, or by the
    file-based store this replaced (nothing was copied over) -- is an orphan: `get`
    does not find it, usage does not count it, and the sweep removes it once its
    mtime is older than the grace.

    What must not interleave holds a Postgres advisory lock, so it holds across
    threads, processes and replicas alike, and no wider than it must:

    - an upload and the sweep's re-check and removal of the SAME asset hold that
      asset's lock (`asset_lock_key`), so a removal never delays an upload of other
      content;
    - an upload of content not yet stored also holds the store's lock
      (`ASSET_LOCK_KEY`) for its quota check and insert, so two uploads cannot both
      take the last slot. A re-upload of stored content needs no room and skips it.
      Always taken after the asset's lock, and the sweep never takes it, so the two
      cannot deadlock;
    - `use` needs only its row: the sweep locks that row before it re-checks it.
    """

    def __init__(
        self,
        root: Path,
        pool: ConnectionPool[Connection[DictRow]] | None = None,
        *,
        max_total_bytes: int = 0,
        max_count: int = 0,
    ) -> None:
        self.root = root
        self._pool = pool
        #: Whether a sweep has removed the file-based store's leftovers yet.
        self._legacy_removed = False
        #: 0 is no limit, for either.
        self.max_total_bytes = max_total_bytes
        self.max_count = max_count

    def _require(self) -> ConnectionPool[Connection[DictRow]]:
        if self._pool is None:
            raise AssetStoreUnavailableError
        return self._pool

    def _blobs(self) -> dict[str, Path]:
        """Every stored blob by id, as the directory lists it now."""
        return _blobs_in(self.root)

    def _usage(self, count: int, total: int) -> AssetUsage:
        return AssetUsage(
            count=count,
            bytes=total,
            max_count=self.max_count,
            max_total_bytes=self.max_total_bytes,
        )

    def _counted(self, conn: Connection[DictRow]) -> AssetUsage:
        row = conn.execute(
            "SELECT count(*) AS count, coalesce(sum(size), 0)::bigint AS total FROM assets"
        ).fetchone()
        assert row is not None
        return self._usage(row["count"], row["total"])

    def usage(self) -> AssetUsage:
        """One aggregate over the rows: what `put` checks its caps against."""
        with self._require().connection() as conn:
            return self._counted(conn)

    def _require_room(self, usage: AssetUsage, size: int) -> None:
        if self.max_count and usage.count + 1 > self.max_count:
            raise AssetQuotaError(
                f"the upload store already holds {usage.count} files, the most "
                f"SCADBUDDY_ASSET_MAX_COUNT ({self.max_count}) allows; a file no output, "
                "preset or render uses is removed once unused for the sweep's grace period",
                usage,
            )
        if self.max_total_bytes and usage.bytes + size > self.max_total_bytes:
            raise AssetQuotaError(
                f"storing this file ({size} bytes) would take the upload store to "
                f"{usage.bytes + size} bytes, past SCADBUDDY_ASSET_MAX_TOTAL_BYTES "
                f"({self.max_total_bytes}); a file no output, preset or render uses is "
                "removed once unused for the sweep's grace period",
                usage,
            )

    def blob_path(self, meta: AssetMeta) -> Path:
        return self.root / f"{meta.id}.{meta.kind}"

    def row(self, asset_id: str) -> AssetMeta:
        """The asset's row, whether or not this root holds its file: a render worker's
        ``GET …/render/assets/{id}`` (#5298), whose copy is on its own volume."""
        if not ASSET_ID_RE.fullmatch(asset_id):
            raise AssetNotFoundError(asset_id)
        with self._require().connection() as conn:
            found = conn.execute(_SELECT_META, (asset_id,)).fetchone()
        if found is None:
            raise AssetNotFoundError(asset_id)
        return AssetMeta.model_validate(found)

    def get(self, asset_id: str) -> AssetMeta:
        meta = self.row(asset_id)
        if not self.blob_path(meta).is_file():
            raise AssetNotFoundError(asset_id)
        return meta

    def use_row(self, asset_id: str) -> AssetMeta:
        """`use` of a file this root does not hold: mark the row used and answer it. A
        render worker's ``POST …/render/assets/{id}/use`` (#5298); the worker checks its
        own copy, before and after, under its lock of the id (`RemoteAssetStore.use`).
        Waits on the sweep's row lock, as `use` does."""
        if not ASSET_ID_RE.fullmatch(asset_id):
            raise AssetNotFoundError(asset_id)
        with self._require().connection() as conn:
            found = conn.execute(_MARK_USED, (datetime.now(UTC), asset_id)).fetchone()
        if found is None:
            raise AssetNotFoundError(asset_id)
        return AssetMeta.model_validate(found)

    def last_used(self, ids: Sequence[str]) -> dict[str, datetime | None]:
        """Each id's last use; None for one with no row. At most `LAST_USED_MAX` ids."""
        if len(ids) > LAST_USED_MAX:
            raise ValueError(f"at most {LAST_USED_MAX} ids at once")
        with self._require().connection() as conn:
            found = {
                row["id"]: row["last_used_at"]
                for row in conn.execute(
                    "SELECT id, last_used_at FROM assets WHERE id = ANY(%s)", (list(ids),)
                )
            }
        return {asset_id: found.get(asset_id) for asset_id in ids}

    def use(self, asset_id: str) -> AssetMeta:
        """`get`, and mark the asset used now, so a sweep already under way skips it.

        The update waits on the row lock the sweep takes before it re-checks the last
        use, so either this lands first and the sweep sees the asset fresh, or the
        sweep deleted the row first and this is a not-found.
        """
        if not ASSET_ID_RE.fullmatch(asset_id):
            raise AssetNotFoundError(asset_id)
        with self._require().connection() as conn, conn.transaction():
            found = conn.execute(_MARK_USED, (datetime.now(UTC), asset_id)).fetchone()
            if found is None:
                raise AssetNotFoundError(asset_id)
            meta = AssetMeta.model_validate(found)
            if not self.blob_path(meta).is_file():
                # Raised inside the transaction: a row whose blob is gone is not
                # kept alive by a use that could not have read it.
                raise AssetNotFoundError(asset_id)
        return meta

    def put(
        self,
        data: bytes,
        filename: str | None,
        *,
        source_url: str | None = None,
        licence: AssetLicence | None = None,
    ) -> AssetMeta:
        """Validate, sanitise and store an upload; the same content is stored once.

        ``source_url`` and ``licence`` are a fetch's (#959): a later upload of the same
        content without them keeps the ones stored. The answer is the row as stored."""
        if len(data) > MAX_ASSET_BYTES:
            raise AssetRejectedError(f"the file is larger than {MAX_ASSET_BYTES} bytes")
        kind = sniff(data)
        width: int | None = None
        height: int | None = None
        view_box: list[float] | None = None
        path_count: int | None = None
        triangles: int | None = None
        bounds: list[float] | None = None
        if kind == "png":
            stored, width, height = normalise_png(data)
        elif kind == "svg":
            stored = sanitise_svg(data)
            view_box, path_count = svg_shape(stored)
        else:
            from scadbuddy.library.meshes import MeshRejectedError, normalise_mesh

            try:
                mesh = normalise_mesh(kind, data)
            except MeshRejectedError as error:
                raise AssetRejectedError(str(error)) from None
            stored, triangles, bounds = mesh.data, mesh.triangles, mesh.bounds
        meta = AssetMeta(
            id=hashlib.sha256(stored).hexdigest(),
            name=display_name(filename, kind),
            kind=kind,
            size=len(stored),
            width=width,
            height=height,
            view_box=view_box,
            path_count=path_count,
            source_url=source_url,
            licence=licence,
            triangles=triangles,
            bounds=bounds,
        )
        with self._require().connection() as conn, conn.transaction():
            conn.execute(_LOCK_XACT, (asset_lock_key(meta.id),))
            known = conn.execute("SELECT 1 FROM assets WHERE id = %s", (meta.id,)).fetchone()
            # Content already stored costs nothing, so a full store still takes it:
            # re-uploading a file an output uses must keep working at the cap.
            if known is None:
                conn.execute(_LOCK_XACT, (ASSET_LOCK_KEY,))
                self._require_room(self._counted(conn), meta.size)
            blob = self.blob_path(meta)
            # Before the row: a failed write rolls the insert back, so no row is ever
            # without its blob. The id is the content hash, so a blob already there
            # (an orphan, or this asset's own) is these bytes; the asset's lock keeps
            # the sweep from removing it until this commits.
            if not blob.is_file():
                self.root.mkdir(parents=True, exist_ok=True)
                _write_atomically(blob, stored)
            now = datetime.now(UTC)
            row = conn.execute(
                f"INSERT INTO assets ({_META_COLUMNS}, created_at, last_used_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (id) DO UPDATE"
                " SET name = EXCLUDED.name, last_used_at = EXCLUDED.last_used_at,"
                " view_box = coalesce(assets.view_box, EXCLUDED.view_box),"
                " path_count = coalesce(assets.path_count, EXCLUDED.path_count),"
                " source_url = coalesce(assets.source_url, EXCLUDED.source_url),"
                " licence = coalesce(assets.licence, EXCLUDED.licence)"
                f" RETURNING {_META_COLUMNS}",
                (*_meta_values(meta), now, now),
            ).fetchone()
        assert row is not None
        return AssetMeta.model_validate(row)

    def adopt(self, meta: AssetMeta, data: bytes) -> None:
        """Keep an asset fetched from the blob store exactly as it was stored elsewhere.

        No caps: the upload that created it was checked against them. The bytes must be
        the id, so a damaged or substituted download never becomes a render's input.
        """
        _require_bytes(meta, data)
        with self._require().connection() as conn, conn.transaction():
            conn.execute(_LOCK_XACT, (asset_lock_key(meta.id),))
            blob = self.blob_path(meta)
            if not blob.is_file():
                self.root.mkdir(parents=True, exist_ok=True)
                _write_atomically(blob, data)
            now = datetime.now(UTC)
            conn.execute(_ADOPT, (*_meta_values(meta), now, now))

    def adopt_row(self, meta: AssetMeta) -> None:
        """`adopt` of a file this root does not hold: the row only, under the asset's
        lock, as `adopt` writes it. A render worker's ``POST …/render/assets/{id}/adopt``
        (#5298), which writes its copy on its own volume first; the route passes the
        meta of the API's own blob-store copy, never the worker's."""
        if not ASSET_ID_RE.fullmatch(meta.id):
            raise AssetRejectedError(f"not an asset id: {meta.id[:80]!r}")
        with self._require().connection() as conn, conn.transaction():
            conn.execute(_LOCK_XACT, (asset_lock_key(meta.id),))
            now = datetime.now(UTC)
            conn.execute(_ADOPT, (*_meta_values(meta), now, now))

    def spend_fetch_grant(
        self, nonce: str, idempotency_key: str | None, expires_at: datetime
    ) -> bool:
        """Spend a one-off host grant's nonce (#959, `library/fetch_grants.py`): whether
        this fetch may use it, the first time, or again under the Idempotency-Key that
        first spent it (a re-send of the same fetch). Expired rows go as it runs."""
        with self._require().connection() as conn, conn.transaction():
            conn.execute("DELETE FROM asset_fetch_grants WHERE expires_at < now()")
            spent = conn.execute(
                "INSERT INTO asset_fetch_grants (nonce, idempotency_key, expires_at)"
                " VALUES (%s, %s, %s) ON CONFLICT (nonce) DO NOTHING RETURNING nonce",
                (nonce, idempotency_key, expires_at),
            ).fetchone()
            if spent is not None:
                return True
            row = conn.execute(
                "SELECT idempotency_key FROM asset_fetch_grants WHERE nonce = %s", (nonce,)
            ).fetchone()
        return (
            row is not None
            and idempotency_key is not None
            and row["idempotency_key"] == idempotency_key
        )

    def ids(self) -> list[str]:
        """Every blob this root holds, by id."""
        return sorted(self._blobs())

    def _remove_legacy_files(self) -> None:
        """What the file-based store left (#591): a ``<id>.json`` metadata sidecar per
        asset, and the running total and the flock beside the store. Nothing reads
        them, and nothing was copied from them, so they only take room."""
        legacy = [
            self.root.with_name(f".{self.root.name}.usage.json"),
            self.root.with_name(f".{self.root.name}.lock"),
        ]
        with suppress(FileNotFoundError):
            legacy += [e for e in self.root.iterdir() if _SIDECAR_RE.fullmatch(e.name)]
        for path in legacy:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.exception("could not remove a leftover of the file-based upload store")

    def _orphan_last_used(self, asset_id: str) -> float | None:
        """A blob with no row has only its mtime to go by; None once it is gone."""
        return _mtime_in(self.root, asset_id)

    def sweep(
        self,
        referenced: Collection[str],
        *,
        grace: float,
        now: float | None = None,
        stop: threading.Event | None = None,
    ) -> list[str]:
        """Remove every asset not in ``referenced`` and last used more than ``grace``
        seconds before ``now``; answer the ids removed.

        The candidates are every row and every blob on disk, so an orphan blob (no
        row: an upload whose insert failed, or one from before #591) goes too, once
        its mtime is past the grace. The file-based store's leftovers are removed
        by the first sweep of this store (`_remove_legacy_files`): nothing writes
        them any more, so one pass per process is enough.

        ``referenced`` is collected before the call (`referenced_asset_ids`), so a
        reference made while the sweep runs is not in it. What protects that asset
        is its last use: every path that creates a reference -- an upload, a render
        submit, a preset save -- marks the asset used first, and each removal
        re-checks the last use with the row locked, under the asset's advisory lock.
        That lock is held from before the re-check until the blob is gone, which is
        after the row's delete has committed: `get` stops finding the asset before
        its bytes go, and an upload of the same content waits until they have.
        Anything that cannot be removed -- a file error or a database error -- is
        logged and skipped, like the other sweeps, and the rest are still tried.
        ``stop``, once set, ends the sweep before its next asset (#2140): the rest wait
        for the next sweep.
        """
        pool = self._require()
        cutoff = (time.time() if now is None else now) - grace
        cutoff_at = datetime.fromtimestamp(cutoff, UTC)
        if not self._legacy_removed:
            self._remove_legacy_files()
            self._legacy_removed = True
        blobs = self._blobs()
        with pool.connection() as conn:
            # Only the stale rows (`assets_last_used`), and which blobs have a row
            # at all (the primary key): never the whole table.
            stale = {row["id"] for row in conn.execute(STALE_ROWS, (cutoff_at,))}
            owned = {row["id"] for row in conn.execute(_ROWS_AMONG, (list(blobs),))}
        # A row last used inside the grace needs no second look; an orphan's mtime is
        # read under the lock.
        candidates = sorted((stale | (blobs.keys() - owned)) - set(referenced))
        removed: list[str] = []
        for done, asset_id in enumerate(candidates):
            if stopped(stop):
                logger.info("asset sweep stopped early", extra={"left": len(candidates) - done})
                break
            key = asset_lock_key(asset_id)
            try:
                with pool.connection() as conn:
                    # A session lock, not a transaction's: it must outlive the commit
                    # of the row's delete, until the blob is gone. A connection that
                    # breaks takes the lock with it.
                    conn.execute(_LOCK_SESSION, (key,))
                    try:
                        if self._remove(conn, asset_id, cutoff, cutoff_at):
                            removed.append(asset_id)
                    finally:
                        conn.execute(_UNLOCK_SESSION, (key,))
            # psycopg.Error covers the pool's own failures too: PoolTimeout and
            # PoolClosed are psycopg.OperationalError subclasses.
            except (OSError, psycopg.Error):
                logger.exception("could not remove an unused asset", extra={"asset": asset_id})
        return removed

    def prune_local(self, *, grace: float, now: float | None = None) -> list[str]:
        """Remove this root's copies of assets last used more than ``grace`` seconds
        before ``now``, and answer their ids; every row stays.

        For a root that is a copy (#426): the rows are the API's (#591), and only the
        API's `sweep`, which knows the references, removes one. A render worker on its
        own volume prunes through `RemoteAssetStore.prune_local` (#5298). A copy with a
        row goes by the row's last use; one without a row by its mtime. Each copy is
        removed with its row locked, so a `use` either lands first and keeps it, or finds
        it gone and the store brings it back.
        """
        pool = self._require()
        cutoff = (time.time() if now is None else now) - grace
        cutoff_at = datetime.fromtimestamp(cutoff, UTC)
        removed: list[str] = []
        for asset_id in sorted(self._blobs()):
            try:
                with pool.connection() as conn, conn.transaction():
                    row = conn.execute(
                        "SELECT last_used_at FROM assets WHERE id = %s FOR UPDATE", (asset_id,)
                    ).fetchone()
                    if row is not None:
                        stale = row["last_used_at"] < cutoff_at
                    else:
                        last_used = self._orphan_last_used(asset_id)
                        stale = last_used is not None and last_used < cutoff
                    if not stale:
                        continue
                    for kind in MEDIA_TYPES:
                        (self.root / f"{asset_id}.{kind}").unlink(missing_ok=True)
                removed.append(asset_id)
            except (OSError, psycopg.Error):
                logger.exception(
                    "could not remove a local copy of an asset", extra={"asset": asset_id}
                )
        return removed

    def _remove(
        self, conn: Connection[DictRow], asset_id: str, cutoff: float, cutoff_at: datetime
    ) -> bool:
        """One candidate of the sweep, under its lock: whether it went."""
        with conn.transaction():
            row = conn.execute(
                "SELECT last_used_at FROM assets WHERE id = %s FOR UPDATE", (asset_id,)
            ).fetchone()
            if row is not None:
                if row["last_used_at"] >= cutoff_at:
                    return False
                conn.execute("DELETE FROM assets WHERE id = %s", (asset_id,))
        if row is None:
            last_used = self._orphan_last_used(asset_id)
            if last_used is None or last_used >= cutoff:
                return False
        # After the delete has committed: a failure here leaves an orphan blob, which
        # a later sweep removes, never a row without its blob.
        for kind in MEDIA_TYPES:
            (self.root / f"{asset_id}.{kind}").unlink(missing_ok=True)
        return True


class AssetRows(Protocol):
    """The ``assets`` rows a render worker reads and writes through the API's CRUD routes
    (#5298, `crud/render.py` `RemoteAssetRows`). Each raises what `AssetStore` raised:
    `AssetNotFoundError` for no row, `AssetRejectedError` for an adopt refused."""

    def get(self, asset_id: str) -> AssetMeta: ...

    def use(self, asset_id: str) -> AssetMeta: ...

    def adopt(self, meta: AssetMeta) -> None: ...

    def last_used(self, ids: Sequence[str]) -> dict[str, datetime | None]: ...


class UsesAssets(Protocol):
    """What a render reads uploads through, and `RemoteAssets` brings them into:
    `AssetStore` on the API (and its in-process worker), `RemoteAssetStore` on a render
    worker (#5298)."""

    def blob_path(self, meta: AssetMeta) -> Path: ...

    def get(self, asset_id: str) -> AssetMeta: ...

    def use(self, asset_id: str) -> AssetMeta: ...

    def adopt(self, meta: AssetMeta, data: bytes) -> None: ...

    def ids(self) -> list[str]: ...

    def prune_local(self, *, grace: float, now: float | None = None) -> list[str]: ...


class _IdLocks:
    """A `threading.Lock` per asset id, made on first hold and forgotten once no thread
    holds or waits for it."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, tuple[threading.Lock, int]] = {}

    def held(self) -> int:
        with self._guard:
            return len(self._locks)

    def _take(self, asset_id: str) -> threading.Lock:
        with self._guard:
            lock, users = self._locks.get(asset_id, (threading.Lock(), 0))
            self._locks[asset_id] = (lock, users + 1)
        lock.acquire()
        return lock

    def _give(self, asset_id: str) -> None:
        with self._guard:
            lock, users = self._locks[asset_id]
            lock.release()
            if users == 1:
                del self._locks[asset_id]
            else:
                self._locks[asset_id] = (lock, users - 1)

    @contextmanager
    def hold(self, ids: Iterable[str]) -> Iterator[None]:
        """Hold every id's lock, taken in sorted order, so two holders never deadlock."""
        taken: list[str] = []
        try:
            for asset_id in sorted(set(ids)):
                self._take(asset_id)
                taken.append(asset_id)
            yield
        finally:
            for asset_id in reversed(taken):
                self._give(asset_id)


class RemoteAssetStore:
    """A render worker's uploads (#5298, inventory R19-R21): the files under its own
    ``data/assets/``, the rows the API's, through ``rows`` (`crud/render.py`
    `RemoteAssetRows`). The worker holds no database secret.

    The SQL store's `prune_local` held the row's lock while it removed the file, so a
    `use` on this worker either landed first and kept the file, or waited and found it
    gone. Now the prune reads the last uses with no lock held, then removes each stale
    copy under a lock per asset id that `use` and `adopt` also hold, and skips a copy
    this process used or adopted since the prune started: each records when it ended
    (`time.monotonic`) before it lets the id go. So a `use` is never held up by the
    prune's round trip, and never answers a file the prune then removes. The API's sweep,
    which removes the row, still locks the row before its delete there, and the API's
    `use` waits on it.
    """

    def __init__(self, root: Path, rows: AssetRows, *, metrics: Metrics | None = None) -> None:
        self.root = root
        self._rows = rows
        #: Counts a batch whose last uses could not be read (`asset_prune_failures`).
        self._metrics = metrics
        self._locks = _IdLocks()
        #: When `use` or `adopt` last let each id go, by `time.monotonic`.
        self._touched: dict[str, float] = {}
        self._touched_guard = threading.Lock()
        #: One prune at a time: each forgets the stamps older than its own start.
        self._pruning = threading.Lock()

    def blob_path(self, meta: AssetMeta) -> Path:
        return self.root / f"{meta.id}.{meta.kind}"

    def ids(self) -> list[str]:
        """Every copy this root holds, by id."""
        return sorted(_blobs_in(self.root))

    def get(self, asset_id: str) -> AssetMeta:
        if not ASSET_ID_RE.fullmatch(asset_id):
            raise AssetNotFoundError(asset_id)
        meta = self._rows.get(asset_id)
        if not self.blob_path(meta).is_file():
            raise AssetNotFoundError(asset_id)
        return meta

    @contextmanager
    def _touching(self, asset_id: str) -> Iterator[None]:
        """Hold the id's lock, and record when it is let go, so a prune that started
        before then keeps the copy."""
        with self._locks.hold([asset_id]):
            try:
                yield
            finally:
                with self._touched_guard:
                    self._touched[asset_id] = time.monotonic()

    def use(self, asset_id: str) -> AssetMeta:
        """`AssetStore.use`: `get`, and mark the asset used now. With no copy here it is
        not found and the row is not marked, as the SQL `use` rolled its mark back."""
        if not ASSET_ID_RE.fullmatch(asset_id):
            raise AssetNotFoundError(asset_id)
        with self._touching(asset_id):
            if _mtime_in(self.root, asset_id) is None:
                raise AssetNotFoundError(asset_id)
            meta = self._rows.use(asset_id)
            if not self.blob_path(meta).is_file():
                raise AssetNotFoundError(asset_id)
            return meta

    def adopt(self, meta: AssetMeta, data: bytes) -> None:
        """`AssetStore.adopt`: the file first, then the row, so no row the API answers
        this worker lacks the bytes of. The API writes the row from its own copy's meta."""
        _require_bytes(meta, data)
        with self._touching(meta.id):
            blob = self.blob_path(meta)
            written = not blob.is_file()
            if written:
                self.root.mkdir(parents=True, exist_ok=True)
                _write_atomically(blob, data)
            try:
                self._rows.adopt(meta)
            except AssetRejectedError:
                # Not the API's copy: forget the file this call wrote, which has no row.
                if written:
                    blob.unlink(missing_ok=True)
                raise

    def prune_local(self, *, grace: float, now: float | None = None) -> list[str]:
        """`AssetStore.prune_local`: remove the copies last used more than ``grace``
        seconds before ``now`` (one with no row by its mtime), and answer their ids;
        every row stays. The last uses come `LAST_USED_MAX` ids at a time, read with no
        lock held; each stale copy then goes under its id's lock, unless this process
        used or adopted it since the prune started. A batch that fails is logged and
        skipped."""
        with self._pruning:
            started = time.monotonic()
            cutoff = (time.time() if now is None else now) - grace
            cutoff_at = datetime.fromtimestamp(cutoff, UTC)
            local = sorted(_blobs_in(self.root))
            removed: list[str] = []
            for start in range(0, len(local), LAST_USED_MAX):
                batch = local[start : start + LAST_USED_MAX]
                try:
                    stamps = self._rows.last_used(batch)
                except Exception:
                    logger.exception(
                        "could not read the last uses of a batch of assets",
                        extra={"assets": len(batch)},
                    )
                    if self._metrics is not None:
                        self._metrics.asset_prune_failures.inc()
                    continue
                for asset_id in batch:
                    used = stamps.get(asset_id)
                    if used is not None and used >= cutoff_at:
                        continue
                    if self._prune_one(
                        asset_id, row=used is not None, since=started, cutoff=cutoff
                    ):
                        removed.append(asset_id)
            with self._touched_guard:
                for asset_id, at in list(self._touched.items()):
                    if at < started:
                        del self._touched[asset_id]
            return removed

    def _prune_one(self, asset_id: str, *, row: bool, since: float, cutoff: float) -> bool:
        """Remove one stale copy under its id's lock; whether it went. ``row``: its row
        said stale; without one, its mtime decides, read under the lock."""
        with self._locks.hold([asset_id]):
            with self._touched_guard:
                touched = self._touched.get(asset_id)
            if touched is not None and touched >= since:
                return False
            if not row:
                last_used = _mtime_in(self.root, asset_id)
                if last_used is None or last_used >= cutoff:
                    return False
            try:
                for kind in MEDIA_TYPES:
                    (self.root / f"{asset_id}.{kind}").unlink(missing_ok=True)
            except OSError:
                logger.exception(
                    "could not remove a local copy of an asset", extra={"asset": asset_id}
                )
                return False
            return True


def _require_bytes(meta: AssetMeta, data: bytes) -> None:
    """The bytes must be the id, so a damaged or substituted download never becomes a
    render's input."""
    if hashlib.sha256(data).hexdigest() != meta.id or len(data) != meta.size:
        raise AssetRejectedError(f"the fetched file does not match asset {meta.id}")


def _blobs_in(root: Path) -> dict[str, Path]:
    """Every stored blob under ``root`` by id, as the directory lists it now."""
    try:
        entries = list(root.iterdir())
    except FileNotFoundError:
        return {}
    found: dict[str, Path] = {}
    for entry in entries:
        match = _BLOB_RE.fullmatch(entry.name)
        if match:
            found[match.group(1)] = entry
    return found


def _mtime_in(root: Path, asset_id: str) -> float | None:
    """The newest mtime of the asset's files under ``root``; None when there are none."""
    stamps: list[float] = []
    for kind in MEDIA_TYPES:
        with suppress(FileNotFoundError):
            stamps.append((root / f"{asset_id}.{kind}").stat().st_mtime)
    return max(stamps) if stamps else None


def _write_atomically(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _ids_in(data: bytes) -> set[str]:
    return {match.decode("ascii") for match in _ID_IN_TEXT.findall(data)}


def asset_ids_in(params: Mapping[str, object]) -> set[str]:
    """Every asset id a render's parameters could name (as loose as the sweep's scan)."""
    return _ids_in(json.dumps(params, sort_keys=True).encode())


def _ids_in_archive(archive: Path) -> set[str]:
    """The ids in a 3MF's root model, where its provenance is stamped.

    Not `provenance.read`: that answers None for a stamp it cannot parse as well as
    for none at all, which is right for "Edit in ScadBuddy" and wrong here -- a
    damaged or foreign-version stamp must still keep what it names. So the root
    model's raw text is matched, as a JSON record's is. An archive that cannot be
    opened at all raises OSError, which skips the whole sweep.
    """
    try:
        with zipfile.ZipFile(archive) as opened:
            return _ids_in(opened.read(ROOT_MODEL))
    except FileNotFoundError:  # removed since the listing: it keeps nothing now
        return set()
    except (KeyError, zipfile.BadZipFile) as error:
        raise OSError(f"cannot read {archive} for the asset ids it names: {error}") from None


def stopped(stop: threading.Event | None) -> bool:
    """Whether a sweep's ``stop`` is set: the one check every stop point of the asset
    sweep makes, so they cannot drift (#3513)."""
    return stop is not None and stop.is_set()


def referenced_asset_ids(
    paths: DataPaths,
    records: Iterable[Mapping[str, Any]] = (),
) -> set[str]:
    """Every asset id something keeps (#296): the sweep removes nothing in here.

    - every output's records (``params.json`` and the rest of its JSON), and for an
      output whose ``params.json`` is gone, the root model of its 3MF, where the
      provenance "Edit in ScadBuddy" falls back to is stamped;
    - ``records``: JSON records from Postgres, each scanned whole: every saved
      preset's and every job's inputs (the ``render_jobs`` projection, finished or
      not), and every order item's (#4249, orders spec §3.3) -- their ``params`` and
      the template UI's own state beside them alike (#725), which names an upload by
      the same id a ``file`` parameter holds;
    - every template's shipped ``presets.json`` and ``model.json``, mine and built-in;

    A source that exists but cannot be read raises OSError: a sweep that cannot see
    every reference must not remove anything, so the caller skips the whole sweep
    rather than guessing.
    """
    found: set[str] = set()

    def scan(path: Path) -> None:
        # Removed since the listing: it keeps nothing now.
        with suppress(FileNotFoundError):
            found.update(_ids_in(path.read_bytes()))

    for directory in paths.outputs.glob("*/*/"):
        for record in directory.glob("*.json"):
            scan(record)
        archive = directory / "model.3mf"
        if not (directory / "params.json").is_file() and archive.is_file():
            found.update(_ids_in_archive(archive))
    for pattern in (f"*/{LEGACY_PRESETS_NAME}", "*/model.json"):
        for template_file in (*paths.models.glob(pattern), *paths.builtins.glob(pattern)):
            scan(template_file)
    for document in records:
        found.update(_ids_in(json.dumps(document).encode()))
    return found


def sample_files(model_dir: Path, accept: Sequence[str] = FILE_KINDS) -> list[str]:
    """The sample files a template ships for a file parameter taking ``accept``.

    A sample is a regular file directly in ``model_dir`` (never a subdirectory, never
    a symlink, which could point anywhere) whose name is bare -- the rule the runner
    enforces on every `file` value -- and whose extension is an accepted kind. The
    render's staged uploads and wrappers, and the catalogue thumbnail, are not.
    Sorted, so the list is stable.
    """
    kinds = {kind.lower() for kind in accept}
    try:
        entries = list(model_dir.iterdir())
    except OSError:
        return []
    return sorted(
        entry.name
        for entry in entries
        if is_bare_filename(entry.name)
        and not entry.name.startswith(WRAPPER_PREFIX)
        and entry.name not in NOT_SAMPLES
        and entry.suffix.lower().lstrip(".") in kinds
        and not entry.is_symlink()
        and entry.is_file()
    )


def with_samples(schema: CustomizerSchema, model_dir: Path) -> CustomizerSchema:
    """``schema`` with each `file` parameter's `samples` listed from ``model_dir``.

    Done when the schema is served rather than when it is derived, because the cache
    is keyed by the source's hash and a sample can be added or removed without it.
    """
    if not any(parameter.type == "file" for parameter in schema.parameters):
        return schema
    available = sample_files(model_dir)
    return schema.model_copy(
        update={
            "parameters": [
                parameter.model_copy(
                    update={
                        "samples": [
                            name
                            for name in available
                            if Path(name).suffix.lower().lstrip(".") in parameter.accept
                        ]
                    }
                )
                if parameter.type == "file"
                else parameter
                for parameter in schema.parameters
            ]
        }
    )


def file_assets(
    schema: CustomizerSchema,
    params: Mapping[str, ParamValue],
    store: UsesAssets,
    model_dir: Path,
) -> dict[str, AssetMeta]:
    """The uploaded asset behind each `file` parameter in ``params``.

    A file parameter takes the empty string, the model's own default, one of the
    sample files in ``model_dir`` it accepts (`sample_files`), or the id of an asset
    in the store of a kind the parameter accepts -- nothing else, so no client value
    can name a path. A sample needs no asset; it is read where it is. Raises
    ValueError, which a route answers with 422.
    """
    found: dict[str, AssetMeta] = {}
    samples: dict[tuple[str, ...], list[str]] = {}
    for parameter in schema.parameters:
        if parameter.type != "file" or parameter.name not in params:
            continue
        value = params[parameter.name]
        if not isinstance(value, str):
            raise ValueError(f"parameter {parameter.name!r} expects an uploaded file id")
        if value in ("", parameter.initial):
            continue
        accept = tuple(parameter.accept)
        if accept not in samples:
            samples[accept] = sample_files(model_dir, accept)
        if value in samples[accept]:
            continue
        try:
            # `use`, not `get`: whatever validates a value here is about to keep it
            # (a job, a preset), so it is marked used before a sweep can take it.
            meta = store.use(value)
        except AssetNotFoundError:
            raise AssetUnavailableError(parameter.name, value) from None
        if meta.kind not in parameter.accept:
            raise ValueError(
                f"parameter {parameter.name!r} accepts {', '.join(parameter.accept)}, "
                f"not {meta.kind}"
            )
        found[parameter.name] = meta
    return found
