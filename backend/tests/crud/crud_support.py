"""The workers' CRUD routes served for a test (#5298): the routes of `api/crud_projects.py`
over a real `FlowStore` and `OperationStore`, or `api/crud_render.py` over a settings pool
(its blob refs, piece geometry and blob index too) and a `JobProjection`, and
`api/crud_areas.py`, behind the real `auth_check` and gate and the CRUD listener on a
local port, and a client that reaches them with an in-process credential, as the worker
does. `api_payload_keys` gives the routes the API's payload keys on a store's schema, and
`served_projects` serves a projects worker's store and payload keys as the worker's. With
a bootstrap verifier the app also takes the service-token exchange, so a worker can get its
own credential as it does with no database. `static_render_crud` is a render worker's
client of a stand-in API that answers fixed store settings and serves no job route, for a
test of the worker that renders nothing. `threaded_render_crud` is a render worker's
blocking client of the render routes served on a thread of their own, for a test that
calls them from its event loop or a sync test; `threaded_render_index` is the worker's
`RemoteBlobIndex` over it. `FakeClock` is a
client clock that moves only by the client's own waits, so a test's count of tries does
not depend on wall time."""

from __future__ import annotations

import base64
import hashlib
import threading
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import psycopg
import uvicorn
from fastapi import Depends, FastAPI
from temporalio.client import Client

from scadbuddy.api import crud_areas, crud_projects, crud_render, service_token
from scadbuddy.auth.check import auth_check
from scadbuddy.auth.component import AUTH
from scadbuddy.auth.exchange import BootstrapVerifier
from scadbuddy.auth.gate import AuthGate
from scadbuddy.auth.service_credentials import (
    InProcessProvider,
    ServiceAuth,
    ServiceCredentialStore,
)
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.problems import install_problem_handlers
from scadbuddy.core.secrets import kek_from_base64, seal_bytes
from scadbuddy.core.settings import Settings
from scadbuddy.crud import listener
from scadbuddy.crud.client import CrudClient, CrudSyncClient
from scadbuddy.crud.guard import CRUD_HEADERS
from scadbuddy.crud.projects import RemoteFlowStore, RemotePayloadKeys
from scadbuddy.crud.render import (
    RemoteBlobIndex,
    RemoteBlobRefs,
    RemoteGeometryIndex,
    RemoteJobProjection,
)
from scadbuddy.flows.component import FLOWS
from scadbuddy.flows.store import FlowStore
from scadbuddy.library.assets import AssetStore
from scadbuddy.library.settings_store import RenderStoreSettings, SettingsStore
from scadbuddy.operations.component import OPERATIONS
from scadbuddy.operations.kinds import OperationKind
from scadbuddy.operations.store import OperationStore
from scadbuddy.render.projection import JobProjection
from scadbuddy.store import BlobRefs
from scadbuddy.store.index import Pool
from scadbuddy.workflows.flows_client import connect_flows
from scadbuddy.workflows.payload_codec import Connect, PgPayloadKeys, payload_key_context

#: The agent's migrations that make `ai_payload_keys` and `ai_forgotten_subjects`.
AGENT_MIGRATIONS = Path(__file__).resolve().parent.parent / "fixtures" / "agent-migrations"
#: The KEK of the API and the worker in a test.
TEST_KEK = kek_from_base64(base64.b64encode(b"\x5a" * 32).decode())


class FakeClock:
    """A `CrudClient` clock that moves only by the client's own waits, so a test's count
    of tries does not depend on how long a round trip takes."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Noted:
    def note(self, *args: object) -> None: ...


class _NoSessions:
    def resolve(self, token: str) -> None:
        return None


def agent_key_tables(store: FlowStore) -> Connect:
    """A connection to `store`'s schema, with the agent's key tables made there first."""
    conninfo = cast(str, store._pool.conninfo)
    with psycopg.connect(conninfo, autocommit=True) as conn:
        made = conn.execute("SELECT to_regclass('ai_payload_keys')").fetchone()
        if made is None or made[0] is None:
            for sql in sorted(AGENT_MIGRATIONS.glob("*.sql")):
                conn.execute(sql.read_text().encode())

    @asynccontextmanager
    async def connect() -> AsyncIterator[psycopg.AsyncConnection[Any]]:
        async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as conn:
            yield conn

    return connect


def api_payload_keys(store: FlowStore) -> PgPayloadKeys:
    """The API's payload keys under `TEST_KEK`, on `store`'s schema."""
    return PgPayloadKeys(agent_key_tables(store), TEST_KEK)


class DerivedPayloadKeys(PgPayloadKeys):
    """The API's payload keys, each made as `tests/flows/flows_support.py` `Keys` derives
    it, so a history recorded over the routes replays under `Keys`
    (`test_project_replay.py`), and a test's own `Keys` opens what the worker sealed. It
    skips `create_key`'s advisory lock on purpose: a derived key is the same whoever makes
    it. Production code takes the lock."""

    async def sealed_key(self, subject: str, create: bool) -> tuple[bytes, str] | None:
        if create:
            key = hashlib.sha256(subject.encode()).digest()
            sealed = seal_bytes(TEST_KEK.key, key, payload_key_context(subject))
            async with self._connect() as conn:
                await conn.execute(
                    "INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id)"
                    " SELECT %s, %s, %s WHERE NOT EXISTS"
                    " (SELECT 1 FROM ai_forgotten_subjects WHERE subject = %s)"
                    " ON CONFLICT (subject) DO NOTHING",
                    (subject, sealed, TEST_KEK.id, subject),
                )
        return await super().sealed_key(subject, create)


def crud_app(
    store: FlowStore,
    credentials: ServiceCredentialStore,
    *,
    client: Client | None = None,
    keys: PgPayloadKeys | None = None,
    operations: OperationStore | None = None,
    kinds: Mapping[str, OperationKind] | None = None,
    settings_store: SettingsStore | None = None,
    bootstrap: BootstrapVerifier | None = None,
) -> FastAPI:
    """The routes with the flows component's `store`, Temporal `client` (a Reset's) and
    payload `keys`, the operations component's store and `kinds`, and the API's
    `settings_store`; the exchange too, of the proofs `bootstrap` takes, when given."""
    app = FastAPI(dependencies=[Depends(auth_check)])
    parts: dict[Any, Any] = {
        AUTH: SimpleNamespace(
            would_refuse=_Noted(),
            verifiers={} if bootstrap is None else {"bootstrap": bootstrap},
            credentials=credentials,
            core=SimpleNamespace(metrics=Metrics()),
        ),
        FLOWS: SimpleNamespace(store=store, client=client, keys=keys),
        OPERATIONS: SimpleNamespace(store=operations, kinds=kinds or {}),
    }
    app.state.scadbuddy = SimpleNamespace(
        settings=SimpleNamespace(auth_mode="enforce"),
        components=SimpleNamespace(get=lambda key: parts[key]),
        settings_store=settings_store,
    )
    app.include_router(crud_projects.router, prefix="/api/v1")
    app.include_router(crud_areas.router, prefix="/api/v1")
    if bootstrap is not None:
        app.include_router(service_token.router, prefix="/api/v1")
    install_problem_handlers(app)
    app.add_middleware(AuthGate, sessions=lambda: _NoSessions(), credentials=lambda: credentials)
    return app


@asynccontextmanager
async def served_crud(
    store: FlowStore,
    *,
    client: Client | None = None,
    keys: PgPayloadKeys | None = None,
    operations: OperationStore | None = None,
    kinds: Mapping[str, OperationKind] | None = None,
    settings_store: SettingsStore | None = None,
    **options: Any,
) -> AsyncIterator[CrudClient]:
    """A projects `CrudClient` of `crud_app` on a local port, its credential minted in
    process on `store`'s schema; `options` are the `CrudClient`'s."""
    credentials = ServiceCredentialStore(store._pool)
    app = crud_app(
        store,
        credentials,
        client=client,
        keys=keys,
        operations=operations,
        kinds=kinds,
        settings_store=settings_store,
    )
    await listener.start(app, 0)
    crud = CrudClient(
        f"http://127.0.0.1:{listener.crud_listener_port(app)}",
        "projects",
        ServiceAuth(InProcessProvider("projects", lambda: credentials)),
        **options,
    )
    try:
        yield crud
    finally:
        await crud.aclose()
        await listener.stop(app)


@asynccontextmanager
async def served_flow_store(
    store: FlowStore, *, client: Client | None = None, **options: Any
) -> AsyncIterator[RemoteFlowStore]:
    """`RemoteFlowStore` over `served_crud`."""
    async with served_crud(store, client=client, **options) as crud:
        yield RemoteFlowStore(crud)


def render_crud_app(
    pool: Pool,
    settings: Settings | None,
    credentials: ServiceCredentialStore,
    *,
    projection: JobProjection | None = None,
) -> FastAPI:
    """The render routes, reading the store settings from ``pool`` under ``settings``
    (none: a test of the job routes only), writing the job rows through ``projection``,
    and the blob refs, piece geometry, blob index and the uploads' rows on ``pool`` (the
    API's upload files are not these routes': its `AssetStore` has no root to read)."""
    app = FastAPI(dependencies=[Depends(auth_check)])
    parts: dict[Any, Any] = {AUTH: SimpleNamespace(would_refuse=_Noted())}
    app.state.scadbuddy = SimpleNamespace(
        settings=settings or SimpleNamespace(auth_mode="enforce"),
        settings_store=SimpleNamespace(pool=pool),
        projection=projection,
        refs=BlobRefs(pool),
        assets=AssetStore(Path("/nonexistent/api-assets"), pool),
        components=SimpleNamespace(get=lambda key: parts[key]),
    )
    app.include_router(crud_render.router, prefix="/api/v1")
    app.include_router(crud_areas.router, prefix="/api/v1")
    install_problem_handlers(app)
    app.add_middleware(AuthGate, sessions=lambda: _NoSessions(), credentials=lambda: credentials)
    return app


@asynccontextmanager
async def served_render_crud(
    pool: Pool,
    settings: Settings | None = None,
    *,
    projection: JobProjection | None = None,
    **options: Any,
) -> AsyncIterator[CrudClient]:
    """A render worker's `CrudClient` over `render_crud_app` on a local port, its
    credential minted in process on ``pool``'s schema; `options` are the client's."""
    credentials = ServiceCredentialStore(pool)
    app = render_crud_app(pool, settings, credentials, projection=projection)
    await listener.start(app, 0)
    crud = CrudClient(
        f"http://127.0.0.1:{listener.crud_listener_port(app)}",
        "render",
        ServiceAuth(InProcessProvider("render", lambda: credentials)),
        **options,
    )
    try:
        yield crud
    finally:
        await crud.aclose()
        await listener.stop(app)


@asynccontextmanager
async def served_render_jobs(
    projection: JobProjection, **options: Any
) -> AsyncIterator[RemoteJobProjection]:
    """The render worker's `RemoteJobProjection` over the job routes on ``projection``,
    as `served_render_crud` serves them."""
    async with served_render_crud(projection.pool, projection=projection, **options) as crud:
        jobs = RemoteJobProjection(crud.blocking())
        try:
            yield jobs
        finally:
            jobs.close()


@asynccontextmanager
async def served_render_refs(
    projection: JobProjection, **options: Any
) -> AsyncIterator[tuple[RemoteBlobRefs, RemoteGeometryIndex]]:
    """The render worker's `RemoteBlobRefs` and `RemoteGeometryIndex` over the routes on
    ``projection``'s pool, as `served_render_crud` serves them."""
    async with served_render_crud(projection.pool, projection=projection, **options) as crud:
        client = crud.blocking()
        try:
            yield RemoteBlobRefs(client), RemoteGeometryIndex(client)
        finally:
            client.close()


@contextmanager
def threaded_render_crud(pool: Pool) -> Iterator[CrudSyncClient]:
    """A render worker's `CrudSyncClient` of the render routes on ``pool``, served by a
    server on a thread and event loop of its own, so a blocking call from the test's loop
    (or a sync test) is answered."""
    credentials = ServiceCredentialStore(pool)
    app = render_crud_app(pool, None, credentials)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, name="crud render routes", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "the server did not start"
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    client = CrudSyncClient(
        f"http://127.0.0.1:{port}",
        "render",
        ServiceAuth(InProcessProvider("render", lambda: credentials)),
        unavailable_budget=5,
    )
    try:
        yield client
    finally:
        client.close()
        server.should_exit = True
        thread.join(30)


@contextmanager
def threaded_render_index(pool: Pool) -> Iterator[RemoteBlobIndex]:
    """`RemoteBlobIndex` over `threaded_render_crud`, for a test that calls the index from
    its event loop, as `tests/store` calls the SQL one."""
    with threaded_render_crud(pool) as client:
        yield RemoteBlobIndex(client)


class FixedToken:
    """A render credential the stand-in API takes."""

    def token(self) -> str | None:
        return "sbsvc_render"

    def invalidate(self, token: str) -> None: ...


def static_render_crud(current: RenderStoreSettings | None = None) -> CrudClient:
    """A render worker's client of an API that lists the ``settings``, ``jobs``, ``refs``,
    ``blob-index``, ``store-folders`` and ``assets`` areas and answers ``current`` (the
    defaults: the local store) as the store settings. Its job, ref, geometry, index, folder
    and upload routes are missing (a 404)."""
    body = (current or RenderStoreSettings()).model_dump(mode="json")

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/render/areas"):
            return httpx.Response(
                200,
                json={
                    "areas": ["settings", "jobs", "refs", "blob-index", "store-folders", "assets"]
                },
                headers=CRUD_HEADERS,
            )
        if request.url.path.endswith("/render/settings"):
            return httpx.Response(200, json=body, headers=CRUD_HEADERS)
        return httpx.Response(404, headers=CRUD_HEADERS)

    return CrudClient(
        "http://api", "render", ServiceAuth(FixedToken()), transport=httpx.MockTransport(answer)
    )


@asynccontextmanager
async def served_projects(
    store: FlowStore, temporal_address: str
) -> AsyncIterator[tuple[Client, RemoteFlowStore, Client]]:
    """The API's flows client, on `DerivedPayloadKeys`, and a projects worker's store and
    client over `served_crud`, its payload keys through the routes (`RemotePayloadKeys`)."""
    keys = DerivedPayloadKeys(agent_key_tables(store), TEST_KEK)
    client = await connect_flows(temporal_address, "default", keys)
    async with served_crud(store, client=client, keys=keys) as crud:
        worker = await connect_flows(temporal_address, "default", RemotePayloadKeys(crud, TEST_KEK))
        yield client, RemoteFlowStore(crud), worker
