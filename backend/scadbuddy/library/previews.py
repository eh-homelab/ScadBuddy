"""Default-render previews: a model's catalogue thumbnail when it has none of its own
and no generated output to fall back on.

A preview is the plate image (`plate_1.png`) of a render at the model's default
parameters, made in the background by :mod:`scadbuddy.render.previews`. It is a
derived file, like the schema cache: it lives under ``cache/previews/``, never in the
model's directory, so it is not committed to the models history and never shows up
among the model's outputs or in a print flow.

Beside each image is a record of the source it was rendered from. That is what
decides whether a preview is current, and it is kept for a failed render too, so a
model whose default render fails is not retried until its source changes.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime

from pydantic import BaseModel, ValidationError

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.catalogue import _write_atomic
from scadbuddy.library.libraries import declared_libraries

logger = logging.getLogger(__name__)

#: How much of the source key names a preview to a client -- enough to tell two
#: renders of one model apart, which is all its cache key needs.
PREVIEW_ID_LENGTH = 16


class PreviewRecord(BaseModel):
    #: :func:`source_key` of what the render read.
    key: str
    ok: bool
    error: str | None = None
    rendered_at: datetime


def source_key(paths: DataPaths, slug: str) -> str | None:
    """What a default render of ``slug`` depends on: its ``model.scad`` and the
    libraries its ``model.json`` declares. None when the model has no source.

    Not the model's revision: that moves on a README or metadata edit too, which
    would re-render a picture that cannot have changed.
    """
    try:
        source = paths.model_source(slug).read_bytes()
    except FileNotFoundError:
        return None
    try:
        libraries = declared_libraries(paths.model_dir(slug))
    except (OSError, ValueError):
        # A model.json that cannot be read renders with no libraries, as every
        # render of it would fail anyway.
        libraries = []
    digest = hashlib.sha256(source)
    digest.update(b"\0" + json.dumps(sorted(libraries)).encode())
    return digest.hexdigest()


class PreviewStore:
    """``data/cache/previews/<slug>.png`` and its ``<slug>.json`` record."""

    def __init__(self, paths: DataPaths) -> None:
        self.paths = paths

    def record(self, slug: str) -> PreviewRecord | None:
        path = self.paths.model_preview_record(slug)
        try:
            return PreviewRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValidationError):
            logger.warning("ignored an unreadable preview record", extra={"slug": slug})
            return None

    def image(self, slug: str) -> bytes | None:
        try:
            return self.paths.model_preview(slug).read_bytes()
        except FileNotFoundError:
            return None

    def preview_id(self, slug: str) -> str | None:
        """Which render the preview is, or None when there is no image to serve.

        The catalogue's cache key for it: a re-render after a source edit is a new
        image under the same model, with no commit of its own to say so.
        """
        if not self.paths.model_preview(slug).is_file():
            return None
        record = self.record(slug)
        if record is None or not record.ok:
            return None
        return record.key[:PREVIEW_ID_LENGTH]

    def write(self, slug: str, key: str, png: bytes) -> None:
        """Keep ``png`` as the preview rendered from ``key``. The image goes first, so
        a record never names a render whose image is not there yet."""
        self.paths.previews.mkdir(parents=True, exist_ok=True)
        _write_atomic(self.paths.model_preview(slug), png)
        self._write_record(slug, PreviewRecord(key=key, ok=True, rendered_at=_now()))

    def record_failure(self, slug: str, key: str, error: str) -> None:
        """No preview for ``key``, and why -- so the same source is not tried again."""
        self.paths.previews.mkdir(parents=True, exist_ok=True)
        self.paths.model_preview(slug).unlink(missing_ok=True)
        self._write_record(slug, PreviewRecord(key=key, ok=False, error=error, rendered_at=_now()))

    def drop(self, slug: str) -> None:
        for path in (self.paths.model_preview(slug), self.paths.model_preview_record(slug)):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.exception("could not remove a preview", extra={"path": str(path)})

    def _write_record(self, slug: str, record: PreviewRecord) -> None:
        payload = json.dumps(record.model_dump(mode="json"), indent=2) + "\n"
        _write_atomic(self.paths.model_preview_record(slug), payload.encode())


def _now() -> datetime:
    return datetime.now(UTC)
