"""The render worker on a Postgres role of its own (#601, spec 2026-09-27 §9): it
renders holding exactly `RENDER_GRANTS`, never migrates, and cannot read `settings`."""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
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

from scadbuddy.bambuddy.client import BambuddyConfig
from scadbuddy.core.settings import Settings
from scadbuddy.library.assets import AssetMeta, AssetStore
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.libraries import CheckoutLeases, InstallPermits
from scadbuddy.library.settings_store import RENDER_FIELDS, load_render_store_settings
from scadbuddy.render import worker_role
from scadbuddy.render.job_models import Job
from scadbuddy.render.pg_store import Migration, migrate
from scadbuddy.render.projection import JobProjection
from scadbuddy.render.solids import WRAPPER_PREFIX
from scadbuddy.render.worker_role import (
    RENDER_GRANTS,
    WorkerStoppedError,
    grant_at_start,
    grant_render_role,
    missing_privileges,
    pending_migrations,
    wait_until_ready,
)
from scadbuddy.store.bambuddy import BambuddyContentBackend, BambuddyTarget
from scadbuddy.store.content import BlobScope, ContentStore
from scadbuddy.store.index import BlobIndex
from scadbuddy.worker import run_worker
from scadbuddy.workflows.models import piece_key
from tests.bambuddy.conftest import BASE_URL, recorded_schema, shaped
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS, fake_3mf_openscad
from tests.support.renders import render_to_end
from tests.support.temporal import temporal_client

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


def test_the_role_holds_every_grant_and_cannot_read_the_settings_table(granted: Pool) -> None:
    with granted.connection() as conn:
        assert missing_privileges(conn) == []
        assert pending_migrations(conn) == []
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT name FROM settings")
        for table in ("operations", "print_runs", "saved_presets", "model_print_choices"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql.SQL("SELECT 1 FROM {}").format(sql.Identifier(table)))


def test_the_role_cannot_migrate(granted: Pool) -> None:
    with granted.connection() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        migrate(conn, (Migration("29991231T2359Z_later", "CREATE TABLE later (id int)"),))


def test_a_role_that_does_not_exist_is_granted_nothing(owner: Pool) -> None:
    with owner.connection() as conn:
        assert grant_render_role(conn, f"absent_{uuid.uuid4().hex[:8]}") is False


def test_every_grant_names_a_relation_the_schema_has(owner: Pool) -> None:
    with owner.connection() as conn:
        for table in RENDER_GRANTS:
            row = conn.execute("SELECT to_regclass(%s) AS found", (table,)).fetchone()
            assert row is not None and row["found"] is not None, table


# ── the settings it may read ──────────────────────────────────────────────────


def test_the_full_key_is_hidden_once_a_render_key_is_stored(
    owner: Pool, granted: Pool, tmp_path: Path
) -> None:
    _put(owner, "store_backend", "bambuddy")
    _put(owner, "bambuddy_url", BASE_URL)
    _put(owner, "library_folder_id", 1)
    _put(owner, "bambuddy_api_key", "full")
    _put(owner, "google_fonts_api_key", "fonts")
    # No render key: the fallback the app alerts on (ScadBuddyRenderKeyFallback).
    current = load_render_store_settings(granted, _defaults(tmp_path))
    assert (current.store_backend, current.api_key, current.key_is_fallback) == (
        "bambuddy",
        "full",
        True,
    )
    _put(owner, "bambuddy_render_api_key", "narrow")
    current = load_render_store_settings(granted, _defaults(tmp_path))
    assert (current.api_key, current.key_is_fallback) == ("narrow", False)
    with granted.connection() as conn:
        names = {row["name"] for row in conn.execute("SELECT name FROM render_settings")}
    assert names == {
        "store_backend",
        "bambuddy_url",
        "library_folder_id",
        "bambuddy_render_api_key",
    }
    # A render key cleared in Settings (a JSON null) is no render key.
    _put(owner, "bambuddy_render_api_key", None)
    assert load_render_store_settings(granted, _defaults(tmp_path)).api_key == "full"


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


def test_the_api_grants_the_role_at_start(
    owner: Pool, role: str, worker_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_role, "RENDER_ROLE", role)
    grant_at_start(owner)
    pool = _pool(worker_conninfo)
    try:
        with pool.connection() as conn:
            assert missing_privileges(conn) == []
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


# ── waiting for the API ───────────────────────────────────────────────────────


def test_the_worker_waits_for_migrations_then_grants(
    pg_conninfo: str, role: str, worker_conninfo: str
) -> None:
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(conn.execute("SELECT current_schema()").fetchone()[0]),  # type: ignore[index]
                sql.Identifier(role),
            )
        )
    pool = _pool(worker_conninfo)
    try:
        with pool.connection() as conn:
            assert pending_migrations(conn)  # nothing migrated yet
        with pytest.raises(WorkerStoppedError):
            wait_until_ready(pool, stopping=lambda: True, poll=0.01)
        with psycopg.connect(pg_conninfo, autocommit=True) as conn:
            migrate(conn)
            conn.execute(
                sql.SQL("GRANT SELECT ON scadbuddy_migrations TO {}").format(sql.Identifier(role))
            )
        with pool.connection() as conn:
            assert pending_migrations(conn) == []
            assert "render_jobs: INSERT" in missing_privileges(conn)
        with pytest.raises(WorkerStoppedError):
            wait_until_ready(pool, stopping=lambda: True, poll=0.01)
        with psycopg.connect(pg_conninfo, autocommit=True) as conn:
            grant_render_role(conn, role)
        wait_until_ready(pool, stopping=lambda: True, poll=0.01)
    finally:
        pool.close()


# ── what a render does, as the role ───────────────────────────────────────────


def test_leases_pins_and_install_slots_work_as_the_role(granted: Pool, tmp_path: Path) -> None:
    leases = CheckoutLeases(granted, tmp_path)
    token = leases.take("job-1", [tmp_path / "BOSL2" / "abc"])
    leases.renew(token)
    leases.drop(token)
    pin = leases.take_pin()
    leases.renew_pin(pin)
    leases.drop_pin(pin)
    permits = InstallPermits(2, granted)
    claimed = permits.claim()
    assert claimed is not None


def test_uploads_are_adopted_used_and_pruned_as_the_role(granted: Pool, tmp_path: Path) -> None:
    data = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
    meta = AssetMeta(id=hashlib.sha256(data).hexdigest(), name="x.svg", kind="svg", size=len(data))
    store = AssetStore(tmp_path / "assets", granted)
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
async def test_the_bambuddy_store_works_as_the_role(granted: Pool) -> None:
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
    backend = BambuddyContentBackend(target, granted)
    content = ContentStore(backend, BlobIndex(granted))
    try:
        scope = BlobScope(slug="kit", title="Kit")
        await content.put("piece", b"a", name="a.zip", scope=scope, key="piece-a")
        await content.touch("piece-a")
        await content.put("piece", b"b", name="b.zip", scope=scope, key="piece-b")
        # The object is gone from Bambuddy: its row is forgotten.
        assert await content.stat("piece-a") is None
    finally:
        await backend.aclose()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.mark.requires_temporal
@pytest.mark.requires_git
async def test_the_worker_renders_as_its_own_role(
    settings: Settings,
    paths: Any,
    model: str,
    owner: Pool,
    granted: Pool,
    worker_conninfo: str,
    pg_conninfo: str,
    tmp_path: Path,
) -> None:
    queue = f"t-{uuid.uuid4().hex[:8]}"
    cfg = settings.model_copy(
        update={
            "database_url": worker_conninfo,
            "openscad": fake_3mf_openscad(tmp_path / "bin"),
            "temporal_task_queue_render": queue,
            "revision": f"test-{uuid.uuid4().hex[:8]}",
        }
    )
    revision = ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX).ensure_repo()
    assert revision is not None
    params: dict[str, Any] = {"width": 12}
    job = Job(
        id=uuid.uuid4().hex,
        slug=model,
        params=params,
        inputs={"params": params},
        model_version=revision,
        created_at=datetime.now(UTC),
    )
    async with temporal_client() as client:
        stop = asyncio.Event()
        worker = asyncio.create_task(
            run_worker(cfg, stop=stop, health_port=_free_port(), client=client)
        )
        try:
            job = await render_to_end(client, queue, job)
        finally:
            stop.set()
            await asyncio.wait_for(worker, timeout=60)

    projection = JobProjection(pg_conninfo, pool_size=1)
    projection.open()
    try:
        stored = projection.read(job.id)
    finally:
        projection.close()
    assert stored.state == "done", stored.error
    assert (cfg.data_dir / "blobs" / piece_key(model, revision, "model.scad", params)).is_dir()
    with owner.connection() as conn:
        kinds = {
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM events WHERE payload->>'job_id' = %s", (job.id,)
            )
        }
        refs = conn.execute(
            "SELECT count(*) AS n FROM blob_refs WHERE holder_id = %s", (job.id,)
        ).fetchone()
    assert {"job.running", "job.done"} <= kinds
    assert refs is not None and refs["n"] >= 1
