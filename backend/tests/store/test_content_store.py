"""§6.2's byte store: dedupe, caps, integrity, compare-and-swap, sweep."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scadbuddy.store.content import (
    BlobCorruptError,
    BlobKind,
    BlobMissingError,
    BlobRef,
    BlobScope,
    RefusedDeleteError,
    StoreFullError,
    sweep_content,
)
from scadbuddy.store.index import Pool
from scadbuddy.store.local import LocalContentBackend
from scadbuddy.store.refs import BlobRefs
from tests.support.store import local_content

pytestmark = pytest.mark.requires_postgres
SCOPE = BlobScope(slug="demo", title="Demo")


async def test_put_then_read_and_the_same_bytes_are_stored_once(tmp_path: Path, pool: Pool) -> None:
    store = local_content(tmp_path / "remote", pool)
    first = await store.put("asset", b"<svg/>", name="a.svg", scope=SCOPE)
    stored = tmp_path / "remote" / first.backend_id
    os.utime(stored, ns=(1_000_000_000, 1_000_000_000))
    again = await store.put("asset", b"<svg/>", name="b.svg", scope=SCOPE)
    assert first == again
    assert stored.stat().st_mtime_ns == 1_000_000_000  # not uploaded a second time
    assert await store.read(first) == b"<svg/>"
    usage = store.usage()
    assert (usage.count, usage.bytes, usage.by_kind) == (1, 6, {"asset": 6})
    assert (await store.stat(f"asset-{first.sha256}")) is not None


async def test_put_refuses_past_the_cap_except_a_re_put(tmp_path: Path, pool: Pool) -> None:
    store = local_content(tmp_path / "remote", pool, max_count=1)
    await store.put("asset", b"one", name="1", scope=SCOPE)
    with pytest.raises(StoreFullError, match="SCADBUDDY_STORE_MAX_COUNT"):
        await store.put("asset", b"two", name="2", scope=SCOPE)
    await store.put("asset", b"one", name="1 again", scope=SCOPE)  # already stored: never refused


async def test_put_refuses_past_the_byte_cap_except_a_re_put(tmp_path: Path, pool: Pool) -> None:
    store = local_content(tmp_path / "remote", pool, max_total_bytes=5)
    await store.put("asset", b"abc", name="1", scope=SCOPE)
    with pytest.raises(StoreFullError, match="SCADBUDDY_STORE_MAX_TOTAL_BYTES"):
        await store.put("asset", b"def", name="2", scope=SCOPE)
    await store.put("asset", b"abc", name="1 again", scope=SCOPE)


async def test_altered_or_missing_bytes_are_reported_not_returned(
    tmp_path: Path, pool: Pool
) -> None:
    store = local_content(tmp_path / "remote", pool)
    ref = await store.put("piece", b"real", name="p.zip", scope=SCOPE, key="k")
    (tmp_path / "remote" / ref.backend_id).write_bytes(b"fake")
    with pytest.raises(BlobCorruptError):
        await store.read(ref)
    (tmp_path / "remote" / ref.backend_id).unlink()
    with pytest.raises(BlobMissingError):
        await store.read(ref)
    assert await store.stat("k") is None  # a vanished object drops its index row
    assert store.index.get("k") is None


async def test_replace_is_a_compare_and_swap(tmp_path: Path, pool: Pool) -> None:
    store = local_content(tmp_path / "remote", pool)
    v1 = await store.replace("k", "piece", b"v1", name="p", scope=SCOPE, expected=None)
    assert v1 is not None
    lost = await store.replace("k", "piece", b"other", name="p", scope=SCOPE, expected=None)
    assert lost is None
    assert not (tmp_path / "remote" / "piece" / hashlib.sha256(b"other").hexdigest()).exists()
    v2 = await store.replace("k", "piece", b"v2", name="p", scope=SCOPE, expected=v1.sha256)
    assert v2 is not None
    assert not (tmp_path / "remote" / v1.backend_id).exists()  # the superseded object is gone
    stale = await store.replace("k", "piece", b"v3", name="p", scope=SCOPE, expected=v1.sha256)
    assert stale is None
    named = store.index.get("k")
    assert named is not None and named.ref.sha256 == v2.sha256
    assert not (tmp_path / "remote" / "piece" / hashlib.sha256(b"v3").hexdigest()).exists()


async def test_delete_keeps_an_object_another_key_still_names(tmp_path: Path, pool: Pool) -> None:
    store = local_content(tmp_path / "remote", pool)
    ref = await store.put("snapshot", b"same", name="a", scope=SCOPE, key="a")
    await store.put("snapshot", b"same", name="b", scope=SCOPE, key="b")
    await store.delete("a")
    assert await store.read(ref) == b"same"
    await store.delete("b")
    assert not (tmp_path / "remote" / ref.backend_id).exists()


async def test_sweep_takes_only_unreferenced_stale_pieces_and_snapshots(
    tmp_path: Path, pool: Pool
) -> None:
    store = local_content(tmp_path / "remote", pool)
    refs = BlobRefs(pool)
    for key in ("kept", "fresh", "stale"):
        await store.put("piece", key.encode(), name=key, scope=SCOPE, key=key)
    await store.put("asset", b"asset", name="a.svg", scope=SCOPE, key="asset-x")
    refs.add("kept", "job", "j1")
    old = datetime.now(UTC) - timedelta(hours=2)
    with pool.connection() as conn:
        conn.execute(
            "UPDATE store_blobs SET touched_at = %s WHERE key IN ('kept', 'stale', 'asset-x')",
            (old,),
        )
    assert await sweep_content(store, refs, grace=3600, now=time.time()) == ["stale"]
    assert {s.key for s in store.index.stats(None)} == {"kept", "fresh", "asset-x"}
    assert not (tmp_path / "remote" / "piece" / hashlib.sha256(b"stale").hexdigest()).exists()


def _age(pool: Pool, *keys: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE store_blobs SET touched_at = %s WHERE key = ANY(%s)",
            (datetime.now(UTC) - timedelta(hours=2), list(keys)),
        )


async def test_the_sweep_leaves_another_backends_rows_alone(
    tmp_path: Path, pool: Pool, caplog: pytest.LogCaptureFixture
) -> None:
    store = local_content(tmp_path / "remote", pool)
    foreign = BlobRef(sha256="0" * 64, kind="piece", backend="bambuddy", backend_id="42", size=1)
    store.index.put("switched", foreign, slug="demo", meta={})
    await store.put("piece", b"stale", name="p", scope=SCOPE, key="stale")
    _age(pool, "switched", "stale")
    assert await sweep_content(store, BlobRefs(pool), grace=3600, now=time.time()) == ["stale"]
    assert store.index.get("switched") is not None
    assert [s.key async for s in store.list(SCOPE)] == []
    assert await store.stat("switched") is None  # not this backend's to check
    await store.delete("switched")  # nothing of this backend's to delete
    assert store.index.get("switched") is not None
    # A put over the switched key lands here and leaves the old object where it is.
    with caplog.at_level(logging.WARNING, logger="scadbuddy.store.content"):
        ref = await store.put("piece", b"new", name="p", scope=SCOPE, key="switched")
    named = store.index.get("switched")
    assert named is not None and named.ref == ref and ref.backend == "local"
    assert [r.message for r in caplog.records if getattr(r, "key", None) == "switched"] == [
        "left a replaced blob on another backend"
    ]


class _RefusingBackend(LocalContentBackend):
    def __init__(self, root: Path, refused: str) -> None:
        super().__init__(root)
        self.refused = refused

    async def remove(self, backend_id: str) -> None:
        if backend_id == self.refused:
            raise RefusedDeleteError(backend_id)
        await super().remove(backend_id)


async def test_a_refused_delete_is_logged_and_the_sweep_goes_on(
    tmp_path: Path, pool: Pool, caplog: pytest.LogCaptureFixture
) -> None:
    store = local_content(tmp_path / "remote", pool)
    refused = f"piece/{hashlib.sha256(b'a').hexdigest()}"
    store.backend = _RefusingBackend(tmp_path / "remote", refused)
    await store.put("piece", b"a", name="a", scope=SCOPE, key="a-refused")
    await store.put("piece", b"b", name="b", scope=SCOPE, key="b-stale")
    _age(pool, "a-refused", "b-stale")
    with caplog.at_level(logging.ERROR, logger="scadbuddy.store.content"):
        removed = await sweep_content(store, BlobRefs(pool), grace=3600, now=time.time())
    assert removed == ["b-stale"]
    refusals = [r for r in caplog.records if getattr(r, "key", None) == "a-refused"]
    assert len(refusals) == 1


class _GatedBackend(LocalContentBackend):
    """Holds each upload until both racing puts have uploaded."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.gate = asyncio.Barrier(2)

    async def upload(self, kind: BlobKind, data: bytes, *, name: str, scope: BlobScope) -> str:
        backend_id = await super().upload(kind, data, name=name, scope=scope)
        await self.gate.wait()
        return backend_id


@pytest.mark.parametrize("existing", [True, False], ids=["over-a-row", "new-key"])
async def test_racing_puts_to_one_key_leave_no_orphan(
    tmp_path: Path, pool: Pool, existing: bool
) -> None:
    root = tmp_path / "remote"
    store = local_content(root, pool)
    if existing:
        await store.put("piece", b"previous", name="p", scope=SCOPE, key="k")
    store.backend = _GatedBackend(root)
    await asyncio.gather(
        store.put("piece", b"A", name="p", scope=SCOPE, key="k"),
        store.put("piece", b"B", name="p", scope=SCOPE, key="k"),
    )
    named = store.index.get("k")
    assert named is not None
    assert [s.key for s in store.index.stats(None)] == ["k"]
    assert sorted(p.name for p in (root / "piece").iterdir()) == [
        named.ref.backend_id.removeprefix("piece/")
    ]


def test_a_local_object_id_with_a_trailing_newline_is_refused(tmp_path: Path) -> None:
    backend = LocalContentBackend(tmp_path)
    with pytest.raises(ValueError, match="not a local object id"):
        backend._path(f"piece/{'0' * 64}\n")


class _CancelledRemove(LocalContentBackend):
    async def remove(self, backend_id: str) -> None:
        raise asyncio.CancelledError


async def test_a_release_cancelled_midway_keeps_the_row(tmp_path: Path, pool: Pool) -> None:
    store = local_content(tmp_path / "remote", pool)
    await store.put("piece", b"p", name="p", scope=SCOPE, key="k")
    store.backend = _CancelledRemove(tmp_path / "remote")
    with pytest.raises(asyncio.CancelledError):
        await store.delete("k")
    assert store.index.get("k") is not None


async def test_a_failed_release_that_loses_to_a_put_releases_the_old_object(
    tmp_path: Path, pool: Pool
) -> None:
    store = local_content(tmp_path / "remote", pool)
    old = await store.put("piece", b"old", name="p", scope=SCOPE, key="k")

    class PutMeanwhile(LocalContentBackend):
        first = True

        async def remove(self, backend_id: str) -> None:
            if self.first:
                self.first = False
                await store.put("piece", b"new", name="p", scope=SCOPE, key="k")
                raise RuntimeError("Bambuddy is restarting")
            await super().remove(backend_id)

    store.backend = PutMeanwhile(tmp_path / "remote")
    with pytest.raises(RuntimeError, match="restarting"):
        await store.delete("k")
    row = store.index.get("k")
    assert row is not None and row.ref.sha256 == hashlib.sha256(b"new").hexdigest()
    assert not (tmp_path / "remote" / old.backend_id).exists()  # not left untracked
