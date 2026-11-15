"""What the render worker reads and writes goes through the API's CRUD routes (#5298: it
holds no database, `tests/test_render_worker_nodb.py`; the API takes back its old role
`scadbuddy_render` at every start, `tests/auth/test_revoke.py`): the store settings the
`render_settings` view releases, its library leases, its uploads and its Bambuddy
store's index and folders."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.bambuddy import commits
from scadbuddy.bambuddy.client import BambuddyConfig
from scadbuddy.core.config import available_cpus, default_solid_concurrency
from scadbuddy.core.settings import Settings
from scadbuddy.crud.render import (
    RemoteAssetRows,
    RemoteBlobIndex,
    RemoteBlobRefs,
    RemoteCheckoutLeases,
    RemoteGeometryIndex,
    RemoteInstallSlots,
    RemoteJobProjection,
    RemoteRenderSettingsSource,
    RemoteStoreFolders,
)
from scadbuddy.library.assets import AssetMeta, RemoteAssetStore
from scadbuddy.library.libraries import InstallPermits
from scadbuddy.library.settings_store import (
    RENDER_FIELDS,
    load_render_store_settings,
)
from scadbuddy.render.pg_store import migrate
from scadbuddy.store.assets import asset_key
from scadbuddy.store.bambuddy import BambuddyContentBackend, BambuddyTarget
from scadbuddy.store.content import BlobScope, ContentStore
from scadbuddy.store.content_models import BlobRef as IndexedRef
from scadbuddy.store.index import BlobIndex
from scadbuddy.worker import build_worker_deps
from scadbuddy.workflows.activities import RenderActivities, WorkerDeps
from scadbuddy.workflows.models import PrepareResult
from tests.bambuddy.conftest import BASE_URL, recorded_schema, shaped
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS
from tests.crud.crud_support import served_render_crud, threaded_render_crud

pytestmark = pytest.mark.requires_postgres

#: Piece keys: the only keys the render credential stores (`api/crud_render.py`).
PIECE_A, PIECE_B = "a" * 64, "b" * 64

Pool = ConnectionPool[Connection[DictRow]]


def _pool(conninfo: str) -> Pool:
    pool: Pool = ConnectionPool(
        conninfo,
        min_size=1,
        max_size=4,
        open=False,
        connection_class=Connection[DictRow],
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    pool.open(wait=True, timeout=30)
    return pool


@pytest.fixture
def owner(pg_conninfo: str) -> Iterator[Pool]:
    """The API's side: the schema migrated by the role that owns it."""
    pool = _pool(pg_conninfo)
    with pool.connection() as conn:
        migrate(conn)
    try:
        yield pool
    finally:
        pool.close()


def _put(owner: Pool, name: str, value: object) -> None:
    with owner.connection() as conn:
        conn.execute(
            "INSERT INTO settings (name, value) VALUES (%s, %s)"
            " ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value",
            (name, Jsonb(value)),
        )


def _defaults(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_url=UNUSED_DATABASE_URL,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
    )


def test_the_full_key_is_hidden_once_a_render_key_is_stored(owner: Pool, tmp_path: Path) -> None:
    """What the API reads for the CRUD route that answers a render worker (#5298),
    through the `render_settings` view. The route withholds the fallback full key
    (`tests/crud/test_crud_render_settings.py`)."""
    _put(owner, "store_backend", "bambuddy")
    _put(owner, "bambuddy_url", BASE_URL)
    _put(owner, "library_folder_id", 1)
    _put(owner, "bambuddy_api_key", "full")
    _put(owner, "google_fonts_api_key", "fonts")
    # No render key: the fallback the app alerts on (ScadBuddyRenderKeyFallback).
    current = load_render_store_settings(owner, _defaults(tmp_path))
    assert (current.store_backend, current.api_key, current.key_is_fallback) == (
        "bambuddy",
        "full",
        True,
    )
    _put(owner, "bambuddy_render_api_key", "narrow")
    current = load_render_store_settings(owner, _defaults(tmp_path))
    assert (current.api_key, current.key_is_fallback) == ("narrow", False)
    _put(owner, "solid_concurrency", 3)
    assert load_render_store_settings(owner, _defaults(tmp_path)).solid_concurrency == 3
    with owner.connection() as conn:
        names = {row["name"] for row in conn.execute("SELECT name FROM render_settings")}
    assert names == {
        "store_backend",
        "bambuddy_url",
        "library_folder_id",
        "bambuddy_render_api_key",
        "solid_concurrency",
    }
    # A render key cleared in Settings (a JSON null) is no render key.
    _put(owner, "bambuddy_render_api_key", None)
    assert load_render_store_settings(owner, _defaults(tmp_path)).api_key == "full"


def test_the_view_passes_every_render_field(owner: Pool) -> None:
    """`RENDER_FIELDS` and the view move together: a field the loader reads that the
    view drops would follow the environment on the worker, silently."""
    for name in RENDER_FIELDS:
        _put(owner, name, "x")
    with owner.connection() as conn:
        names = {row["name"] for row in conn.execute("SELECT name FROM render_settings")}
        assert names == set(RENDER_FIELDS) - {"bambuddy_api_key"}
        conn.execute("DELETE FROM settings WHERE name = 'bambuddy_render_api_key'")
        names = {row["name"] for row in conn.execute("SELECT name FROM render_settings")}
        assert names == set(RENDER_FIELDS) - {"bambuddy_render_api_key"}


async def test_the_settings_pages_solid_concurrency_reaches_the_worker_live(
    owner: Pool, tmp_path: Path
) -> None:
    """The per-colour slots a person saves reach a worker that reads settings only
    through the API's CRUD route (#601, #5298), and a change reaches it without a
    restart."""
    settings = _defaults(tmp_path)
    # A remote store: the worker refuses the local one (#5298 plan D11).
    _put(owner, "store_backend", "bambuddy")
    _put(owner, "bambuddy_url", BASE_URL)
    _put(owner, "library_folder_id", INBOX)
    async with served_render_crud(owner, _defaults(tmp_path)) as crud:
        # Re-read on every call, not on the 30 s TTL.
        source = RemoteRenderSettingsSource(crud, ttl=0)
        current = await source.current()
        remote = crud.blocking()
        deps, store = await asyncio.to_thread(
            build_worker_deps,
            settings,
            source,
            current,
            jobs=RemoteJobProjection(remote),
            refs=RemoteBlobRefs(remote),
            geometry=RemoteGeometryIndex(remote),
            index=RemoteBlobIndex(remote),
            folders=RemoteStoreFolders(crud),
            assets=RemoteAssetRows(remote),
            leases=RemoteCheckoutLeases(remote),
            slots=RemoteInstallSlots(remote),
        )
        try:
            await _the_slots_follow_the_settings(owner, deps)
        finally:
            await store.aclose()
            remote.close()


async def _the_slots_follow_the_settings(owner: Pool, deps: WorkerDeps) -> None:
    acts = RenderActivities(deps)
    prepared = PrepareResult(version="v", scad="model.scad", schema_cache="")
    # The worker serves no editor checks, so its default keeps no CPU for them.
    derived = default_solid_concurrency(available_cpus(), deps.config.render_concurrency, 0)

    async def slots() -> tuple[int, int]:
        config = await acts._solids_config(prepared)
        return config.solid_concurrency, config.solid_slots()

    assert await slots() == (0, derived)
    _put(owner, "solid_concurrency", 3)
    assert await slots() == (3, 3)
    _put(owner, "solid_concurrency", 5)
    assert await slots() == (5, 5)
    # 0 is "work it out": the derived default again.
    _put(owner, "solid_concurrency", 0)
    assert await slots() == (0, derived)
    # Cleared in Settings: the environment's, as the API resolves it.
    _put(owner, "solid_concurrency", 4)
    _put(owner, "solid_concurrency", None)
    assert await slots() == (0, derived)


# ── what a render does, through the API ───────────────────────────────────────


def test_leases_pins_and_install_slots_go_through_the_api(owner: Pool, tmp_path: Path) -> None:
    """The worker's leases, pin holds and install slots go through the API's CRUD routes
    (#5298), not the three ``library_*`` tables."""
    with threaded_render_crud(owner) as client:
        leases = RemoteCheckoutLeases(client)
        token = leases.take("job-1", [tmp_path / "BOSL2" / ("c" * 40)])
        leases.renew(token)
        leases.drop(token)
        pin = leases.take_pin()
        leases.renew_pin(pin)
        leases.drop_pin(pin)
        permits = InstallPermits(2, slots=RemoteInstallSlots(client))
        claimed = permits.claim()
        assert claimed is not None


def test_uploads_are_adopted_used_and_pruned_through_the_api(owner: Pool, tmp_path: Path) -> None:
    """The worker's uploads keep their files on its volume and their `assets` rows
    through the API's CRUD routes (#5298)."""
    data = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
    meta = AssetMeta(id=hashlib.sha256(data).hexdigest(), name="x.svg", kind="svg", size=len(data))
    # The API's copy in the blob store, whose meta the API adopts the row from.
    ref = IndexedRef(sha256=meta.id, kind="asset", backend="local", backend_id=meta.id, size=1)
    BlobIndex(owner).put(asset_key(meta.id), ref, slug=None, meta=meta.model_dump())
    with threaded_render_crud(owner) as client:
        store = RemoteAssetStore(tmp_path / "assets", RemoteAssetRows(client))
        store.adopt(meta, data)
        store.adopt(meta, data)  # again: the conflict path
        assert store.use(meta.id).id == meta.id
        assert store.prune_local(grace=3600) == []


INBOX = 1
API = f"{BASE_URL}/api/v1"


def _folder(**values: Any) -> dict[str, Any]:
    missing = set(recorded_schema("FolderTreeItem")["required"]) - set(values)
    assert not missing
    return shaped("FolderTreeItem", **values)


@respx.mock
async def test_the_bambuddy_store_works_with_no_pool(
    owner: Pool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its index and its folders through the API's CRUD routes (#5298), with no pool, as
    the worker builds it: the API finds a folder on its own render target, the one
    Settings names."""
    monkeypatch.setattr(commits, "COMMIT_WAITS", (0.0, 0.0, 0.0, 0.0))

    async def target() -> BambuddyTarget:
        return BambuddyTarget(config=BambuddyConfig(base_url=BASE_URL, api_key="k"), inbox_id=INBOX)

    async def create_folder(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        ids = {"Kit": 10, "Work": 11}
        return httpx.Response(200, json=_folder(id=ids[body["name"]], **body))

    respx.get(f"{API}/library/folders").mock(
        return_value=httpx.Response(
            200, json=[_folder(id=INBOX, name="ScadBuddy", parent_id=None, children=[])]
        )
    )
    respx.post(f"{API}/library/folders/").mock(side_effect=create_folder)
    # Really deleted, not merely uncommitted (#2263): the read-back never finds it.
    read_back = respx.get(f"{API}/library/folders/11").mock(
        return_value=httpx.Response(404, json={"detail": "Folder not found"})
    )
    respx.post(f"{API}/library/files").mock(
        side_effect=[
            httpx.Response(
                200, json=shaped("FileUploadResponse", id=500, filename="f", file_size=1)
            ),
            # The Work folder was deleted in Bambuddy: forgotten, made again.
            httpx.Response(404, json={"detail": "Folder not found"}),
            httpx.Response(
                200, json=shaped("FileUploadResponse", id=501, filename="f", file_size=1)
            ),
        ]
    )
    respx.get(f"{API}/library/files/500").mock(return_value=httpx.Response(404))
    respx.route(host="127.0.0.1").pass_through()
    with owner.connection() as conn:
        conn.execute(
            "INSERT INTO settings (name, value) VALUES"
            " ('bambuddy_url', %s), ('library_folder_id', %s)",
            (Jsonb(BASE_URL), Jsonb(INBOX)),
        )
    defaults = _defaults(tmp_path).model_copy(update={"bambuddy_api_key": "k"})
    async with served_render_crud(owner, defaults) as crud:
        remote = crud.blocking()
        index = RemoteBlobIndex(remote)
        backend = BambuddyContentBackend(
            target, None, index=index, folders=RemoteStoreFolders(crud)
        )
        try:
            await _the_bambuddy_store_works(backend, ContentStore(backend, index), read_back)
        finally:
            remote.close()


async def _the_bambuddy_store_works(
    backend: BambuddyContentBackend, content: ContentStore, read_back: respx.Route
) -> None:
    try:
        scope = BlobScope(slug="kit", title="Kit")
        await content.put("piece", b"a", name="a.zip", scope=scope, key=PIECE_A)
        await content.touch(PIECE_A)
        await content.put("piece", b"b", name="b.zip", scope=scope, key=PIECE_B)
        # The object is gone from Bambuddy: its row is forgotten.
        assert await content.stat(PIECE_A) is None
        assert read_back.call_count == len(commits.COMMIT_WAITS)
    finally:
        await backend.aclose()
