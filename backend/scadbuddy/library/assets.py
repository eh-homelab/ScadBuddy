"""Files a viewer attaches to a render: the store behind `// file` parameters (#204).

An upload is sniffed, never trusted by its name: a PNG is decoded and re-encoded
(downscaled to `MAX_PNG_SIDE`, because `surface()` cost grows with pixel count), an
SVG is parsed and stripped of everything that is not geometry. What is stored is
the result, keyed by its SHA-256 -- so the id IS the content hash, and a render's
`params.json` names exactly the bytes it read.

Assets are never pruned. An output's parameters point at one, and "re-render" and
"Customize this version" must reproduce the output long after the upload.

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
import os
import re
import secrets
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from lxml import etree
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel

from scadbuddy.library.catalogue import THUMBNAIL_NAME
from scadbuddy.render.schema import FILE_KINDS, CustomizerSchema, ParamValue, is_bare_filename
from scadbuddy.render.solids import WRAPPER_PREFIX

AssetKind = Literal["svg", "png"]

ASSET_ID_PATTERN = r"^[0-9a-f]{64}$"
ASSET_ID_RE = re.compile(ASSET_ID_PATTERN)

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
    """``data/assets/<sha256>.<kind>`` plus ``<sha256>.json`` for its metadata."""

    def __init__(self, root: Path) -> None:
        self.root = root

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
        self.root.mkdir(parents=True, exist_ok=True)
        _write_atomically(self.blob_path(meta), stored)
        _write_atomically(
            self._meta_path(meta.id), (json.dumps(meta.model_dump(), indent=2) + "\n").encode()
        )
        return meta


def _write_atomically(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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
            meta = store.get(value)
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
