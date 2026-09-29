"""A template's images and videos (#274): the files live in ``<model>/media/``, their
order and captions in ``model.json`` under ``media``. The first item is the cover.

Images are committed with the template, as ``thumbnail.png`` always was; videos are
not (``history._gitignore_body``), so the models repository stays small. The entry
is committed either way, which is how a restore can bring back an entry whose video
file is gone: it is listed ``missing`` rather than dropped.

A built-in's media (#722) is an overlay: what it ships (its bundled ``model.json``,
read-only, ``readonly`` in the API) followed by what people added to it, kept outside
the models repository in ``builtin-media/<slug>/`` with `template_media` rows, so an
addition never moves the built-in's revision. Since the shipped items come first,
its cover is a choice of its own (`template_media_cover`): the chosen item, shipped
or added, is listed first; with none chosen the first shipped item is the cover.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

MediaKind = Literal["image", "video"]

MEDIA_DIR = "media"
#: An upload being streamed to ``cache/`` before it is moved into ``media/``; the
#: boot sweep removes one a crash left (``Catalogue.sweep_duplicate_staging``).
MEDIA_UPLOAD_PREFIX = "media-upload-"
#: The id of the one item synthesized from a template's ``thumbnail.png`` while its
#: ``model.json`` has no ``media`` yet. Never stored: the first media write turns it
#: into an ordinary item with an id of its own.
LEGACY_ID = "thumbnail"

#: An id is a path segment of the media routes. A generated one is 12 hex chars; a
#: bundled model.json may name its own (``front``), so the pattern is wider.
MEDIA_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
#: A bare file name inside ``media/``: no separator, no dotfile, so an entry in a
#: hand-edited or uploaded model.json can never name a file outside it.
MEDIA_FILE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"

#: Extension -> (kind, content type). The extension of a stored file is the one
#: `sniff_kind` gave it, so this is what the file is.
EXTENSIONS: dict[str, tuple[MediaKind, str]] = {
    "png": ("image", "image/png"),
    "jpg": ("image", "image/jpeg"),
    "jpeg": ("image", "image/jpeg"),
    "webp": ("image", "image/webp"),
    "mp4": ("video", "video/mp4"),
    "webm": ("video", "video/webm"),
}
VIDEO_EXTENSIONS = tuple(ext for ext, (kind, _) in EXTENSIONS.items() if kind == "video")

#: How many leading bytes `sniff_kind` needs.
SNIFF_BYTES = 16

#: The most images and videos one template holds: the catalogue lists every item of
#: every template, and a carousel of hundreds is no longer a gallery.
MAX_MEDIA_ITEMS = 64

#: The largest image (or poster) an upload may carry. Images are committed to the
#: models repository, which keeps each one for good -- the same reason, and the same
#: 10 MiB, as the thumbnail's cap. Videos are not committed and take the upload limit.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

#: The longest caption, in characters.
MAX_CAPTION_CHARS = 1000


class MediaItem(BaseModel):
    """One ``model.json`` ``media`` entry."""

    id: str = Field(pattern=MEDIA_ID_PATTERN)
    #: The file inside ``media/`` (``thumbnail.png`` beside the source for the legacy item).
    file: str = Field(pattern=MEDIA_FILE_PATTERN)
    kind: MediaKind
    caption: str = ""
    #: A poster image in ``media/``; videos only.
    poster: str | None = Field(default=None, pattern=MEDIA_FILE_PATTERN)


class MediaView(MediaItem):
    """An item as the API reports it, with what the disk says about its file."""

    #: The entry is there and its file is not: a video after a history restore.
    missing: bool = False
    #: Shipped with a built-in template (#722): it cannot be captioned, moved or
    #: removed. The media added to a built-in, and all of a template of mine's, is not.
    readonly: bool = False
    content_type: str
    #: Bytes; None when missing.
    size: int | None


@dataclass(frozen=True)
class StagedMedia:
    """An upload already written to the data volume and typed by its bytes, waiting
    to be moved into ``media/``."""

    path: Path
    kind: MediaKind
    extension: str


def readable_media(value: Any) -> list[MediaItem]:
    """The entries of a ``media`` value that are items. Anything else a hand edit
    left is dropped rather than stopping the model listing, as unreadable library
    pins are -- and so is a second entry with an id already taken."""
    if not isinstance(value, list):
        return []
    readable: list[MediaItem] = []
    for entry in value:
        with contextlib.suppress(ValidationError):
            item = MediaItem.model_validate(entry)
            if all(item.id != seen.id for seen in readable):
                readable.append(item)
    return readable


def sniff_kind(head: bytes) -> tuple[MediaKind, str, str] | None:
    """``(kind, extension, content type)`` from a file's first bytes, or None.

    WebM is recognised by its EBML header, which Matroska shares: an ``.mkv`` passes
    as WebM, and the browser decides whether it plays it.
    """
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "png", "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image", "jpg", "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", "webp", "image/webp"
    if head[4:8] == b"ftyp":
        return "video", "mp4", "video/mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video", "webm", "video/webm"
    return None


def content_type_of(file: str) -> str:
    known = EXTENSIONS.get(file.rpartition(".")[2].lower())
    return known[1] if known is not None else "application/octet-stream"


def new_media_id() -> str:
    return uuid.uuid4().hex[:12]
