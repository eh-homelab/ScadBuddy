"""A batch print's state in Postgres (#2672), migration ``20261013T0449Z_print_batches``.

A batch is the library files a person picked, each with the choices its step made, the
step in view, and once queued what Queue all did with each file. It lives here rather
than in the browser so a reload mid-batch resumes where it was left.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any, Literal

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.print_run import PrintRunRequest

BatchStatus = Literal["open", "queued"]


class BatchSummary(BaseModel):
    """What the review lists for a file, as its step showed it."""

    printer: str | None = None
    plate: str | None = None
    spools: list[str] = Field(default_factory=list)
    print_seconds: float | None = None
    filament_g: float | None = None
    #: The file's slots as ``<slot>:<material>:<colour>``: the next file takes this one's
    #: spools as defaults only when its own are the same.
    slots: list[str] = Field(default_factory=list)
    #: What its check warned of: never a refusal, so it does not flag the file (a sliced
    #: file's mismatches are only ever these, #2671).
    warnings: list[str] = Field(default_factory=list)


class BatchOutcome(BaseModel):
    """What Queue all did with a file: the run it started, or why it started none."""

    status: Literal["started", "refused", "unchosen"]
    run_id: str | None = None
    detail: str | None = None


class BatchItem(BaseModel):
    position: int
    library_file_id: int
    filename: str
    #: Sliced already: queued as it is (#2671).
    sliced: bool = False
    #: Skipped in its step: stays in the strip, and Queue all leaves it out.
    skipped: bool = False
    #: Removed in the review: Queue all leaves it out, and the strip no longer shows it.
    removed: bool = False
    request: PrintRunRequest | None = None
    summary: BatchSummary | None = None
    problems: list[str] = Field(default_factory=list)
    outcome: BatchOutcome | None = None


class PrintBatch(BaseModel):
    id: str
    status: BatchStatus
    #: The step in view: an item's position, or the item count for the review.
    step: int
    created_at: datetime
    items: list[BatchItem]


class NewBatchFile(BaseModel):
    library_file_id: int
    filename: str
    sliced: bool = False


class BatchChoice(BaseModel):
    """One step's choices, saved as the person leaves it."""

    request: PrintRunRequest | None = None
    skipped: bool = False
    removed: bool = False
    summary: BatchSummary | None = None
    problems: list[str] = Field(default_factory=list)


def _request_json(request: PrintRunRequest | None) -> Jsonb | None:
    # Unset fields stay unset: an omitted ``project_id`` is the remembered project, an
    # explicit null is none (``chosen_project``).
    return None if request is None else Jsonb(request.model_dump(mode="json", exclude_unset=True))


def _summary_json(summary: BatchSummary | None) -> Jsonb | None:
    return None if summary is None else Jsonb(summary.model_dump(mode="json"))


class PrintBatchStore:
    """``print_batches`` and ``print_batch_items`` on the process's pool."""

    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

    async def create(self, files: list[NewBatchFile]) -> PrintBatch:
        return await asyncio.to_thread(self._create, files)

    async def get(self, batch_id: str) -> PrintBatch | None:
        return await asyncio.to_thread(self._get, batch_id)

    async def set_step(self, batch_id: str, step: int) -> PrintBatch | None:
        return await asyncio.to_thread(self._set_step, batch_id, step)

    async def choose(self, batch_id: str, position: int, choice: BatchChoice) -> PrintBatch | None:
        return await asyncio.to_thread(self._choose, batch_id, position, choice)

    async def record_outcome(self, batch_id: str, position: int, outcome: BatchOutcome) -> None:
        await asyncio.to_thread(self._record_outcome, batch_id, position, outcome)

    async def mark_queued(self, batch_id: str) -> None:
        await asyncio.to_thread(self._mark_queued, batch_id)

    async def delete(self, batch_id: str) -> bool:
        return await asyncio.to_thread(self._delete, batch_id)

    def _create(self, files: list[NewBatchFile]) -> PrintBatch:
        batch_id = uuid.uuid4().hex
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("INSERT INTO print_batches (id) VALUES (%s)", (batch_id,))
            with conn.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO print_batch_items"
                    " (batch_id, position, library_file_id, filename, sliced)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    [
                        (batch_id, position, file.library_file_id, file.filename, file.sliced)
                        for position, file in enumerate(files)
                    ],
                )
        found = self._get(batch_id)
        assert found is not None
        return found

    def _get(self, batch_id: str) -> PrintBatch | None:
        with self._pool.connection() as conn:
            batch = conn.execute(
                "SELECT id, status, step, created_at FROM print_batches WHERE id = %s",
                (batch_id,),
            ).fetchone()
            if batch is None:
                return None
            rows = conn.execute(
                "SELECT position, library_file_id, filename, sliced, skipped, removed, request,"
                " summary, problems, outcome FROM print_batch_items WHERE batch_id = %s"
                " ORDER BY position",
                (batch_id,),
            ).fetchall()
        return PrintBatch(
            id=batch["id"],
            status=batch["status"],
            step=batch["step"],
            created_at=batch["created_at"],
            items=[_item(row) for row in rows],
        )

    def _set_step(self, batch_id: str, step: int) -> PrintBatch | None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE print_batches SET step = %s, updated_at = now() WHERE id = %s",
                (step, batch_id),
            )
        return self._get(batch_id)

    def _choose(self, batch_id: str, position: int, choice: BatchChoice) -> PrintBatch | None:
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(
                "UPDATE print_batch_items SET request = %s, skipped = %s, removed = %s,"
                " summary = %s, problems = %s WHERE batch_id = %s AND position = %s",
                (
                    _request_json(choice.request),
                    choice.skipped,
                    choice.removed,
                    _summary_json(choice.summary),
                    Jsonb(choice.problems),
                    batch_id,
                    position,
                ),
            )
            conn.execute("UPDATE print_batches SET updated_at = now() WHERE id = %s", (batch_id,))
        return self._get(batch_id)

    def _record_outcome(self, batch_id: str, position: int, outcome: BatchOutcome) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE print_batch_items SET outcome = %s WHERE batch_id = %s AND position = %s",
                (Jsonb(outcome.model_dump(mode="json")), batch_id, position),
            )

    def _mark_queued(self, batch_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE print_batches SET status = 'queued', updated_at = now() WHERE id = %s",
                (batch_id,),
            )

    def _delete(self, batch_id: str) -> bool:
        with self._pool.connection() as conn:
            deleted = conn.execute("DELETE FROM print_batches WHERE id = %s", (batch_id,))
            return deleted.rowcount > 0


def _item(row: dict[str, Any]) -> BatchItem:
    return BatchItem(
        position=row["position"],
        library_file_id=row["library_file_id"],
        filename=row["filename"],
        sliced=row["sliced"],
        skipped=row["skipped"],
        removed=row["removed"],
        request=None if row["request"] is None else PrintRunRequest.model_validate(row["request"]),
        summary=None if row["summary"] is None else BatchSummary.model_validate(row["summary"]),
        problems=list(row["problems"] or []),
        outcome=None if row["outcome"] is None else BatchOutcome.model_validate(row["outcome"]),
    )
