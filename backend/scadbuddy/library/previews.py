"""Default-render previews: a model's catalogue thumbnail when it has none of its own
and no generated output to fall back on.

A preview is the plate image (`plate_1.png`) of a render at the model's default
parameters, made in the background by :mod:`scadbuddy.render.previews`. It is
derived state, never in the model's directory, so it is not committed to the models
history and never shows up among the model's outputs or in a print flow.

With it is kept a record of the source it was rendered from. That is what decides
whether a preview is current, and it is kept for a failed render too, so a model
whose default render fails is not retried until its source changes.

Two stores, chosen like the render queue's (``SCADBUDDY_DATABASE_URL``):

- `PostgresPreviewStore`: one ``model_previews`` row per model id (``builtin:`` ids
  included), the record and the PNG together, so the two can never disagree. It
  survives a restart, so only the first boot renders anything.
- `MemoryPreviewStore`, without a database: this process only. Previews are
  regenerable, so a restart simply renders them again, behind any requested render.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any, Protocol

from psycopg import Connection
from psycopg.rows import DictRow
from pydantic import BaseModel

from scadbuddy.core.paths import DataPaths

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


class PreviewStore(Protocol):
    """Each model's preview image and the record of what it was rendered from.

    ``write``, ``record_failure`` and ``drop`` for one model are serialised: a
    render's "still wanted?" check and its write are one step against a drop, so a
    thumbnail set or a delete landing while a render finishes is never undone by it.
    """

    def record(self, slug: str) -> PreviewRecord | None: ...

    def image(self, slug: str) -> bytes | None: ...

    def preview_id(self, slug: str) -> str | None:
        """Which render the preview is, or None when there is no image to serve.

        The catalogue's cache key for it: a re-render after a source edit is a new
        image under the same model, with no commit of its own to say so.
        """
        ...

    def write(
        self, slug: str, key: str, png: bytes, *, wanted: Callable[[], bool] | None = None
    ) -> bool:
        """Keep ``png`` as the preview rendered from ``key``, if ``wanted()`` still
        says so -- asked while holding the model's lock, so no drop can land between
        the answer and the write. False when nothing was written."""
        ...

    def record_failure(
        self, slug: str, key: str, error: str, *, wanted: Callable[[], bool] | None = None
    ) -> bool:
        """No preview for ``key``, and why -- so the same source is not tried again.
        ``wanted`` as for :meth:`write`."""
        ...

    def drop(self, slug: str) -> None: ...

    def current(self, slug: str, key: str) -> bool:
        """Whether the preview on record was made -- or failed -- from ``key``."""
        ...

    def slugs(self) -> list[str]:
        """Every model id with a preview or a failure on record: the orphan sweep's
        candidates."""
        ...


def _preview_id(record: PreviewRecord | None) -> str | None:
    if record is None or not record.ok:
        return None
    return record.key[:PREVIEW_ID_LENGTH]


class MemoryPreviewStore:
    """Previews in this process, for when there is no database."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[PreviewRecord, bytes | None]] = {}

    def record(self, slug: str) -> PreviewRecord | None:
        entry = self._entries.get(slug)
        return entry[0] if entry is not None else None

    def image(self, slug: str) -> bytes | None:
        entry = self._entries.get(slug)
        return entry[1] if entry is not None else None

    def preview_id(self, slug: str) -> str | None:
        return _preview_id(self.record(slug))

    def write(
        self, slug: str, key: str, png: bytes, *, wanted: Callable[[], bool] | None = None
    ) -> bool:
        with self._lock:
            if wanted is not None and not wanted():
                return False
            self._entries[slug] = (PreviewRecord(key=key, ok=True, rendered_at=_now()), png)
            return True

    def record_failure(
        self, slug: str, key: str, error: str, *, wanted: Callable[[], bool] | None = None
    ) -> bool:
        with self._lock:
            if wanted is not None and not wanted():
                return False
            record = PreviewRecord(key=key, ok=False, error=error, rendered_at=_now())
            self._entries[slug] = (record, None)
            return True

    def drop(self, slug: str) -> None:
        with self._lock:
            self._entries.pop(slug, None)

    def current(self, slug: str, key: str) -> bool:
        record = self.record(slug)
        return record is not None and record.key == key

    def slugs(self) -> list[str]:
        with self._lock:
            return sorted(self._entries)


PreviewConnection = Callable[[], AbstractContextManager[Connection[DictRow]]]


class PostgresPreviewStore:
    """The ``model_previews`` table (`pg_store.MIGRATIONS`), on the render queue's
    pool: ``connect`` is `PostgresJobStore.connection`, so the table exists once the
    queue has started.

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
        return _preview_id(self.record(slug))

    def write(
        self, slug: str, key: str, png: bytes, *, wanted: Callable[[], bool] | None = None
    ) -> bool:
        return self._put(slug, key, ok=True, error=None, png=png, wanted=wanted)

    def record_failure(
        self, slug: str, key: str, error: str, *, wanted: Callable[[], bool] | None = None
    ) -> bool:
        return self._put(slug, key, ok=False, error=error, png=None, wanted=wanted)

    def drop(self, slug: str) -> None:
        with self._connect() as conn, conn.transaction():
            _lock(conn, slug)
            conn.execute("DELETE FROM model_previews WHERE model_id = %s", (slug,))

    def current(self, slug: str, key: str) -> bool:
        record = self.record(slug)
        return record is not None and record.key == key

    def slugs(self) -> list[str]:
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
