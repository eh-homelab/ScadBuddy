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
    " failure, sliced_blob_key, warnings, created_at, finished_at"
)


class NativeSlice(BaseModel):
    id: int
    workflow_id: str
    subject: str
    library_file_id: int
    plate_id: int
    preset_key: str
    input_sha256: str | None
    status: Literal["running", "completed", "failed"]
    failure: str | None
    sliced_blob_key: str | None
    warnings: list[str]
    created_at: datetime
    finished_at: datetime | None


def native_workflow_id(library_file_id: int, preset_key: str) -> str:
    """The execution that slices copy ``library_file_id`` with ``preset_key``: known
    before its bytes are read, so the dialog and the run name one (Ruling C2). Reuse
    across copies of identical bytes is `slice_prepare`'s (Ruling C1)."""
    digest = hashlib.sha256(preset_key.encode()).hexdigest()[:32]
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
        """The row of ``workflow_id``, inserted if there is none. A failed one is set
        back to running, its failure and finish cleared, for the execution started next."""

        def run() -> NativeSlice:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "INSERT INTO native_slices"
                    " (workflow_id, subject, library_file_id, plate_id, preset_key)"
                    " VALUES (%s, %s, %s, %s, %s)"
                    " ON CONFLICT (workflow_id) DO UPDATE SET"
                    " status = CASE WHEN native_slices.status = 'failed' THEN 'running'"
                    " ELSE native_slices.status END,"
                    " failure = CASE WHEN native_slices.status = 'failed' THEN NULL"
                    " ELSE native_slices.failure END,"
                    " finished_at = CASE WHEN native_slices.status = 'failed' THEN NULL"
                    " ELSE native_slices.finished_at END"
                    f" RETURNING {_COLUMNS}",
                    (workflow_id, subject, library_file_id, plate_id, preset_key),
                ).fetchone()
            assert row is not None
            return NativeSlice.model_validate(row)

        return await asyncio.to_thread(run)

    async def prepared(self, slice_id: int, input_sha256: str) -> None:
        await self._execute(
            "UPDATE native_slices SET input_sha256 = %s WHERE id = %s", (input_sha256, slice_id)
        )

    async def finish(
        self,
        slice_id: int,
        *,
        status: Literal["completed", "failed"],
        sliced_blob_key: str | None,
        failure: str | None,
        warnings: list[str],
    ) -> None:
        await self._execute(
            "UPDATE native_slices SET status = %s, sliced_blob_key = %s, failure = %s,"
            " warnings = %s, finished_at = now() WHERE id = %s",
            (status, sliced_blob_key, failure, Jsonb(warnings), slice_id),
        )

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
