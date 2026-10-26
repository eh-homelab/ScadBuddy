"""An attachment's bytes as a blob (#4677), and what the table refuses (#4678)."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path

import psycopg
import pytest

from scadbuddy.operations.claims import ClaimStore
from scadbuddy.orders.attachments import BLOB_FILE, blob_key, blob_path, keep
from scadbuddy.orders.store import ATTACHMENT_HOLDER, OrderStore, order_subject
from scadbuddy.store.cache import CachedBlobStore
from scadbuddy.store.content import ContentStore
from scadbuddy.store.local import LocalBlobStore
from tests.conftest import PgPool
from tests.orders.test_store import BROWSER, make_order
from tests.support.store import local_content

pytestmark = pytest.mark.requires_postgres

VIDEO = b"\0\0\0\x18ftypisom" + b"v" * 4096
SHA = hashlib.sha256(VIDEO).hexdigest()


@pytest.fixture
async def content(tmp_path: Path, pool: PgPool) -> AsyncIterator[ContentStore]:
    store = local_content(tmp_path / "remote", pool)
    try:
        yield store
    finally:
        await store.aclose()


def replica(root: Path, content: ContentStore) -> CachedBlobStore:
    return CachedBlobStore(LocalBlobStore(root / "blobs"), content, max_bytes=1 << 30, min_age=0)


def claim(root: Path) -> tuple[ClaimStore, str]:
    claims = ClaimStore(root / "claims")
    claims.hold(VIDEO)
    return claims, SHA


async def test_bytes_stored_already_are_kept_without_downloading_them(
    tmp_path: Path, content: ContentStore, pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = replica(tmp_path / "a", content), replica(tmp_path / "b", content)
    claims, sha = claim(tmp_path)
    key = await keep(a, claims, sha, sha)
    assert key == blob_key(SHA)
    with pool.connection() as conn:
        conn.execute("UPDATE store_blobs SET touched_at = now() - interval '1 day'")
    stale = content.index.get(key)
    assert stale is not None

    downloads = content.backend.download
    calls = 0

    def counting(backend_id: str) -> AsyncGenerator[bytes]:
        nonlocal calls
        calls += 1
        return downloads(backend_id)

    monkeypatch.setattr(content.backend, "download", counting)
    # Another replica, whose cache never held it: the same bytes are its blob as it is,
    # and not one of them is downloaded to learn that (a video runs to a gigabyte).
    assert await keep(b, claims, sha, sha) == key
    assert calls == 0
    assert not (b.local.root / key / BLOB_FILE).exists()
    # Touched, so the sweep's grace keeps it until the second row's hold commits.
    touched = content.index.get(key)
    assert touched is not None and touched.touched_at > stale.touched_at
    # Served from b, it is fetched then.
    path = await blob_path(b, key)
    assert path is not None and path.read_bytes() == VIDEO
    assert calls == 1


async def test_a_local_blob_left_half_written_is_written_again(tmp_path: Path) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    claims, sha = claim(tmp_path)
    key = blob_key(SHA)
    # `dir_for` ran for a write that never finished: an empty directory, and a stray
    # staging file, hold no blob.
    (blobs.dir_for(key) / f".{BLOB_FILE}.dead").write_bytes(b"part")
    assert not await blobs.claim(key)
    assert await keep(blobs, claims, sha, sha) == key
    assert (blobs.dir_for(key) / BLOB_FILE).read_bytes() == VIDEO
    assert await blobs.claim(key)
    assert not await blobs.claim(blob_key("0" * 64))
    # A file where a blob's directory would be is no blob either, as for `exists`.
    stray = blob_key("1" * 64)
    (blobs.root / stray).write_bytes(b"x")
    assert not await blobs.claim(stray)


def attached(rows: OrderStore) -> tuple[str, str]:
    """An order with one file on it: the order's id and the attachment's."""
    customer = rows.insert_customer(
        display_name="Ada", company=None, notes="", tags=[], contacts=[], addresses=[]
    )
    order = make_order(rows, customer.id)
    with rows.transaction() as conn:
        attachment = rows.insert_attachment(
            conn,
            attachment_id="a1",
            order_id=order.id,
            subject=order_subject(order.id),
            kind="video",
            role="other",
            filename="clip.mp4",
            media_type="video/mp4",
            size_bytes=len(VIDEO),
            sha256=SHA,
            blob_key=blob_key(SHA),
            caption="",
            created_by=BROWSER,
        )
    return order.id, attachment.id


def test_an_order_is_not_deleted_from_under_its_files_holds(rows: OrderStore) -> None:
    """#4678: nothing cascades a `blob_refs` hold, so an order's delete is refused
    until each of its files went through `OrderStore.delete_attachment`."""
    order_id, attachment_id = attached(rows)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), rows.transaction() as conn:
        conn.execute("DELETE FROM orders WHERE id = %s", (order_id,))
    with rows.transaction() as conn:
        rows.delete_attachment(conn, attachment_id)
        conn.execute("DELETE FROM orders WHERE id = %s", (order_id,))
        holds = conn.execute(
            "SELECT count(*) AS n FROM blob_refs WHERE holder_kind = %s", (ATTACHMENT_HOLDER,)
        ).fetchone()
    assert holds is not None and holds["n"] == 0


def test_the_old_table_name_reads_and_writes_while_the_api_rolls_out(
    rows: OrderStore, pool: PgPool
) -> None:
    """#4678's rename keeps the name `attachments` as a view (#4754 drops it), so the
    previous build's replicas read and write the same rows until they stop."""
    order_id, attachment_id = attached(rows)
    with pool.connection() as conn:
        conn.execute("UPDATE attachments SET caption = 'old build' WHERE id = %s", (attachment_id,))
        conn.execute(
            "INSERT INTO attachments (id, order_id, subject, kind, role, filename, media_type,"
            " size_bytes, sha256, blob_key, caption, created_by)"
            " VALUES ('a2', %s, %s, 'other', 'other', 'x.bin', 'application/octet-stream',"
            " 1, %s, %s, '', %s) RETURNING id",
            (order_id, order_subject(order_id), SHA, blob_key(SHA), '{"kind": "browser"}'),
        )
        listed = conn.execute(
            "SELECT id FROM attachments WHERE order_id = %s ORDER BY created_at", (order_id,)
        ).fetchall()
    assert [row["id"] for row in listed] == [attachment_id, "a2"]
    assert [a.caption for a in rows.attachments(order_id)] == ["old build", ""]
