"""Default-render previews: a model's catalogue thumbnail when it has none of its own
and no generated output to fall back on.

A preview is the plate image (`plate_1.png`) of a render at the model's default
parameters, made in the background by :mod:`scadbuddy.render.previews`. It is
derived state, never in the model's directory, so it is not committed to the models
history and never shows up among the model's outputs or in a print flow.

With it is kept a record of the source it was rendered from. That is what decides
whether a preview is current, and it is kept for a failed render too, so a model
whose default render fails is not retried until its source changes.

They live in Postgres (#454): one ``model_previews`` row per model id (``builtin:``
ids included), the record and the PNG together, so the two can never disagree, and
they survive a restart, so only the first boot renders anything. Without a database
there are no previews at all (the database becomes required with #401).
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow
from pydantic import BaseModel

from scadbuddy.core.paths import DataPaths

logger = logging.getLogger(__name__)

#: A default render's scratch directory under ``cache/preview-work/``. The PNG it
#: produces goes to the database; the directory is on disk, like any render's.
WORK_PREFIX = ".work-"


def new_work_dir(paths: DataPaths) -> Path:
    """A fresh name for one default render's scratch directory. Not created."""
    return paths.preview_work / f"{WORK_PREFIX}{uuid.uuid4().hex}"


def sweep_work_dirs(paths: DataPaths, max_age: float) -> list[str]:
    """Remove the scratch directories of default renders the process died in.

    A render removes its own on the way out, but not when it is killed mid-render,
    and nothing records one, so this is the only thing that ever will. Another
    replica sharing ``/data`` may be rendering into one, so only those older than
    ``max_age`` -- longer than any render may run -- go. One that cannot be read or
    removed is logged and the rest still go.
    """
    root = paths.preview_work
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

#: The first key of the two-key `pg_advisory_xact_lock` that serialises the changes
#: to one model's preview ("PRVW" in ASCII); the second is a hash of the model id.
PREVIEW_LOCK_CLASS = 0x5052_5657


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


PreviewConnection = Callable[[], AbstractContextManager[Connection[DictRow]]]


class PreviewStore:
    """The ``model_previews`` table (``migrations/20260928T0721Z_model_previews.sql``),
    on the projection's pool: ``connect`` is `JobProjection.pool`'s ``connection``, so
    the table exists once the projection has opened (and migrated).

    A change to one model's preview holds a transaction-scoped advisory lock on its
    id, taken before the row is read or written, so it also orders a write against
    the drop of a row that does not exist yet -- which a row lock cannot -- across
    every process sharing the database.
    """

    def __init__(self, connect: PreviewConnection) -> None:
        self._connect = connect

    def record(self, slug: str) -> PreviewRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT source_key, ok, error, rendered_at FROM model_previews WHERE model_id = %s",
                (slug,),
            ).fetchone()
        return _record(row) if row is not None else None

    def image(self, slug: str) -> bytes | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT png FROM model_previews WHERE model_id = %s", (slug,)
            ).fetchone()
        return bytes(row["png"]) if row is not None and row["png"] is not None else None

    def preview_id(self, slug: str) -> str | None:
        """Which render the preview is, or None when there is no image to serve.

        The catalogue's cache key for it: a re-render after a source edit is a new
        image under the same model, with no commit of its own to say so.
        """
        record = self.record(slug)
        if record is None or not record.ok:
            return None
        return record.key[:PREVIEW_ID_LENGTH]

    def write(
        self, slug: str, key: str, png: bytes, *, wanted: Callable[[], bool] | None = None
    ) -> bool:
        """Keep ``png`` as the preview rendered from ``key``, if ``wanted()`` still
        says so -- asked while holding the model's lock, so no drop can land between
        the answer and the write. False when nothing was written."""
        return self._put(slug, key, ok=True, error=None, png=png, wanted=wanted)

    def record_failure(
        self, slug: str, key: str, error: str, *, wanted: Callable[[], bool] | None = None
    ) -> bool:
        """No preview for ``key``, and why -- so the same source is not tried again.
        ``wanted`` as for :meth:`write`."""
        return self._put(slug, key, ok=False, error=error, png=None, wanted=wanted)

    def drop(self, slug: str) -> None:
        with self._connect() as conn, conn.transaction():
            _lock(conn, slug)
            conn.execute("DELETE FROM model_previews WHERE model_id = %s", (slug,))

    def current(self, slug: str, key: str) -> bool:
        """Whether the preview on record was made -- or failed -- from ``key``."""
        record = self.record(slug)
        return record is not None and record.key == key

    def slugs(self) -> list[str]:
        """Every model id with a preview or a failure on record: the orphan sweep's
        candidates."""
        with self._connect() as conn:
            rows = conn.execute("SELECT model_id FROM model_previews ORDER BY model_id").fetchall()
        return [row["model_id"] for row in rows]

    def _put(
        self,
        slug: str,
        key: str,
        *,
        ok: bool,
        error: str | None,
        png: bytes | None,
        wanted: Callable[[], bool] | None,
    ) -> bool:
        with self._connect() as conn, conn.transaction():
            _lock(conn, slug)
            if wanted is not None and not wanted():
                return False
            conn.execute(
                "INSERT INTO model_previews (model_id, source_key, ok, error, png, rendered_at)"
                " VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (model_id) DO UPDATE SET source_key = EXCLUDED.source_key,"
                " ok = EXCLUDED.ok, error = EXCLUDED.error, png = EXCLUDED.png,"
                " rendered_at = EXCLUDED.rendered_at",
                (slug, key, ok, error, png, _now()),
            )
            return True


def _lock(conn: Connection[Any], slug: str) -> None:
    conn.execute(
        "SELECT pg_advisory_xact_lock(%s::integer, hashtext(%s))", (PREVIEW_LOCK_CLASS, slug)
    )


def _record(row: DictRow) -> PreviewRecord:
    return PreviewRecord(
        key=row["source_key"], ok=row["ok"], error=row["error"], rendered_at=row["rendered_at"]
    )


def _now() -> datetime:
    return datetime.now(UTC)
