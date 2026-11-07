"""The render worker's uploads through the CRUD routes (#5298 Task 18, inventory R19-R21):
`RemoteAssetStore` keeps its files on its own volume and reads and writes the ``assets``
rows through `RemoteAssetRows` and `api/crud_render.py`.

- `use` (R19) marks the row used on the API, which waits on the sweep's row lock there.
- `adopt` (R20) is natural: adopted twice, one row. The API checks the id, takes the
  asset's advisory lock, and writes the row from its own blob index's copy, so the worker
  sets no field of it.
- `prune_local` (R21) reads the last uses in batches of at most `LAST_USED_MAX`, with no
  lock held. It held a row lock while it removed a file; now each stale copy goes under a
  lock per asset id, shared with `use` and `adopt` in the process, unless one of them let
  the id go after the prune started: a `use` is never held up by the round trip, and
  never answers a file the prune removes.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scadbuddy.auth.service_credentials import ServiceAuth
from scadbuddy.crud.client import CrudRefusedError, CrudSyncClient, CrudUnavailableError
from scadbuddy.crud.render import BASE, RemoteAssetRows
from scadbuddy.library import assets as assets_module
from scadbuddy.library.assets import (
    LAST_USED_MAX,
    AssetMeta,
    AssetNotFoundError,
    AssetRejectedError,
    AssetStore,
    RemoteAssetStore,
)
from scadbuddy.store.assets import RemoteAssets
from scadbuddy.store.index import Pool
from tests.crud.crud_support import FixedToken, threaded_render_crud
from tests.support.store import local_content, store_pool

pytestmark = pytest.mark.requires_postgres

SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="4" height="4"/></svg>'


@pytest.fixture
def pool(pg_conninfo: str) -> Iterator[Pool]:
    with store_pool(pg_conninfo) as opened:
        yield opened


@pytest.fixture
def client(pool: Pool) -> Iterator[CrudSyncClient]:
    """On a server thread of its own: the store's calls block, from sync tests and from
    `RemoteAssets.ensure`'s worker threads alike."""
    with threaded_render_crud(pool) as blocking:
        yield blocking


@pytest.fixture
def rows(client: CrudSyncClient) -> RemoteAssetRows:
    return RemoteAssetRows(client)


@pytest.fixture
def api(tmp_path: Path, pool: Pool) -> AssetStore:
    return AssetStore(tmp_path / "api" / "assets", pool)


@pytest.fixture
def remote(tmp_path: Path, pool: Pool) -> RemoteAssets:
    return RemoteAssets(local_content(tmp_path / "blobs", pool))


@pytest.fixture
def worker(tmp_path: Path, rows: RemoteAssetRows) -> RemoteAssetStore:
    return RemoteAssetStore(tmp_path / "worker" / "assets", rows)


async def _uploaded(api: AssetStore, remote: RemoteAssets, data: bytes = SVG) -> AssetMeta:
    """An upload on the API, mirrored into the blob store as the API does."""
    meta = api.put(data, "logo.svg", source_url="https://example.com/logo.svg")
    await remote.mirror(api, meta, slug="demo", title="Demo")
    return meta


def _stored(api: AssetStore, meta: AssetMeta) -> bytes:
    return api.blob_path(meta).read_bytes()


def _last_used(pool: Pool, asset_id: str) -> datetime | None:
    with pool.connection() as conn:
        row = conn.execute("SELECT last_used_at FROM assets WHERE id = %s", (asset_id,)).fetchone()
    return None if row is None else row["last_used_at"]


def _age(pool: Pool, asset_id: str, days: int = 7) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE assets SET last_used_at = %s WHERE id = %s",
            (datetime.now(UTC) - timedelta(days=days), asset_id),
        )


def _count(pool: Pool, asset_id: str) -> int:
    with pool.connection() as conn:
        row = conn.execute("SELECT count(*) AS n FROM assets WHERE id = %s", (asset_id,)).fetchone()
    assert row is not None
    return int(row["n"])


# -- get and use (R19) -------------------------------------------------------------


async def test_a_worker_fetches_uses_and_reads_an_upload(
    api: AssetStore, remote: RemoteAssets, worker: RemoteAssetStore, pool: Pool
) -> None:
    meta = await _uploaded(api, remote)
    assert await remote.ensure(worker, [meta.id]) == []  # fetched and adopted
    assert worker.blob_path(meta).read_bytes() == api.blob_path(meta).read_bytes()
    assert worker.get(meta.id) == meta
    _age(pool, meta.id)
    assert worker.use(meta.id) == meta
    used = _last_used(pool, meta.id)
    assert used is not None and used > datetime.now(UTC) - timedelta(minutes=1)
    assert worker.ids() == [meta.id]


async def test_an_unknown_or_malformed_id_is_not_found(
    worker: RemoteAssetStore, rows: RemoteAssetRows
) -> None:
    for asset_id in ("f" * 64, "not-an-id"):
        with pytest.raises(AssetNotFoundError):
            worker.get(asset_id)
        with pytest.raises(AssetNotFoundError):
            worker.use(asset_id)
        with pytest.raises(AssetNotFoundError):
            rows.get(asset_id)
        with pytest.raises(AssetNotFoundError):
            rows.use(asset_id)


async def test_a_row_without_the_workers_copy_is_not_found_and_not_marked(
    api: AssetStore, remote: RemoteAssets, worker: RemoteAssetStore, pool: Pool
) -> None:
    """As the SQL `use` rolled its mark back: a use that could not read the file does not
    keep the row alive."""
    meta = await _uploaded(api, remote)
    _age(pool, meta.id)
    before = _last_used(pool, meta.id)
    with pytest.raises(AssetNotFoundError):
        worker.use(meta.id)
    with pytest.raises(AssetNotFoundError):
        worker.get(meta.id)
    assert _last_used(pool, meta.id) == before


async def test_an_api_out_of_reach_is_no_missing_upload(tmp_path: Path) -> None:
    """A passing failure is the client's `CrudUnavailableError`, which the activity
    retries, never the `AssetNotFoundError` a render fails on for good."""
    client = CrudSyncClient(
        "http://127.0.0.1:9",
        "render",
        ServiceAuth(FixedToken()),
        unavailable_budget=0.2,
        backoff=0.01,
    )
    store = RemoteAssetStore(tmp_path / "assets", RemoteAssetRows(client))
    data = SVG
    asset_id = hashlib.sha256(data).hexdigest()
    store.root.mkdir(parents=True)
    (store.root / f"{asset_id}.svg").write_bytes(data)
    try:
        with pytest.raises(CrudUnavailableError) as raised:
            store.use(asset_id)
        assert raised.value.passes
    finally:
        client.close()


# -- adopt (R20) -------------------------------------------------------------------


async def test_adopt_twice_is_one_row(
    api: AssetStore, remote: RemoteAssets, worker: RemoteAssetStore, pool: Pool
) -> None:
    meta = await _uploaded(api, remote)
    with pool.connection() as conn:
        conn.execute("DELETE FROM assets WHERE id = %s", (meta.id,))  # swept on the API
    worker.adopt(meta, _stored(api, meta))
    worker.adopt(meta, _stored(api, meta))
    assert _count(pool, meta.id) == 1
    assert worker.use(meta.id) == meta


async def test_adopt_writes_the_row_from_the_api_s_own_copy(
    api: AssetStore, remote: RemoteAssets, worker: RemoteAssetStore, pool: Pool
) -> None:
    """The worker sets no field: a name, source or licence it sends is not stored."""
    meta = await _uploaded(api, remote)
    data = _stored(api, meta)
    with pool.connection() as conn:
        conn.execute("DELETE FROM assets WHERE id = %s", (meta.id,))
    forged = meta.model_copy(
        update={"name": "forged.svg", "source_url": "https://evil.example/", "width": 9999}
    )
    worker.adopt(forged, data)
    assert api.row(meta.id) == meta


async def test_adopt_refuses_a_file_the_api_s_copy_is_not(
    api: AssetStore,
    remote: RemoteAssets,
    rows: RemoteAssetRows,
    client: CrudSyncClient,
    pool: Pool,
) -> None:
    meta = await _uploaded(api, remote)
    with pool.connection() as conn:
        conn.execute("DELETE FROM assets WHERE id = %s", (meta.id,))
    with pytest.raises(AssetRejectedError):
        rows.adopt(meta.model_copy(update={"kind": "png"}))
    with pytest.raises(AssetRejectedError):
        rows.adopt(meta.model_copy(update={"size": meta.size + 1}))
    # No copy in the blob store: nothing to adopt.
    with pytest.raises(AssetRejectedError):
        rows.adopt(meta.model_copy(update={"id": "e" * 64}))
    # A body that is not the route's id, or an id that is not one.
    with pytest.raises(CrudRefusedError) as refused:
        client.request("POST", f"{BASE}/assets/{'e' * 64}/adopt", json=meta.model_dump(mode="json"))
    assert refused.value.status == 422
    with pytest.raises(CrudRefusedError) as refused:
        client.request("POST", f"{BASE}/assets/x/adopt", json=meta.model_dump(mode="json"))
    assert refused.value.status == 422
    assert _count(pool, meta.id) == 0


def test_adopt_refuses_bytes_that_are_not_the_id(worker: RemoteAssetStore) -> None:
    meta = AssetMeta(id="0" * 64, name="x.svg", kind="svg", size=3)
    with pytest.raises(AssetRejectedError):
        worker.adopt(meta, b"abc")
    assert worker.ids() == []


# -- last uses and the local prune (R21) -------------------------------------------


async def test_last_used_answers_each_id_and_null_for_none(
    api: AssetStore, remote: RemoteAssets, rows: RemoteAssetRows, client: CrudSyncClient
) -> None:
    meta = await _uploaded(api, remote)
    stamps = rows.last_used([meta.id, "f" * 64])
    assert set(stamps) == {meta.id, "f" * 64}
    assert stamps["f" * 64] is None
    assert stamps[meta.id] is not None
    with pytest.raises(CrudRefusedError) as refused:
        client.request("POST", f"{BASE}/assets/last-used", json={"ids": ["a" * 64] * 501})
    assert refused.value.status == 422
    assert LAST_USED_MAX == 500
    with pytest.raises(CrudRefusedError):
        client.request("POST", f"{BASE}/assets/last-used", json={"ids": ["../etc"]})


async def test_prune_local_removes_stale_copies_and_keeps_the_rows(
    api: AssetStore,
    remote: RemoteAssets,
    worker: RemoteAssetStore,
    pool: Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(assets_module, "LAST_USED_MAX", 2)  # three ids: two batches
    stale = await _uploaded(api, remote)
    fresh = await _uploaded(api, remote, SVG.replace(b"4", b"5"))
    assert await remote.ensure(worker, [stale.id, fresh.id]) == []
    orphan = "c" * 64
    (worker.root / f"{orphan}.png").write_bytes(b"x")
    old = time.time() - 7 * 86400
    os.utime(worker.root / f"{orphan}.png", (old, old))
    _age(pool, stale.id)
    assert worker.prune_local(grace=3600) == sorted([stale.id, orphan])
    assert worker.ids() == [fresh.id]
    assert _count(pool, stale.id) == 1  # the row is the API's sweep's to remove


class _Paused:
    """`RemoteAssetRows` that can hold a call: `last_used` waits, once answered, until
    ``go`` is set (a prune inside its round trip); `use`, with ``hold_use``, waits before
    it calls the API (a slow use with the id's lock held)."""

    def __init__(self, rows: RemoteAssetRows) -> None:
        self._rows = rows
        self.inside = threading.Event()
        self.go = threading.Event()
        self.hold_use = False
        self.using = threading.Event()
        self.use_go = threading.Event()

    def get(self, asset_id: str) -> AssetMeta:
        return self._rows.get(asset_id)

    def use(self, asset_id: str) -> AssetMeta:
        if self.hold_use:
            self.using.set()
            assert self.use_go.wait(30)
        return self._rows.use(asset_id)

    def adopt(self, meta: AssetMeta) -> None:
        self._rows.adopt(meta)

    def last_used(self, ids: Sequence[str]) -> dict[str, datetime | None]:
        answer = self._rows.last_used(ids)
        self.inside.set()
        assert self.go.wait(30)
        return answer


async def test_a_use_during_the_prunes_round_trip_is_not_held_and_keeps_its_file(
    api: AssetStore, remote: RemoteAssets, rows: RemoteAssetRows, tmp_path: Path, pool: Pool
) -> None:
    """The prune read a stale last use and holds no lock across that read: a `use` then
    goes through at once, and the prune, seeing the use, keeps the file."""
    paused = _Paused(rows)
    worker = RemoteAssetStore(tmp_path / "worker" / "assets", paused)
    meta = await _uploaded(api, remote)
    assert await remote.ensure(worker, [meta.id]) == []
    _age(pool, meta.id)
    pruning = asyncio.create_task(asyncio.to_thread(worker.prune_local, grace=3600))
    assert await asyncio.to_thread(paused.inside.wait, 30)
    found = await asyncio.wait_for(asyncio.to_thread(worker.use, meta.id), 5)  # not held
    assert found == meta
    paused.go.set()
    assert await pruning == []
    assert worker.blob_path(found).is_file()


async def test_a_use_holding_its_id_when_the_prune_comes_keeps_its_file(
    api: AssetStore, remote: RemoteAssets, rows: RemoteAssetRows, tmp_path: Path, pool: Pool
) -> None:
    """A slow `use` holds the id's lock while the prune reads a stale last use (the use
    has not reached the API yet): the prune waits for the id, sees the use, and keeps
    the file the use answered."""
    paused = _Paused(rows)
    paused.go.set()
    worker = RemoteAssetStore(tmp_path / "worker" / "assets", paused)
    meta = await _uploaded(api, remote)
    assert await remote.ensure(worker, [meta.id]) == []
    _age(pool, meta.id)
    paused.hold_use = True
    using = asyncio.create_task(asyncio.to_thread(worker.use, meta.id))
    assert await asyncio.to_thread(paused.using.wait, 30)
    pruning = asyncio.create_task(asyncio.to_thread(worker.prune_local, grace=3600))
    assert await asyncio.to_thread(paused.inside.wait, 30)
    await asyncio.sleep(0.3)
    assert not pruning.done()  # waits on the use's lock of this id
    paused.use_go.set()
    found = await using
    assert await pruning == []
    assert worker.blob_path(found).is_file()


async def test_a_use_after_the_prune_removed_the_file_is_fetched_again(
    api: AssetStore, remote: RemoteAssets, worker: RemoteAssetStore, pool: Pool
) -> None:
    meta = await _uploaded(api, remote)
    assert await remote.ensure(worker, [meta.id]) == []
    _age(pool, meta.id)
    assert worker.prune_local(grace=3600) == [meta.id]
    with pytest.raises(AssetNotFoundError):
        worker.use(meta.id)
    assert await remote.ensure(worker, [meta.id]) == []
    assert worker.use(meta.id) == meta


async def test_a_use_before_the_prune_keeps_the_file(
    api: AssetStore, remote: RemoteAssets, worker: RemoteAssetStore, pool: Pool
) -> None:
    meta = await _uploaded(api, remote)
    assert await remote.ensure(worker, [meta.id]) == []
    _age(pool, meta.id)
    assert worker.use(meta.id) == meta
    assert worker.prune_local(grace=3600) == []
    assert worker.blob_path(meta).is_file()


async def test_the_id_locks_are_forgotten_once_free(
    api: AssetStore, remote: RemoteAssets, worker: RemoteAssetStore
) -> None:
    meta = await _uploaded(api, remote)
    assert await remote.ensure(worker, [meta.id]) == []
    worker.use(meta.id)
    worker.prune_local(grace=3600)
    assert worker._locks.held() == 0
    assert worker._touched == {}  # older than the prune's start: forgotten
