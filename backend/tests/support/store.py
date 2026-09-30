"""A Postgres pool with every migration applied, and a store over a directory."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from scadbuddy.render.pg_store import migrate
from scadbuddy.store.content import ContentStore
from scadbuddy.store.index import BlobIndex, Pool
from scadbuddy.store.local import LocalContentBackend


@contextmanager
def store_pool(conninfo: str) -> Iterator[Pool]:
    pool: Pool = ConnectionPool(
        conninfo,
        min_size=1,
        max_size=6,
        open=True,
        connection_class=Connection[DictRow],
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    try:
        with pool.connection() as conn:
            migrate(conn)
        yield pool
    finally:
        pool.close()


def local_content(
    root: Path, pool: Pool, *, max_total_bytes: int = 0, max_count: int = 0
) -> ContentStore:
    return ContentStore(
        LocalContentBackend(root),
        BlobIndex(pool),
        max_total_bytes=max_total_bytes,
        max_count=max_count,
    )
