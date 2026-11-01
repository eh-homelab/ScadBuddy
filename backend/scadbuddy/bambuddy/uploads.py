"""An output's uploads to Bambuddy's file library, in Postgres (#316, #455).

Every copy of an output's ``model.3mf`` ScadBuddy has put in Bambuddy's *file library*
(not an OpenSCAD library), and every sliced 3MF Bambuddy wrote beside one. They are
the only pointers ScadBuddy has to those files, so they live in the database rather
than in the output's ``meta.json``. The tables are migration
``20260928T0720Z_output_bambuddy_uploads``. Beside them, the printer and nozzle each
Bambuddy project last printed on (``20260928T0937Z_project_print_targets``, #317): what
the project's file is laid out for when Generate files it there.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Literal

from psycopg import AsyncConnection, Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from scadbuddy.print_library.ids import NATIVE_ID_BASE

#: Prefixes the key of :meth:`BambuddyUploadStore.copy_lock`'s advisory lock.
COPY_LOCK_PREFIX = "scadbuddy-library-copy:"

#: What a print or a Generate is filed into (#3651): a Bambuddy project (its folder), or
#: a library folder of its own.
DestinationKind = Literal["project", "folder"]


class SlicedCopy(BaseModel):
    """A sliced 3MF Bambuddy wrote beside one of this output's library copies (#316).

    Bambuddy puts a slice in its source's folder, so it belongs to that copy's entry:
    a slice made for project A is in A's folder, next to the file it was sliced from.
    """

    #: Bambuddy library file id of the sliced 3MF.
    id: int
    #: What it was sliced with: the presets, plate and plate type
    #: (``SliceRequest.preset_key``). A row from before #312 may hold a pipeline's id.
    #: ``None`` when the route did not say.
    preset_key: str | None = None
    #: Bambuddy's SHA-256 of the sliced file, read once (#306). An archive of a print of
    #: it has the same ``content_hash``: the link once the queue item is gone.
    file_hash: str | None = None
    #: The native slicer's blob it was uploaded from (plan 2026-10-16 Task 13):
    #: provenance only. Nothing reads it, and the blob goes with its `native_slices` row.
    sliced_blob_key: str | None = None


class LibraryCopy(BaseModel):
    """One copy of the output's 3MF in Bambuddy's file library (#316).

    Keyed by (``folder_id``, ``target_key``). The folder is what files it under a
    project, and the target is what it was laid out for, so a copy is reusable only
    where both still hold. A copy in a project's folder that was printed from is the
    user's record of what that project printed and is never moved or deleted by
    ScadBuddy. One nothing printed from (Generate's guess at the printer) is replaced
    by the copy a print of the same output puts beside it (#2476); a copy in the inbox
    (Settings' ``library_folder_id``) is always replaced.
    """

    #: Library file id of the unsliced 3MF: Bambuddy's, or on a native run ScadBuddy's
    #: own (`is_native_id`, #3818 tier A Task 4), never read in the other library.
    id: int
    #: ``None`` is the library root.
    folder_id: int | None
    #: :attr:`~scadbuddy.bambuddy.send.Target.key` — the plate and nozzle it was laid
    #: out for.
    target_key: str
    sliced: list[SlicedCopy] = Field(default_factory=list)
    #: :func:`~scadbuddy.render.provenance.content_key` of what was uploaded, for a copy
    #: in a project's folder (#2476); ``None`` elsewhere and before it was recorded.
    content_key: str | None = None


class PreviewSlice(BaseModel):
    """One of the print dialog's background slices (#2169), table
    ``print_preview_slices``: the copy it sliced and with what, and the plate, and what
    Bambuddy said of the job, since its job ids restart from 1 with it."""

    #: The row's own id; ``None`` before it is recorded.
    id: int | None = None
    job_id: int
    #: The run subject it was sliced for: an output id, or ``library:<file id>``.
    subject: str
    library_file_id: int
    preset_key: str
    plate_id: int
    #: The job's ``created_at`` as Bambuddy reported it when it started.
    job_created: str | None = None
    #: The sliced file, once the job was seen completed.
    sliced_file_id: int | None = None
    sliced_name: str | None = None
    #: A run queued it: the sliced file is the print's.
    printed: bool = False
    #: Its job no longer says what it did: kept only for the sweep.
    retired: bool = False


class LostStart(BaseModel):
    """A print run's slice start that got no answer (#2715), table
    ``print_slice_starts``: Bambuddy may still have started its job."""

    id: int
    library_file_id: int
    created_at: datetime


class ProjectTarget(BaseModel):
    """The printer and nozzle a Bambuddy project last printed on (#317)."""

    printer_id: int
    nozzle_diameter: str | None = None
    #: Its ``NozzlePlan`` (#2166), as JSON: this module is below the plan's.
    nozzle_plan: dict[str, Any] | None = None


class BambuddyUploadStore:
    """``output_bambuddy_uploads`` and ``output_bambuddy_slices``, on the process's pool.

    The pool is the projection's (`JobProjection.pool`), opened and migrated at
    startup; this store opens nothing of its own.

    Every public method is a coroutine that runs its query in a worker thread, as the
    render queue does with its store: psycopg's calls here are blocking, and the
    progress poll alone would otherwise hold the event loop for a round trip each time.
    """

    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

    @asynccontextmanager
    async def copy_lock(self, key: str) -> AsyncIterator[None]:
        """Held, across every replica on this database, while one caller finds, uploads
        and records a library copy under ``key`` (#317).

        A session-level advisory lock on a connection of its own, closed on the way
        out, rather than a transaction-level one on the pool's. The section spans a
        Bambuddy upload (up to its upload timeout), and the pool is the render queue's:
        a pooled connection held that long takes a slot from it, and the store's own
        queries inside the section need further slots, so enough folders locked at
        once would exhaust the pool against itself. Closing the connection releases
        the lock however the section ends, cancellation and a lost connection included.
        """
        pool = self._pool
        conninfo = pool.conninfo if isinstance(pool.conninfo, str) else pool.conninfo()
        conn = await AsyncConnection.connect(conninfo, autocommit=True)
        try:
            await conn.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))", (f"{COPY_LOCK_PREFIX}{key}",)
            )
            yield
        finally:
            await conn.close()

    async def for_output(self, output_id: str, *, native: bool = False) -> list[LibraryCopy]:
        """The output's copies in upload order, each with its slices: Bambuddy's, or with
        ``native`` ScadBuddy's own library's (#3818 tier A Task 4). One id space at a
        time, by `is_native_id` (`print_library/ids.py`): a Bambuddy path never reads,
        supersedes or deletes a native copy, nor a native run a Bambuddy one."""
        return (await self.for_outputs([output_id], native=native))[output_id]

    async def for_outputs(
        self, output_ids: Iterable[str], *, native: bool = False
    ) -> dict[str, list[LibraryCopy]]:
        """:meth:`for_output` for several outputs in two queries, not two per output."""
        return await asyncio.to_thread(self._for_outputs, list(dict.fromkeys(output_ids)), native)

    async def record(self, output_id: str, copy: LibraryCopy) -> None:
        """Record an upload, replacing the folder and target of one with the same id."""
        await asyncio.to_thread(self._record, output_id, copy)

    async def same_content(
        self, folder_id: int | None, target_key: str, content_key: str
    ) -> list[tuple[str, LibraryCopy]]:
        """Every recorded copy in ``folder_id``, laid out for ``target_key``, that holds
        ``content_key`` (#2476), oldest first, with the output it is recorded under."""
        return await asyncio.to_thread(self._same_content, folder_id, target_key, content_key)

    async def shared(self, library_file_ids: Iterable[int], output_id: str) -> set[int]:
        """Those of ``library_file_ids`` also recorded under an output other than
        ``output_id``: a copy two identical outputs share (#2476)."""
        return await asyncio.to_thread(self._shared, list(library_file_ids), output_id)

    async def recorded(self, library_file_ids: Iterable[int]) -> set[int]:
        """Those of ``library_file_ids`` that any output records as a copy (#1145)."""
        return await asyncio.to_thread(self._recorded, list(library_file_ids))

    async def recorded_files(self, library_file_ids: Iterable[int]) -> set[int]:
        """Those of ``library_file_ids`` that any output records, as a copy or as a sliced
        file: what an earlier attempt's upload, left unrecorded, is not (Task 13 review I1)."""
        return await asyncio.to_thread(self._recorded_files, list(library_file_ids))

    async def outputs_for_files(self, library_file_ids: Iterable[int]) -> dict[int, str]:
        """The output each of ``library_file_ids`` is a copy of, for those any output
        records (#1864): Arrange reads such a file's objects through that output. A file
        two outputs record is the later upload's."""
        return await asyncio.to_thread(self._outputs_for_files, list(library_file_ids))

    async def relocate(self, library_file_ids: Iterable[int], folder_id: int | None) -> None:
        """Record that a person moved these files into ``folder_id`` (#2670). Bambuddy
        keeps a moved file's id, so its slices and everything else keyed by the id stay
        right; only the folder its copy row names would be stale, and that folder is what
        tells an inbox copy (which ScadBuddy replaces) from a project's (which it never
        touches)."""
        await asyncio.to_thread(self._relocate, list(library_file_ids), folder_id)

    async def forget_files(self, library_file_ids: Iterable[int]) -> None:
        """Stop recording these files as any output's copy, their slices with them
        (#2670): a person moved them into the inbox, and a recorded copy there is one
        ScadBuddy replaces and deletes. Unrecorded, each stays an ordinary library file
        that ScadBuddy never deletes on its own; the next send uploads a copy of its own."""
        await asyncio.to_thread(self._forget_files, list(library_file_ids))

    async def recorded_as(self, library_file_id: int) -> list[tuple[str, str]]:
        """Each ``(output_id, target_key)`` that records ``library_file_id`` as a copy:
        what a byte-for-byte copy of it is a copy of too (#2670)."""
        return await asyncio.to_thread(self._recorded_as, library_file_id)

    async def forget(self, output_id: str, library_file_id: int) -> None:
        """Drop one copy, and its slices, once the file has actually gone.

        Call this *after* the delete has come back — committed or 404 — never before
        it. Clearing first looks safer and is not: a delete that fails for any other
        reason (a 500, a timeout) leaves the file in Bambuddy with nothing pointing at
        it, so nothing would ever delete it. A copy whose delete failed stays recorded
        and is tried again the next time it is superseded.
        """
        await asyncio.to_thread(self._forget, output_id, library_file_id)

    async def record_sliced(self, output_id: str, library_file_id: int, sliced: SlicedCopy) -> None:
        """Record a slice against the copy it was sliced from.

        A no-op when the slice is already recorded, which is what lets the progress
        poll call this on every read, or when the copy is no longer recorded
        (superseded and deleted since): a slice has nowhere to belong then.
        """
        await asyncio.to_thread(self._record_sliced, output_id, library_file_id, sliced)

    async def slices_of(
        self, output_id: str, library_file_id: int, preset_key: str
    ) -> list[SlicedCopy]:
        """The sliced files recorded against copy ``library_file_id`` with these presets
        (:meth:`record_sliced`), newest first, each with the hash it was recorded with:
        what a print may queue rather than slice the same again (#2671)."""
        return await asyncio.to_thread(self._slices_of, output_id, library_file_id, preset_key)

    async def forget_slice(self, output_id: str, sliced_id: int) -> None:
        """Forget a recorded slice whose file Bambuddy no longer has, or whose id now
        holds another file (#2955): neither is ever reused, so its row only costs a
        later reprint a read."""
        await asyncio.to_thread(self._forget_slice, output_id, sliced_id)

    async def forget_gone_slice(self, sliced_id: int) -> None:
        """Forget every recorded slice in library file ``sliced_id``, which Bambuddy no
        longer has (#3495 review): no subject can reuse a file that is gone, so the
        next reprint is not left to find that out again."""
        await asyncio.to_thread(self._forget_gone_slice, sliced_id)

    async def record_preview(self, preview: PreviewSlice) -> None:
        """Record one of the print dialog's background slices (#2169)."""
        await asyncio.to_thread(self._record_preview, preview)

    async def preview(self, job_id: int, subject: str) -> PreviewSlice | None:
        """The newest background slice the dialog started for ``subject`` as Bambuddy
        job ``job_id`` (#2169). Whether the job is still that slice is the caller's to
        check: Bambuddy numbers its jobs from 1 again when it restarts."""
        return await asyncio.to_thread(self._preview, job_id, subject)

    async def previews_for(self, library_file_id: int, preset_key: str) -> list[PreviewSlice]:
        """The background slices of this copy with these presets, newest first: what a
        run may queue instead of slicing again (#2169)."""
        return await asyncio.to_thread(self._previews_for, library_file_id, preset_key)

    async def preview_sliced(self, row_id: int, sliced_file_id: int, name: str | None) -> None:
        """What a background slice sliced to, once seen completed."""
        await asyncio.to_thread(self._preview_sliced, row_id, sliced_file_id, name)

    async def retire_preview(self, row_id: int) -> None:
        """A background slice whose job no longer says what it did: never reused or
        read again, and left to :meth:`previews_to_sweep`."""
        await asyncio.to_thread(self._retire_preview, row_id)

    async def claim_preview(self, row_id: int) -> bool:
        """Mark a background slice as queued by a run, so the sweep leaves its sliced
        file alone; ``False`` when the sweep took it first."""
        return await asyncio.to_thread(self._claim_preview, row_id)

    async def previews_to_sweep(self, older_than: timedelta) -> list[PreviewSlice]:
        """The retired background slices, and every one older than ``older_than``."""
        return await asyncio.to_thread(self._previews_to_sweep, older_than)

    async def take_preview(self, row_id: int) -> bool:
        """Delete an unprinted background slice's row: ``True`` when this call did, and
        its sliced file is then the caller's to remove."""
        return await asyncio.to_thread(self._take_preview, row_id)

    async def forget_preview(self, row_id: int) -> None:
        """Delete a background slice's row, printed or not."""
        await asyncio.to_thread(self._forget_preview, row_id)

    _PREVIEW_COLUMNS = (
        "id, job_id, subject, library_file_id, preset_key, plate_id, job_created,"
        " sliced_file_id, sliced_name, printed, retired"
    )

    def _record_preview(self, preview: PreviewSlice) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO print_preview_slices (job_id, subject, library_file_id,"
                " preset_key, plate_id, job_created) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    preview.job_id,
                    preview.subject,
                    preview.library_file_id,
                    preview.preset_key,
                    preview.plate_id,
                    preview.job_created,
                ),
            )

    def _preview(self, job_id: int, subject: str) -> PreviewSlice | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {self._PREVIEW_COLUMNS} FROM print_preview_slices"
                " WHERE job_id = %s AND subject = %s AND NOT retired"
                " ORDER BY created_at DESC, id DESC LIMIT 1",
                (job_id, subject),
            ).fetchone()
        return PreviewSlice.model_validate(row) if row else None

    def _previews_for(self, library_file_id: int, preset_key: str) -> list[PreviewSlice]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {self._PREVIEW_COLUMNS} FROM print_preview_slices"
                " WHERE library_file_id = %s AND preset_key = %s AND NOT retired"
                " ORDER BY created_at DESC, id DESC",
                (library_file_id, preset_key),
            ).fetchall()
        return [PreviewSlice.model_validate(row) for row in rows]

    def _preview_sliced(self, row_id: int, sliced_file_id: int, name: str | None) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE print_preview_slices SET sliced_file_id = %s, sliced_name = %s"
                " WHERE id = %s",
                (sliced_file_id, name, row_id),
            )

    def _retire_preview(self, row_id: int) -> None:
        with self._pool.connection() as conn:
            conn.execute("UPDATE print_preview_slices SET retired = true WHERE id = %s", (row_id,))

    def _claim_preview(self, row_id: int) -> bool:
        with self._pool.connection() as conn:
            row = conn.execute(
                "UPDATE print_preview_slices SET printed = true"
                " WHERE id = %s AND NOT retired RETURNING id",
                (row_id,),
            ).fetchone()
        return row is not None

    def _previews_to_sweep(self, older_than: timedelta) -> list[PreviewSlice]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {self._PREVIEW_COLUMNS} FROM print_preview_slices"
                " WHERE retired OR created_at < now() - %s ORDER BY id",
                (older_than,),
            ).fetchall()
        return [PreviewSlice.model_validate(row) for row in rows]

    def _take_preview(self, row_id: int) -> bool:
        with self._pool.connection() as conn:
            row = conn.execute(
                "DELETE FROM print_preview_slices WHERE id = %s AND NOT printed RETURNING id",
                (row_id,),
            ).fetchone()
        return row is not None

    def _forget_preview(self, row_id: int) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM print_preview_slices WHERE id = %s", (row_id,))

    async def record_slice_start(self, library_file_id: int, job_id: int | None) -> None:
        """A print run started slice job ``job_id`` of the copy ``library_file_id``, or,
        with ``None``, tried to and got no answer (#2715)."""
        await asyncio.to_thread(self._record_slice_start, library_file_id, job_id)

    async def lost_starts(self) -> list[LostStart]:
        """The lost slice starts the sweep has not finished with, oldest first."""
        return await asyncio.to_thread(self._lost_starts)

    async def finish_lost_start(self, row_id: int) -> None:
        await asyncio.to_thread(self._finish_lost_start, row_id)

    async def slice_jobs_after(self, after: datetime, within: timedelta) -> list[int]:
        """The slice jobs recorded in ``(after, after + within]``, by a run or by the
        print dialog's background slices: Bambuddy's job ids next to a lost start's."""
        return await asyncio.to_thread(self._slice_jobs_after, after, within)

    async def prune_slice_starts(self, before: datetime) -> int:
        """Forget the slice starts recorded before ``before``, answered or lost (#2911):
        the table is read only beside a lost start, and only for a while after it."""
        return await asyncio.to_thread(self._prune_slice_starts, before)

    async def slice_jobs_recorded(self, job_ids: Iterable[int], since: datetime) -> set[int]:
        """Of ``job_ids``, the ones a run or a background slice recorded since ``since``:
        their files are someone's. Ids restart with Bambuddy, hence ``since``."""
        return await asyncio.to_thread(self._slice_jobs_recorded, list(job_ids), since)

    def _record_slice_start(self, library_file_id: int, job_id: int | None) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO print_slice_starts (library_file_id, job_id) VALUES (%s, %s)",
                (library_file_id, job_id),
            )

    def _prune_slice_starts(self, before: datetime) -> int:
        with self._pool.connection() as conn:
            return conn.execute(
                "DELETE FROM print_slice_starts WHERE created_at < %s", (before,)
            ).rowcount

    def _lost_starts(self) -> list[LostStart]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, library_file_id, created_at FROM print_slice_starts"
                " WHERE job_id IS NULL AND NOT swept ORDER BY created_at, id"
            ).fetchall()
        return [LostStart.model_validate(row) for row in rows]

    def _finish_lost_start(self, row_id: int) -> None:
        with self._pool.connection() as conn:
            conn.execute("UPDATE print_slice_starts SET swept = true WHERE id = %s", (row_id,))

    def _slice_jobs_after(self, after: datetime, within: timedelta) -> list[int]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT job_id FROM print_slice_starts WHERE job_id IS NOT NULL"
                " AND created_at > %s AND created_at <= %s"
                " UNION SELECT job_id FROM print_preview_slices"
                " WHERE created_at > %s AND created_at <= %s ORDER BY job_id",
                (after, after + within, after, after + within),
            ).fetchall()
        return [int(row["job_id"]) for row in rows]

    def _slice_jobs_recorded(self, job_ids: list[int], since: datetime) -> set[int]:
        if not job_ids:
            return set()
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT job_id FROM print_slice_starts WHERE job_id = ANY(%s) AND created_at >= %s"
                " UNION SELECT job_id FROM print_preview_slices"
                " WHERE job_id = ANY(%s) AND created_at >= %s",
                (job_ids, since, job_ids, since),
            ).fetchall()
        return {int(row["job_id"]) for row in rows}

    async def record_slice_hash(self, output_id: str, sliced_id: int, file_hash: str) -> None:
        """Keep the hash Bambuddy reports for one of the output's sliced files (#306)."""
        await asyncio.to_thread(self._record_slice_hash, output_id, sliced_id, file_hash)

    async def sent_between(self, output_id: str) -> tuple[datetime, datetime] | None:
        """When the output's first recorded copy was uploaded and its last slice was
        recorded, or ``None`` with no slice (#306).

        A print of one of its slices started no earlier than the first and, unless it
        waited in Bambuddy's queue, not long after the last. A copy is uploaded before
        it is sliced, and a slice is recorded at the send (or, on a pipeline run, by
        the first progress read after it), so neither moves the window past a print.
        """
        return await asyncio.to_thread(self._sent_between, output_id)

    async def delete_outputs(self, output_ids: Iterable[str]) -> None:
        """Forget every copy and slice of deleted outputs. Bambuddy is not touched."""
        await asyncio.to_thread(self._delete_outputs, list(output_ids))

    async def touch(self, output_id: str, library_file_id: int) -> None:
        """A recorded copy was laid out for a run again (#4337): its ``used_at`` is now."""
        await asyncio.to_thread(self._touch, output_id, library_file_id)

    async def native_unused(self, before: datetime) -> list[tuple[str, LibraryCopy]]:
        """The native copies (`is_native_id`) last used before ``before`` that are not
        their subject's newest in their folder (#4337), each with the subject it is
        recorded under and its slices, oldest first."""
        return await asyncio.to_thread(self._native_unused, before)

    async def still_unused(self, output_id: str, library_file_id: int, before: datetime) -> bool:
        """Whether the copy is still recorded, still last used before ``before`` and still
        not its subject's newest in its folder: :meth:`native_unused` again for one copy,
        under its copy lock, since a run may have reused it since the list."""
        return await asyncio.to_thread(self._still_unused, output_id, library_file_id, before)

    async def native_stale_slices(self, before: datetime) -> list[tuple[str, int | None, int]]:
        """``(subject, copy's folder, sliced id)`` of each slice of a native copy made
        before ``before`` that is not that copy's newest slice (#4337), oldest first."""
        return await asyncio.to_thread(self._native_stale_slices, before)

    #: A native copy's rank in its subject's folder, newest use first (#4337).
    _NATIVE_RANKED = (
        "SELECT output_id, library_file_id, folder_id, target_key, used_at,"
        " row_number() OVER (PARTITION BY output_id, folder_id"
        " ORDER BY used_at DESC, library_file_id DESC) AS rank"
        " FROM output_bambuddy_uploads WHERE library_file_id >= %(base)s"
    )

    def _touch(self, output_id: str, library_file_id: int) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE output_bambuddy_uploads SET used_at = now()"
                " WHERE output_id = %s AND library_file_id = %s",
                (output_id, library_file_id),
            )

    def _native_unused(self, before: datetime) -> list[tuple[str, LibraryCopy]]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT output_id, library_file_id FROM ({self._NATIVE_RANKED}) ranked"
                " WHERE rank > 1 AND used_at < %(before)s ORDER BY used_at, library_file_id",
                {"base": NATIVE_ID_BASE, "before": before},
            ).fetchall()
        if not rows:
            return []
        recorded = self._for_outputs(list(dict.fromkeys(r["output_id"] for r in rows)), True)
        found: list[tuple[str, LibraryCopy]] = []
        for row in rows:
            copy = next(
                (c for c in recorded[row["output_id"]] if c.id == row["library_file_id"]), None
            )
            if copy is not None:
                found.append((row["output_id"], copy))
        return found

    def _still_unused(self, output_id: str, library_file_id: int, before: datetime) -> bool:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT 1 FROM ({self._NATIVE_RANKED} AND output_id = %(output)s) ranked"
                " WHERE library_file_id = %(id)s AND rank > 1 AND used_at < %(before)s",
                {
                    "base": NATIVE_ID_BASE,
                    "output": output_id,
                    "id": library_file_id,
                    "before": before,
                },
            ).fetchone()
        return row is not None

    def _native_stale_slices(self, before: datetime) -> list[tuple[str, int | None, int]]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT ranked.output_id, u.folder_id, ranked.sliced_library_file_id FROM ("
                " SELECT *, row_number() OVER (PARTITION BY output_id, source_library_file_id"
                " ORDER BY created_at DESC, sliced_library_file_id DESC) AS rank"
                " FROM output_bambuddy_slices WHERE source_library_file_id >= %(base)s) ranked"
                " JOIN output_bambuddy_uploads u ON u.output_id = ranked.output_id"
                " AND u.library_file_id = ranked.source_library_file_id"
                " WHERE ranked.rank > 1 AND ranked.created_at < %(before)s"
                " ORDER BY ranked.created_at, ranked.sliced_library_file_id",
                {"base": NATIVE_ID_BASE, "before": before},
            ).fetchall()
        return [(r["output_id"], r["folder_id"], r["sliced_library_file_id"]) for r in rows]

    async def project_target(self, project_id: int) -> ProjectTarget | None:
        """What ``project_id`` last printed on, or ``None`` if nothing has been printed
        into it from here (#317). Generate lays the project's file out for it."""
        return await asyncio.to_thread(self._project_target, project_id)

    async def remember_project_target(self, project_id: int, target: ProjectTarget) -> None:
        """Record the printer and nozzle a print into ``project_id`` used (#317)."""
        await asyncio.to_thread(self._remember_project_target, project_id, target)

    async def project_targets(self) -> dict[int, ProjectTarget]:
        """Every project's remembered printer and nozzle, for Settings (#599)."""
        return await asyncio.to_thread(self._project_targets)

    async def forget_project_target(self, project_id: int) -> None:
        """Forget what ``project_id`` last printed on; the next Generate lays it out
        for the default again. Forgetting what is not remembered is not an error."""
        await asyncio.to_thread(self._forget_project_targets, project_id)

    async def forget_all_project_targets(self) -> None:
        """Forget every project's remembered printer and nozzle (#599)."""
        await asyncio.to_thread(self._forget_project_targets, None)

    async def folderless(self, name: str, parent_id: int | None) -> list[int]:
        """The projects created here as ``name`` under ``parent_id`` whose folder is not
        linked yet (#2263), newest first."""
        return await asyncio.to_thread(self._folderless, name, parent_id)

    async def remember_folderless(self, project_id: int, name: str, parent_id: int | None) -> None:
        """Record a project Bambuddy has just created, before its folder is made."""
        await asyncio.to_thread(self._remember_folderless, project_id, name, parent_id)

    async def forget_folderless(self, project_id: int) -> None:
        """The project has its folder; forgetting one not recorded is not an error."""
        await asyncio.to_thread(self._forget_folderless, project_id)

    async def touch_destination(self, kind: DestinationKind, target_id: int) -> None:
        """Note that a project or library folder was just printed into or filed into
        (#3651): the destination picker's "last touched" order."""
        await asyncio.to_thread(self._touch_destination, kind, target_id)

    async def destination_touches(self) -> dict[tuple[DestinationKind, int], datetime]:
        """When each project and folder was last touched from here (#3651)."""
        return await asyncio.to_thread(self._destination_touches)

    # The blocking bodies, run in a worker thread by the coroutines above.

    def _project_targets(self) -> dict[int, ProjectTarget]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT project_id, printer_id, nozzle_diameter, nozzle_plan"
                " FROM project_print_targets ORDER BY project_id"
            ).fetchall()
        return {row["project_id"]: ProjectTarget.model_validate(row) for row in rows}

    def _forget_project_targets(self, project_id: int | None) -> None:
        with self._pool.connection() as conn:
            if project_id is None:
                conn.execute("DELETE FROM project_print_targets")
            else:
                conn.execute(
                    "DELETE FROM project_print_targets WHERE project_id = %s", (project_id,)
                )

    def _project_target(self, project_id: int) -> ProjectTarget | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT printer_id, nozzle_diameter, nozzle_plan FROM project_print_targets"
                " WHERE project_id = %s",
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        return ProjectTarget.model_validate(row)

    def _remember_project_target(self, project_id: int, target: ProjectTarget) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO project_print_targets"
                " (project_id, printer_id, nozzle_diameter, nozzle_plan)"
                " VALUES (%s, %s, %s, %s) ON CONFLICT (project_id) DO UPDATE"
                " SET printer_id = EXCLUDED.printer_id,"
                " nozzle_diameter = EXCLUDED.nozzle_diameter,"
                " nozzle_plan = EXCLUDED.nozzle_plan, updated_at = now()",
                (
                    project_id,
                    target.printer_id,
                    target.nozzle_diameter,
                    Jsonb(target.nozzle_plan) if target.nozzle_plan is not None else None,
                ),
            )

    def _folderless(self, name: str, parent_id: int | None) -> list[int]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT project_id FROM folderless_projects"
                " WHERE name = %s AND parent_id IS NOT DISTINCT FROM %s"
                " ORDER BY created_at DESC, project_id DESC",
                (name, parent_id),
            ).fetchall()
        return [row["project_id"] for row in rows]

    def _remember_folderless(self, project_id: int, name: str, parent_id: int | None) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO folderless_projects (project_id, name, parent_id)"
                " VALUES (%s, %s, %s) ON CONFLICT (project_id) DO UPDATE"
                " SET name = EXCLUDED.name, parent_id = EXCLUDED.parent_id",
                (project_id, name, parent_id),
            )

    def _forget_folderless(self, project_id: int) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM folderless_projects WHERE project_id = %s", (project_id,))

    def _touch_destination(self, kind: DestinationKind, target_id: int) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO destination_touches (kind, target_id) VALUES (%s, %s)"
                " ON CONFLICT (kind, target_id) DO UPDATE SET touched_at = now()",
                (kind, target_id),
            )

    def _destination_touches(self) -> dict[tuple[DestinationKind, int], datetime]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT kind, target_id, touched_at FROM destination_touches"
            ).fetchall()
        return {(row["kind"], int(row["target_id"])): row["touched_at"] for row in rows}

    def _for_outputs(self, ids: list[str], native: bool) -> dict[str, list[LibraryCopy]]:
        found: dict[str, list[LibraryCopy]] = {output_id: [] for output_id in ids}
        space = "library_file_id >= %s" if native else "library_file_id < %s"
        with self._pool.connection() as conn:
            copies = conn.execute(
                "SELECT output_id, library_file_id, folder_id, target_key, content_key"
                f" FROM output_bambuddy_uploads WHERE output_id = ANY(%s) AND {space}"
                " ORDER BY created_at, library_file_id".encode(),
                (ids, NATIVE_ID_BASE),
            ).fetchall()
            slices = conn.execute(
                "SELECT output_id, source_library_file_id, sliced_library_file_id, preset_key,"
                " file_hash"
                " FROM output_bambuddy_slices WHERE output_id = ANY(%s)"
                " ORDER BY created_at, sliced_library_file_id",
                (ids,),
            ).fetchall()
        sliced: dict[tuple[str, int], list[SlicedCopy]] = {}
        for row in slices:
            sliced.setdefault((row["output_id"], row["source_library_file_id"]), []).append(
                SlicedCopy(
                    id=row["sliced_library_file_id"],
                    preset_key=row["preset_key"],
                    file_hash=row["file_hash"],
                )
            )
        for row in copies:
            found[row["output_id"]].append(
                LibraryCopy(
                    id=row["library_file_id"],
                    folder_id=row["folder_id"],
                    target_key=row["target_key"],
                    sliced=sliced.get((row["output_id"], row["library_file_id"]), []),
                    content_key=row["content_key"],
                )
            )
        return found

    def _record(self, output_id: str, copy: LibraryCopy) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO output_bambuddy_uploads"
                " (output_id, library_file_id, folder_id, target_key, content_key)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (output_id, library_file_id) DO UPDATE"
                " SET folder_id = EXCLUDED.folder_id, target_key = EXCLUDED.target_key,"
                " used_at = now(), content_key = COALESCE("
                "EXCLUDED.content_key, output_bambuddy_uploads.content_key)",
                (output_id, copy.id, copy.folder_id, copy.target_key, copy.content_key),
            )

    def _same_content(
        self, folder_id: int | None, target_key: str, content_key: str
    ) -> list[tuple[str, LibraryCopy]]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT output_id, library_file_id, folder_id, target_key, content_key"
                " FROM output_bambuddy_uploads"
                " WHERE folder_id IS NOT DISTINCT FROM %s AND target_key = %s"
                " AND content_key = %s ORDER BY created_at, library_file_id",
                (folder_id, target_key, content_key),
            ).fetchall()
        return [
            (
                row["output_id"],
                LibraryCopy(
                    id=row["library_file_id"],
                    folder_id=row["folder_id"],
                    target_key=row["target_key"],
                    content_key=row["content_key"],
                ),
            )
            for row in rows
        ]

    def _shared(self, ids: list[int], output_id: str) -> set[int]:
        if not ids:
            return set()
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT library_file_id FROM output_bambuddy_uploads"
                " WHERE library_file_id = ANY(%s) AND output_id <> %s",
                (ids, output_id),
            ).fetchall()
        return {row["library_file_id"] for row in rows}

    def _recorded(self, ids: list[int]) -> set[int]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT library_file_id FROM output_bambuddy_uploads"
                " WHERE library_file_id = ANY(%s)",
                (ids,),
            ).fetchall()
        return {row["library_file_id"] for row in rows}

    def _recorded_files(self, ids: list[int]) -> set[int]:
        if not ids:
            return set()
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT library_file_id AS id FROM output_bambuddy_uploads"
                " WHERE library_file_id = ANY(%s)"
                " UNION SELECT sliced_library_file_id FROM output_bambuddy_slices"
                " WHERE sliced_library_file_id = ANY(%s)",
                (ids, ids),
            ).fetchall()
        return {row["id"] for row in rows}

    def _outputs_for_files(self, ids: list[int]) -> dict[int, str]:
        if not ids:
            return {}
        with self._pool.connection() as conn:
            # A library print's copy is recorded under its subject, `library:<file id>`
            # (`send.upload_copy`), which is no output.
            rows = conn.execute(
                "SELECT DISTINCT ON (library_file_id) library_file_id, output_id"
                " FROM output_bambuddy_uploads WHERE library_file_id = ANY(%s)"
                " AND output_id NOT LIKE 'library:%%'"
                " ORDER BY library_file_id, created_at DESC, output_id",
                (ids,),
            ).fetchall()
        return {row["library_file_id"]: row["output_id"] for row in rows}

    def _relocate(self, ids: list[int], folder_id: int | None) -> None:
        if not ids:
            return
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE output_bambuddy_uploads SET folder_id = %s WHERE library_file_id = ANY(%s)",
                (folder_id, ids),
            )

    def _forget_files(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM output_bambuddy_uploads WHERE library_file_id = ANY(%s)", (ids,)
            )

    def _recorded_as(self, library_file_id: int) -> list[tuple[str, str]]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT output_id, target_key FROM output_bambuddy_uploads"
                " WHERE library_file_id = %s ORDER BY created_at, output_id",
                (library_file_id,),
            ).fetchall()
        return [(row["output_id"], row["target_key"]) for row in rows]

    def _forget(self, output_id: str, library_file_id: int) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM output_bambuddy_uploads WHERE output_id = %s AND library_file_id = %s",
                (output_id, library_file_id),
            )

    def _record_sliced(self, output_id: str, library_file_id: int, sliced: SlicedCopy) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO output_bambuddy_slices"
                " (output_id, source_library_file_id, sliced_library_file_id, preset_key,"
                " sliced_blob_key)"
                " SELECT %(output)s, %(source)s, %(sliced)s, %(preset)s, %(blob)s"
                " WHERE EXISTS (SELECT 1 FROM output_bambuddy_uploads"
                "  WHERE output_id = %(output)s AND library_file_id = %(source)s)"
                " ON CONFLICT (output_id, sliced_library_file_id) DO NOTHING",
                {
                    "output": output_id,
                    "source": library_file_id,
                    "sliced": sliced.id,
                    "preset": sliced.preset_key,
                    "blob": sliced.sliced_blob_key,
                },
            )

    def _slices_of(self, output_id: str, library_file_id: int, preset_key: str) -> list[SlicedCopy]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT sliced_library_file_id, preset_key, file_hash FROM output_bambuddy_slices"
                " WHERE output_id = %s AND source_library_file_id = %s AND preset_key = %s"
                " ORDER BY created_at DESC, sliced_library_file_id DESC",
                (output_id, library_file_id, preset_key),
            ).fetchall()
        return [
            SlicedCopy(
                id=row["sliced_library_file_id"],
                preset_key=row["preset_key"],
                file_hash=row["file_hash"],
            )
            for row in rows
        ]

    def _forget_slice(self, output_id: str, sliced_id: int) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM output_bambuddy_slices"
                " WHERE output_id = %s AND sliced_library_file_id = %s",
                (output_id, sliced_id),
            )

    def _forget_gone_slice(self, sliced_id: int) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM output_bambuddy_slices WHERE sliced_library_file_id = %s",
                (sliced_id,),
            )

    def _record_slice_hash(self, output_id: str, sliced_id: int, file_hash: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE output_bambuddy_slices SET file_hash = %s"
                " WHERE output_id = %s AND sliced_library_file_id = %s",
                (file_hash, output_id, sliced_id),
            )

    def _sent_between(self, output_id: str) -> tuple[datetime, datetime] | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                # Bambuddy's copies and slices only (#4312 review M1): the window scans
                # Bambuddy's archives, which a native copy is never in.
                "SELECT (SELECT min(created_at) FROM output_bambuddy_uploads"
                "  WHERE output_id = %(output)s AND library_file_id < %(base)s) AS first_upload,"
                " (SELECT max(created_at) FROM output_bambuddy_slices"
                "  WHERE output_id = %(output)s AND sliced_library_file_id < %(base)s)"
                " AS last_slice",
                {"output": output_id, "base": NATIVE_ID_BASE},
            ).fetchone()
        if row is None or row["first_upload"] is None or row["last_slice"] is None:
            return None
        return row["first_upload"], row["last_slice"]

    def _delete_outputs(self, ids: list[str]) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM output_bambuddy_uploads WHERE output_id = ANY(%s)", (ids,))
