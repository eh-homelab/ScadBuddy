from __future__ import annotations

from collections.abc import Collection
from datetime import timedelta

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool


class BlobRefs:
    """`blob_refs`: which holders (a job now; outputs and presets later) keep a blob.

    The pool must be autocommit, as the render queue's is: every write here is a bare
    `execute`, with no transaction or commit of its own."""

    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

    @property
    def pool(self) -> ConnectionPool[Connection[DictRow]]:
        return self._pool

    def add(self, key: str, holder_kind: str, holder_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES (%s, %s, %s)"
                " ON CONFLICT DO NOTHING",
                (key, holder_kind, holder_id),
            )

    def drop_holder(self, holder_kind: str, holder_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM blob_refs WHERE holder_kind = %s AND holder_id = %s",
                (holder_kind, holder_id),
            )

    def drop_unlisted(
        self, holder_kind: str, keep: Collection[str], *, older_than: timedelta
    ) -> int:
        """Drop every ``holder_kind`` row whose holder is not in ``keep`` and that was
        added more than ``older_than`` ago (a younger one may be a holder about to write
        the row that keeps it); how many went."""
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM blob_refs WHERE holder_kind = %s AND NOT (holder_id = ANY(%s))"
                " AND created_at < now() - %s",
                (holder_kind, list(keep), older_than),
            )
            return cursor.rowcount

    def referenced(self) -> set[str]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT DISTINCT key FROM blob_refs").fetchall()
        return {row["key"] for row in rows}
