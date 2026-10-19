"""`native_slices`: one row per `SliceWorkflow` execution (plan
2026-10-16-native-slicer-rust-worker Task 11). The starter inserts it; the workflow's
activities set its input digest and its outcome. A completed row answers for any copy of
the same bytes sliced with the same presets (Ruling C1)."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta
from typing import Literal

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

#: The `blob_refs` holder kind of a slice's blobs; the holder id is ``str(row.id)``.
HOLDER = "native_slice"

_COLUMNS = (
    "id, workflow_id, subject, library_file_id, plate_id, preset_key, input_sha256, status,"
    " failure, sliced_blob_key, warnings, created_at, started_at, finished_at"
)


class NativeSlice(BaseModel):
    id: int
    workflow_id: str
    subject: str
    library_file_id: int
    plate_id: int
    preset_key: str
    input_sha256: str | None
    status: Literal["running", "completed", "failed", "cancelled"]
    failure: str | None
    sliced_blob_key: str | None
    warnings: list[str]
    created_at: datetime
    #: When the execution writing the row started it (`NativeSliceStore.running`).
    started_at: datetime
    finished_at: datetime | None


class SliceSubjectError(ValueError):
    """A start for a subject other than the one the copy's row was started for."""


def native_workflow_id(
    library_file_id: int, preset_key: str, *, subject: str, copy_sha256: str | None
) -> str:
    """The execution that slices copy ``library_file_id`` for ``subject`` with
    ``preset_key``: known before the slice reads the bytes, so the dialog and the run
    name one (Ruling C2). The copy's ``file_hash`` is in it as well as its id (Task 12
    review M6): Bambuddy's SQLite library hands the id of a deleted newest file out
    again (an ``INTEGER PRIMARY KEY`` without ``AUTOINCREMENT``), so the id alone may
    name another copy. ``None`` when the library states no hash. Reuse across copies of
    identical bytes is `slice_prepare`'s (Ruling C1)."""
    named = "\n".join((subject, copy_sha256 or "", preset_key))
    digest = hashlib.sha256(named.encode()).hexdigest()[:32]
    return f"slice-{library_file_id}-{digest}"


class NativeSliceStore:
    """Over an autocommit pool; every method runs its statements in a thread."""

    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

    async def start(
        self,
        *,
        workflow_id: str,
        subject: str,
        library_file_id: int,
        plate_id: int,
        preset_key: str,
    ) -> NativeSlice:
        """The row of ``workflow_id``, inserted if there is none. A failed or cancelled
        row reads running at once, its failure cleared and ``started_at`` now (Task 12
        review I1): the dialog reads it the moment this answers, before the new
        execution's `running`. A start that no execution follows (one that joined an
        execution as it closed) is the lost-row stop's, which compares that
        ``started_at``. A completed row's ``finished_at`` is refreshed, so the TTL runs
        from its last use (review M5); a running row is left as it is. A copy belongs to
        one subject: the row of another's is refused (`SliceSubjectError`)."""

        def run() -> NativeSlice:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "INSERT INTO native_slices"
                    " (workflow_id, subject, library_file_id, plate_id, preset_key)"
                    " VALUES (%s, %s, %s, %s, %s)"
                    # Only the same subject's row is touched: another's is refused below.
                    " ON CONFLICT (workflow_id) DO UPDATE SET"
                    " status = CASE WHEN native_slices.subject <> EXCLUDED.subject"
                    " THEN native_slices.status"
                    " WHEN native_slices.status IN ('failed', 'cancelled') THEN 'running'"
                    " ELSE native_slices.status END,"
                    " failure = CASE WHEN native_slices.subject <> EXCLUDED.subject"
                    " OR native_slices.status NOT IN ('failed', 'cancelled')"
                    " THEN native_slices.failure END,"
                    " started_at = CASE WHEN native_slices.subject <> EXCLUDED.subject"
                    " OR native_slices.status NOT IN ('failed', 'cancelled')"
                    " THEN native_slices.started_at ELSE clock_timestamp() END,"
                    " finished_at = CASE WHEN native_slices.subject <> EXCLUDED.subject"
                    " THEN native_slices.finished_at"
                    " WHEN native_slices.status = 'completed' THEN now()"
                    " WHEN native_slices.status IN ('failed', 'cancelled') THEN NULL"
                    " ELSE native_slices.finished_at END"
                    f" RETURNING {_COLUMNS}",
                    (workflow_id, subject, library_file_id, plate_id, preset_key),
                ).fetchone()
            assert row is not None
            return NativeSlice.model_validate(row)

        found = await asyncio.to_thread(run)
        if found.subject != subject:
            raise SliceSubjectError(
                f"slice {workflow_id} belongs to {found.subject}, not {subject}"
            )
        return found

    async def running(self, slice_id: int) -> datetime | None:
        """What each execution does first: the row running again, its last outcome
        cleared, its ``started_at`` now. None when there is no row (swept): the
        execution then takes no hold (re-review N2)."""

        def run() -> datetime | None:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "UPDATE native_slices SET status = 'running', failure = NULL,"
                    " finished_at = NULL, sliced_blob_key = NULL, warnings = '[]',"
                    " started_at = clock_timestamp() WHERE id = %s RETURNING started_at",
                    (slice_id,),
                ).fetchone()
            return None if row is None else row["started_at"]

        return await asyncio.to_thread(run)

    async def prepared(self, slice_id: int, input_sha256: str) -> None:
        await self._execute(
            "UPDATE native_slices SET input_sha256 = %s WHERE id = %s", (input_sha256, slice_id)
        )

    async def finish(
        self,
        slice_id: int,
        *,
        status: Literal["completed", "failed", "cancelled"],
        sliced_blob_key: str | None,
        failure: str | None,
        warnings: list[str],
    ) -> float | None:
        """A slice that did not complete drops its holds in the same transaction: its
        blobs serve nothing (`reusable` reads completed rows), and a later execution
        holds them again (review I1). The seconds since its start when this moved the row
        from running, else None (a retried record, a cancel recorded over a finished row,
        a row that is gone), so a slice is timed once (Task 14/15 review M2)."""

        def run() -> float | None:
            with self._pool.connection() as conn, conn.transaction():
                row = conn.execute(
                    "WITH before AS (SELECT id, status FROM native_slices WHERE id = %s"
                    " FOR UPDATE)"
                    " UPDATE native_slices AS s SET status = %s, sliced_blob_key = %s,"
                    " failure = %s, warnings = %s, finished_at = now() FROM before"
                    " WHERE s.id = before.id"
                    " RETURNING before.status AS was,"
                    " EXTRACT(EPOCH FROM s.finished_at - s.started_at)::float8 AS took",
                    (slice_id, status, sliced_blob_key, failure, Jsonb(warnings)),
                ).fetchone()
                if status != "completed":
                    conn.execute(
                        "DELETE FROM blob_refs WHERE holder_kind = %s AND holder_id = %s",
                        (HOLDER, str(slice_id)),
                    )
            if row is None or row["was"] != "running":
                return None
            return float(row["took"])

        return await asyncio.to_thread(run)

    async def lost(self, older_than: timedelta) -> list[NativeSlice]:
        """Rows whose execution started them more than ``older_than`` ago and that read
        running still: the sweep asks Temporal whether each has an execution."""

        def run() -> list[NativeSlice]:
            with self._pool.connection() as conn:
                rows = conn.execute(
                    f"SELECT {_COLUMNS} FROM native_slices WHERE status = 'running'"
                    " AND started_at <= now() - %s ORDER BY id",
                    (older_than,),
                ).fetchall()
            return [NativeSlice.model_validate(row) for row in rows]

        return await asyncio.to_thread(run)

    async def stop_lost(self, row: NativeSlice, failure: str) -> bool:
        """Fail ``row``, as `lost` listed it, and drop its holds: only if it still reads
        running from the same start, so an execution that started it since is left
        alone (re-review N1). Whether it was stopped."""

        def run() -> bool:
            with self._pool.connection() as conn, conn.transaction():
                stopped = conn.execute(
                    "UPDATE native_slices SET status = 'failed', failure = %s,"
                    " finished_at = now() WHERE id = %s AND status = 'running'"
                    " AND started_at = %s",
                    (failure, row.id, row.started_at),
                ).rowcount
                if stopped:
                    conn.execute(
                        "DELETE FROM blob_refs WHERE holder_kind = %s AND holder_id = %s",
                        (HOLDER, str(row.id)),
                    )
            return bool(stopped)

        return await asyncio.to_thread(run)

    async def get(self, slice_id: int, subject: str) -> NativeSlice | None:
        return await self._one(
            f"SELECT {_COLUMNS} FROM native_slices WHERE id = %s AND subject = %s",
            (slice_id, subject),
        )

    async def reusable(self, input_sha256: str, preset_key: str) -> NativeSlice | None:
        """The newest completed slice of these bytes and presets."""
        return await self._one(
            f"SELECT {_COLUMNS} FROM native_slices WHERE input_sha256 = %s AND preset_key = %s"
            " AND status = 'completed' ORDER BY finished_at DESC, id DESC LIMIT 1",
            (input_sha256, preset_key),
        )

    async def sweep(self, older_than: timedelta) -> list[int]:
        """Delete the rows finished more than ``older_than`` ago, and their holds in
        `blob_refs` in the same transaction (the content sweep then collects the blobs
        no other holder keeps). Their ids."""

        def run() -> list[int]:
            with self._pool.connection() as conn, conn.transaction():
                rows = conn.execute(
                    "DELETE FROM native_slices WHERE finished_at IS NOT NULL"
                    " AND finished_at <= now() - %s RETURNING id",
                    (older_than,),
                ).fetchall()
                ids = [row["id"] for row in rows]
                conn.execute(
                    "DELETE FROM blob_refs WHERE holder_kind = %s AND holder_id = ANY(%s)",
                    (HOLDER, [str(i) for i in ids]),
                )
            return ids

        return await asyncio.to_thread(run)

    async def _execute(self, query: str, params: tuple[object, ...]) -> None:
        def run() -> None:
            with self._pool.connection() as conn:
                conn.execute(query, params)

        await asyncio.to_thread(run)

    async def _one(self, query: str, params: tuple[object, ...]) -> NativeSlice | None:
        def run() -> NativeSlice | None:
            with self._pool.connection() as conn:
                row = conn.execute(query, params).fetchone()
            return None if row is None else NativeSlice.model_validate(row)

        return await asyncio.to_thread(run)
