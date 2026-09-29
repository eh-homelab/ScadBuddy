from __future__ import annotations

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool


class BlobRefs:
    """`blob_refs`: which holders (a job now; outputs and presets later) keep a blob."""

    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

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

    def referenced(self) -> set[str]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT DISTINCT key FROM blob_refs").fetchall()
        return {row["key"] for row in rows}
