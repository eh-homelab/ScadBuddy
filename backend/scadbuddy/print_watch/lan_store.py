"""The watcher's records of prints on LAN-only printers (#4699 phase 4):
``print_lan_imports`` (``20261111T1313Z_print_lan_imports.sql``). Only the API (the CRUD
service) reads or writes it: the print worker posts each record to the CRUD route
(`api/crud_print.py` ``POST /lan-imports``), and the API in-process calls
`StoreLanRecorder` itself.

A row and its activity row are one transaction (`LanImportStore.record`). A job (the
printer, ``subtask_name`` and ``started_at``) is recorded once: a record sent again
finds the row and answers it as it is, so a retried attempt or a resend after a lost
answer writes nothing more. The job's printer is ``printer_key``, the registry id it was
recorded for, never null: a printer deleted before the first send or between two sends
leaves ``printer`` null, and the resend still finds its row. A printer's merge moves the
key to the kept printer, unless the kept printer has a record of the same job
(`printers/store.py` `_repoint_lan_imports`, #6045).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.activity.index import ActivityIndex
from scadbuddy.print_watch.activity import lan_row_of
from scadbuddy.print_watch.lan import LanCapture, LanImport, decide

_COLUMNS = (
    "subtask_name",
    "started_at",
    "state",
    "slug",
    "matched_by",
    "source",
    "library_file_id",
    "printer_path",
    "filename",
    "size",
    "sha256",
    "reason",
    "suggested",
)
#: Every column a read returns, the printer as text under the model's name.
_RETURNED = ", ".join(("id", "printer::text AS printer_id", *_COLUMNS, "seen_at", "dismissed_at"))
#: The job's record, by the registry id it was recorded for (never null), its name and
#: start.
_BY_JOB = (
    f"SELECT {_RETURNED} FROM print_lan_imports"
    " WHERE printer_key = %s AND subtask_name = %s AND started_at = %s"
)


class LanImportStore:
    """``print_lan_imports`` on the projection's pool, the pool `ActivityIndex` uses, so
    a row and its activity row share one transaction. Every public method runs its query
    in a worker thread."""

    def __init__(
        self, pool: ConnectionPool[Connection[DictRow]], activity: ActivityIndex | None = None
    ) -> None:
        self._pool = pool
        self.activity = activity

    async def find(
        self, printer_id: str, subtask_name: str, started_at: datetime
    ) -> LanImport | None:
        """The job's record, when it has one."""
        return await asyncio.to_thread(self._find, printer_id, subtask_name, started_at)

    async def slugs_by_hash(self, sha256: str) -> set[str]:
        """The models earlier imports of the file with this SHA-256 were linked to: an
        archive's import (``print_imports``, by the printed file's hash) or a LAN-only
        printer's linked record."""
        return await asyncio.to_thread(self._slugs_by_hash, sha256)

    async def record(self, row: LanImport) -> LanImport:
        """Write ``row`` and its activity row in one transaction; the job's record as it
        is when one was written before (nothing is written then)."""
        return await asyncio.to_thread(self._record, row)

    def _find(self, printer_id: str, subtask_name: str, started_at: datetime) -> LanImport | None:
        with self._pool.connection() as conn:
            found = conn.execute(_BY_JOB, (printer_id, subtask_name, started_at)).fetchone()
        return None if found is None else LanImport.model_validate(dict(found))

    def _slugs_by_hash(self, sha256: str) -> set[str]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                # As `AutoImportStore.slugs_by_hash` reads ``print_imports``.
                "SELECT slug FROM print_imports"
                " WHERE content_hash = %(hash)s OR (content_hash IS NULL AND sha256 = %(hash)s)"
                " UNION SELECT slug FROM print_lan_imports"
                " WHERE sha256 = %(hash)s AND state = 'linked'",
                {"hash": sha256},
            ).fetchall()
        return {row["slug"] for row in rows}

    def _record(self, row: LanImport) -> LanImport:
        values: dict[str, Any] = row.model_dump(include=set(_COLUMNS))
        values["printer"] = row.printer_id
        columns = ("printer", "printer_key", *_COLUMNS)
        # A printer deleted since the job started is no printer: its record names none,
        # and keeps the job's registry id as its key.
        placed = [
            "(SELECT id FROM printers WHERE id = %(printer)s::uuid)",
            "%(printer)s",
            *(f"%({c})s" for c in _COLUMNS),
        ]
        with self._pool.connection() as conn, conn.transaction():
            inserted = conn.execute(
                f"INSERT INTO print_lan_imports ({', '.join(columns)})"
                f" VALUES ({', '.join(placed)})"
                " ON CONFLICT (printer_key, subtask_name, started_at) DO NOTHING"
                f" RETURNING {_RETURNED}",
                values,
            ).fetchone()
            if inserted is None:
                found = conn.execute(
                    _BY_JOB, (row.printer_id, row.subtask_name, row.started_at)
                ).fetchone()
                assert found is not None
                return LanImport.model_validate(dict(found))
            written = LanImport.model_validate(dict(inserted))
            if self.activity is not None:
                self.activity.upsert_in(conn, lambda: lan_row_of(written))
        return written


class StoreLanRecorder:
    """`print_watch.lan.LanRecorder` on the API: match a model (`decide`) and write the
    record. ``slugs`` and ``exists`` are the catalogue's, run in a thread. The CRUD route
    and the API's in-process print worker both record through it."""

    def __init__(
        self,
        store: LanImportStore,
        slugs: Callable[[], list[str]],
        exists: Callable[[str], bool],
    ) -> None:
        self.store = store
        self._slugs = slugs
        self._exists = exists

    async def record(self, capture: LanCapture) -> LanImport:
        job = capture.job
        known = await self.store.find(job.printer_id, job.subtask_name, job.started_at)
        if known is not None:
            return known
        earlier: list[str] = []
        if capture.sha256 is not None:
            for slug in sorted(await self.store.slugs_by_hash(capture.sha256)):
                if await asyncio.to_thread(self._exists, slug):
                    earlier.append(slug)
        slugs = await asyncio.to_thread(self._slugs)
        return await self.store.record(decide(capture, slugs=slugs, earlier=earlier))
