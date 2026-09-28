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
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from scadbuddy.core.files import write_atomic
from scadbuddy.core.paths import DataPaths

logger = logging.getLogger(__name__)

#: The one gate for every change to a preview file in this process: a store's writes
#: and drops, and the catalogue's cleanup of a gone or reused slug, which runs
#: whether or not a store is attached (previews off). A render finishing, a
#: thumbnail being set and a slug being reused are separate threads; without one
#: lock, a removal landing between a render's "still wanted?" check and its write --
#: or between its image and its record -- leaves a record with no image. Writes are
#: rare and small, so one lock costs nothing and keeps no key per model.
_LOCK = threading.Lock()


def drop_preview(paths: DataPaths, slug: str) -> None:
    """Remove ``slug``'s preview image and record, under the preview lock."""
    with _LOCK:
        for path in (paths.model_preview(slug), paths.model_preview_record(slug)):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.exception("could not remove a preview", extra={"path": str(path)})


def remove_preview_file(path: Path) -> bool:
    """Remove one file under ``cache/previews/``, under the preview lock -- for the
    orphan sweep, which finds them one by one. True when it removed one."""
    with _LOCK:
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            logger.exception("could not remove a preview", extra={"path": str(path)})
            return False
        return True


#: A default render's scratch directory under ``cache/previews/``: a dotfile, so the
#: orphan sweep, which reads the directory by slug, never mistakes it for a model's.
WORK_PREFIX = ".work-"


def new_work_dir(paths: DataPaths) -> Path:
    """A fresh name for one default render's scratch directory. Not created."""
    return paths.previews / f"{WORK_PREFIX}{uuid.uuid4().hex}"


def sweep_work_dirs(paths: DataPaths, max_age: float) -> list[str]:
    """Remove the scratch directories of default renders the process died in.

    A render removes its own on the way out, but not when it is killed mid-render,
    and nothing records one, so this is the only thing that ever will. Another
    replica sharing ``/data`` may be rendering into one, so only those older than
    ``max_age`` -- longer than any render may run -- go. One that cannot be read or
    removed is logged and the rest still go.
    """
    root = paths.previews
    if not root.is_dir():
        return []
    cutoff = time.time() - max_age
    removed: list[str] = []
    for entry in sorted(root.glob(f"{WORK_PREFIX}*")):
        try:
            if entry.stat().st_mtime > cutoff:
                continue
            shutil.rmtree(entry)
        except FileNotFoundError:
            continue
        except OSError:
            logger.exception("could not remove a preview's scratch", extra={"path": str(entry)})
            continue
        removed.append(entry.name)
    return removed


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
    libraries its ``model.json`` declares, pins included -- so re-pinning a library
    renders it again. None when the model has no source.

    Not the model's revision: that moves on a README or metadata edit too, which
    would re-render a picture that cannot have changed.
    """
    try:
        source = paths.model_source(slug).read_bytes()
    except FileNotFoundError:
        return None
    try:
        meta = json.loads(paths.model_meta(slug).read_text(encoding="utf-8"))
        libraries = meta.get("libraries") if isinstance(meta, dict) else None
    except (OSError, ValueError):
        # No model.json, or one that cannot be read: nothing declared to depend on.
        libraries = None
    digest = hashlib.sha256(source)
    digest.update(b"\0" + json.dumps(libraries, sort_keys=True).encode())
    return digest.hexdigest()


class PreviewStore:
    """``data/cache/previews/<slug>.png`` and its ``<slug>.json`` record."""

    def __init__(self, paths: DataPaths) -> None:
        self.paths = paths
        #: The process-wide preview lock (`_LOCK`), shared with the catalogue's
        #: cleanup, which may have no store to go through.
        self._lock = _LOCK

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

    def write(
        self, slug: str, key: str, png: bytes, *, wanted: Callable[[], bool] | None = None
    ) -> bool:
        """Keep ``png`` as the preview rendered from ``key``, if ``wanted()`` still
        says so -- asked under the lock, so no drop can land between the answer and
        the write. The image goes first, so a record never names an image that is
        not there yet. False when nothing was written."""
        with self._lock:
            if wanted is not None and not wanted():
                return False
            self.paths.previews.mkdir(parents=True, exist_ok=True)
            write_atomic(self.paths.model_preview(slug), png)
            self._write_record(slug, PreviewRecord(key=key, ok=True, rendered_at=_now()))
            return True

    def record_failure(
        self, slug: str, key: str, error: str, *, wanted: Callable[[], bool] | None = None
    ) -> bool:
        """No preview for ``key``, and why -- so the same source is not tried again.
        ``wanted`` as for :meth:`write`."""
        with self._lock:
            if wanted is not None and not wanted():
                return False
            self.paths.previews.mkdir(parents=True, exist_ok=True)
            self.paths.model_preview(slug).unlink(missing_ok=True)
            record = PreviewRecord(key=key, ok=False, error=error, rendered_at=_now())
            self._write_record(slug, record)
            return True

    def drop(self, slug: str) -> None:
        drop_preview(self.paths, slug)

    def current(self, slug: str, key: str) -> bool:
        """Whether the preview on record was made -- or failed -- from ``key``.

        A record that says it rendered but whose image is gone is not current: it
        would otherwise be trusted for as long as the source stays the same, and the
        model would never get its preview back.
        """
        record = self.record(slug)
        if record is None or record.key != key:
            return False
        return not record.ok or self.paths.model_preview(slug).is_file()

    def _write_record(self, slug: str, record: PreviewRecord) -> None:
        payload = json.dumps(record.model_dump(mode="json"), indent=2) + "\n"
        write_atomic(self.paths.model_preview_record(slug), payload.encode())


def _now() -> datetime:
    return datetime.now(UTC)
