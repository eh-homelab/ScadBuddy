"""A Postgres pool with every migration applied, and a store over a directory or S3."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from scadbuddy.library.settings_store import S3Settings
from scadbuddy.render.pg_store import migrate
from scadbuddy.store.content import ContentStore
from scadbuddy.store.content_models import BlobKind
from scadbuddy.store.index import BlobIndex, Pool
from scadbuddy.store.local import LocalContentBackend
from scadbuddy.store.s3 import S3ContentBackend, object_key


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


def s3_content(
    settings: S3Settings, pool: Pool, *, max_total_bytes: int = 0, max_count: int = 0
) -> ContentStore:
    return ContentStore(
        S3ContentBackend(settings),
        BlobIndex(pool),
        max_total_bytes=max_total_bytes,
        max_count=max_count,
    )


def object_id(store: ContentStore, kind: BlobKind, data: bytes) -> str:
    """The id ``store``'s backend gives ``data`` stored as ``kind``, stored or not."""
    sha256 = hashlib.sha256(data).hexdigest()
    backend = store.backend
    if isinstance(backend, S3ContentBackend):
        return object_key(backend.settings.prefix, kind, sha256)
    return f"{kind}/{sha256}"


async def overwrite_object(store: ContentStore, backend_id: str, data: bytes) -> None:
    """Replace a stored object's bytes behind the store's back."""
    backend = store.backend
    if isinstance(backend, S3ContentBackend):
        client = await backend.client()
        await client.put_object(Bucket=backend.settings.bucket, Key=backend_id, Body=data)
    else:
        assert isinstance(backend, LocalContentBackend)
        (backend.root / backend_id).write_bytes(data)
