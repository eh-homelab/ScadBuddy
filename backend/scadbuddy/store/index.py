"""`store_blobs`: which blob a key names, where its bytes are, when it was last wanted."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.store.content_models import BlobRef, BlobStat

Pool = ConnectionPool[Connection[DictRow]]
_COLUMNS = "key, sha256, kind, backend, backend_id, size, slug, meta, touched_at"


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

    def put(self, key: str, ref: BlobRef, *, slug: str | None, meta: dict[str, Any]) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO store_blobs (key, sha256, kind, backend, backend_id, size, slug, meta)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (key) DO UPDATE SET"
                " sha256 = EXCLUDED.sha256, kind = EXCLUDED.kind, backend = EXCLUDED.backend,"
                " backend_id = EXCLUDED.backend_id, size = EXCLUDED.size, slug = EXCLUDED.slug,"
                " meta = EXCLUDED.meta, touched_at = now()",
                (
                    key,
                    ref.sha256,
                    ref.kind,
                    ref.backend,
                    ref.backend_id,
                    ref.size,
                    slug,
                    Jsonb(meta),
                ),
            )

    def swap(
        self,
        key: str,
        ref: BlobRef,
        *,
        expected: str | None,
        slug: str | None,
        meta: dict[str, Any],
    ) -> bool:
        """Point ``key`` at ``ref`` only if it still names ``expected`` (None: no row)."""
        values = (ref.sha256, ref.kind, ref.backend, ref.backend_id, ref.size, slug, Jsonb(meta))
        with self._pool.connection() as conn:
            if expected is None:
                cursor = conn.execute(
                    "INSERT INTO store_blobs"
                    " (sha256, kind, backend, backend_id, size, slug, meta, key)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (key) DO NOTHING",
                    (*values, key),
                )
            else:
                cursor = conn.execute(
                    "UPDATE store_blobs SET sha256 = %s, kind = %s, backend = %s, backend_id = %s,"
                    " size = %s, slug = %s, meta = %s, touched_at = now()"
                    " WHERE key = %s AND sha256 = %s",
                    (*values, key, expected),
                )
            return cursor.rowcount == 1

    def touch(self, key: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("UPDATE store_blobs SET touched_at = now() WHERE key = %s", (key,))

    def delete(self, key: str) -> BlobStat | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"DELETE FROM store_blobs WHERE key = %s RETURNING {_COLUMNS}", (key,)
            ).fetchone()
        return _stat(row) if row is not None else None

    def delete_if_stale(self, key: str, cutoff: datetime) -> BlobStat | None:
        """Atomic against a concurrent `touch`: a blob claimed after the sweep's snapshot
        has a fresh `touched_at` and is not matched."""
        with self._pool.connection() as conn:
            row = conn.execute(
                f"DELETE FROM store_blobs WHERE key = %s AND touched_at <= %s RETURNING {_COLUMNS}",
                (key, cutoff),
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

    def stats(self, kinds: Sequence[str] | None, slug: str | None = None) -> list[BlobStat]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM store_blobs"
                " WHERE (%s::text[] IS NULL OR kind = ANY(%s::text[]))"
                " AND (%s::text IS NULL OR slug = %s) ORDER BY key",
                (list(kinds) if kinds is not None else None,) * 2 + (slug, slug),
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
