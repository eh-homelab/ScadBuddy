"""§6.2's byte store: dedupe, caps, integrity, compare-and-swap, sweep."""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scadbuddy.store.content import (
    BlobCorruptError,
    BlobMissingError,
    BlobScope,
    StoreFullError,
    sweep_content,
)
from scadbuddy.store.index import Pool
from scadbuddy.store.refs import BlobRefs
from tests.support.store import local_content

pytestmark = pytest.mark.requires_postgres
SCOPE = BlobScope(slug="demo", title="Demo")


async def test_put_then_read_and_the_same_bytes_are_stored_once(tmp_path: Path, pool: Pool) -> None:
    store = local_content(tmp_path / "remote", pool)
    first = await store.put("asset", b"<svg/>", name="a.svg", scope=SCOPE)
    again = await store.put("asset", b"<svg/>", name="b.svg", scope=SCOPE)
    assert first == again
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
    assert v2 is not None and store.index.get("k") is not None
    assert not (tmp_path / "remote" / v1.backend_id).exists()  # the superseded object is gone


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
