"""A batch print's state in Postgres (#2672), migration ``20261013T0449Z_print_batches``.

A batch is the library files a person picked, each with the choices its step made, the
step in view, and once queued what Queue all did with each file. It lives here rather
than in the browser so a reload mid-batch resumes where it was left.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.print_run import PrintRunRequest

BatchStatus = Literal["open", "queued"]

#: An item Queue all is still following, in SQL on ``print_batch_items`` aliased ``i``:
#: its batch is kept so the run can be followed rather than started twice (#2718 review).
_PENDING = "NOT i.removed AND i.outcome->>'status' = 'pending'"


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
    #: A refusal whose file was edited since: its choices are not the ones refused, and
    #: the next Queue all sends the new ones (#3798). Kept, not cleared, for ``attempt``.
    stale: bool = False


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

    async def get(self, batch_id: str, *, touch: bool = False) -> PrintBatch | None:
        """The batch; with ``touch``, a read that counts as using it, so a batch kept
        open on its review is not pruned as untouched (#2718 review)."""
        return await asyncio.to_thread(self._get, batch_id, touch)

    async def with_pending(self) -> list[PrintBatch]:
        """Every open batch with a file still being checked (``pending``), newest first:
        one closed then is kept so Queue all can follow that run, and this is how it is
        found again (#2718 review)."""
        return await asyncio.to_thread(self._with_pending)

    async def set_step(self, batch_id: str, step: int) -> PrintBatch | None:
        """The batch with its step moved, or ``None`` when it is gone or no longer open:
        a Queue all that queued it since the caller looked keeps its step (#3798)."""
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

    async def mark_queued(self, batch_id: str) -> bool:
        """Queued, only if every file kept has a started run as this write lands: a
        failed run that reopened the batch since the caller looked keeps it open
        (#2718 review)."""
        return await asyncio.to_thread(self._mark_queued, batch_id)

    async def prune(self, older_than: timedelta, *, pending_older_than: timedelta) -> Pruned:
        """Forgets every batch nobody has touched for ``older_than``: one closed without
        Cancel or Done (a tab closed) would otherwise stay for good (#2718 review). One
        with a file still ``pending`` is kept, so Queue all can follow that run, until
        ``pending_older_than`` (#3798): those are named, so the caller can say so."""
        return await asyncio.to_thread(self._prune, older_than, pending_older_than)

    def _prune(self, older_than: timedelta, pending_older_than: timedelta) -> Pruned:
        pending = (
            f"EXISTS (SELECT 1 FROM print_batch_items i WHERE i.batch_id = b.id AND {_PENDING})"
        )
        with self._pool.connection() as conn:
            deleted = conn.execute(
                "DELETE FROM print_batches b WHERE b.updated_at < now() - %s"
                f" AND (NOT {pending} OR b.updated_at < now() - %s)"
                f" RETURNING b.id, {pending} AS pending",
                (older_than, pending_older_than),
            ).fetchall()
        return Pruned(
            count=len(deleted),
            pending=sorted(row["id"] for row in deleted if row["pending"]),
        )

    async def delete(self, batch_id: str) -> Deleted:
        """``pending`` refuses it: a file still being checked is followed by the batch,
        and a new one would send it again (#3798). One statement, so it cannot race."""
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

    def _with_pending(self) -> list[PrintBatch]:
        # Two reads on one connection, whatever the count (#3798).
        with self._pool.connection() as conn:
            batches = conn.execute(
                "SELECT b.id, b.status, b.step, b.created_at FROM print_batches b"
                " WHERE b.status = 'open' AND EXISTS ("
                f"SELECT 1 FROM print_batch_items i WHERE i.batch_id = b.id AND {_PENDING})"
                " ORDER BY b.created_at DESC"
            ).fetchall()
            rows = conn.execute(
                f"SELECT batch_id, {_ITEM_COLUMNS} FROM print_batch_items"
                " WHERE batch_id = ANY(%s) ORDER BY batch_id, position",
                ([batch["id"] for batch in batches],),
            ).fetchall()
        items: dict[str, list[BatchItem]] = {}
        for row in rows:
            items.setdefault(row["batch_id"], []).append(_item(row))
        return [_batch(batch, items.get(batch["id"], [])) for batch in batches]

    def _get(self, batch_id: str, touch: bool = False) -> PrintBatch | None:
        with self._pool.connection() as conn:
            read = (
                "UPDATE print_batches SET updated_at = now() WHERE id = %s"
                " RETURNING id, status, step, created_at"
                if touch
                else "SELECT id, status, step, created_at FROM print_batches WHERE id = %s"
            )
            batch = conn.execute(read, (batch_id,)).fetchone()
            if batch is None:
                return None
            rows = conn.execute(
                f"SELECT {_ITEM_COLUMNS} FROM print_batch_items WHERE batch_id = %s"
                " ORDER BY position",
                (batch_id,),
            ).fetchall()
        return _batch(batch, [_item(row) for row in rows])

    def _set_step(self, batch_id: str, step: int) -> PrintBatch | None:
        with self._pool.connection() as conn:
            moved = conn.execute(
                "UPDATE print_batches SET step = %s, updated_at = now()"
                " WHERE id = %s AND status = 'open'",
                (step, batch_id),
            ).rowcount
        return self._get(batch_id) if moved else None

    def _choose(self, batch_id: str, position: int, choice: BatchChoice) -> PrintBatch | None:
        with self._pool.connection() as conn, conn.transaction():
            request = _request_json(choice.request)
            changed = conn.execute(
                "UPDATE print_batch_items SET request = %s, skipped = %s, removed = %s,"
                " summary = %s, problems = %s,"
                # A refusal of other choices than these says so (#3798).
                " outcome = CASE WHEN outcome->>'status' = 'refused'"
                " AND request IS DISTINCT FROM %s"
                " THEN outcome || '{\"stale\": true}'::jsonb ELSE outcome END"
                " WHERE batch_id = %s AND position = %s"
                # Not while Queue all is sending it, or once it started (#2718 review).
                " AND (outcome IS NULL OR outcome->>'status' NOT IN ('started', 'pending'))",
                (
                    request,
                    choice.skipped,
                    choice.removed,
                    _summary_json(choice.summary),
                    Jsonb(choice.problems),
                    request,
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

    def _mark_queued(self, batch_id: str) -> bool:
        with self._pool.connection() as conn:
            return (
                conn.execute(
                    "UPDATE print_batches SET status = 'queued', updated_at = now()"
                    " WHERE id = %s AND status = 'open' AND NOT EXISTS ("
                    "SELECT 1 FROM print_batch_items WHERE batch_id = %s"
                    " AND NOT (skipped OR removed)"
                    " AND (outcome IS NULL OR outcome->>'status' IS DISTINCT FROM 'started'))",
                    (batch_id, batch_id),
                ).rowcount
                == 1
            )

    def _delete(self, batch_id: str) -> Deleted:
        with self._pool.connection() as conn:
            found = conn.execute(
                "WITH gone AS (DELETE FROM print_batches b WHERE b.id = %s AND NOT EXISTS ("
                f"SELECT 1 FROM print_batch_items i WHERE i.batch_id = b.id AND {_PENDING})"
                " RETURNING b.id)"
                " SELECT EXISTS (SELECT 1 FROM gone) AS deleted,"
                " EXISTS (SELECT 1 FROM print_batches WHERE id = %s) AS kept",
                (batch_id, batch_id),
            ).fetchone()
        assert found is not None
        if found["deleted"]:
            return "deleted"
        return "pending" if found["kept"] else "missing"


Deleted = Literal["deleted", "pending", "missing"]


@dataclass(frozen=True)
class Pruned:
    count: int
    #: The batches pruned with a file still ``pending``, kept the longer TTL (#3798).
    pending: list[str]


_ITEM_COLUMNS = (
    "position, library_file_id, filename, sliced, skipped, removed, request, summary,"
    " problems, outcome"
)


def _batch(row: dict[str, Any], items: list[BatchItem]) -> PrintBatch:
    return PrintBatch(
        id=row["id"],
        status=row["status"],
        step=row["step"],
        created_at=row["created_at"],
        items=items,
    )


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
