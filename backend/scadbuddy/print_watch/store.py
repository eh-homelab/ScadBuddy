"""The watcher's records (#4699): ``print_auto_imports``, one row per archive it took, and
``print_auto_import_watch``, when it was switched on. Both are created by
``20261110T2250Z_print_auto_imports.sql``. ``print_auto_import_failures``
(``20261111T0616Z_print_auto_import_failures.sql``, #6023) holds when an archive's import
last raised, so a pass takes it after the others. Only the API (the CRUD service) reads or
writes them: the pass runs in its ``library`` worker (`workflows/housekeeping.py`).

A row and its link are written in one transaction (`record`), under the archive's import
lock (`print_links.import_in`), so a manual import (#4698) and the watcher never both
take one archive, and a crash never leaves a link without its row or a row without its
link.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from scadbuddy.activity.index import ActivityIndex
from scadbuddy.bambuddy.models import Archive
from scadbuddy.bambuddy.print_links import (
    IMPORT_LOCK_CLASS,
    ImportSource,
    PrintImport,
    import_in,
    owner_in,
)
from scadbuddy.print_watch.activity import imported_row, row_of

#: `pg_advisory_xact_lock` key (hashed) under which `AutoImportStore.floor` reads the
#: switch and writes the floor.
FLOOR_LOCK = "scadbuddy:print-watch:floor"
#: Why a linked row is shown in the inbox: a model delete removed its link.
MODEL_GONE = "its model was deleted"

AutoImportState = Literal["linked", "inbox", "dismissed"]
#: How the watcher matched a model: an earlier import of the same printed file, or the
#: job's name, word for word.
AutoMatchedBy = Literal["earlier_import", "job_name"]

_COLUMNS = (
    "archive_id",
    "printer_id",
    "print_name",
    "content_hash",
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


class AutoImport(BaseModel):
    """One archive the watcher took: linked to a model, in the inbox, or dismissed from it."""

    archive_id: int
    printer_id: int | None = None
    print_name: str | None = None
    content_hash: str | None = None
    started_at: datetime | None = None
    state: AutoImportState
    #: The model a linked print is a print of; None in the inbox.
    slug: str | None = None
    matched_by: AutoMatchedBy | None = None
    #: Where its 3MF is: the source the watcher chose; None when no source had it.
    source: ImportSource | None = None
    library_file_id: int | None = None
    printer_path: str | None = None
    filename: str | None = None
    size: int | None = None
    sha256: str | None = None
    #: Why it is in the inbox: no source had the 3MF, or no model matched with confidence.
    reason: str | None = None
    #: The models the job's name suggests (`print_import.suggested_slugs`).
    suggested: list[str] = Field(default_factory=list)
    #: Set by the database.
    seen_at: datetime | None = None
    #: When a person or the assistant dismissed it from the inbox (state ``dismissed``).
    dismissed_at: datetime | None = None


class AutoImportLinkedError(Exception):
    """A dismiss of a row that is not in the inbox: a link names its archive."""

    def __init__(self, archive_id: int, subject: str) -> None:
        super().__init__(f"archive {archive_id} is a print of {subject}, not in Unsorted imports")
        self.subject = subject


class AutoImportStore:
    """``print_auto_imports`` on the projection's pool, the pool `PrintLinkStore` and
    `ActivityIndex` use, so a row, its link and its activity row share one transaction.
    Every public method runs its query in a worker thread."""

    def __init__(
        self, pool: ConnectionPool[Connection[DictRow]], activity: ActivityIndex | None = None
    ) -> None:
        self._pool = pool
        self.activity = activity

    async def floor(self) -> datetime | None:
        """When the watcher was switched on, or None when it is off. It reads the switch
        (``print_auto_import`` in ``settings``) and writes the floor in one transaction,
        under `FLOOR_LOCK`, which every caller takes (review of #6002, #6011): a pass that
        read the settings before a ``PUT /settings`` committed never deletes the floor the
        write recorded. The first call that finds the switch on records now; a call that
        finds it off forgets that, so switching it on again never imports the time it
        was off."""
        return await asyncio.to_thread(self._floor)

    async def known(self, archives: Sequence[Archive]) -> set[int]:
        """Which of ``archives`` the watcher took already: by id, or as the same job (the
        printer, the printed file's hash and the start), which Bambuddy can archive again
        under a new id."""
        if not archives:
            return set()
        return await asyncio.to_thread(self._known, list(archives))

    async def slugs_by_hash(self, sha256: str) -> set[str]:
        """The models earlier imports of the printed file with this SHA-256 (an archive's
        ``content_hash``) were linked to, whatever source each import read."""
        return await asyncio.to_thread(self._slugs_by_hash, sha256)

    async def record(self, row: AutoImport, *, plate_id: int | None = None) -> bool:
        """Write ``row``; for a linked one its import too (`print_links.import_in`), and
        its activity row, in one transaction. False, with nothing written, when a link
        names the archive already (ScadBuddy's own print, or a person's import) or the
        watcher took it or its job before."""
        return await asyncio.to_thread(self._record, row, plate_id)

    async def failures(self, archive_ids: Sequence[int]) -> dict[int, datetime]:
        """When the import of each of ``archive_ids`` last raised; an archive with no
        failure is left out (#6023)."""
        if not archive_ids:
            return {}
        return await asyncio.to_thread(self._failures, list(archive_ids))

    async def failed(self, archive_id: int, *, forget_before: datetime) -> None:
        """Record now as when the import of ``archive_id`` last raised, so the next passes
        take it after the archives that did not fail (#6023). It also forgets the failures
        recorded before ``forget_before``: their archives are past the pass's look-back.
        Until the next failure such rows stay, which is harmless: `failures` is asked only
        about archives inside the look-back."""
        await asyncio.to_thread(self._failed, archive_id, forget_before)

    async def page(
        self, *, state: AutoImportState | None, limit: int, before: int | None = None
    ) -> list[AutoImport]:
        """Up to ``limit`` rows below archive ``before``, newest archive first. An inbox
        row whose archive a link names now (a person imported it, or a run's link was
        found since) is left out. A linked row whose model link a model delete removed
        is shown as an inbox row, with `MODEL_GONE` as its reason (#6011)."""
        return await asyncio.to_thread(self._page, state, limit, before)

    async def dismiss(self, archive_id: int) -> AutoImport | None:
        """Mark an inbox row ``dismissed``, so it leaves the inbox, and rewrite its activity
        row in the same transaction. The row stays, so the
        watcher never takes the archive again (`known`). None when the watcher has no
        row for the archive. A row dismissed before is answered as it is.
        `AutoImportLinkedError` when a link names the archive: the print is a model's,
        an output's or a library file's, not in the inbox. A linked row whose model link
        a model delete removed is in the inbox (`page`), so it can be dismissed."""
        return await asyncio.to_thread(self._dismiss, archive_id)

    def _floor(self) -> datetime | None:
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (FLOOR_LOCK,))
            # Read after the lock: each statement sees what committed before it began.
            switch = conn.execute(
                "SELECT value FROM settings WHERE name = 'print_auto_import'"
            ).fetchone()
            # Stored as JSON `true` when on; no row (the default) or `false` is off.
            if switch is None or switch["value"] is not True:
                conn.execute("DELETE FROM print_auto_import_watch")
                return None
            row = conn.execute(
                "INSERT INTO print_auto_import_watch (only_row) VALUES (true)"
                " ON CONFLICT (only_row) DO UPDATE SET since = print_auto_import_watch.since"
                " RETURNING since"
            ).fetchone()
        assert row is not None
        since: datetime = row["since"]
        return since

    def _known(self, archives: list[Archive]) -> set[int]:
        ids = [archive.id for archive in archives]
        keyed = [
            archive
            for archive in archives
            if archive.printer_id is not None
            and archive.content_hash is not None
            and archive.started_at is not None
        ]
        with self._pool.connection() as conn:
            found = {
                row["archive_id"]
                for row in conn.execute(
                    "SELECT archive_id FROM print_auto_imports WHERE archive_id = ANY(%s)",
                    (ids,),
                ).fetchall()
            }
            if keyed:
                jobs = conn.execute(
                    "SELECT printer_id, content_hash, started_at FROM print_auto_imports"
                    " WHERE content_hash = ANY(%s)",
                    ([archive.content_hash for archive in keyed],),
                ).fetchall()
                taken = {(r["printer_id"], r["content_hash"], r["started_at"]) for r in jobs}
                found |= {
                    archive.id
                    for archive in keyed
                    if (archive.printer_id, archive.content_hash, archive.started_at) in taken
                }
        return found

    def _failures(self, archive_ids: list[int]) -> dict[int, datetime]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT archive_id, failed_at FROM print_auto_import_failures"
                " WHERE archive_id = ANY(%s)",
                (archive_ids,),
            ).fetchall()
        return {row["archive_id"]: row["failed_at"] for row in rows}

    def _failed(self, archive_id: int, forget_before: datetime) -> None:
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(
                "DELETE FROM print_auto_import_failures WHERE failed_at < %s", (forget_before,)
            )
            conn.execute(
                "INSERT INTO print_auto_import_failures (archive_id) VALUES (%s)"
                " ON CONFLICT (archive_id) DO UPDATE SET failed_at = now()",
                (archive_id,),
            )

    def _slugs_by_hash(self, sha256: str) -> set[str]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                # The printed file's hash; a row from before `content_hash` was kept
                # has its source's, which is the printed file's for an archive or a
                # library file matched by hash.
                "SELECT DISTINCT slug FROM print_imports"
                " WHERE content_hash = %(hash)s OR (content_hash IS NULL AND sha256 = %(hash)s)",
                {"hash": sha256},
            ).fetchall()
        return {row["slug"] for row in rows}

    def _record(self, row: AutoImport, plate_id: int | None) -> bool:
        try:
            return self._write(row, plate_id)
        except _Owned:
            return False

    def _write(self, row: AutoImport, plate_id: int | None) -> bool:
        values: dict[str, Any] = row.model_dump(include=set(_COLUMNS))
        with self._pool.connection() as conn, conn.transaction():
            if row.slug is None:
                # The same lock an import takes, so the owner read below stands.
                conn.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (IMPORT_LOCK_CLASS, row.archive_id % (2**31)),
                )
                if owner_in(conn, row.archive_id) is not None:
                    return False
            inserted = conn.execute(
                f"INSERT INTO print_auto_imports ({', '.join(_COLUMNS)})"
                f" VALUES ({', '.join(f'%({c})s' for c in _COLUMNS)})"
                " ON CONFLICT DO NOTHING RETURNING archive_id",
                values,
            ).fetchone()
            if inserted is None:
                return False
            conn.execute(
                "DELETE FROM print_auto_import_failures WHERE archive_id = %s", (row.archive_id,)
            )
            if row.slug is not None:
                assert row.source is not None and row.filename is not None
                owner = import_in(
                    conn,
                    PrintImport(
                        archive_id=row.archive_id,
                        slug=row.slug,
                        source=row.source,
                        library_file_id=row.library_file_id,
                        printer_id=row.printer_id if row.source == "printer" else None,
                        printer_path=row.printer_path,
                        filename=row.filename,
                        size=row.size,
                        sha256=row.sha256,
                        content_hash=row.content_hash,
                    ),
                    printer_id=row.printer_id,
                    plate_id=plate_id,
                )
                if owner is not None:
                    # Nothing of this row may stand: roll the insert back.
                    raise _Owned
            if self.activity is not None:
                self.activity.upsert_in(conn, lambda: row_of(row))
        return True

    def _dismiss(self, archive_id: int) -> AutoImport | None:
        returned = ", ".join((*_COLUMNS, "seen_at", "dismissed_at"))
        with self._pool.connection() as conn, conn.transaction():
            # The lock an import and the watcher's record take, so the owner read stands.
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                (IMPORT_LOCK_CLASS, archive_id % (2**31)),
            )
            row = conn.execute(
                f"SELECT {returned} FROM print_auto_imports WHERE archive_id = %s FOR UPDATE",
                (archive_id,),
            ).fetchone()
            if row is None:
                return None
            if row["state"] == "dismissed":
                return AutoImport.model_validate(dict(row))
            owner = owner_in(conn, archive_id)
            if owner is not None:
                raise AutoImportLinkedError(archive_id, owner.subject)
            # A linked row is here only when its model link is gone (the owner read
            # above): it loses its model, as the inbox shows it.
            dismissed = conn.execute(
                "UPDATE print_auto_imports SET state = 'dismissed', slug = NULL,"
                " matched_by = NULL, dismissed_at = now()"
                f" WHERE archive_id = %s RETURNING {returned}",
                (archive_id,),
            ).fetchone()
            assert dismissed is not None
            updated = AutoImport.model_validate(dict(dismissed))
            # The activity row says where the print is now, in the same transaction.
            if self.activity is not None:
                self.activity.upsert_in(conn, lambda: row_of(updated))
        return updated

    def imported_in(self, conn: Connection[DictRow], record: PrintImport) -> None:
        """Import print (#4698) linked ``record.archive_id`` to a model: when the watcher
        took that archive (a dismissed or an inbox row), rewrite its activity row in the
        import's transaction, so the "Where" facet is not stale (#6029). The
        ``print_auto_imports`` row stays as it is."""
        if self.activity is None:
            return
        found = conn.execute(
            f"SELECT {', '.join((*_COLUMNS, 'seen_at', 'dismissed_at'))}"
            " FROM print_auto_imports WHERE archive_id = %s",
            (record.archive_id,),
        ).fetchone()
        if found is None:
            return
        self.activity.upsert_in(
            conn, lambda: imported_row(AutoImport.model_validate(dict(found)), record)
        )

    def _page(
        self, state: AutoImportState | None, limit: int, before: int | None
    ) -> list[AutoImport]:
        # A linked row whose model link is gone (a model delete removed it,
        # `PrintLinkStore.delete_models`) is shown as an inbox row (#6011): the print is
        # a person's to sort again, and `known` still keeps the watcher from taking it
        # twice. Like any inbox row, it leaves the inbox once a link names its archive.
        plain = ", ".join(
            f"r.{c}" for c in _COLUMNS if c not in ("state", "slug", "matched_by", "reason")
        )
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {plain}, r.shown AS state,"
                " CASE WHEN r.gone THEN NULL ELSE r.slug END AS slug,"
                " CASE WHEN r.gone THEN NULL ELSE r.matched_by END AS matched_by,"
                " CASE WHEN r.gone THEN %(gone)s ELSE r.reason END AS reason,"
                " r.seen_at, r.dismissed_at"
                " FROM (SELECT g.*, CASE WHEN g.gone THEN 'inbox' ELSE g.state END AS shown"
                "  FROM (SELECT a.*,"
                "   a.state = 'linked' AND NOT EXISTS (SELECT 1 FROM print_links l"
                "    WHERE l.archive_id = a.archive_id AND l.subject = 'model:' || a.slug)"
                "   AS gone,"
                "   EXISTS (SELECT 1 FROM print_links l WHERE l.archive_id = a.archive_id)"
                "   AS any_link"
                "   FROM print_auto_imports a"
                "   WHERE (%(before)s::bigint IS NULL OR a.archive_id < %(before)s)) g) r"
                " WHERE (%(state)s::text IS NULL OR r.shown = %(state)s)"
                " AND (r.shown = 'linked' OR NOT r.any_link)"
                " ORDER BY r.archive_id DESC LIMIT %(limit)s",
                {"state": state, "before": before, "limit": limit, "gone": MODEL_GONE},
            ).fetchall()
        return [AutoImport.model_validate(dict(row)) for row in rows]


class _Owned(Exception):  # noqa: N818 - a rollback signal, not an error
    """A link took the archive between the pass's read and the record."""
