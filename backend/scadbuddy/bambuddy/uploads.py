"""An output's uploads to Bambuddy's file library, in Postgres (#316, #455).

Every copy of an output's ``model.3mf`` ScadBuddy has put in Bambuddy's *file library*
(not an OpenSCAD library), and every sliced 3MF Bambuddy wrote beside one. They are
the only pointers ScadBuddy has to those files, so they live in the database rather
than in the output's ``meta.json``. The tables are migration
``20260928T0720Z_output_bambuddy_uploads``.

The database is required (#401): without ``SCADBUDDY_DATABASE_URL`` there is no
fallback, and every call raises `DatabaseRequiredError`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field


class DatabaseRequiredError(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            "recording an output's Bambuddy uploads needs the database; "
            "set SCADBUDDY_DATABASE_URL (#401)"
        )


class SlicedCopy(BaseModel):
    """A sliced 3MF Bambuddy wrote beside one of this output's library copies (#316).

    Bambuddy puts a slice in its source's folder, so it belongs to that copy's entry:
    a slice made for project A is in A's folder, next to the file it was sliced from.
    """

    #: Bambuddy library file id of the sliced 3MF.
    id: int
    #: What it was sliced with: the pipeline's id on a pipeline run, or the presets,
    #: plate and plate type on the slice-and-queue route (``SliceRequest.preset_key``).
    #: ``None`` when the route did not say.
    preset_key: str | None = None
    #: Bambuddy's SHA-256 of the sliced file, read once (#306). An archive of a print of
    #: it has the same ``content_hash``: the link once the queue item is gone.
    file_hash: str | None = None


class LibraryCopy(BaseModel):
    """One copy of the output's 3MF in Bambuddy's file library (#316).

    Keyed by (``folder_id``, ``target_key``). The folder is what files it under a
    project, and the target is what it was laid out for, so a copy is reusable only
    where both still hold. A copy in a project's folder is the user's record of what
    that project printed and is never moved or deleted by ScadBuddy; only a copy in
    the inbox (Settings' ``library_folder_id``) is ever replaced.
    """

    #: Bambuddy library file id of the unsliced 3MF.
    id: int
    #: ``None`` is the library root.
    folder_id: int | None
    #: :attr:`~scadbuddy.bambuddy.send.Target.key` — the plate and nozzle it was laid
    #: out for.
    target_key: str
    sliced: list[SlicedCopy] = Field(default_factory=list)


class BambuddyUploadStore:
    """``output_bambuddy_uploads`` and ``output_bambuddy_slices``, on the process's pool.

    The pool is the render queue's (`PostgresJobStore.pool`), opened and migrated at
    startup; this store opens nothing of its own.

    Every public method is a coroutine that runs its query in a worker thread, as the
    render queue does with its store: psycopg's calls here are blocking, and the
    progress poll alone would otherwise hold the event loop for a round trip each time.
    """

    def __init__(self, pool: ConnectionPool[Connection[DictRow]] | None) -> None:
        self._pool = pool

    def _require(self) -> ConnectionPool[Connection[DictRow]]:
        if self._pool is None:
            raise DatabaseRequiredError
        return self._pool

    async def for_output(self, output_id: str) -> list[LibraryCopy]:
        """The output's copies in upload order, each with its slices."""
        return (await self.for_outputs([output_id]))[output_id]

    async def for_outputs(self, output_ids: Iterable[str]) -> dict[str, list[LibraryCopy]]:
        """:meth:`for_output` for several outputs in two queries, not two per output."""
        return await asyncio.to_thread(self._for_outputs, list(dict.fromkeys(output_ids)))

    async def record(self, output_id: str, copy: LibraryCopy) -> None:
        """Record an upload, replacing the folder and target of one with the same id."""
        await asyncio.to_thread(self._record, output_id, copy)

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

    async def record_slice_hash(self, output_id: str, sliced_id: int, file_hash: str) -> None:
        """Keep the hash Bambuddy reports for one of the output's sliced files (#306)."""
        await asyncio.to_thread(self._record_slice_hash, output_id, sliced_id, file_hash)

    async def delete_outputs(self, output_ids: Iterable[str]) -> None:
        """Forget every copy and slice of deleted outputs. Bambuddy is not touched."""
        await asyncio.to_thread(self._delete_outputs, list(output_ids))

    # The blocking bodies, run in a worker thread by the coroutines above.

    def _for_outputs(self, ids: list[str]) -> dict[str, list[LibraryCopy]]:
        found: dict[str, list[LibraryCopy]] = {output_id: [] for output_id in ids}
        with self._require().connection() as conn:
            copies = conn.execute(
                "SELECT output_id, library_file_id, folder_id, target_key"
                " FROM output_bambuddy_uploads WHERE output_id = ANY(%s)"
                " ORDER BY created_at, library_file_id",
                (ids,),
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
                )
            )
        return found

    def _record(self, output_id: str, copy: LibraryCopy) -> None:
        with self._require().connection() as conn:
            conn.execute(
                "INSERT INTO output_bambuddy_uploads"
                " (output_id, library_file_id, folder_id, target_key) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (output_id, library_file_id) DO UPDATE"
                " SET folder_id = EXCLUDED.folder_id, target_key = EXCLUDED.target_key",
                (output_id, copy.id, copy.folder_id, copy.target_key),
            )

    def _forget(self, output_id: str, library_file_id: int) -> None:
        with self._require().connection() as conn:
            conn.execute(
                "DELETE FROM output_bambuddy_uploads WHERE output_id = %s AND library_file_id = %s",
                (output_id, library_file_id),
            )

    def _record_sliced(self, output_id: str, library_file_id: int, sliced: SlicedCopy) -> None:
        with self._require().connection() as conn:
            conn.execute(
                "INSERT INTO output_bambuddy_slices"
                " (output_id, source_library_file_id, sliced_library_file_id, preset_key)"
                " SELECT %(output)s, %(source)s, %(sliced)s, %(preset)s"
                " WHERE EXISTS (SELECT 1 FROM output_bambuddy_uploads"
                "  WHERE output_id = %(output)s AND library_file_id = %(source)s)"
                " ON CONFLICT (output_id, sliced_library_file_id) DO NOTHING",
                {
                    "output": output_id,
                    "source": library_file_id,
                    "sliced": sliced.id,
                    "preset": sliced.preset_key,
                },
            )

    def _record_slice_hash(self, output_id: str, sliced_id: int, file_hash: str) -> None:
        with self._require().connection() as conn:
            conn.execute(
                "UPDATE output_bambuddy_slices SET file_hash = %s"
                " WHERE output_id = %s AND sliced_library_file_id = %s",
                (file_hash, output_id, sliced_id),
            )

    def _delete_outputs(self, ids: list[str]) -> None:
        with self._require().connection() as conn:
            conn.execute("DELETE FROM output_bambuddy_uploads WHERE output_id = ANY(%s)", (ids,))
