"""A batch print's state in Postgres (#2672), migration ``20261013T0449Z_print_batches``.

A batch is the library files a person picked, each with the choices its step made, the
step in view, and once queued what Queue all did with each file. It lives here rather
than in the browser so a reload mid-batch resumes where it was left.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
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
    """What Queue all did with a file: the run it started, or why it started none.

    ``pending`` is an answer that is not a refusal: ScadBuddy was still checking the
    print, or Temporal was unreachable and the run may have started. Queue all sends it
    again with the same ``request_id``, which attaches to that run rather than starting
    a second one. Queue all also marks a file ``pending`` while it sends it, so its
    choices cannot change under the send (#2718 review).

    ``failed`` is a run that started and then failed (a slice or an upload, after the
    202): refused in all but name, so the file can be edited and sent again, as a new
    run (``attempt``), never re-attached to the failed one."""

    status: Literal["started", "pending", "refused", "unchosen", "failed"]
    run_id: str | None = None
    detail: str | None = None
    #: How many failed runs came before this one: part of its ``request_id``.
    attempt: int = 0


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
    #: The step to show next, moved with the choice in one write (#2718 review): a
    #: choice saved with its step left behind would read as lost. Omitted, it stays.
    step: int | None = Field(default=None, ge=0)


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
        """The batch after the choice, or ``None`` when there is no such file, or Queue
        all has it ``pending`` or ``started`` (#2718 review)."""
        return await asyncio.to_thread(self._choose, batch_id, position, choice)

    async def record_outcome(
        self,
        batch_id: str,
        position: int,
        outcome: BatchOutcome,
        *,
        if_unchanged: BatchItem | None = None,
        reopen: bool = False,
    ) -> bool:
        """Record what Queue all did with a file; with ``if_unchanged``, only while the
        file is still as that read of it (its choices and outcome), else ``False``
        (#2718 review). With ``reopen``, a write that lands also opens the batch again: a
        file whose run failed after Queue all can then be edited and sent again."""
        return await asyncio.to_thread(
            self._record_outcome, batch_id, position, outcome, if_unchanged, reopen
        )

    async def mark_queued(self, batch_id: str) -> None:
        await asyncio.to_thread(self._mark_queued, batch_id)

    async def prune(self, older_than: timedelta) -> int:
        """Forgets every batch nobody has touched for ``older_than``: one closed without
        Cancel or Done (a tab closed) would otherwise stay for good (#2718 review)."""
        return await asyncio.to_thread(self._prune, older_than)

    def _prune(self, older_than: timedelta) -> int:
        with self._pool.connection() as conn:
            deleted = conn.execute(
                "DELETE FROM print_batches WHERE updated_at < now() - %s", (older_than,)
            )
            return deleted.rowcount

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
            changed = conn.execute(
                "UPDATE print_batch_items SET request = %s, skipped = %s, removed = %s,"
                " summary = %s, problems = %s WHERE batch_id = %s AND position = %s"
                # Not while Queue all is sending it, or once it started (#2718 review).
                " AND (outcome IS NULL OR outcome->>'status' NOT IN ('started', 'pending'))",
                (
                    _request_json(choice.request),
                    choice.skipped,
                    choice.removed,
                    _summary_json(choice.summary),
                    Jsonb(choice.problems),
                    batch_id,
                    position,
                ),
            ).rowcount
            if not changed:
                return None
            conn.execute(
                "UPDATE print_batches SET step = COALESCE(%s, step), updated_at = now()"
                " WHERE id = %s",
                (choice.step, batch_id),
            )
        return self._get(batch_id)

    def _record_outcome(
        self,
        batch_id: str,
        position: int,
        outcome: BatchOutcome,
        if_unchanged: BatchItem | None,
        reopen: bool = False,
    ) -> bool:
        guard = ""
        params: list[Any] = [Jsonb(outcome.model_dump(mode="json")), batch_id, position]
        if if_unchanged is not None:
            guard = (
                " AND request IS NOT DISTINCT FROM %s AND skipped = %s AND removed = %s"
                " AND outcome IS NOT DISTINCT FROM %s"
            )
            was = if_unchanged.outcome
            params += [
                _request_json(if_unchanged.request),
                if_unchanged.skipped,
                if_unchanged.removed,
                None if was is None else Jsonb(was.model_dump(mode="json")),
            ]
        with self._pool.connection() as conn, conn.transaction():
            updated = conn.execute(
                "UPDATE print_batch_items SET outcome = %s"
                f" WHERE batch_id = %s AND position = %s{guard}",
                params,
            ).rowcount
            # The prune counts from the last Queue all too, not only the last choice.
            conn.execute(
                "UPDATE print_batches SET updated_at = now(),"
                " status = CASE WHEN %s THEN 'open' ELSE status END WHERE id = %s",
                (reopen and updated == 1, batch_id),
            )
        return updated == 1

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
