"""The watcher's records of prints on LAN-only printers (#4699 phase 4):
``print_lan_imports`` (``20261111T1313Z_print_lan_imports.sql``). Only the API (the CRUD
service) reads or writes it: the print worker posts each record to the CRUD route
(`api/crud_print.py` ``POST /lan-imports``), and the API in-process calls
`StoreLanRecorder` itself.

A row and its activity row are one transaction (`LanImportStore.record`, and a person's
or the assistant's `dismiss` or `link`, #6041). A job (the
printer, ``subtask_name`` and ``started_at``) is recorded once: a record sent again
finds the row and answers it as it is, so a retried attempt or a resend after a lost
answer writes nothing more. The job's printer is ``printer_key``, the registry id it was
recorded for, never null: a printer deleted before the first send or between two sends
leaves ``printer`` null, and the resend still finds its row.
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
from scadbuddy.print_watch.lan import LanCapture, LanImport, LanState, decide

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
#: Why a linked record is shown in the inbox: its model is gone. The same words as an
#: archive row's (`store.MODEL_GONE`; `store` imports this module, so it is not imported
#: here).
MODEL_GONE = "its model was deleted"
#: `link`'s and `dismiss`'s read of one record, with its printer's name, locked.
_ONE = (
    "SELECT l.id, l.printer::text AS printer_id, "
    + ", ".join(f"l.{c}" for c in _COLUMNS)
    + ", l.seen_at, l.dismissed_at, p.name AS printer_name"
    " FROM print_lan_imports l LEFT JOIN printers p ON p.id = l.printer"
    " WHERE l.id = %s FOR UPDATE OF l"
)


class LanPrintRecord(LanImport):
    """A record as the routes answer it (#6041), with its printer's name: None once the
    printer was deleted."""

    printer_name: str | None = None


class LanImportLinkedError(Exception):
    """A dismiss or a link of a record that is linked to another model, and that model
    is there."""

    def __init__(self, record_id: int, slug: str) -> None:
        super().__init__(
            f"LAN print {record_id} is a print of model {slug!r}, not in Unsorted imports"
        )
        self.slug = slug


class LanImportNoFileError(Exception):
    """A link of a record whose 3MF the watcher did not keep: a model link needs the file."""

    def __init__(self, record_id: int, reason: str | None) -> None:
        super().__init__(
            f"LAN print {record_id} has no kept 3MF"
            + (f" ({reason})" if reason else "")
            + ": a model link needs the file"
        )


def _held(row: LanImport, exists: Callable[[str], bool]) -> str | None:
    """The model a record is linked to when that model is there; None for a record in the
    inbox, a dismissed one, or a linked one whose model is gone."""
    if row.state == "linked" and row.slug is not None and exists(row.slug):
        return row.slug
    return None


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

    async def page(
        self,
        *,
        state: LanState | None,
        limit: int,
        before: int | None,
        slugs: list[str],
        slug: str | None = None,
    ) -> list[LanPrintRecord]:
        """Up to ``limit`` records below id ``before``, newest first, each with its
        printer's name. ``slugs`` are the models that are there now: a linked record
        whose model is not one of them is shown as an inbox record, with `MODEL_GONE` as
        its reason, as an archive row is (`AutoImportStore.page`). ``slug`` keeps only
        the records linked to that model."""
        return await asyncio.to_thread(self._page, state, limit, before, slugs, slug)

    async def dismiss(self, record_id: int, exists: Callable[[str], bool]) -> LanPrintRecord | None:
        """Mark a record ``dismissed`` and rewrite its activity row in the same
        transaction. None when there is no such record; a record dismissed before is
        answered as it is. `LanImportLinkedError` when it is linked to a model that is
        there (``exists``). A linked record whose model is gone is in the inbox, so it
        can be dismissed."""
        return await asyncio.to_thread(self._dismiss, record_id, exists)

    async def link(
        self, record_id: int, slug: str, exists: Callable[[str], bool]
    ) -> LanPrintRecord | None:
        """Link a record to the model ``slug`` (``matched_by`` ``import``: a person's or
        the assistant's link) and rewrite its activity row in the same transaction. None
        when there is no such record. A record linked to ``slug`` already is answered as
        it is. `LanImportLinkedError` when it is linked to another model that is there;
        `LanImportNoFileError` when the watcher kept no 3MF for it. A dismissed record
        can be linked."""
        return await asyncio.to_thread(self._link, record_id, slug, exists)

    def _page(
        self,
        state: LanState | None,
        limit: int,
        before: int | None,
        slugs: list[str],
        slug: str | None,
    ) -> list[LanPrintRecord]:
        plain = ", ".join(
            f"r.{c}" for c in _COLUMNS if c not in ("state", "slug", "matched_by", "reason")
        )
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT r.id, r.printer_id, {plain}, r.shown AS state,"
                " CASE WHEN r.gone THEN NULL ELSE r.slug END AS slug,"
                " CASE WHEN r.gone THEN NULL ELSE r.matched_by END AS matched_by,"
                " CASE WHEN r.gone THEN %(gone)s ELSE r.reason END AS reason,"
                " r.seen_at, r.dismissed_at, r.printer_name"
                " FROM (SELECT g.*, CASE WHEN g.gone THEN 'inbox' ELSE g.state END AS shown"
                "  FROM (SELECT l.*, l.printer::text AS printer_id, p.name AS printer_name,"
                "   l.state = 'linked' AND NOT (l.slug = ANY(%(slugs)s::text[])) AS gone"
                "   FROM print_lan_imports l LEFT JOIN printers p ON p.id = l.printer"
                "   WHERE (%(before)s::bigint IS NULL OR l.id < %(before)s)) g) r"
                " WHERE (%(state)s::text IS NULL OR r.shown = %(state)s)"
                " AND (%(slug)s::text IS NULL OR (r.shown = 'linked' AND r.slug = %(slug)s))"
                " ORDER BY r.id DESC LIMIT %(limit)s",
                {
                    "state": state,
                    "before": before,
                    "limit": limit,
                    "slugs": slugs,
                    "slug": slug,
                    "gone": MODEL_GONE,
                },
            ).fetchall()
        return [LanPrintRecord.model_validate(dict(row)) for row in rows]

    def _dismiss(self, record_id: int, exists: Callable[[str], bool]) -> LanPrintRecord | None:
        with self._pool.connection() as conn, conn.transaction():
            found = conn.execute(_ONE, (record_id,)).fetchone()
            if found is None:
                return None
            row = LanPrintRecord.model_validate(dict(found))
            if row.state == "dismissed":
                return row
            held = _held(row, exists)
            if held is not None:
                raise LanImportLinkedError(record_id, held)
            # A linked record is here only when its model is gone: it loses its model,
            # as the inbox shows it.
            return self._update(
                conn,
                row,
                "state = 'dismissed', slug = NULL, matched_by = NULL, dismissed_at = now()",
                {},
            )

    def _link(
        self, record_id: int, slug: str, exists: Callable[[str], bool]
    ) -> LanPrintRecord | None:
        with self._pool.connection() as conn, conn.transaction():
            found = conn.execute(_ONE, (record_id,)).fetchone()
            if found is None:
                return None
            row = LanPrintRecord.model_validate(dict(found))
            if row.state == "linked" and row.slug == slug:
                return row
            held = _held(row, exists)
            if held is not None:
                raise LanImportLinkedError(record_id, held)
            if row.source is None:
                raise LanImportNoFileError(record_id, row.reason)
            return self._update(
                conn,
                row,
                "state = 'linked', slug = %(slug)s, matched_by = 'import', reason = NULL,"
                " dismissed_at = NULL",
                {"slug": slug},
            )

    def _update(
        self,
        conn: Connection[DictRow],
        row: LanPrintRecord,
        assignments: str,
        values: dict[str, Any],
    ) -> LanPrintRecord:
        """Apply ``assignments`` to ``row``'s record and write its activity row, in the
        caller's transaction."""
        updated = conn.execute(
            f"UPDATE print_lan_imports SET {assignments} WHERE id = %(id)s RETURNING {_RETURNED}",
            {**values, "id": row.id},
        ).fetchone()
        assert updated is not None
        written = LanPrintRecord.model_validate({**dict(updated), "printer_name": row.printer_name})
        # The activity row says where the print is now, in the same transaction.
        if self.activity is not None:
            self.activity.upsert_in(conn, lambda: lan_row_of(written))
        return written

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
