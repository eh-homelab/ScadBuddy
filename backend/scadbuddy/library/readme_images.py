"""An image a person puts in a template's README (#5475).

The image goes into the template's own ``images/`` folder as one revision, so the
template's git history keeps it. The README then links it as ``images/<name>``, which
``GET /models/{slug}/images/{path}`` (``api/lsp.py``) serves and the browser's
``safeImageSrc`` (``frontend/src/lib/safeUrl.ts``) accepts.

The type comes from the bytes, never from the name or the client's content type. Only
PNG, JPEG, WebP and GIF are taken. The README renderer also shows SVG, but an SVG is a
document that can hold script, so an upload never adds one. Pillow then reads each
image's header, so a file with a good signature and a broken body is refused too.
"""

from __future__ import annotations

import io
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, UnidentifiedImageError

if TYPE_CHECKING:
    from scadbuddy.library.catalogue import Catalogue, ModelRecord

#: The folder in a template that holds its README's images.
IMAGES_DIR = "images"

#: The most images one template's ``images/`` folder may hold. Each image is a
#: revision that the models repository keeps for good.
MAX_README_IMAGES = 100

#: The most pixels an image may declare. A README shows a picture, not a poster.
MAX_README_IMAGE_PIXELS = 40_000_000

#: The longest stem of a stored name, in characters.
MAX_STEM_CHARS = 64

#: The canonical extension and Pillow's format name of each type taken, by media type.
TYPES: dict[str, tuple[str, str]] = {
    "image/png": ("png", "PNG"),
    "image/jpeg": ("jpg", "JPEG"),
    "image/webp": ("webp", "WEBP"),
    "image/gif": ("gif", "GIF"),
}

#: A stored name: a plain stem and one of the extensions above.
IMAGE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\.(?:png|jpg|webp|gif)$")

_NOT_STEM = re.compile(r"[^a-z0-9_-]+")


class ReadmeImageError(ValueError):
    """The bytes are not an image a README may take; the text says why."""


class TooManyReadmeImagesError(ValueError):
    """The template's ``images/`` folder already holds `MAX_README_IMAGES`."""


class ImagesFolderError(ValueError):
    """The template's ``images`` entry is not a plain folder (a file or a link)."""


def sniff(data: bytes) -> str | None:
    """The media type the bytes start with, or None for any other file."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def image_type(data: bytes) -> str:
    """The media type of an image a README may take, or :class:`ReadmeImageError`.

    The signature decides the type. Pillow then reads the header and checks the
    structure; it must agree on the format, and the image must not declare more than
    `MAX_README_IMAGE_PIXELS`.
    """
    media_type = sniff(data)
    if media_type is None:
        raise ReadmeImageError("the file is not a PNG, JPEG, WebP or GIF image")
    expected = TYPES[media_type][1]
    try:
        with Image.open(io.BytesIO(data), formats=[expected]) as image:
            width, height = image.size
            if width * height > MAX_README_IMAGE_PIXELS:
                raise ReadmeImageError(
                    f"the image is {width}x{height}; at most {MAX_README_IMAGE_PIXELS} pixels"
                    " are taken"
                )
            image.verify()
    except Image.DecompressionBombError:
        raise ReadmeImageError(
            f"the image declares more than {MAX_README_IMAGE_PIXELS} pixels"
        ) from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as error:
        if isinstance(error, ReadmeImageError):
            raise
        raise ReadmeImageError(f"the file is not a readable {expected} image") from None
    return media_type


def stored_name(name: str | None, media_type: str) -> str:
    """The name to store an image under: the last part of ``name`` (any folder in it
    dropped), its extension dropped, lower-cased, with every run of other characters
    made one ``-``, then the extension of the type the bytes have. ``image`` when
    nothing of the name is left."""
    base = re.split(r"[/\\]", name or "")[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    stem = _NOT_STEM.sub("-", stem.lower()).strip("-_")[:MAX_STEM_CHARS].strip("-_")
    return f"{stem or 'image'}.{TYPES[media_type][0]}"


def free_name(folder: Path, name: str) -> str:
    """``name``, or ``<stem>-2.<ext>``, ``-3`` and on: the first that names nothing in
    ``folder``. An existing file is never written over."""
    stem, _, extension = name.rpartition(".")
    candidate = name
    number = 1
    while os.path.lexists(folder / candidate):
        number += 1
        # The suffix stays inside the pattern's stem length.
        candidate = f"{stem[: MAX_STEM_CHARS - len(str(number)) - 1]}-{number}.{extension}"
    return candidate


def add_readme_image(
    catalogue: Catalogue, slug: str, name: str, data: bytes
) -> tuple[ModelRecord, str]:
    """Write ``data`` into ``slug``'s ``images/`` as one revision; answer the record and
    the stored path relative to the template (``images/<name>``).

    The name is chosen, and the folder counted, under the history's write lock
    (``Catalogue.update_files``), so two adds at once never take one name.
    """
    if not IMAGE_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"{name!r} is not a stored image name")
    stored: list[str] = []

    def compute(directory: Path) -> dict[str, bytes | None]:
        folder = directory / IMAGES_DIR
        if folder.is_symlink() or (os.path.lexists(folder) and not folder.is_dir()):
            raise ImagesFolderError(IMAGES_DIR)
        if folder.is_dir():
            held = sum(1 for entry in folder.iterdir() if not entry.name.startswith("."))
            if held >= MAX_README_IMAGES:
                raise TooManyReadmeImagesError(held)
        chosen = free_name(folder, name)
        stored[:] = [f"{IMAGES_DIR}/{chosen}"]
        return {stored[0]: data}

    record = catalogue.update_files(slug, compute, message=f"Add an image to the {slug} README")
    return record, stored[0]
