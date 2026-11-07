"""The render worker holds no database and no KEK (#5298 Task 20): it starts and renders
a job through accept, its pieces and finish with every way to open a Postgres connection
refused, its credential exchanged for its bootstrap proof, on a remote (Bambuddy) store;
on the local store it refuses to start (plan D11)."""

from __future__ import annotations

import asyncio
import contextvars
import faulthandler
import logging
import secrets
import socket
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
import psycopg_pool
import pytest
from starlette.types import ASGIApp, Receive, Scope, Send

from scadbuddy import worker as worker_module
from scadbuddy.auth.exchange import BootstrapVerifier
from scadbuddy.auth.service_credentials import ServiceCredentialStore
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.pg_listener import PgListener
from scadbuddy.core.settings import NoDatabaseSettings, Settings
from scadbuddy.crud import listener
from scadbuddy.library.history import ModelHistory
from scadbuddy.render.job_models import Job
from scadbuddy.render.projection import JobProjection
from scadbuddy.render.solids import WRAPPER_PREFIX
from scadbuddy.workflows.models import piece_key
from tests.conftest import UNUSED_TEMPORAL_ADDRESS, fake_3mf_openscad
from tests.crud.crud_support import render_crud_app, static_render_crud
from tests.support.render_store import fake_bambuddy, pin_snapshot, use_bambuddy_store
from tests.support.renders import render_to_end
from tests.support.temporal import current_address, temporal_client

#: Set while the API's side serves a request: the API holds SQL, the worker never does.
_API_SIDE = contextvars.ContextVar("api_side", default=False)


class _ApiSide:
    """Marks every request the API's routes serve as the API's (`_API_SIDE`)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        token = _API_SIDE.set(True)
        try:
            await self.app(scope, receive, send)
        finally:
            _API_SIDE.reset(token)


def _no_database(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every way the worker could open Postgres refused, outside the API's own requests;
    the list names each attempt, in case the caller swallowed the refusal."""
    tried: list[str] = []

    def refuse(name: str, original: Any) -> Any:
        def refused(*args: Any, **kwargs: Any) -> Any:
            if _API_SIDE.get():
                return original(*args, **kwargs)
            tried.append(name)
            raise AssertionError(f"the render worker called {name}")

        return refused

    monkeypatch.setattr(psycopg, "connect", refuse("psycopg.connect", psycopg.connect))
    # `psycopg.connect` is a copy of `Connection.connect`, so it is refused too. The
    # API's pools grow through it from their own threads, outside any request, so a
    # call from `psycopg_pool` passes: the worker's own pool is refused at its start.
    connect = psycopg.Connection.connect

    def connection_connect(cls: type[psycopg.Connection[Any]], *args: Any, **kwargs: Any) -> Any:
        if _API_SIDE.get() or _from_a_pool():
            return connect(*args, **kwargs)
        tried.append("Connection.connect")
        raise AssertionError("the render worker called Connection.connect")

    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(connection_connect))
    monkeypatch.setattr(
        psycopg.AsyncConnection,
        "connect",
        refuse("AsyncConnection.connect", psycopg.AsyncConnection.connect),
    )
    monkeypatch.setattr(
        psycopg_pool.ConnectionPool,
        "__init__",
        refuse("ConnectionPool", psycopg_pool.ConnectionPool.__init__),
    )
    monkeypatch.setattr(
        psycopg_pool.AsyncConnectionPool,
        "__init__",
        refuse("AsyncConnectionPool", psycopg_pool.AsyncConnectionPool.__init__),
    )
    return tried


def _from_a_pool() -> bool:
    """Whether a `psycopg_pool` frame is on the caller's stack: a pool growing."""
    frame = sys._getframe(2)
    while frame is not None:
        if frame.f_globals.get("__name__", "").startswith("psycopg_pool"):
            return True
        frame = frame.f_back  # type: ignore[assignment]
    return False


def test_the_render_queue_reads_settings_with_no_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[Settings] = []

    async def run(settings: Settings, queue: str) -> None:
        started.append(settings)

    monkeypatch.delenv("SCADBUDDY_DATABASE_URL", raising=False)
    monkeypatch.delenv("SCADBUDDY_SECRET_KEY_FILE", raising=False)
    monkeypatch.setenv("SCADBUDDY_TEMPORAL_ADDRESS", UNUSED_TEMPORAL_ADDRESS)
    monkeypatch.setattr("sys.argv", ["scadbuddy.worker", "--queue", "render"])
    monkeypatch.setattr(worker_module, "_main", run)
    monkeypatch.setattr(worker_module, "configure_tracing", lambda *a, **k: None)
    monkeypatch.setattr(worker_module, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(faulthandler, "register", lambda *a, **k: None)
    worker_module.main()
    [settings] = started
    assert isinstance(settings, NoDatabaseSettings) and settings.database_url == ""
    assert settings.secret_key_file is None


async def test_on_the_local_store_it_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Plan D11: the local backend shares the API's volume and git, so it runs only
    in-process; a worker with no database needs a remote store."""
    tried = _no_database(monkeypatch)
    settings = NoDatabaseSettings(
        data_dir=tmp_path,
        database_url="postgresql://render@db.invalid/scadbuddy",
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
    )
    with (
        caplog.at_level(logging.WARNING, logger="scadbuddy.worker"),
        pytest.raises(worker_module.LocalStoreRefusedError, match="D11"),
    ):
        await worker_module.run_worker(settings, health_port=None, crud=static_render_crud())
    assert tried == []
    assert "ignoring SCADBUDDY_DATABASE_URL" in caplog.text


class _Api:
    def __init__(self, url: str, secret: Path) -> None:
        self.url = url
        self.secret = secret


@asynccontextmanager
async def _api(
    projection: JobProjection, settings: Settings, tmp_path: Path
) -> AsyncIterator[_Api]:
    """The API's side, made before the database is refused: the render routes with the
    exchange of a bootstrap secret for ``render``."""
    secrets_dir = tmp_path / "bootstrap"
    secrets_dir.mkdir()
    secret = secrets_dir / "render"
    secret.write_text(secrets.token_urlsafe(32))
    app = render_crud_app(
        projection.pool,
        settings,
        ServiceCredentialStore(projection.pool),
        projection=projection,
        bootstrap=BootstrapVerifier.from_dir(secrets_dir),
    )
    app.add_middleware(_ApiSide)
    await listener.start(app, 0)
    try:
        yield _Api(f"http://127.0.0.1:{listener.crud_listener_port(app)}", secret)
    finally:
        await listener.stop(app)


@pytest.fixture
def projection(pg_conninfo: str) -> Iterator[JobProjection]:
    """The API's job rows, with its event bus: it writes them for the worker."""
    events = PgNotifyEventBus(pg_conninfo, listener=PgListener(pg_conninfo))
    store = JobProjection(pg_conninfo, pool_size=4, events=events)
    store.open()
    try:
        yield store
    finally:
        store.close()


@pytest.mark.requires_postgres
@pytest.mark.requires_temporal
@pytest.mark.requires_git
async def test_a_render_runs_on_a_render_worker_with_no_database(
    settings: Settings,
    model: str,
    projection: JobProjection,
    tmp_path: Path,
    health_socket: socket.socket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accept, the pieces and finish, each through the API's CRUD routes under a
    credential the worker exchanged its bootstrap secret for; the piece and the
    template's snapshot through the Bambuddy store."""
    monkeypatch.delenv("SCADBUDDY_DATABASE_URL", raising=False)
    monkeypatch.delenv("SCADBUDDY_SECRET_KEY_FILE", raising=False)
    revision = ModelHistory(settings.data_dir / "models", wrapper_prefix=WRAPPER_PREFIX)
    commit = revision.ensure_repo()
    assert commit is not None
    params: dict[str, Any] = {"width": 12}
    job = Job(
        id=uuid.uuid4().hex,
        slug=model,
        params=params,
        inputs={"params": params},
        model_version=commit,
        created_at=datetime.now(UTC),
    )
    worker_data = tmp_path / "worker-data"
    with fake_bambuddy() as bambuddy:
        use_bambuddy_store(projection.pool, bambuddy)
        # The API's pin before it starts the render: the worker has no git.
        await pin_snapshot(projection.pool, settings, model, commit)
        async with temporal_client() as client, _api(projection, settings, tmp_path) as api:
            queue = f"t-{uuid.uuid4().hex[:8]}"
            cfg = NoDatabaseSettings(
                data_dir=worker_data,
                openscad=fake_3mf_openscad(tmp_path / "bin"),
                temporal_address=current_address(client),
                temporal_namespace=client.namespace,
                temporal_sdk_metrics_port=0,
                temporal_task_queue_render=queue,
                revision=f"test-{uuid.uuid4().hex[:8]}",
                api_internal_url=api.url,
                service_bootstrap_file=api.secret,
            )
            assert cfg.database_url == "" and cfg.secret_key_file is None
            stop = asyncio.Event()
            # Until the worker stops: the fixtures' own teardown connects.
            with pytest.MonkeyPatch.context() as refused:
                tried = _no_database(refused)
                worker = asyncio.create_task(
                    worker_module.run_worker(cfg, stop=stop, health_port=health_socket)
                )
                try:
                    port = health_socket.getsockname()[1]
                    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
                        health = await _healthy(http, worker)
                        job = await render_to_end(client, queue, job)
                finally:
                    stop.set()
                    await asyncio.wait_for(worker, timeout=60)
    assert tried == []
    assert health["store"]["backend"] == "bambuddy"
    stored = projection.read(job.id)
    assert stored.state == "done", stored.error
    # The source came from the store, not a volume the API shares.
    assert (worker_data / "blobs" / piece_key(model, commit, "model.scad", params)).is_dir()
    with projection.pool.connection() as conn:
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


async def _healthy(http: httpx.AsyncClient, worker: asyncio.Task[None]) -> dict[str, Any]:
    for _ in range(600):
        if worker.done():
            worker.result()
            raise AssertionError("the worker exited before serving /healthz")
        try:
            response = await http.get("/healthz")
        except httpx.TransportError:
            await asyncio.sleep(0.1)
            continue
        assert response.status_code == 200
        body: dict[str, Any] = response.json()
        return body
    raise AssertionError("/healthz never answered")
