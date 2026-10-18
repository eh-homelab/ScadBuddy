"""A Postgres pool with every migration applied, and a store over a directory or S3.

The ``pool``, ``s3`` and ``content`` fixtures are shared: a conftest takes them with
``from tests.support.store import content, pool, s3  # noqa: F401``."""

from __future__ import annotations

import hashlib
import socket
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
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


@pytest.fixture
def pool(pg_conninfo: str) -> Iterator[Pool]:
    with store_pool(pg_conninfo) as opened:
        yield opened


@pytest.fixture
def s3() -> Iterator[S3Settings]:
    """moto's S3 on a free port with the bucket ``sb-test`` (S3 needs three characters or more)."""
    from moto.server import ThreadedMotoServer

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    settings = S3Settings(
        endpoint=f"http://127.0.0.1:{port}",
        bucket="sb-test",
        prefix="t",
        access_key_id="testing",
        secret_access_key="testing",
    )
    httpx.put(f"{settings.endpoint}/sb-test").raise_for_status()
    try:
        yield settings
    finally:
        # moto's state is the process's, not the server's: the next test starts empty.
        httpx.post(f"{settings.endpoint}/moto-api/reset").raise_for_status()
        server.stop()


@pytest.fixture(params=["local", "s3"], ids=["local", "s3"])
async def content(
    request: pytest.FixtureRequest, tmp_path: Path, pool: Pool
) -> AsyncIterator[ContentStore]:
    """A content store over a directory, then over moto's S3."""
    if request.param == "local":
        store = local_content(tmp_path / "remote", pool)
    else:
        store = s3_content(request.getfixturevalue("s3"), pool)
    try:
        yield store
    finally:
        await store.aclose()
        if isinstance(store.backend, S3ContentBackend):
            await store.backend.aclose()
