"""The projects worker's CRUD routes served for a test (#5298): the routes of
`api/crud_projects.py` over a real `FlowStore` and `api/crud_areas.py`, behind the real
`auth_check` and gate and the CRUD listener on a local port, and a `RemoteFlowStore`
that reaches them with an in-process credential, as the projects worker does."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

from fastapi import Depends, FastAPI
from temporalio.client import Client

from scadbuddy.api import crud_areas, crud_projects
from scadbuddy.auth.check import auth_check
from scadbuddy.auth.component import AUTH
from scadbuddy.auth.gate import AuthGate
from scadbuddy.auth.service_credentials import (
    InProcessProvider,
    ServiceAuth,
    ServiceCredentialStore,
)
from scadbuddy.core.problems import install_problem_handlers
from scadbuddy.crud import listener
from scadbuddy.crud.client import CrudClient
from scadbuddy.crud.projects import RemoteFlowStore
from scadbuddy.flows.component import FLOWS
from scadbuddy.flows.store import FlowStore


class _Noted:
    def note(self, *args: object) -> None: ...


class _NoSessions:
    def resolve(self, token: str) -> None:
        return None


def crud_app(
    store: FlowStore, credentials: ServiceCredentialStore, *, client: Client | None = None
) -> FastAPI:
    """The routes with the flows component's `store` and Temporal `client` (a Reset's)."""
    app = FastAPI(dependencies=[Depends(auth_check)])
    parts: dict[Any, Any] = {
        AUTH: SimpleNamespace(would_refuse=_Noted()),
        FLOWS: SimpleNamespace(store=store, client=client),
    }
    app.state.scadbuddy = SimpleNamespace(
        settings=SimpleNamespace(auth_mode="enforce"),
        components=SimpleNamespace(get=lambda key: parts[key]),
    )
    app.include_router(crud_projects.router, prefix="/api/v1")
    app.include_router(crud_areas.router, prefix="/api/v1")
    install_problem_handlers(app)
    app.add_middleware(AuthGate, sessions=lambda: _NoSessions(), credentials=lambda: credentials)
    return app


@asynccontextmanager
async def served_flow_store(
    store: FlowStore, *, client: Client | None = None, **options: Any
) -> AsyncIterator[RemoteFlowStore]:
    """`RemoteFlowStore` over `crud_app` on a local port, its credential minted in process
    on `store`'s schema; `options` are the `CrudClient`'s."""
    credentials = ServiceCredentialStore(store._pool)
    app = crud_app(store, credentials, client=client)
    await listener.start(app, 0)
    crud = CrudClient(
        f"http://127.0.0.1:{listener.crud_listener_port(app)}",
        "projects",
        ServiceAuth(InProcessProvider("projects", lambda: credentials)),
        **options,
    )
    try:
        yield RemoteFlowStore(crud)
    finally:
        await crud.aclose()
        await listener.stop(app)
