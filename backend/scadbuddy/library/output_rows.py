"""`OutputRows`: every saved output as one row of ``outputs`` (#7255, outputs-in-Postgres
spec 2026-11-23 §4.2), and the log of the copy runs that fill it (``output_copy_runs``).

The files under ``data/outputs/<slug>/<id>/`` still hold the bytes until #7256; this
store holds the records. It is synchronous, like `OutputIndex`: callers on the loop use a
thread. It imports `OutputMeta` only inside a function, because `outputs.py` will import
this module.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

if TYPE_CHECKING:
    from scadbuddy.library.outputs import OutputMeta

_COLUMNS = (
    "output_id, slug, meta, params, inputs, record, manifest, bom, arranged_from,"
    " backfill, has_plate_cover, copied_at"
)


@dataclass(frozen=True)
class OutputRow:
    output_id: str
    slug: str
    meta: OutputMeta
    params: dict[str, Any]
    #: ``None`` is a record from before inputs.
    inputs: dict[str, Any] | None
    record: dict[str, Any] | None
    manifest: list[Any] | None
    bom: list[Any] | None
    arranged_from: list[str] | None
    backfill: dict[str, Any] | None
    has_plate_cover: bool
    #: When the copy last wrote the row; `upsert` sets it, so the value given is ignored.
    copied_at: datetime


def _json(value: Any) -> Jsonb | None:
    return None if value is None else Jsonb(value)


def _meta_json(meta: OutputMeta) -> Jsonb:
    return Jsonb(meta.model_dump(mode="json"))


def _row(data: DictRow) -> OutputRow:
    from scadbuddy.library.outputs import OutputMeta

    return OutputRow(
        output_id=data["output_id"],
        slug=data["slug"],
        meta=OutputMeta.model_validate(data["meta"]),
        params=data["params"],
        inputs=data["inputs"],
        record=data["record"],
        manifest=data["manifest"],
        bom=data["bom"],
        arranged_from=data["arranged_from"],
        backfill=data["backfill"],
        has_plate_cover=data["has_plate_cover"],
        copied_at=data["copied_at"],
    )


def _values(row: OutputRow) -> tuple[Any, ...]:
    return (
        row.output_id,
        row.slug,
        _meta_json(row.meta),
        Jsonb(row.params),
        _json(row.inputs),
        _json(row.record),
        _json(row.manifest),
        _json(row.bom),
        _json(row.arranged_from),
        _json(row.backfill),
        row.has_plate_cover,
        row.meta.created_at,
    )


_INSERT = (
    "INSERT INTO outputs (output_id, slug, meta, params, inputs, record, manifest, bom,"
    " arranged_from, backfill, has_plate_cover, created_at)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)

_UPSERT = (
    _INSERT + " ON CONFLICT (output_id) DO UPDATE SET slug = EXCLUDED.slug, meta = EXCLUDED.meta,"
    " params = EXCLUDED.params, inputs = EXCLUDED.inputs, record = EXCLUDED.record,"
    " manifest = EXCLUDED.manifest, bom = EXCLUDED.bom,"
    " arranged_from = EXCLUDED.arranged_from, backfill = EXCLUDED.backfill,"
    " has_plate_cover = EXCLUDED.has_plate_cover, created_at = EXCLUDED.created_at,"
    " copied_at = clock_timestamp()"
)


class OutputRows:
    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool
        self._copied = False
        self._lock = threading.Lock()

    def get(self, output_id: str) -> OutputRow | None:
        with self._pool.connection() as conn:
            found = conn.execute(
                f"SELECT {_COLUMNS} FROM outputs WHERE output_id = %s", (output_id,)
            ).fetchone()
        return None if found is None else _row(found)

    def slug_of(self, output_id: str) -> str | None:
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT slug FROM outputs WHERE output_id = %s", (output_id,)
            ).fetchone()
        return None if found is None else str(found["slug"])

    def existing(self, output_ids: Iterable[str]) -> set[str]:
        ids = list(output_ids)
        if not ids:
            return set()
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT output_id FROM outputs WHERE output_id = ANY(%s)", (ids,)
            ).fetchall()
        return {str(item["output_id"]) for item in found}

    def for_slug(self, slug: str) -> list[OutputRow]:
        """The model's outputs, newest first."""
        with self._pool.connection() as conn:
            found = conn.execute(
                f"SELECT {_COLUMNS} FROM outputs WHERE slug = %s"
                " ORDER BY created_at DESC, output_id DESC",
                (slug,),
            ).fetchall()
        return [_row(item) for item in found]

    def insert(self, row: OutputRow, conn: Connection[DictRow] | None = None) -> None:
        """Add the row; a row with that id already is an error. With ``conn``, inside its
        transaction."""
        if conn is not None:
            conn.execute(_INSERT, _values(row))
            return
        with self._pool.connection() as own:
            own.execute(_INSERT, _values(row))

    def upsert(self, row: OutputRow) -> None:
        """The copy's write: add the row or replace it, and set ``copied_at`` to now."""
        with self._pool.connection() as conn:
            conn.execute(_UPSERT, _values(row))

    def upsert_many(self, rows: Sequence[OutputRow]) -> None:
        """`upsert` for each row, in one transaction: one commit for a batch of the copy."""
        if not rows:
            return
        with self._pool.connection() as conn, conn.transaction():
            for row in rows:
                conn.execute(_UPSERT, _values(row))

    def copied_times(self) -> dict[str, datetime]:
        """Each row's ``copied_at``, by output id: what the copy compares the files with."""
        with self._pool.connection() as conn:
            found = conn.execute("SELECT output_id, copied_at FROM outputs").fetchall()
        return {str(item["output_id"]): item["copied_at"] for item in found}

    def update_meta(self, output_id: str, meta: OutputMeta) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE outputs SET meta = %s WHERE output_id = %s",
                (_meta_json(meta), output_id),
            )

    def set_backfill(self, output_id: str, backfill: dict[str, Any] | None) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE outputs SET backfill = %s WHERE output_id = %s",
                (_json(backfill), output_id),
            )

    def attach_backfill(self, output_id: str, record: dict[str, Any], manifest: list[Any]) -> None:
        """A backfill's attach in one statement: the record it has stays, the manifest
        is the re-render's, and the backfill is cleared."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE outputs SET record = COALESCE(record, %s), manifest = %s,"
                " backfill = NULL WHERE output_id = %s",
                (_json(record), _json(manifest), output_id),
            )

    def delete(self, output_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM outputs WHERE output_id = %s", (output_id,))

    def delete_slug(self, slug: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM outputs WHERE slug = %s", (slug,))

    def pending_backfills(self) -> list[tuple[str, dict[str, Any]]]:
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT output_id, backfill FROM outputs WHERE backfill IS NOT NULL"
                " ORDER BY created_at, output_id"
            ).fetchall()
        return [(str(item["output_id"]), item["backfill"]) for item in found]

    def oldest_with_plate_cover(self, slug: str) -> str | None:
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT output_id FROM outputs WHERE slug = %s AND has_plate_cover"
                " ORDER BY created_at, output_id LIMIT 1",
                (slug,),
            ).fetchone()
        return None if found is None else str(found["output_id"])

    def ids_copied_before(self, before: datetime) -> set[str]:
        """The rows the running copy did not refresh: written before ``before``."""
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT output_id FROM outputs WHERE copied_at < %s", (before,)
            ).fetchall()
        return {str(item["output_id"]) for item in found}

    def copied(self) -> bool:
        """Whether a copy run has finished. Once true it stays true for this object."""
        with self._lock:
            if self._copied:
                return True
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT 1 FROM output_copy_runs WHERE finished_at IS NOT NULL LIMIT 1"
            ).fetchone()
        if found is None:
            return False
        with self._lock:
            self._copied = True
        return True

    def start_run(self) -> int:
        with self._pool.connection() as conn:
            found = conn.execute(
                "INSERT INTO output_copy_runs DEFAULT VALUES RETURNING id"
            ).fetchone()
        assert found is not None
        return int(found["id"])

    def run_started_at(self, run_id: int) -> datetime:
        """When the run started, by the database's clock, as every ``copied_at`` is."""
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT started_at FROM output_copy_runs WHERE id = %s", (run_id,)
            ).fetchone()
        if found is None:
            raise LookupError(f"no copy run {run_id}")
        started: datetime = found["started_at"]
        return started

    def finish_run(
        self,
        run_id: int,
        *,
        copied: int,
        refreshed: int,
        dropped: int,
        not_copied: list[str],
        damaged: list[dict[str, Any]],
    ) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE output_copy_runs SET finished_at = clock_timestamp(), copied = %s,"
                " refreshed = %s, dropped = %s, not_copied = %s, damaged = %s WHERE id = %s",
                (copied, refreshed, dropped, Jsonb(not_copied), Jsonb(damaged), run_id),
            )

    def last_run(self) -> dict[str, Any] | None:
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT id, started_at, finished_at, copied, refreshed, dropped, not_copied,"
                " damaged FROM output_copy_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return None if found is None else dict(found)
