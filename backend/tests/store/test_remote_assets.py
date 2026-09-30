from __future__ import annotations

from pathlib import Path

import pytest

from scadbuddy.library.assets import AssetMeta, AssetRejectedError, AssetStore, asset_ids_in
from scadbuddy.store.assets import RemoteAssets, asset_key
from scadbuddy.store.index import Pool
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
    assert await remote.ensure(worker, asset_ids_in(params)) == [meta.id]
    assert worker.get(meta.id) == meta
    assert worker.blob_path(meta).read_bytes() == api.blob_path(meta).read_bytes()
    assert await remote.ensure(worker, [meta.id]) == []  # already local: no download
    assert await remote.ensure(worker, ["f" * 64]) == []  # unknown id: the render reports it
    await remote.drop([meta.id])
    assert remote.content.index.get(asset_key(meta.id)) is None


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
