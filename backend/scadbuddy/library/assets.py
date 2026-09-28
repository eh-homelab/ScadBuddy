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
import os
import re
import secrets
import time
import unicodedata
import zipfile
from collections.abc import Collection, Iterable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import psycopg
from lxml import etree
from PIL import Image, UnidentifiedImageError
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from scadbuddy.core.paths import LEGACY_PRESETS_NAME, DataPaths
from scadbuddy.library.catalogue import THUMBNAIL_NAME
from scadbuddy.render.provenance import ROOT_MODEL
from scadbuddy.render.schema import FILE_KINDS, CustomizerSchema, ParamValue, is_bare_filename
from scadbuddy.render.solids import WRAPPER_PREFIX

logger = logging.getLogger(__name__)

AssetKind = Literal["svg", "png"]

ASSET_ID_PATTERN = r"^[0-9a-f]{64}$"
ASSET_ID_RE = re.compile(ASSET_ID_PATTERN)
#: A stored blob's file name: the id and the kind, nothing else. A write's temporary
#: file never matches.
_BLOB_RE = re.compile(r"^([0-9a-f]{64})\.(svg|png)$")
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
_SELECT_META = "SELECT id, name, kind, size, width, height FROM assets WHERE id = %s"
#: The sweep's candidates among the rows: those last used before the cutoff, read
#: through the `assets_last_used` index.
STALE_ROWS = "SELECT id FROM assets WHERE last_used_at < %s"
_ROWS_AMONG = "SELECT id FROM assets WHERE id = ANY(%s)"
_MARK_USED = (
    "UPDATE assets SET last_used_at = %s WHERE id = %s"
    " RETURNING id, name, kind, size, width, height"
)
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

MEDIA_TYPES: dict[AssetKind, str] = {"svg": "image/svg+xml", "png": "image/png"}

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
    """The upload is not an SVG or PNG this store will keep."""


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


class AssetMeta(BaseModel):
    #: The SHA-256 of the stored bytes (after sanitising), hex.
    id: str
    #: The uploaded file's name, for display only. Never a path.
    name: str
    kind: AssetKind
    size: int
    width: int | None = None
    height: int | None = None


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
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise AssetRejectedError(f"the PNG could not be decoded: {error}") from None


def sniff(data: bytes) -> AssetKind:
    """What the bytes are, whatever the upload called them."""
    if data.startswith(PNG_MAGIC):
        return "png"
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"<") and b"<svg" in data[:65536]:
        return "svg"
    raise AssetRejectedError("only SVG and PNG files can be attached")


class AssetStoreUnavailableError(RuntimeError):
    """The upload store's metadata lives in Postgres, and this store has no pool."""

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
    replica reads the same numbers. The pool is the render queue's
    (`PostgresJobStore.pool`), opened and migrated at startup; this store opens
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
        try:
            entries = list(self.root.iterdir())
        except FileNotFoundError:
            return {}
        found: dict[str, Path] = {}
        for entry in entries:
            match = _BLOB_RE.fullmatch(entry.name)
            if match:
                found[match.group(1)] = entry
        return found

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

    def get(self, asset_id: str) -> AssetMeta:
        if not ASSET_ID_RE.fullmatch(asset_id):
            raise AssetNotFoundError(asset_id)
        with self._require().connection() as conn:
            found = conn.execute(_SELECT_META, (asset_id,)).fetchone()
        if found is None:
            raise AssetNotFoundError(asset_id)
        meta = AssetMeta.model_validate(found)
        if not self.blob_path(meta).is_file():
            raise AssetNotFoundError(asset_id)
        return meta

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

    def put(self, data: bytes, filename: str | None) -> AssetMeta:
        """Validate, sanitise and store an upload; the same content is stored once."""
        if len(data) > MAX_ASSET_BYTES:
            raise AssetRejectedError(f"the file is larger than {MAX_ASSET_BYTES} bytes")
        kind = sniff(data)
        width: int | None = None
        height: int | None = None
        if kind == "png":
            stored, width, height = normalise_png(data)
        else:
            stored = sanitise_svg(data)
        meta = AssetMeta(
            id=hashlib.sha256(stored).hexdigest(),
            name=display_name(filename, kind),
            kind=kind,
            size=len(stored),
            width=width,
            height=height,
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
            conn.execute(
                "INSERT INTO assets"
                " (id, name, kind, size, width, height, created_at, last_used_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (id) DO UPDATE"
                " SET name = EXCLUDED.name, last_used_at = EXCLUDED.last_used_at",
                (meta.id, meta.name, meta.kind, meta.size, meta.width, meta.height, now, now),
            )
        return meta

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
        stamps: list[float] = []
        for kind in MEDIA_TYPES:
            with suppress(FileNotFoundError):
                stamps.append((self.root / f"{asset_id}.{kind}").stat().st_mtime)
        return max(stamps) if stamps else None

    def sweep(
        self, referenced: Collection[str], *, grace: float, now: float | None = None
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
        for asset_id in candidates:
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


def _write_atomically(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _ids_in(data: bytes) -> set[str]:
    return {match.decode("ascii") for match in _ID_IN_TEXT.findall(data)}


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


def referenced_asset_ids(
    paths: DataPaths, params: Iterable[Mapping[str, ParamValue]] = ()
) -> set[str]:
    """Every asset id something keeps (#296): the sweep removes nothing in here.

    - every output's records (``params.json`` and the rest of its JSON), and for an
      output whose ``params.json`` is gone, the root model of its 3MF, where the
      provenance "Edit in ScadBuddy" falls back to is stamped;
    - ``params``: every saved preset's values (they are in Postgres) and every job's
      in the render queue's store, finished or not;
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
    for values in params:
        found.update(_ids_in(json.dumps(values).encode()))
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
    store: AssetStore,
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
            raise ValueError(
                f"parameter {parameter.name!r} is not an uploaded or sample file: {value[:80]!r}"
            ) from None
        if meta.kind not in parameter.accept:
            raise ValueError(
                f"parameter {parameter.name!r} accepts {', '.join(parameter.accept)}, "
                f"not {meta.kind}"
            )
        found[parameter.name] = meta
    return found
