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
    " backfill, has_plate_cover, copied_at, files_stamp"
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
    #: When a writer last wrote the row. The store sets it, so the value given is ignored.
    copied_at: datetime
    #: The copy's stamp of the record files it read; ``None`` for a row a save wrote.
    files_stamp: str | None = None


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
        files_stamp=data["files_stamp"],
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

#: A save's row replaces a row the copy wrote (it has a stamp) of the same folder: the
#: copy may have read the save's folder before the save wrote its row. A row of another
#: slug stays, so a second save of an id (`orders/quote.py` makes them with uuid5) under
#: another model still fails on the key.
_DROP_COPIED = "DELETE FROM outputs WHERE output_id = %s AND slug = %s AND files_stamp IS NOT NULL"

#: The copy's new row. A row a save wrote meanwhile wins.
_COPY_INSERT = (
    "INSERT INTO outputs (output_id, slug, meta, params, inputs, record, manifest, bom,"
    " arranged_from, backfill, has_plate_cover, created_at, files_stamp, copied_at)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    " ON CONFLICT (output_id) DO NOTHING"
)

#: The copy's refresh: only a row no writer touched since the mark. A row deleted
#: meanwhile stays deleted.
_COPY_REFRESH = (
    "UPDATE outputs SET slug = %s, meta = %s, params = %s, inputs = %s, record = %s,"
    " manifest = %s, bom = %s, arranged_from = %s, backfill = %s, has_plate_cover = %s,"
    " created_at = %s, files_stamp = %s, copied_at = %s"
    " WHERE output_id = %s AND copied_at < %s"
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
        """Add a save's row. It replaces a row the copy wrote of the same slug; any other
        row with that id is an error. With ``conn``, inside its transaction."""
        if conn is not None:
            conn.execute(_DROP_COPIED, (row.output_id, row.slug))
            conn.execute(_INSERT, _values(row))
            return
        with self._pool.connection() as own, own.transaction():
            own.execute(_DROP_COPIED, (row.output_id, row.slug))
            own.execute(_INSERT, _values(row))

    def drop_copied(self, output_id: str, slug: str) -> None:
        """Delete the row the copy wrote of the folder ``<slug>/<output_id>``; a row a
        save wrote (no stamp) stays. A failed save calls it after it removed its folder."""
        with self._pool.connection() as conn:
            conn.execute(_DROP_COPIED, (output_id, slug))

    def clock(self) -> datetime:
        """The database's clock now, as every ``copied_at`` is: the copy's mark."""
        with self._pool.connection() as conn:
            found = conn.execute("SELECT clock_timestamp() AS now").fetchone()
        assert found is not None
        now: datetime = found["now"]
        return now

    def write_copy(
        self, new: Sequence[OutputRow], changed: Sequence[OutputRow], mark: datetime
    ) -> tuple[set[str], set[str]]:
        """One batch of the copy, in one transaction, with ``copied_at`` set to ``mark``
        (taken before the batch's folders were read). A ``new`` row is added unless a
        save added it meanwhile; a ``changed`` row is written only when no writer touched
        it since ``mark``, and never when it was deleted. The ids added and refreshed."""
        added: set[str] = set()
        refreshed: set[str] = set()
        if not new and not changed:
            return added, refreshed
        with self._pool.connection() as conn, conn.transaction():
            for row in new:
                cursor = conn.execute(_COPY_INSERT, (*_values(row), row.files_stamp, mark))
                if cursor.rowcount:
                    added.add(row.output_id)
            for row in changed:
                cursor = conn.execute(
                    _COPY_REFRESH,
                    (*_values(row)[1:], row.files_stamp, mark, row.output_id, mark),
                )
                if cursor.rowcount:
                    refreshed.add(row.output_id)
        return added, refreshed

    def undo_copy(self, output_ids: Iterable[str], mark: datetime) -> set[str]:
        """Delete the rows the copy wrote at ``mark`` whose folder went after its read;
        a row a writer touched since stays. The ids deleted."""
        ids = list(output_ids)
        if not ids:
            return set()
        with self._pool.connection() as conn:
            found = conn.execute(
                "DELETE FROM outputs WHERE output_id = ANY(%s) AND copied_at = %s"
                " RETURNING output_id",
                (ids, mark),
            ).fetchall()
        return {str(item["output_id"]) for item in found}

    def drop_unseen(self, output_ids: Iterable[str], started: datetime) -> int:
        """The copy's drop, in one statement: the rows of folders the walk did not see,
        unless a writer touched them after the run started. How many went."""
        ids = list(output_ids)
        if not ids:
            return 0
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM outputs WHERE output_id = ANY(%s) AND copied_at < %s",
                (ids, started),
            )
            return cursor.rowcount

    def stamps(self) -> dict[str, str | None]:
        """Each row's ``files_stamp``, by output id: what the copy compares the files with."""
        with self._pool.connection() as conn:
            found = conn.execute("SELECT output_id, files_stamp FROM outputs").fetchall()
        return {str(item["output_id"]): item["files_stamp"] for item in found}

    def update_meta(self, output_id: str, meta: OutputMeta) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE outputs SET meta = %s, copied_at = clock_timestamp() WHERE output_id = %s",
                (_meta_json(meta), output_id),
            )

    def set_backfill(self, output_id: str, backfill: dict[str, Any] | None) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE outputs SET backfill = %s, copied_at = clock_timestamp()"
                " WHERE output_id = %s",
                (_json(backfill), output_id),
            )

    def attach_backfill(self, output_id: str, record: dict[str, Any], manifest: list[Any]) -> None:
        """A backfill's attach in one statement: the record it has stays, the manifest
        is the re-render's, and the backfill is cleared."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE outputs SET record = COALESCE(record, %s), manifest = %s,"
                " backfill = NULL, copied_at = clock_timestamp() WHERE output_id = %s",
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

    def end_incomplete_run(
        self,
        run_id: int,
        *,
        copied: int,
        refreshed: int,
        dropped: int,
        not_copied: list[str],
        damaged: list[dict[str, Any]],
    ) -> None:
        """The end of a run whose walk may have missed folders: its counts and
        ``incomplete``, and no ``finished_at``, so it does not make `copied` true."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE output_copy_runs SET incomplete = true, copied = %s, refreshed = %s,"
                " dropped = %s, not_copied = %s, damaged = %s WHERE id = %s",
                (copied, refreshed, dropped, Jsonb(not_copied), Jsonb(damaged), run_id),
            )

    def last_run(self) -> dict[str, Any] | None:
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT id, started_at, finished_at, incomplete, copied, refreshed, dropped,"
                " not_copied, damaged FROM output_copy_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return None if found is None else dict(found)
