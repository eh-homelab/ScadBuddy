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
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection, Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

#: Prefixes the key of :meth:`BambuddyUploadStore.copy_lock`'s advisory lock.
COPY_LOCK_PREFIX = "scadbuddy-library-copy:"


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

    async def for_output(self, output_id: str) -> list[LibraryCopy]:
        """The output's copies in upload order, each with its slices."""
        return (await self.for_outputs([output_id]))[output_id]

    async def for_outputs(self, output_ids: Iterable[str]) -> dict[str, list[LibraryCopy]]:
        """:meth:`for_output` for several outputs in two queries, not two per output."""
        return await asyncio.to_thread(self._for_outputs, list(dict.fromkeys(output_ids)))

    async def record(self, output_id: str, copy: LibraryCopy) -> None:
        """Record an upload, replacing the folder and target of one with the same id."""
        await asyncio.to_thread(self._record, output_id, copy)

    async def recorded(self, library_file_ids: Iterable[int]) -> set[int]:
        """Those of ``library_file_ids`` that any output records as a copy (#1145)."""
        return await asyncio.to_thread(self._recorded, list(library_file_ids))

    async def outputs_for_files(self, library_file_ids: Iterable[int]) -> dict[int, str]:
        """The output each of ``library_file_ids`` is a copy of, for those any output
        records (#1864): Arrange reads such a file's objects through that output. A file
        two outputs record is the later upload's."""
        return await asyncio.to_thread(self._outputs_for_files, list(library_file_ids))

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

    async def drop_preview(self, row_id: int) -> None:
        """Forget a background slice whose job no longer says what it did."""
        await asyncio.to_thread(self._drop_preview, row_id)

    _PREVIEW_COLUMNS = (
        "id, job_id, subject, library_file_id, preset_key, plate_id, job_created,"
        " sliced_file_id, sliced_name"
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
                " WHERE job_id = %s AND subject = %s ORDER BY created_at DESC, id DESC LIMIT 1",
                (job_id, subject),
            ).fetchone()
        return PreviewSlice.model_validate(row) if row else None

    def _previews_for(self, library_file_id: int, preset_key: str) -> list[PreviewSlice]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {self._PREVIEW_COLUMNS} FROM print_preview_slices"
                " WHERE library_file_id = %s AND preset_key = %s"
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

    def _drop_preview(self, row_id: int) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM print_preview_slices WHERE id = %s", (row_id,))

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

    def _for_outputs(self, ids: list[str]) -> dict[str, list[LibraryCopy]]:
        found: dict[str, list[LibraryCopy]] = {output_id: [] for output_id in ids}
        with self._pool.connection() as conn:
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
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO output_bambuddy_uploads"
                " (output_id, library_file_id, folder_id, target_key) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (output_id, library_file_id) DO UPDATE"
                " SET folder_id = EXCLUDED.folder_id, target_key = EXCLUDED.target_key",
                (output_id, copy.id, copy.folder_id, copy.target_key),
            )

    def _recorded(self, ids: list[int]) -> set[int]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT library_file_id FROM output_bambuddy_uploads"
                " WHERE library_file_id = ANY(%s)",
                (ids,),
            ).fetchall()
        return {row["library_file_id"] for row in rows}

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
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE output_bambuddy_slices SET file_hash = %s"
                " WHERE output_id = %s AND sliced_library_file_id = %s",
                (file_hash, output_id, sliced_id),
            )

    def _sent_between(self, output_id: str) -> tuple[datetime, datetime] | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT (SELECT min(created_at) FROM output_bambuddy_uploads"
                "  WHERE output_id = %(output)s) AS first_upload,"
                " (SELECT max(created_at) FROM output_bambuddy_slices"
                "  WHERE output_id = %(output)s) AS last_slice",
                {"output": output_id},
            ).fetchone()
        if row is None or row["first_upload"] is None or row["last_slice"] is None:
            return None
        return row["first_upload"], row["last_slice"]

    def _delete_outputs(self, ids: list[str]) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM output_bambuddy_uploads WHERE output_id = ANY(%s)", (ids,))
