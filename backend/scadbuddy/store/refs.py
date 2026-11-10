from __future__ import annotations

from collections.abc import Collection
from datetime import timedelta
from typing import Protocol

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool


class HoldsBlobs(Protocol):
    """What a render worker's activities call on its blob refs (`WorkerDeps.refs`):
    `BlobRefs` over SQL, `crud/render.py` `RemoteBlobRefs` over the API's routes, and
    `workflows/verify_pipeline.py`'s, which holds nothing."""

    def add(
        self, key: str, holder_kind: str, holder_id: str, *, budget: float | None = None
    ) -> None: ...


class BlobRefs:
    """`blob_refs`: which holders (a job now; outputs and presets later) keep a blob.

    The pool must be autocommit, as the render queue's is: every write here is a bare
    `execute`, with no transaction or commit of its own."""

    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

    @property
    def pool(self) -> ConnectionPool[Connection[DictRow]]:
        return self._pool

    def add(
        self, key: str, holder_kind: str, holder_id: str, *, budget: float | None = None
    ) -> None:
        """``budget`` is `crud/render.py` `RemoteBlobRefs.add`'s, ignored here: the
        connection waits the pool's own timeout."""
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES (%s, %s, %s)"
                " ON CONFLICT DO NOTHING",
                (key, holder_kind, holder_id),
            )

    def hold(self, key: str, holder_kind: str, holder_id: str) -> None:
        """``add``, except that a row the holder already has gets a fresh ``created_at``,
        so a reaper's grace counts from this hold (#5353: a quote's next attempt takes
        its holds again). Needs ``UPDATE`` on the table, which ``add`` does not."""
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES (%s, %s, %s)"
                " ON CONFLICT (key, holder_kind, holder_id) DO UPDATE SET created_at = now()",
                (key, holder_kind, holder_id),
            )

    def drop_holder(self, holder_kind: str, holder_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM blob_refs WHERE holder_kind = %s AND holder_id = %s",
                (holder_kind, holder_id),
            )

    def release(
        self, key: str, holder_kind: str, holder_id: str, *, older_than: timedelta | None = None
    ) -> bool:
        """Drop one holder's row of ``key``; whether there was one. With ``older_than``,
        only a row held longer ago than that (a hold taken again since is kept)."""
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM blob_refs WHERE key = %s AND holder_kind = %s AND holder_id = %s"
                " AND (%s::interval IS NULL OR created_at < now() - %s::interval)",
                (key, holder_kind, holder_id, older_than, older_than),
            )
            return cursor.rowcount > 0

    def held_by_kind(
        self,
        holder_kind: str,
        *,
        older_than: timedelta,
        after: tuple[str, str] | None = None,
        limit: int,
    ) -> list[tuple[str, str]]:
        """``(holder_id, key)`` of the ``holder_kind`` rows held longer ago than
        ``older_than``, in that order, from past ``after``; at most ``limit``."""
        holder, key = after if after is not None else ("", "")
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT holder_id, key FROM blob_refs WHERE holder_kind = %s"
                " AND created_at < now() - %s AND (holder_id, key) > (%s, %s)"
                " ORDER BY holder_id, key LIMIT %s",
                (holder_kind, older_than, holder, key, limit),
            ).fetchall()
        return [(row["holder_id"], row["key"]) for row in rows]

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
