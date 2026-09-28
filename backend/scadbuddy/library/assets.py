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
(SCADBUDDY_ASSET_SWEEP_GRACE), is removed by `AssetStore.sweep`. The store is capped
in total bytes and in count (SCADBUDDY_ASSET_MAX_TOTAL_BYTES / _MAX_COUNT); an upload
past either is refused, and re-uploading what is already stored never is.

Nothing here ever becomes a path OpenSCAD sees. The render stages a copy under a
name it generates (`render/jobs.py`); the original file name is display-only.

A template's own sample files are the other thing a file parameter can name: the
bare names `sample_files` lists from the model's directory. They are never copied
anywhere -- OpenSCAD reads them where they are, as it does any bundled file.
"""

from __future__ import annotations

import fcntl
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
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Literal

from lxml import etree
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel

from scadbuddy.core.paths import TEMPLATE_PRESETS_NAME, DataPaths
from scadbuddy.library.catalogue import THUMBNAIL_NAME
from scadbuddy.render.provenance import ROOT_MODEL
from scadbuddy.render.schema import FILE_KINDS, CustomizerSchema, ParamValue, is_bare_filename
from scadbuddy.render.solids import WRAPPER_PREFIX

logger = logging.getLogger(__name__)

AssetKind = Literal["svg", "png"]

ASSET_ID_PATTERN = r"^[0-9a-f]{64}$"
ASSET_ID_RE = re.compile(ASSET_ID_PATTERN)
#: A stored blob's file name: the id and the kind, nothing else. The metadata and a
#: write's temporary file never match.
_BLOB_RE = re.compile(r"^([0-9a-f]{64})\.(svg|png)$")
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


class AssetStore:
    """``data/assets/<sha256>.<kind>`` plus ``<sha256>.json`` for its metadata.

    An asset's LAST USE is the later of its two files' mtimes: rewritten by every
    upload of the content (a re-upload included) and touched by every render or
    preset save that names it (`use`). The sweep removes only what was last used
    before its grace period.
    """

    def __init__(self, root: Path, *, max_total_bytes: int = 0, max_count: int = 0) -> None:
        self.root = root
        #: 0 is no limit, for either.
        self.max_total_bytes = max_total_bytes
        self.max_count = max_count

    @property
    def lock_path(self) -> Path:
        """Beside the store rather than in it (``data/.assets.lock``), so the store
        directory holds nothing but assets."""
        return self.root.with_name(f".{self.root.name}.lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialise the steps that must not interleave: storing (with its quota
        check), marking used, and the sweep's re-check and removal of one asset.

        An ``flock`` on `lock_path` rather than a ``threading.Lock``, so it
        also holds between processes sharing the volume (replicas on a
        ReadWriteMany PVC). Each call opens its own descriptor, so threads of one
        process exclude each other too.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

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

    def usage(self) -> AssetUsage:
        count = 0
        total = 0
        for blob in self._blobs().values():
            try:
                total += blob.stat().st_size
            except FileNotFoundError:  # swept between the listing and the stat
                continue
            count += 1
        return AssetUsage(
            count=count,
            bytes=total,
            max_count=self.max_count,
            max_total_bytes=self.max_total_bytes,
        )

    def _require_room(self, size: int) -> None:
        usage = self.usage()
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

    def _meta_path(self, asset_id: str) -> Path:
        return self.root / f"{asset_id}.json"

    def get(self, asset_id: str) -> AssetMeta:
        if not ASSET_ID_RE.fullmatch(asset_id):
            raise AssetNotFoundError(asset_id)
        path = self._meta_path(asset_id)
        if not path.is_file():
            raise AssetNotFoundError(asset_id)
        meta = AssetMeta.model_validate_json(path.read_text(encoding="utf-8"))
        if not self.blob_path(meta).is_file():
            raise AssetNotFoundError(asset_id)
        return meta

    def use(self, asset_id: str) -> AssetMeta:
        """`get`, and mark the asset used now, so a sweep already under way skips it.

        Under the lock: a sweep re-checks the last use under the same lock just
        before it removes anything, so either this finds the asset and the sweep
        then sees it fresh, or the sweep removed it first and this is a not-found.
        """
        with self._locked():
            meta = self.get(asset_id)
            os.utime(self._meta_path(asset_id))
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
        with self._locked():
            # Content already stored costs nothing, so a full store still takes it:
            # re-uploading a file an output uses must keep working at the cap.
            if not (self.blob_path(meta).is_file() and self._meta_path(meta.id).is_file()):
                self._require_room(len(stored))
            _write_atomically(self.blob_path(meta), stored)
            _write_atomically(
                self._meta_path(meta.id),
                (json.dumps(meta.model_dump(), indent=2) + "\n").encode(),
            )
        return meta

    def _last_used(self, asset_id: str, blob: Path) -> float | None:
        """The later of the blob's and the metadata's mtime; None once both are gone."""
        stamps: list[float] = []
        for path in (blob, self._meta_path(asset_id)):
            try:
                stamps.append(path.stat().st_mtime)
            except FileNotFoundError:
                continue
        return max(stamps) if stamps else None

    def sweep(
        self, referenced: Collection[str], *, grace: float, now: float | None = None
    ) -> list[str]:
        """Remove every asset not in ``referenced`` and last used more than ``grace``
        seconds before ``now``; answer the ids removed.

        ``referenced`` is collected before the call (`referenced_asset_ids`), so a
        reference made while the sweep runs is not in it. What protects that asset
        is its last use: every path that creates a reference -- an upload, a render
        submit, a preset save -- marks the asset used under the lock first, and each
        removal re-checks the last use under that same lock. Removal takes the
        metadata first, so `get` stops finding the asset before its bytes go.
        Anything that cannot be removed is logged and skipped, like the other sweeps.
        """
        cutoff = (time.time() if now is None else now) - grace
        removed: list[str] = []
        for asset_id, blob in self._blobs().items():
            if asset_id in referenced:
                continue
            with self._locked():
                last_used = self._last_used(asset_id, blob)
                if last_used is None or last_used >= cutoff:
                    continue
                try:
                    self._meta_path(asset_id).unlink(missing_ok=True)
                    blob.unlink(missing_ok=True)
                except OSError:
                    logger.exception("could not remove an unused asset", extra={"asset": asset_id})
                    continue
            removed.append(asset_id)
        return removed


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
    - every saved preset (``data/presets/``);
    - every template's shipped ``presets.json`` and ``model.json``, mine and built-in;
    - ``params``: the jobs in the render queue's store, finished or not.

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
    for preset_file in paths.presets.glob("*.json"):
        scan(preset_file)
    for pattern in (f"*/{TEMPLATE_PRESETS_NAME}", "*/model.json"):
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
