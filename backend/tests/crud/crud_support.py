"""The workers' CRUD routes served for a test (#5298): the routes of
`api/crud_projects.py` over a real `FlowStore` and `OperationStore`, or `api/crud_render.py`
over a settings pool, and `api/crud_areas.py`, behind the real `auth_check` and gate and the
CRUD listener on a local port, and a client that reaches them with an in-process credential,
as the worker does. `static_render_crud` is a render worker's client of a stand-in API that
answers fixed store settings, for a test of the worker rather than the routes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import Depends, FastAPI
from temporalio.client import Client

from scadbuddy.api import crud_areas, crud_projects, crud_render
from scadbuddy.auth.check import auth_check
from scadbuddy.auth.component import AUTH
from scadbuddy.auth.gate import AuthGate
from scadbuddy.auth.service_credentials import (
    InProcessProvider,
    ServiceAuth,
    ServiceCredentialStore,
)
from scadbuddy.core.problems import install_problem_handlers
from scadbuddy.core.settings import Settings
from scadbuddy.crud import listener
from scadbuddy.crud.client import CrudClient
from scadbuddy.crud.guard import CRUD_HEADERS
from scadbuddy.crud.projects import RemoteFlowStore
from scadbuddy.flows.component import FLOWS
from scadbuddy.flows.store import FlowStore
from scadbuddy.library.settings_store import RenderStoreSettings, SettingsStore
from scadbuddy.operations.component import OPERATIONS
from scadbuddy.operations.kinds import OperationKind
from scadbuddy.operations.store import OperationStore
from scadbuddy.store.index import Pool


class _Noted:
    def note(self, *args: object) -> None: ...


class _NoSessions:
    def resolve(self, token: str) -> None:
        return None


def crud_app(
    store: FlowStore,
    credentials: ServiceCredentialStore,
    *,
    client: Client | None = None,
    operations: OperationStore | None = None,
    kinds: Mapping[str, OperationKind] | None = None,
    settings_store: SettingsStore | None = None,
) -> FastAPI:
    """The routes with the flows component's `store` and Temporal `client` (a Reset's),
    the operations component's store and `kinds`, and the API's `settings_store`."""
    app = FastAPI(dependencies=[Depends(auth_check)])
    parts: dict[Any, Any] = {
        AUTH: SimpleNamespace(would_refuse=_Noted()),
        FLOWS: SimpleNamespace(store=store, client=client),
        OPERATIONS: SimpleNamespace(store=operations, kinds=kinds or {}),
    }
    app.state.scadbuddy = SimpleNamespace(
        settings=SimpleNamespace(auth_mode="enforce"),
        components=SimpleNamespace(get=lambda key: parts[key]),
        settings_store=settings_store,
    )
    app.include_router(crud_projects.router, prefix="/api/v1")
    app.include_router(crud_areas.router, prefix="/api/v1")
    install_problem_handlers(app)
    app.add_middleware(AuthGate, sessions=lambda: _NoSessions(), credentials=lambda: credentials)
    return app


@asynccontextmanager
async def served_crud(
    store: FlowStore,
    *,
    client: Client | None = None,
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


def render_crud_app(pool: Pool, settings: Settings, credentials: ServiceCredentialStore) -> FastAPI:
    """The render routes, reading the store settings from ``pool`` under ``settings``."""
    app = FastAPI(dependencies=[Depends(auth_check)])
    parts: dict[Any, Any] = {AUTH: SimpleNamespace(would_refuse=_Noted())}
    app.state.scadbuddy = SimpleNamespace(
        settings=settings,
        settings_store=SimpleNamespace(pool=pool),
        components=SimpleNamespace(get=lambda key: parts[key]),
    )
    app.include_router(crud_render.router, prefix="/api/v1")
    app.include_router(crud_areas.router, prefix="/api/v1")
    install_problem_handlers(app)
    app.add_middleware(AuthGate, sessions=lambda: _NoSessions(), credentials=lambda: credentials)
    return app


@asynccontextmanager
async def served_render_crud(
    pool: Pool, settings: Settings, **options: Any
) -> AsyncIterator[CrudClient]:
    """A render worker's `CrudClient` over `render_crud_app` on a local port, its
    credential minted in process on ``pool``'s schema; `options` are the client's."""
    credentials = ServiceCredentialStore(pool)
    app = render_crud_app(pool, settings, credentials)
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


class FixedToken:
    """A render credential the stand-in API takes."""

    def token(self) -> str | None:
        return "sbsvc_render"

    def invalidate(self, token: str) -> None: ...


def static_render_crud(current: RenderStoreSettings | None = None) -> CrudClient:
    """A render worker's client of an API that serves the ``settings`` area and answers
    ``current`` (the defaults: the local store) as the store settings."""
    body = (current or RenderStoreSettings()).model_dump(mode="json")

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/render/areas"):
            return httpx.Response(200, json={"areas": ["settings"]}, headers=CRUD_HEADERS)
        if request.url.path.endswith("/render/settings"):
            return httpx.Response(200, json=body, headers=CRUD_HEADERS)
        return httpx.Response(404, headers=CRUD_HEADERS)

    return CrudClient(
        "http://api", "render", ServiceAuth(FixedToken()), transport=httpx.MockTransport(answer)
    )
