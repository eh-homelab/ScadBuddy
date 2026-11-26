"""The camera callers' filtered change streams (camera recording, phase 1,
`api/crud_camera.py`, `api/crud_camera_relay.py`): only ``printer.changed`` and
``camera.reconnect`` events of ``scadbuddy_events`` arrive, live and in replay."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
import pytest
import pytest_asyncio

from scadbuddy.agent_changes import hub as hub_module
from scadbuddy.api import crud_agent_changes, crud_camera, crud_camera_relay
from scadbuddy.auth.service_credentials import (
    ServiceCaller,
)
from scadbuddy.core.events import ModelEvent, PrinterEvent
from tests.crud.crud_support import PROMPTLY, Served, Sse, served_changes

pytestmark = pytest.mark.requires_postgres

ROUTES: list[tuple[ServiceCaller, str]] = [
    ("camera", "/api/v1/internal/crud/camera"),
    ("camera-relay", "/api/v1/internal/crud/camera-relay"),
]


def _printer(kind: str = "printer.changed") -> PrinterEvent:
    return PrinterEvent(kind=kind, printer_id="p1")  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def served(pg_conninfo: str) -> AsyncIterator[Served]:
    async with served_changes(
        pg_conninfo, crud_camera.router, crud_camera_relay.router, crud_agent_changes.router
    ) as it:
        yield it


@asynccontextmanager
async def _opened(
    client: httpx.AsyncClient,
    served: Served,
    caller: ServiceCaller,
    base: str,
    last: str | None = None,
) -> AsyncIterator[Sse]:
    """One open stream, closed when the block ends, also when an assertion in it fails."""
    headers = {**served.token(caller), **({"last-event-id": last} if last is not None else {})}
    async with client.stream("GET", f"{served.url}{base}/changes", headers=headers) as response:
        assert response.status_code == 200, await response.aread()
        yield Sse(response)


@pytest.mark.parametrize(("caller", "base"), ROUTES)
async def test_camera_stream_filters_kinds(
    served: Served, caller: ServiceCaller, base: str
) -> None:
    async with (
        httpx.AsyncClient(timeout=None) as client,
        _opened(client, served, caller, base) as sse,
    ):
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
    async with (
        httpx.AsyncClient(timeout=None) as client,
        _opened(client, served, caller, base) as sse,
    ):
        await sse.next()
        served.bus.publish(first)
        assert (await sse.next())["id"] == first.id
        for event in (skipped, second, other):
            served.bus.publish(event)
        await _logged(served, other.id)
        async with _opened(client, served, caller, base, first.id) as sse:
            replayed = await sse.next()
            ready = await sse.next()
            assert replayed["id"] == second.id
            assert ready["event"] == "ready"
            assert ready["data"] == {"resync": False, "reconnect": True}
            assert ready["id"] == other.id
            later = _printer()
            served.bus.publish(later)
            assert (await sse.next())["id"] == later.id


@pytest.mark.parametrize(("caller", "base"), ROUTES)
async def test_camera_stream_resync_on_bad_id(
    served: Served, caller: ServiceCaller, base: str
) -> None:
    async with (
        httpx.AsyncClient(timeout=None) as client,
        _opened(client, served, caller, base, "not an id") as sse,
    ):
        ready = await sse.next()
        assert ready["event"] == "ready"
        assert ready["data"] == {"resync": True, "reconnect": True}


async def test_the_camera_callers_have_the_changes_area(served: Served) -> None:
    async with httpx.AsyncClient(timeout=PROMPTLY) as client:
        for caller, base in ROUTES:
            areas = await client.get(f"{served.url}{base}/areas", headers=served.token(caller))
            assert "changes" in areas.json()["areas"]
        refused = await client.get(
            f"{served.url}{ROUTES[0][1]}/changes", headers=served.token("camera-relay")
        )
        assert refused.status_code == 403


async def _logged(served: Served, event_id: str) -> None:
    async def wait() -> None:
        while await served.bus.newest() != event_id:
            await asyncio.sleep(0.05)

    await asyncio.wait_for(wait(), PROMPTLY)


async def _status(
    client: httpx.AsyncClient, served: Served, caller: ServiceCaller, base: str
) -> int:
    """The status of one request to a stream route, read and closed at once, so an answer that
    streams on never holds the test."""

    async def ask() -> int:
        async with client.stream(
            "GET", f"{served.url}{base}/changes", headers=served.token(caller)
        ) as response:
            return response.status_code

    return await asyncio.wait_for(ask(), PROMPTLY)


async def test_camera_streams_have_a_budget_of_their_own(served: Served) -> None:
    """Each token is a new credential, so the cap counts the caller group, and the agent's
    streams stay free while it is full."""
    agent_base = "/api/v1/internal/crud/agent"
    async with httpx.AsyncClient(timeout=None) as client, AsyncExitStack() as opened:
        for i in range(crud_camera.CAMERA_STREAMS_MAX):
            caller, base = ROUTES[i % 2]
            await opened.enter_async_context(_opened(client, served, caller, base))
        for caller, base in ROUTES:
            assert await _status(client, served, caller, base) == 429
        assert await _status(client, served, "agent", agent_base) == 200


@pytest.mark.parametrize(("caller", "base"), ROUTES)
async def test_a_closed_camera_stream_frees_its_place(
    served: Served, caller: ServiceCaller, base: str
) -> None:
    async with httpx.AsyncClient(timeout=None) as client:
        for _ in range(crud_camera.CAMERA_STREAMS_MAX + 1):
            async with _opened(client, served, caller, base) as sse:
                assert (await sse.next())["event"] == "ready"
            await asyncio.wait_for(_closed(served), PROMPTLY)


async def _closed(served: Served) -> None:
    while served.hub.streams:
        await asyncio.sleep(0.02)


async def test_a_long_run_of_filtered_events_is_a_resync(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hub_module, "REPLAY_MAX", 2)
    first = _printer()
    others = [_printer("printer.report") for _ in range(3)]
    async with (
        httpx.AsyncClient(timeout=None) as client,
        _opened(client, served, "camera", ROUTES[0][1]) as sse,
    ):
        await sse.next()
        served.bus.publish(first)
        assert (await sse.next())["id"] == first.id
        for event in others:
            served.bus.publish(event)
        await _logged(served, others[-1].id)
        async with _opened(client, served, "camera", ROUTES[0][1], first.id) as sse:
            ready = await sse.next()
            assert ready["data"] == {"resync": True, "reconnect": True}
