from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scadbuddy.api.deps import AppState
from scadbuddy.library.assets import AssetMeta, AssetRejectedError, AssetStore, asset_ids_in
from scadbuddy.main import drop_swept_assets
from scadbuddy.store.assets import RemoteAssets, asset_key
from scadbuddy.store.content import BlobRef
from scadbuddy.store.index import Pool
from scadbuddy.store.local import LocalContentBackend
from tests.conftest import PgPool
from tests.support.store import local_content

pytestmark = pytest.mark.requires_postgres
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="4" height="4"/></svg>'


async def test_an_upload_on_the_api_is_readable_on_a_worker(
    tmp_path: Path, pool: Pool, pg_pool: PgPool
) -> None:
    remote = RemoteAssets(local_content(tmp_path / "remote", pool))
    api = AssetStore(tmp_path / "api" / "assets", pg_pool)
    meta = api.put(SVG, "logo.svg")
    await remote.mirror(api, meta, slug="demo", title="Demo")
    worker = AssetStore(tmp_path / "worker" / "assets", pg_pool)
    params = {"logo": meta.id, "width": 3}
    assert await remote.ensure(worker, asset_ids_in(params)) == []  # fetched
    assert worker.get(meta.id) == meta
    assert worker.blob_path(meta).read_bytes() == api.blob_path(meta).read_bytes()
    assert await remote.ensure(worker, [meta.id]) == []  # already local: no download
    assert await remote.ensure(worker, ["f" * 64]) == ["f" * 64]  # the render reports it
    stored = tmp_path / "remote" / f"asset/{meta.id}"
    assert stored.is_file()
    assert await remote.drop([meta.id], cutoff=await remote.clock()) == [meta.id]
    assert remote.content.index.get(asset_key(meta.id)) is None
    assert not stored.exists()


async def test_backfill_mirrors_uploads_made_before_the_store(
    tmp_path: Path, pool: Pool, pg_pool: PgPool
) -> None:
    remote = RemoteAssets(local_content(tmp_path / "remote", pool))
    api = AssetStore(tmp_path / "assets", pg_pool)
    meta = api.put(SVG, "old.svg")
    assert await remote.backfill(api) == 1
    assert await remote.backfill(api) == 0
    assert remote.content.index.get(asset_key(meta.id)) is not None


def test_adopt_refuses_bytes_that_are_not_the_id(tmp_path: Path) -> None:
    store = AssetStore(tmp_path / "assets")
    meta = AssetMeta(id="0" * 64, name="x.svg", kind="svg", size=3)
    with pytest.raises(AssetRejectedError):
        store.adopt(meta, b"abc")


def _age(pool: Pool, key: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE store_blobs SET touched_at = %s WHERE key = %s",
            (datetime.now(UTC) - timedelta(hours=2), key),
        )


async def test_a_re_upload_during_the_sweep_keeps_its_row(
    tmp_path: Path, pool: Pool, pg_pool: PgPool
) -> None:
    remote = RemoteAssets(local_content(tmp_path / "remote", pool))
    api = AssetStore(tmp_path / "api" / "assets", pg_pool)
    meta = api.put(SVG, "logo.svg")
    await remote.mirror(api, meta, slug="demo", title="Demo")
    _age(pool, asset_key(meta.id))
    cutoff = await remote.clock()  # the sweep starts, and removes the upload locally
    await remote.mirror(api, api.put(SVG, "logo.svg"), slug="demo", title="Demo")  # again
    assert await remote.drop([meta.id], cutoff=cutoff) == []
    assert remote.content.index.get(asset_key(meta.id)) is not None


class _FlakyBackend(LocalContentBackend):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.failures = 1

    async def remove(self, backend_id: str) -> None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("Bambuddy is restarting")
        await super().remove(backend_id)


async def test_a_failed_drop_is_retried_by_the_next_sweep(
    tmp_path: Path, pool: Pool, pg_pool: PgPool
) -> None:
    content = local_content(tmp_path / "remote", pool)
    content.backend = _FlakyBackend(tmp_path / "remote")
    remote = RemoteAssets(content)
    api = AssetStore(tmp_path / "api" / "assets", pg_pool)
    first, second = api.put(SVG, "a.svg"), api.put(SVG.replace(b"4", b"5"), "b.svg")
    for meta in (first, second):
        await remote.mirror(api, meta, slug="demo", title="Demo")
        _age(pool, asset_key(meta.id))
    swept = AssetStore(tmp_path / "swept" / "assets", pg_pool)  # the API after its sweep: neither
    dropped = await remote.drop(sorted([first.id, second.id]), cutoff=await remote.clock())
    assert len(dropped) == 1  # one failed and was kept; the other still went
    kept = next(i for i in (first.id, second.id) if i not in dropped)
    assert remote.content.index.get(asset_key(kept)) is not None
    assert await remote.reconcile(swept, cutoff=await remote.clock()) == [kept]
    assert remote.content.index.stats(["asset"]) == []


async def test_ensure_leaves_another_backends_row_and_drops_a_corrupt_copy(
    tmp_path: Path, pool: Pool, pg_pool: PgPool
) -> None:
    remote = RemoteAssets(local_content(tmp_path / "remote", pool))
    api = AssetStore(tmp_path / "api" / "assets", pg_pool)
    meta = api.put(SVG, "logo.svg")
    await remote.mirror(api, meta, slug="demo", title="Demo")
    (tmp_path / "remote" / f"asset/{meta.id}").write_bytes(b"altered")
    foreign_id = "e" * 64
    foreign = BlobRef(sha256=foreign_id, kind="asset", backend="bambuddy", backend_id="7", size=1)
    remote.content.index.put(asset_key(foreign_id), foreign, slug=None, meta={})
    worker = AssetStore(tmp_path / "worker" / "assets", pg_pool)
    assert await remote.ensure(worker, [meta.id, foreign_id]) == sorted([meta.id, foreign_id])
    assert remote.content.index.get(asset_key(foreign_id)) is not None
    assert remote.content.index.get(asset_key(meta.id)) is None  # dropped, object too
    assert not (tmp_path / "remote" / f"asset/{meta.id}").exists()


async def test_backfill_re_mirrors_a_row_on_another_backend(
    tmp_path: Path, pool: Pool, pg_pool: PgPool
) -> None:
    remote = RemoteAssets(local_content(tmp_path / "remote", pool))
    api = AssetStore(tmp_path / "assets", pg_pool)
    meta = api.put(SVG, "old.svg")
    foreign = BlobRef(sha256=meta.id, kind="asset", backend="bambuddy", backend_id="7", size=1)
    remote.content.index.put(asset_key(meta.id), foreign, slug=None, meta={})
    assert await remote.backfill(api) == 1
    row = remote.content.index.get(asset_key(meta.id))
    assert row is not None and row.ref.backend == "local"


async def test_backfill_skips_an_upload_swept_meanwhile(
    tmp_path: Path, pool: Pool, pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = RemoteAssets(local_content(tmp_path / "remote", pool))
    api = AssetStore(tmp_path / "assets", pg_pool)
    kept = api.put(SVG, "a.svg")
    monkeypatch.setattr(api, "ids", lambda: ["d" * 64, kept.id])  # one swept since
    assert await remote.backfill(api) == 1


async def test_drop_swept_assets_drops_the_stores_copies(
    tmp_path: Path, pool: Pool, pg_pool: PgPool
) -> None:
    remote = RemoteAssets(local_content(tmp_path / "remote", pool))
    api = AssetStore(tmp_path / "api" / "assets", pg_pool)
    meta = api.put(SVG, "logo.svg")
    await remote.mirror(api, meta, slug="demo", title="Demo")
    state = cast(AppState, SimpleNamespace(store=SimpleNamespace(remote_assets=remote)))
    await drop_swept_assets(state, [meta.id], cutoff=await remote.clock())
    assert remote.content.index.get(asset_key(meta.id)) is None


async def test_nothing_to_drop_on_the_local_store(tmp_path: Path) -> None:
    state: Any = SimpleNamespace(store=SimpleNamespace(remote_assets=None))
    await drop_swept_assets(cast(AppState, state), ["a" * 64], cutoff=datetime.now(UTC))


def _mark_swept(pool: Pool, key: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE store_blobs SET meta = meta || '{\"swept\": true}' WHERE key = %s", (key,)
        )


async def test_reconcile_leaves_a_copy_the_sweep_never_decided_to_drop(
    tmp_path: Path, pool: Pool, pg_pool: PgPool
) -> None:
    """A volume restored from a backup lacks recent uploads: their copies are all left."""
    remote = RemoteAssets(local_content(tmp_path / "remote", pool))
    api = AssetStore(tmp_path / "api" / "assets", pg_pool)
    meta = api.put(SVG, "logo.svg")
    await remote.mirror(api, meta, slug="demo", title="Demo")
    _age(pool, asset_key(meta.id))
    restored = AssetStore(tmp_path / "restored" / "assets", pg_pool)
    assert await remote.reconcile(restored, cutoff=await remote.clock()) == []
    assert remote.content.index.get(asset_key(meta.id)) is not None


async def test_reconcile_spares_a_marked_copy_touched_after_the_cutoff(
    tmp_path: Path, pool: Pool, pg_pool: PgPool
) -> None:
    remote = RemoteAssets(local_content(tmp_path / "remote", pool))
    api = AssetStore(tmp_path / "api" / "assets", pg_pool)
    cutoff = await remote.clock()
    meta = api.put(SVG, "logo.svg")
    await remote.mirror(api, meta, slug="demo", title="Demo")  # after the cutoff
    _mark_swept(pool, asset_key(meta.id))
    elsewhere = AssetStore(tmp_path / "elsewhere" / "assets", pg_pool)  # not (yet) local here
    assert await remote.reconcile(elsewhere, cutoff=cutoff) == []
    assert remote.content.index.get(asset_key(meta.id)) is not None


async def test_a_re_upload_clears_the_swept_mark(
    tmp_path: Path, pool: Pool, pg_pool: PgPool
) -> None:
    remote = RemoteAssets(local_content(tmp_path / "remote", pool))
    api = AssetStore(tmp_path / "api" / "assets", pg_pool)
    meta = api.put(SVG, "logo.svg")
    await remote.mirror(api, meta, slug="demo", title="Demo")
    _mark_swept(pool, asset_key(meta.id))
    await remote.mirror(api, api.put(SVG, "logo.svg"), slug="demo", title="Demo")
    row = remote.content.index.get(asset_key(meta.id))
    assert row is not None and "swept" not in row.meta


async def test_ensure_marks_a_local_upload_used_so_the_workers_sweep_keeps_it(
    tmp_path: Path, pool: Pool, pg_pool: PgPool
) -> None:
    """Final review I2: a worker prunes its uploads by last use, so a hit is a use."""
    remote = RemoteAssets(local_content(tmp_path / "remote", pool))
    worker = AssetStore(tmp_path / "worker" / "assets", pg_pool)
    meta = worker.put(SVG, "logo.svg")
    old = time.time() - 7 * 86400
    os.utime(worker.blob_path(meta), (old, old))
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE assets SET last_used_at = %s WHERE id = %s",
            (datetime.now(UTC) - timedelta(days=7), meta.id),
        )
    assert await remote.ensure(worker, [meta.id]) == []  # a hit
    assert worker.prune_local(grace=3600) == []
    assert worker.get(meta.id) == meta
