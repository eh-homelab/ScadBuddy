"""`store_blobs`: which blob a key names, where its bytes are, when it was last wanted."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.errors import DeadlockDetected
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.store.content_models import BlobRef, BlobStat, ReuseLostError

logger = logging.getLogger(__name__)
Pool = ConnectionPool[Connection[DictRow]]
_COLUMNS = "key, sha256, kind, backend, backend_id, size, slug, meta, touched_at"
#: Attempts at an index write that Postgres picks as a deadlock's victim (`_retrying`).
_DEADLOCK_ATTEMPTS = 3


def _stat(row: DictRow) -> BlobStat:
    return BlobStat(
        key=row["key"],
        ref=BlobRef(
            sha256=row["sha256"],
            kind=row["kind"],
            backend=row["backend"],
            backend_id=row["backend_id"],
            size=row["size"],
        ),
        slug=row["slug"],
        meta=row["meta"],
        touched_at=row["touched_at"],
    )


class BlobIndex:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    def get(self, key: str) -> BlobStat | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM store_blobs WHERE key = %s", (key,)
            ).fetchone()
        return _stat(row) if row is not None else None

    def by_sha(self, sha256: str, kind: str, backend: str) -> BlobRef | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM store_blobs WHERE sha256 = %s AND kind = %s"
                " AND backend = %s LIMIT 1",
                (sha256, kind, backend),
            ).fetchone()
        return _stat(row).ref if row is not None else None

    @staticmethod
    def _locked(
        conn: Connection[DictRow], key: str, ref: BlobRef, *, reuse: bool, columns: str
    ) -> DictRow | None:
        """Lock ``key``'s row FOR UPDATE, in the caller's transaction, and return it.

        With ``reuse``, ``ref`` names an existing object, and this also holds a row
        that names it, so a put or delete that would free that row waits for this
        transaction, and its `shares_backend_id` check afterwards sees the new row. If
        ``key``'s own row names the object, its lock is the hold. Otherwise another
        row is locked FOR SHARE. If the object was freed first, no row matches, and this
        raises so the caller uploads its own copy. A false miss (the one matched row
        moved on under READ COMMITTED) only costs an upload.

        Rows are locked in key order, as `mark` locks them: a row naming the object
        below ``key`` first, then ``key``'s, then one above it. Two puts swapping
        objects between two keys then both start at the lower key, and one waits for
        the other instead of each holding its own key and waiting for the other's
        (#1605). That deadlock was retried, but the victim's retry could re-take its
        key before the winner took it and deadlock again. Only a hold that moved on
        between the two steps is taken out of order, and that rare deadlock is what
        `_retrying` is still for. The hold is never ``key``'s own row FOR SHARE, so two
        re-puts of one key cannot both share it and then deadlock on the upgrade.

        On the local backend the caller's own copy is the same path (`kind/sha`), so a
        release that already missed `shares_backend_id` can still unlink that re-upload
        before its row lands. No production path uses the local `ContentBackend` for
        this: the store builds a `ContentStore` only over Bambuddy, whose every upload
        is a new file."""
        where = "backend = %s AND backend_id = %s"
        held = reuse and (
            conn.execute(
                f"SELECT 1 FROM store_blobs WHERE {where} AND key < %s"
                " ORDER BY key LIMIT 1 FOR SHARE",
                (ref.backend, ref.backend_id, key),
            ).fetchone()
            is not None
        )
        own = conn.execute(
            f"SELECT {columns} FROM store_blobs WHERE key = %s FOR UPDATE", (key,)
        ).fetchone()
        if not reuse or held:
            return own
        if own is not None and (own["backend"], own["backend_id"]) == (ref.backend, ref.backend_id):
            return own
        above = conn.execute(
            f"SELECT 1 FROM store_blobs WHERE {where} AND key <> %s"
            " ORDER BY key DESC LIMIT 1 FOR SHARE",
            (ref.backend, ref.backend_id, key),
        ).fetchone()
        if above is None:
            raise ReuseLostError(f"{ref.backend}:{ref.backend_id} was freed while being reused")
        return own

    def _retrying[T](self, key: str, write: Callable[[Connection[DictRow]], T]) -> T:
        """Run ``write`` (its own transactions) on one pooled connection, again when
        Postgres breaks a deadlock with it. `_locked` takes its rows in key order, so
        only a hold it had to take out of order can deadlock. The last attempt's
        `DeadlockDetected` propagates."""
        with self._pool.connection() as conn:
            for attempt in range(1, _DEADLOCK_ATTEMPTS + 1):
                try:
                    return write(conn)
                except DeadlockDetected:
                    if attempt == _DEADLOCK_ATTEMPTS:
                        raise
                    logger.warning(
                        "retrying an index write Postgres picked as a deadlock's victim",
                        extra={"key": key, "attempt": attempt},
                    )
        raise AssertionError("unreachable")

    def put(
        self,
        key: str,
        ref: BlobRef,
        *,
        slug: str | None,
        meta: dict[str, Any],
        reuse: bool = False,
    ) -> BlobRef | None:
        """Point ``key`` at ``ref`` and return what it named before, read under the
        row's lock in the same transaction, so two racing puts each release exactly the
        object the other one replaced. A key another put created meanwhile is retried
        as an update: a single upsert would not see that row's object. With ``reuse``,
        ``ref`` names an existing object (`_locked`)."""
        values = (ref.sha256, ref.kind, ref.backend, ref.backend_id, ref.size, slug, Jsonb(meta))

        def write(conn: Connection[DictRow]) -> BlobRef | None:
            while True:
                with conn.transaction():
                    row = self._locked(conn, key, ref, reuse=reuse, columns=_COLUMNS)
                    if row is not None:
                        conn.execute(
                            "UPDATE store_blobs SET sha256 = %s, kind = %s, backend = %s,"
                            " backend_id = %s, size = %s, slug = %s, meta = %s,"
                            " touched_at = now() WHERE key = %s",
                            (*values, key),
                        )
                        return _stat(row).ref
                    inserted = conn.execute(
                        "INSERT INTO store_blobs"
                        " (sha256, kind, backend, backend_id, size, slug, meta, key)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (key) DO NOTHING",
                        (*values, key),
                    )
                    if inserted.rowcount == 1:
                        return None

        return self._retrying(key, write)

    def swap(
        self,
        key: str,
        ref: BlobRef,
        *,
        expected: str | None,
        slug: str | None,
        meta: dict[str, Any],
        reuse: bool = False,
    ) -> bool:
        """Point ``key`` at ``ref`` only if it still names ``expected`` (None: no row).
        With ``reuse``, ``ref`` names an existing object (`_locked`)."""
        values = (ref.sha256, ref.kind, ref.backend, ref.backend_id, ref.size, slug, Jsonb(meta))

        def write(conn: Connection[DictRow]) -> bool:
            with conn.transaction():
                self._locked(conn, key, ref, reuse=reuse, columns="backend, backend_id")
                if expected is None:
                    cursor = conn.execute(
                        "INSERT INTO store_blobs"
                        " (sha256, kind, backend, backend_id, size, slug, meta, key)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (key) DO NOTHING",
                        (*values, key),
                    )
                else:
                    cursor = conn.execute(
                        "UPDATE store_blobs SET sha256 = %s, kind = %s, backend = %s,"
                        " backend_id = %s, size = %s, slug = %s, meta = %s, touched_at = now()"
                        " WHERE key = %s AND sha256 = %s",
                        (*values, key, expected),
                    )
                return cursor.rowcount == 1

        return self._retrying(key, write)

    def now(self) -> datetime:
        """The database's clock, which every `touched_at` is stamped with."""
        with self._pool.connection() as conn:
            row = conn.execute("SELECT now() AS now").fetchone()
        assert row is not None
        now: datetime = row["now"]
        return now

    def mark(self, keys: Sequence[str], *, backend: str, cutoff: datetime, **meta: Any) -> None:
        """Merge ``meta`` into the rows of ``keys`` on ``backend`` not touched since
        ``cutoff``. A later `put` rewrites `meta`, which clears the mark."""
        where = "key = ANY(%s) AND backend = %s AND touched_at <= %s"
        args = (list(keys), backend, cutoff)

        def write(conn: Connection[DictRow]) -> None:
            with conn.transaction():
                # Keys first, in key order, as every other index write locks them; an
                # UPDATE over many rows would take them in scan order.
                conn.execute(
                    f"SELECT 1 FROM store_blobs WHERE {where} ORDER BY key FOR UPDATE", args
                )
                conn.execute(
                    f"UPDATE store_blobs SET meta = meta || %s WHERE {where}", (Jsonb(meta), *args)
                )

        self._retrying(",".join(sorted(keys)), write)

    def touch(self, key: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("UPDATE store_blobs SET touched_at = now() WHERE key = %s", (key,))

    def delete(self, key: str, *, backend: str | None = None) -> BlobStat | None:
        """Drop ``key``'s row; with ``backend``, only a row on that backend."""
        with self._pool.connection() as conn:
            row = conn.execute(
                f"DELETE FROM store_blobs WHERE key = %s"
                f" AND (%s::text IS NULL OR backend = %s) RETURNING {_COLUMNS}",
                (key, backend, backend),
            ).fetchone()
        return _stat(row) if row is not None else None

    def delete_if_stale(
        self, key: str, cutoff: datetime, *, backend: str | None = None
    ) -> BlobStat | None:
        """Atomic against a concurrent `touch`: a blob claimed after the sweep's snapshot
        has a fresh `touched_at` and is not matched. With ``backend``, only a row on it."""
        with self._pool.connection() as conn:
            row = conn.execute(
                f"DELETE FROM store_blobs WHERE key = %s AND touched_at <= %s"
                f" AND (%s::text IS NULL OR backend = %s) RETURNING {_COLUMNS}",
                (key, cutoff, backend, backend),
            ).fetchone()
        return _stat(row) if row is not None else None

    def shares_backend_id(self, backend: str, backend_id: str) -> bool:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM store_blobs WHERE backend = %s AND backend_id = %s)"
                " AS shared",
                (backend, backend_id),
            ).fetchone()
        return bool(row and row["shared"])

    def stats(
        self,
        kinds: Sequence[str] | None,
        slug: str | None = None,
        *,
        backend: str | None = None,
    ) -> list[BlobStat]:
        """Rows by kind, template and backend; None matches any."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM store_blobs"
                " WHERE (%s::text[] IS NULL OR kind = ANY(%s::text[]))"
                " AND (%s::text IS NULL OR slug = %s)"
                " AND (%s::text IS NULL OR backend = %s) ORDER BY key",
                (list(kinds) if kinds is not None else None,) * 2 + (slug, slug, backend, backend),
            ).fetchall()
        return [_stat(row) for row in rows]

    def usage(self, backend: str) -> tuple[int, int, dict[str, int]]:
        """Distinct stored objects and their bytes, in total and per kind."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT kind, count(*) AS n, coalesce(sum(size), 0) AS b FROM ("
                " SELECT DISTINCT ON (backend_id) backend_id, kind, size FROM store_blobs"
                " WHERE backend = %s) AS objects GROUP BY kind",
                (backend,),
            ).fetchall()
        by_kind = {row["kind"]: int(row["b"]) for row in rows}
        return sum(int(row["n"]) for row in rows), sum(by_kind.values()), by_kind
