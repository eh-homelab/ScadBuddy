"""The render worker's Postgres role (#601, spec 2026-09-27 §9), which the worker no
longer uses (#5298: it holds no database, `tests/test_render_worker_nodb.py`): the API
grants it `RENDER_GRANTS` (nothing now) at start until Task 22 revokes it, it never
migrates, and it cannot read `settings`; and what the worker does instead goes through
the API's CRUD routes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from psycopg import Connection, sql
from psycopg.conninfo import make_conninfo
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
from scadbuddy.render import worker_role
from scadbuddy.render.pg_store import Migration, migrate
from scadbuddy.render.worker_role import (
    grant_at_start,
    grant_render_role,
)
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


@pytest.fixture
def role(pg_conninfo: str) -> Iterator[str]:
    """A login role of its own, with no grants: roles are per cluster, so the name is
    unique, and it is dropped afterwards with whatever it was granted."""
    name = f"render_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(name), sql.Literal(name)
            )
        )
    try:
        yield name
    finally:
        with psycopg.connect(pg_conninfo, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


@pytest.fixture
def worker_conninfo(pg_conninfo: str, role: str) -> str:
    return make_conninfo(pg_conninfo, user=role, password=role)


@pytest.fixture
def granted(owner: Pool, role: str, worker_conninfo: str) -> Iterator[Pool]:
    """A pool as the worker's role, holding exactly `RENDER_GRANTS`."""
    with owner.connection() as conn:
        assert grant_render_role(conn, role)
    pool = _pool(worker_conninfo)
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


# ── the role's reach ──────────────────────────────────────────────────────────


def test_the_role_cannot_read_the_settings(granted: Pool) -> None:
    with granted.connection() as conn:
        # The store settings come from the API's CRUD route (#5298), not the view.
        for relation in ("settings", "render_settings"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql.SQL("SELECT name FROM {}").format(sql.Identifier(relation)))
        # The job rows, their events and activity rows, the blob refs, the piece
        # geometry, the blob index, the store's folders, the uploads' rows and the
        # library leases, pin holds and install slots go through the API too (#5298).
        for table in (
            "render_jobs",
            "events",
            "activity",
            "blob_refs",
            "piece_geometry",
            "store_blobs",
            "store_folders",
            "assets",
            "library_leases",
            "library_pin_holds",
            "library_install_slots",
            # Nor waits for the API's migrations: it waits for its CRUD areas.
            "scadbuddy_migrations",
            "operations",
            "print_runs",
            "saved_presets",
            "model_print_choices",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql.SQL("SELECT 1 FROM {}").format(sql.Identifier(table)))


def _granted_activity_before(owner: Pool, role: str) -> None:
    """`activity` as a role granted before #5298 moved the job rows to the API holds it,
    until Task 22 revokes it: `RENDER_ROWS` still holds it to its renders' rows."""
    with owner.connection() as conn:
        conn.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE ON activity TO {}").format(sql.Identifier(role))
        )


def test_the_role_reaches_only_render_activity_rows(owner: Pool, granted: Pool, role: str) -> None:
    _granted_activity_before(owner, role)
    with owner.connection() as conn:
        conn.execute(
            "INSERT INTO activity (id, kind, status, title, source, source_id, principal)"
            " VALUES ('operation:o1', 'model_import', 'running', 'Import', 'operations', 'o1',"
            " 'token:t1')"
        )
    with granted.connection() as conn:
        assert conn.execute("SELECT id FROM activity").fetchall() == []
        conn.execute(
            "INSERT INTO activity (id, kind, status, title, source, source_id)"
            " VALUES ('render:r1', 'render', 'running', 'Render', 'render_jobs', 'r1')"
        )
        updated = conn.execute(
            "UPDATE activity SET status = 'failed' WHERE id = 'operation:o1' RETURNING id"
        ).fetchall()
        assert updated == []
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO activity (id, kind, status, title, source, source_id)"
                " VALUES ('operation:o2', 'model_import', 'running', 'Import', 'operations', 'o2')"
            )
    with owner.connection() as conn:
        rows = conn.execute("SELECT id, status FROM activity ORDER BY id").fetchall()
    assert [(r["id"], r["status"]) for r in rows] == [
        ("operation:o1", "running"),
        ("render:r1", "running"),
    ]


def test_a_changed_row_predicate_replaces_the_old_policy(
    owner: Pool, granted: Pool, role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_role, "RENDER_ROWS", {"activity": "id LIKE 'render:x%'"})
    with owner.connection() as conn:
        # af75581 named the policy without a suffix; it goes too.
        conn.execute(
            sql.SQL(
                "CREATE POLICY {} ON activity AS RESTRICTIVE TO {} USING (id LIKE 'render:%')"
            ).format(sql.Identifier(f"only_{role}"), sql.Identifier(role))
        )
        assert grant_render_role(conn, role)
    _granted_activity_before(owner, role)
    with owner.connection() as conn:
        conn.execute(
            "INSERT INTO activity (id, kind, status, title, source, source_id) VALUES"
            " ('render:a1', 'render', 'running', 'A', 'render_jobs', 'a1'),"
            " ('render:x1', 'render', 'running', 'X', 'render_jobs', 'x1')"
        )
        mine = [
            r["policyname"]
            for r in conn.execute(
                "SELECT policyname FROM pg_policies WHERE schemaname = current_schema()"
                " AND tablename = 'activity' AND starts_with(policyname, %s)",
                (f"only_{role}",),
            )
        ]
    assert len(mine) == 1
    with granted.connection() as conn:
        assert [r["id"] for r in conn.execute("SELECT id FROM activity")] == ["render:x1"]


def test_the_grant_waits_for_another_replicas_lock(
    owner: Pool, role: str, pg_conninfo: str
) -> None:
    key = worker_role.GRANT_LOCK
    with psycopg.connect(pg_conninfo) as other:  # not autocommit: the lock holds till commit
        other.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
        with ThreadPoolExecutor(1) as pool:
            pids: list[int] = []

            def grant() -> bool:
                with owner.connection() as conn:
                    pids.append(conn.info.backend_pid)
                    return grant_render_role(conn, role)

            def waiting() -> bool:
                return (
                    bool(pids)
                    and look.execute(
                        "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                        " AND pid = %s AND classid::bigint = %s AND objid::bigint = %s"
                        " AND objsubid = 1",
                        (pids[0], key >> 32, key & 0xFFFF_FFFF),
                    ).fetchone()
                    is not None
                )

            granting = pool.submit(grant)
            with psycopg.connect(pg_conninfo, autocommit=True) as look:
                deadline = time.monotonic() + 30.0
                while not waiting():
                    assert time.monotonic() < deadline, "the grant never waited on the lock"
                    if granting.done():
                        failed = granting.exception()
                        pytest.fail(f"the grant finished without waiting: {failed or 'granted'}")
                    time.sleep(0.05)
            assert not granting.done()
            other.commit()
            assert granting.result(timeout=30.0)


def test_the_role_cannot_migrate(granted: Pool) -> None:
    with granted.connection() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        migrate(conn, (Migration("29991231T2359Z_later", "CREATE TABLE later (id int)"),))


def test_a_role_that_does_not_exist_is_granted_nothing(owner: Pool) -> None:
    with owner.connection() as conn:
        assert grant_render_role(conn, f"absent_{uuid.uuid4().hex[:8]}") is False


# ── the settings it may read ──────────────────────────────────────────────────


def test_the_full_key_is_hidden_once_a_render_key_is_stored(owner: Pool, tmp_path: Path) -> None:
    """What the API's CRUD route releases to a render worker (#5298), read through the
    `render_settings` view."""
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


def test_the_api_grants_the_role_at_start(
    owner: Pool, role: str, worker_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_role, "RENDER_ROLE", role)
    grant_at_start(owner)
    pool = _pool(worker_conninfo)
    try:
        # `RENDER_GRANTS` is empty (#5298): the start grants the role no table.
        with pool.connection() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT 1 FROM render_jobs LIMIT 1")
    finally:
        pool.close()


def test_a_failed_grant_does_not_stop_the_api(
    owner: Pool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(*_: object, **__: object) -> bool:
        raise psycopg.errors.InsufficientPrivilege("not the owner")

    monkeypatch.setattr(worker_role, "grant_render_role", refuse)
    grant_at_start(owner)
    assert "could not grant the render worker's database role" in caplog.text


# ── what a render does, as the role ───────────────────────────────────────────


def test_leases_pins_and_install_slots_go_through_the_api(owner: Pool, tmp_path: Path) -> None:
    """The role holds nothing on the three ``library_*`` tables (#5298): the worker's
    leases, pin holds and install slots go through the API's CRUD routes."""
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


def test_uploads_are_adopted_used_and_pruned_through_the_api(
    owner: Pool, granted: Pool, tmp_path: Path
) -> None:
    """The role holds nothing on `assets` (#5298): the worker's uploads keep their files
    on its volume and their rows through the API's CRUD routes."""
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
async def test_the_bambuddy_store_works_as_the_role(
    granted: Pool, owner: Pool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its index and its folders through the API's CRUD routes (#5298): the API finds a
    folder on its own render target, the one Settings names."""
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
            target, granted, index=index, folders=RemoteStoreFolders(crud)
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
        await content.put("piece", b"a", name="a.zip", scope=scope, key="piece-a")
        await content.touch("piece-a")
        await content.put("piece", b"b", name="b.zip", scope=scope, key="piece-b")
        # The object is gone from Bambuddy: its row is forgotten.
        assert await content.stat("piece-a") is None
        assert read_back.call_count == len(commits.COMMIT_WAITS)
    finally:
        await backend.aclose()
