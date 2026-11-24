"""The camera callers' filtered change streams (camera recording, phase 1,
`api/crud_camera.py`, `api/crud_camera_relay.py`): only ``printer.changed`` and
``camera.reconnect`` events of ``scadbuddy_events`` arrive, live and in replay."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import psycopg
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI

from scadbuddy.agent_changes.component import CHANGES
from scadbuddy.agent_changes.hub import ChangeHub
from scadbuddy.api import crud_areas, crud_camera, crud_camera_relay
from scadbuddy.auth.check import auth_check
from scadbuddy.auth.component import AUTH
from scadbuddy.auth.gate import AuthGate
from scadbuddy.auth.service_credentials import (
    InProcessProvider,
    ServiceCaller,
    ServiceCredentialStore,
)
from scadbuddy.core.events import ModelEvent, PrinterEvent
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.pg_listener import PgListener
from scadbuddy.core.problems import install_problem_handlers
from scadbuddy.crud import listener as crud_listener
from scadbuddy.render.pg_store import migrate
from tests.conftest import open_pg_pool
from tests.crud.crud_support import _NoSessions, _Noted
from tests.crud.test_crud_agent_changes import PROMPTLY, Served, Sse

pytestmark = pytest.mark.requires_postgres

ROUTES: list[tuple[ServiceCaller, str]] = [
    ("camera", "/api/v1/internal/crud/camera"),
    ("camera-relay", "/api/v1/internal/crud/camera-relay"),
]


def _printer(kind: str = "printer.changed") -> PrinterEvent:
    return PrinterEvent(kind=kind, printer_id="p1")  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def served(pg_conninfo: str) -> AsyncIterator[Served]:
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        migrate(conn)
    pool = open_pg_pool(pg_conninfo)
    credentials = ServiceCredentialStore(pool)
    listener = PgListener(pg_conninfo, check_interval=1.0, backoff=0.05, max_backoff=0.2)
    bus = PgNotifyEventBus(pg_conninfo, listener=listener)
    hub = ChangeHub(per_credential=4)
    hub.attach(listener, bus)
    await bus.start()
    parts: dict[Any, Any] = {
        AUTH: SimpleNamespace(
            would_refuse=_Noted(),
            verifiers={},
            credentials=credentials,
            core=SimpleNamespace(metrics=Metrics()),
        ),
        CHANGES: hub,
    }
    app = FastAPI(dependencies=[Depends(auth_check)])
    app.state.scadbuddy = SimpleNamespace(
        settings=SimpleNamespace(auth_mode="enforce"),
        components=SimpleNamespace(get=lambda key: parts[key]),
        events=bus,
    )
    app.include_router(crud_camera.router, prefix="/api/v1")
    app.include_router(crud_camera_relay.router, prefix="/api/v1")
    app.include_router(crud_areas.router, prefix="/api/v1")
    install_problem_handlers(app)
    app.add_middleware(AuthGate, sessions=lambda: _NoSessions(), credentials=lambda: credentials)
    await crud_listener.start(app, 0)
    port = crud_listener.crud_listener_port(app)

    def token(caller: ServiceCaller = "camera") -> dict[str, str]:
        minted = InProcessProvider(caller, lambda: credentials).token()
        assert minted is not None
        return {"authorization": f"Bearer {minted}"}

    try:
        yield Served(f"http://127.0.0.1:{port}", bus, hub, token, pg_conninfo)
    finally:
        hub.shutdown()
        await crud_listener.stop(app)
        await bus.aclose()
        pool.close()


async def _open(
    client: httpx.AsyncClient,
    served: Served,
    caller: ServiceCaller,
    base: str,
    last: str | None = None,
) -> tuple[Any, Sse]:
    headers = {**served.token(caller), **({"last-event-id": last} if last is not None else {})}
    context = client.stream("GET", f"{served.url}{base}/changes", headers=headers)
    response = await context.__aenter__()
    assert response.status_code == 200, await response.aread()
    return context, Sse(response)


@pytest.mark.parametrize(("caller", "base"), ROUTES)
async def test_camera_stream_filters_kinds(
    served: Served, caller: ServiceCaller, base: str
) -> None:
    async with httpx.AsyncClient(timeout=None) as client:
        ctx, sse = await _open(client, served, caller, base)
        assert (await sse.next())["event"] == "ready"
        served.bus.publish(_printer("printer.report"))
        served.bus.publish(ModelEvent(kind="model.updated", slug="m"))
        wanted = _printer()
        served.bus.publish(wanted)
        got = await sse.next()
        assert got["id"] == wanted.id
        assert json.loads(got["data"]["p"])["kind"] == "printer.changed"
        # No other channel's change reaches it either.
        with pytest.raises(TimeoutError):
            await sse.next(0.5)
        await ctx.__aexit__(None, None, None)


@pytest.mark.parametrize(("caller", "base"), ROUTES)
async def test_camera_stream_replays_after_last_event_id(
    served: Served, caller: ServiceCaller, base: str
) -> None:
    first, skipped, second, other = (
        _printer(),
        _printer("printer.report"),
        _printer(),
        ModelEvent(kind="model.updated", slug="x"),
    )
    async with httpx.AsyncClient(timeout=None) as client:
        ctx, sse = await _open(client, served, caller, base)
        await sse.next()
        served.bus.publish(first)
        assert (await sse.next())["id"] == first.id
        await ctx.__aexit__(None, None, None)
        for event in (skipped, second, other):
            served.bus.publish(event)
        while await served.bus.newest() != other.id:
            await asyncio.sleep(0.05)
        ctx, sse = await _open(client, served, caller, base, first.id)
        replayed = await sse.next()
        ready = await sse.next()
        assert replayed["id"] == second.id
        assert ready["event"] == "ready"
        assert ready["data"] == {"resync": False, "reconnect": True}
        assert ready["id"] == other.id
        later = _printer()
        served.bus.publish(later)
        assert (await sse.next())["id"] == later.id
        await ctx.__aexit__(None, None, None)


@pytest.mark.parametrize(("caller", "base"), ROUTES)
async def test_camera_stream_resync_on_bad_id(
    served: Served, caller: ServiceCaller, base: str
) -> None:
    async with httpx.AsyncClient(timeout=None) as client:
        ctx, sse = await _open(client, served, caller, base, "not an id")
        ready = await sse.next()
        assert ready["event"] == "ready"
        assert ready["data"] == {"resync": True, "reconnect": True}
        await ctx.__aexit__(None, None, None)


async def test_the_camera_callers_have_the_changes_area(served: Served) -> None:
    async with httpx.AsyncClient(timeout=PROMPTLY) as client:
        for caller, base in ROUTES:
            areas = await client.get(f"{served.url}{base}/areas", headers=served.token(caller))
            assert "changes" in areas.json()["areas"]
        refused = await client.get(
            f"{served.url}{ROUTES[0][1]}/changes", headers=served.token("camera-relay")
        )
        assert refused.status_code == 403
