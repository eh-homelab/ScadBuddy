"""The projects worker holds no database (#5298 Task 9): it starts and runs a flow with
every way to open a Postgres connection refused, its credential exchanged for its
bootstrap proof, and it refuses to start with no proof to exchange."""

from __future__ import annotations

import asyncio
import base64
import faulthandler
import logging
import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

import httpx
import psycopg
import psycopg_pool
import pytest

from scadbuddy import worker as worker_module
from scadbuddy.auth.exchange import BootstrapVerifier
from scadbuddy.auth.service_credentials import ServiceCredentialConfigError, ServiceCredentialStore
from scadbuddy.core.settings import NoDatabaseSettings, Settings
from scadbuddy.crud import listener
from scadbuddy.flows.grants import GrantSpec
from scadbuddy.flows.operations import request_id
from scadbuddy.flows.store import FlowStore
from scadbuddy.operations.store import OperationStore
from scadbuddy.render.projection import JobProjection
from scadbuddy.workflows.flows_client import connect_flows
from tests.conftest import UNUSED_TEMPORAL_ADDRESS
from tests.crud.crud_support import TEST_KEK, DerivedPayloadKeys, agent_key_tables, crud_app
from tests.flows.fake_api import FakeApi, Outward
from tests.flows.flows_support import script
from tests.support.temporal import temporal_available, temporal_server


def _no_database(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every way the worker could open Postgres refused; the list names each attempt,
    in case the caller swallowed the refusal."""
    tried: list[str] = []

    def refuse(name: str) -> Any:
        def refused(*args: Any, **kwargs: Any) -> NoReturn:
            tried.append(name)
            raise AssertionError(f"the projects worker called {name}")

        return refused

    monkeypatch.setattr(psycopg, "connect", refuse("psycopg.connect"))
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", refuse("AsyncConnection.connect"))
    monkeypatch.setattr(psycopg_pool.ConnectionPool, "__init__", refuse("ConnectionPool"))
    return tried


def _kek_file(tmp_path: Path) -> Path:
    path = tmp_path / "kek"
    path.write_text(base64.b64encode(TEST_KEK.key).decode())
    return path


def test_the_projects_queue_reads_settings_with_no_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[Settings] = []

    async def run(settings: Settings, queue: str) -> None:
        started.append(settings)

    monkeypatch.delenv("SCADBUDDY_DATABASE_URL", raising=False)
    monkeypatch.setenv("SCADBUDDY_TEMPORAL_ADDRESS", UNUSED_TEMPORAL_ADDRESS)
    monkeypatch.setattr("sys.argv", ["scadbuddy.worker", "--queue", "projects"])
    monkeypatch.setattr(worker_module, "_main", run)
    monkeypatch.setattr(worker_module, "configure_tracing", lambda *a, **k: None)
    monkeypatch.setattr(worker_module, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(faulthandler, "register", lambda *a, **k: None)
    worker_module.main()
    [settings] = started
    assert isinstance(settings, NoDatabaseSettings) and settings.database_url == ""


async def test_with_no_proof_to_exchange_it_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No view to fall back to: a credential comes only from the exchange."""
    tried = _no_database(monkeypatch)
    settings = NoDatabaseSettings(
        data_dir=tmp_path,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        secret_key_file=_kek_file(tmp_path),
        api_internal_url="http://scadbuddy:8080",
    )
    with pytest.raises(ServiceCredentialConfigError, match="SCADBUDDY_SERVICE_TOKEN_FILE"):
        await worker_module.run_projects_worker(settings, health_port=None)
    assert tried == []


@pytest.fixture(scope="module")
def temporal_address() -> Iterator[str]:
    if not temporal_available():
        pytest.skip("no Temporal")
    with temporal_server() as address:
        yield address


@pytest.fixture
def jobs(pg_conninfo: str) -> Iterator[JobProjection]:
    store = JobProjection(pg_conninfo, pool_size=10)
    store.open()
    try:
        yield store
    finally:
        store.close()


class _Served:
    def __init__(self, url: str, secret: Path, flows: Outward) -> None:
        self.url = url
        self.secret = secret
        self.flows = flows


@asynccontextmanager
async def _api(
    jobs: JobProjection, temporal_address: str, tmp_path: Path
) -> AsyncIterator[_Served]:
    """The API's side, made before the database is refused: `crud_app` with the exchange
    of a bootstrap secret for `projects`, its payload keys on a connection of its own."""
    store = FlowStore(jobs.pool)
    agent_key_tables(store)
    conninfo = str(store._pool.conninfo)
    connect = psycopg.AsyncConnection.connect

    @asynccontextmanager
    async def api_connect() -> AsyncIterator[psycopg.AsyncConnection[Any]]:
        async with await connect(conninfo, autocommit=True) as conn:
            yield conn

    keys = DerivedPayloadKeys(api_connect, TEST_KEK)
    client = await connect_flows(temporal_address, "default", keys)
    secrets_dir = tmp_path / "bootstrap"
    secrets_dir.mkdir()
    secret = secrets_dir / "projects"
    secret.write_text(secrets.token_urlsafe(32))
    app = crud_app(
        store,
        ServiceCredentialStore(jobs.pool),
        client=client,
        keys=keys,
        operations=OperationStore(jobs.pool),
        bootstrap=BootstrapVerifier.from_dir(secrets_dir),
    )
    await listener.start(app, 0)
    queue = f"projects-{uuid.uuid4().hex[:8]}"
    try:
        yield _Served(
            f"http://127.0.0.1:{listener.crud_listener_port(app)}",
            secret,
            Outward(client, queue, store),
        )
    finally:
        await listener.stop(app)


@pytest.mark.requires_postgres
@pytest.mark.requires_temporal
async def test_a_flow_runs_on_a_projects_worker_with_no_database(
    jobs: JobProjection,
    temporal_address: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Its record, its grant's decision and consume, and its close all go through the
    API's CRUD routes, under a credential it exchanged its bootstrap secret for."""
    fake = FakeApi()

    def outward_api(base_url: str, auth: httpx.Auth) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=fake.app), base_url="http://api", auth=auth
        )

    monkeypatch.setattr(worker_module, "api_client", outward_api)
    monkeypatch.delenv("SCADBUDDY_DATABASE_URL", raising=False)
    async with _api(jobs, temporal_address, tmp_path) as api:
        flows = api.flows
        settings = NoDatabaseSettings(
            data_dir=tmp_path,
            temporal_address=temporal_address,
            temporal_namespace="default",
            temporal_sdk_metrics_port=0,
            temporal_task_queue_projects=flows.queue,
            revision=f"test-{uuid.uuid4().hex[:8]}",
            secret_key_file=_kek_file(tmp_path),
            api_internal_url=api.url,
            service_bootstrap_file=api.secret,
        )
        stop = asyncio.Event()
        # Until the worker stops: the fixtures' own teardown connects.
        with (
            pytest.MonkeyPatch.context() as refused,
            caplog.at_level(logging.WARNING, logger="scadbuddy.worker"),
        ):
            tried = _no_database(refused)
            worker = asyncio.create_task(
                worker_module.run_projects_worker(settings, stop=stop, health_port=None)
            )
            try:
                spec = GrantSpec.model_validate(
                    {
                        "expires_at": datetime.now(UTC) + timedelta(hours=1),
                        "rules": [{"fn": "arrange"}],
                    }
                )
                async with asyncio.timeout(60):
                    run_id = await flows.run(
                        script(
                            "a = await arrange({'objects': [{'output_id': 'o1'}]})",
                            "return a['status']",
                        ),
                        grant=spec,
                    )
                    run = await flows.finished(run_id)
            finally:
                stop.set()
                await asyncio.wait_for(worker, timeout=30)
    assert tried == []
    assert run.result == "result: 'done'", run.result
    [step] = run.steps
    assert (step.fn, step.status) == ("arrange", "succeeded") and step.grant_id
    decision = await flows.store.get_decision(request_id(run, step.call_id))
    assert decision is not None
    assert (decision.outcome, decision.responder) == ("approved", f"grant:{step.grant_id}")
    [use] = await flows.store.grant_uses(run_id)
    assert use.request_id == decision.request_id
    assert len(fake.to("/api/v1/outputs/arrange")) == 1
    assert "SCADBUDDY_DATABASE_URL" not in caplog.text


async def test_a_database_url_is_ignored_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A Deployment that still sets the secret: the worker never opens it, and gets its
    credential only from the exchange."""
    tried = _no_database(monkeypatch)
    settings = NoDatabaseSettings(
        data_dir=tmp_path,
        database_url="postgresql://projects@db.invalid/scadbuddy",
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        secret_key_file=_kek_file(tmp_path),
        api_internal_url="http://scadbuddy:8080",
    )
    with (
        caplog.at_level(logging.WARNING, logger="scadbuddy.worker"),
        pytest.raises(ServiceCredentialConfigError),
    ):
        await worker_module.run_projects_worker(settings, health_port=None)
    assert "ignoring SCADBUDDY_DATABASE_URL" in caplog.text and tried == []
