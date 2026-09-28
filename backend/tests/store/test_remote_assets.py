from __future__ import annotations

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
from tests.support.store import local_content

pytestmark = pytest.mark.requires_postgres
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="4" height="4"/></svg>'


async def test_an_upload_on_the_api_is_readable_on_a_worker(tmp_path: Path, pool: Pool) -> None:
    remote = RemoteAssets(local_content(tmp_path / "remote", pool))
    api = AssetStore(tmp_path / "api" / "assets")
    meta = api.put(SVG, "logo.svg")
    await remote.mirror(api, meta, slug="demo", title="Demo")
    worker = AssetStore(tmp_path / "worker" / "assets")
    params = {"logo": meta.id, "width": 3}
    assert await remote.ensure(worker, asset_ids_in(params)) == [meta.id]
    assert worker.get(meta.id) == meta
    assert worker.blob_path(meta).read_bytes() == api.blob_path(meta).read_bytes()
    assert await remote.ensure(worker, [meta.id]) == []  # already local: no download
    assert await remote.ensure(worker, ["f" * 64]) == []  # unknown id: the render reports it
    stored = tmp_path / "remote" / f"asset/{meta.id}"
    assert stored.is_file()
    assert await remote.drop([meta.id], cutoff=await remote.clock()) == [meta.id]
    assert remote.content.index.get(asset_key(meta.id)) is None
    assert not stored.exists()


async def test_backfill_mirrors_uploads_made_before_the_store(tmp_path: Path, pool: Pool) -> None:
    remote = RemoteAssets(local_content(tmp_path / "remote", pool))
    api = AssetStore(tmp_path / "assets")
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


async def test_a_re_upload_during_the_sweep_keeps_its_row(tmp_path: Path, pool: Pool) -> None:
    remote = RemoteAssets(local_content(tmp_path / "remote", pool))
    api = AssetStore(tmp_path / "api" / "assets")
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


async def test_a_failed_drop_is_retried_by_the_next_sweep(tmp_path: Path, pool: Pool) -> None:
    content = local_content(tmp_path / "remote", pool)
    content.backend = _FlakyBackend(tmp_path / "remote")
    remote = RemoteAssets(content)
    api = AssetStore(tmp_path / "api" / "assets")
    first, second = api.put(SVG, "a.svg"), api.put(SVG.replace(b"4", b"5"), "b.svg")
    for meta in (first, second):
        await remote.mirror(api, meta, slug="demo", title="Demo")
        _age(pool, asset_key(meta.id))
    swept = AssetStore(tmp_path / "swept" / "assets")  # the API after its sweep: neither
    dropped = await remote.drop(sorted([first.id, second.id]), cutoff=await remote.clock())
    assert len(dropped) == 1  # one failed and was kept; the other still went
    kept = next(i for i in (first.id, second.id) if i not in dropped)
    assert remote.content.index.get(asset_key(kept)) is not None
    assert await remote.reconcile(swept, cutoff=await remote.clock()) == [kept]
    assert remote.content.index.stats(["asset"]) == []


async def test_ensure_leaves_another_backends_row_and_drops_a_corrupt_copy(
    tmp_path: Path, pool: Pool
) -> None:
    remote = RemoteAssets(local_content(tmp_path / "remote", pool))
    api = AssetStore(tmp_path / "api" / "assets")
    meta = api.put(SVG, "logo.svg")
    await remote.mirror(api, meta, slug="demo", title="Demo")
    (tmp_path / "remote" / f"asset/{meta.id}").write_bytes(b"altered")
    foreign_id = "e" * 64
    foreign = BlobRef(sha256=foreign_id, kind="asset", backend="bambuddy", backend_id="7", size=1)
    remote.content.index.put(asset_key(foreign_id), foreign, slug=None, meta={})
    worker = AssetStore(tmp_path / "worker" / "assets")
    assert await remote.ensure(worker, [meta.id, foreign_id]) == []
    assert remote.content.index.get(asset_key(foreign_id)) is not None
    assert remote.content.index.get(asset_key(meta.id)) is None  # dropped, object too
    assert not (tmp_path / "remote" / f"asset/{meta.id}").exists()


async def test_backfill_re_mirrors_a_row_on_another_backend(tmp_path: Path, pool: Pool) -> None:
    remote = RemoteAssets(local_content(tmp_path / "remote", pool))
    api = AssetStore(tmp_path / "assets")
    meta = api.put(SVG, "old.svg")
    foreign = BlobRef(sha256=meta.id, kind="asset", backend="bambuddy", backend_id="7", size=1)
    remote.content.index.put(asset_key(meta.id), foreign, slug=None, meta={})
    assert await remote.backfill(api) == 1
    row = remote.content.index.get(asset_key(meta.id))
    assert row is not None and row.ref.backend == "local"


async def test_backfill_skips_an_upload_swept_meanwhile(
    tmp_path: Path, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = RemoteAssets(local_content(tmp_path / "remote", pool))
    api = AssetStore(tmp_path / "assets")
    kept = api.put(SVG, "a.svg")
    monkeypatch.setattr(api, "ids", lambda: ["d" * 64, kept.id])  # one swept since
    assert await remote.backfill(api) == 1


async def test_drop_swept_assets_drops_the_stores_copies(tmp_path: Path, pool: Pool) -> None:
    remote = RemoteAssets(local_content(tmp_path / "remote", pool))
    api = AssetStore(tmp_path / "api" / "assets")
    meta = api.put(SVG, "logo.svg")
    await remote.mirror(api, meta, slug="demo", title="Demo")
    state = cast(AppState, SimpleNamespace(store=SimpleNamespace(remote_assets=remote)))
    await drop_swept_assets(state, [meta.id], cutoff=await remote.clock())
    assert remote.content.index.get(asset_key(meta.id)) is None


async def test_nothing_to_drop_without_a_store(tmp_path: Path) -> None:
    state: Any = SimpleNamespace()
    await drop_swept_assets(cast(AppState, state), ["a" * 64], cutoff=datetime.now(UTC))
