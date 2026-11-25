"""The fleet caller's filtered change stream (agent fleet chat, `api/crud_fleet.py`): only
``fleet.`` kinds of ``scadbuddy_events`` arrive, live and in replay."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg
import pytest
import pytest_asyncio

from scadbuddy.agent_changes import hub as hub_module
from scadbuddy.api import crud_agent_changes, crud_camera, crud_fleet
from scadbuddy.auth.service_credentials import ServiceCaller
from scadbuddy.core.events import PG_CHANNEL, ModelEvent, OpaqueEvent, PrinterEvent, encode_event
from scadbuddy.core.pg_events import hold_event_log
from tests.crud.crud_support import PROMPTLY, Served, Sse, served_changes

pytestmark = pytest.mark.requires_postgres

BASE = "/api/v1/internal/crud/fleet"
AGENT_BASE = "/api/v1/internal/crud/agent"


def _message(n: int = 1) -> OpaqueEvent:
    raw: dict[str, Any] = {
        "id": f"{n:032x}",
        "at": datetime.now(UTC).isoformat(),
        "kind": "fleet.message",
        "topics": [],
        "seq": n,
    }
    return OpaqueEvent.model_validate({**raw, "payload": raw})


def _emit(served: Served, event: OpaqueEvent) -> None:
    """What core (Go) does for a ``fleet.message``: log it and NOTIFY it. The bus publishes
    only known kinds, so this writes the row itself."""
    payload = encode_event(event)
    with psycopg.connect(served.conninfo) as conn:
        hold_event_log(conn)
        conn.execute(
            "INSERT INTO events (event_id, kind, at, payload) VALUES (%s, %s, %s, %s::jsonb)",
            (event.id, event.kind, event.at, payload),
        )
        conn.execute("SELECT pg_notify(%s, %s)", (PG_CHANNEL, payload))


@pytest_asyncio.fixture
async def served(pg_conninfo: str) -> AsyncIterator[Served]:
    async with served_changes(
        pg_conninfo, crud_fleet.router, crud_camera.router, crud_agent_changes.router
    ) as it:
        yield it


async def _open(
    client: httpx.AsyncClient, served: Served, last: str | None = None
) -> tuple[Any, Sse]:
    headers = {**served.token("fleet"), **({"last-event-id": last} if last is not None else {})}
    context = client.stream("GET", f"{served.url}{BASE}/changes", headers=headers)
    response = await context.__aenter__()
    assert response.status_code == 200, await response.aread()
    return context, Sse(response)


async def _logged(served: Served, event_id: str) -> None:
    async def wait() -> None:
        while await served.bus.newest() != event_id:
            await asyncio.sleep(0.05)

    await asyncio.wait_for(wait(), PROMPTLY)


async def test_fleet_stream_carries_only_fleet_kinds(served: Served) -> None:
    async with httpx.AsyncClient(timeout=None) as client:
        ctx, sse = await _open(client, served)
        assert (await sse.next())["event"] == "ready"
        served.bus.publish(PrinterEvent(kind="printer.changed", printer_id="p1"))
        served.bus.publish(ModelEvent(kind="model.updated", slug="m"))
        wanted = _message()
        _emit(served, wanted)
        got = await sse.next()
        assert got["id"] == wanted.id
        payload = json.loads(got["data"]["p"])
        assert payload["kind"] == "fleet.message"
        assert payload["seq"] == 1
        with pytest.raises(TimeoutError):
            await sse.next(0.5)
        await ctx.__aexit__(None, None, None)


async def test_fleet_stream_replays_after_last_event_id(served: Served) -> None:
    first, skipped, second = (
        _message(1),
        PrinterEvent(kind="printer.changed", printer_id="p"),
        _message(2),
    )
    async with httpx.AsyncClient(timeout=None) as client:
        ctx, sse = await _open(client, served)
        await sse.next()
        _emit(served, first)
        assert (await sse.next())["id"] == first.id
        await ctx.__aexit__(None, None, None)
        served.bus.publish(skipped)
        _emit(served, second)
        await _logged(served, second.id)
        ctx, sse = await _open(client, served, first.id)
        replayed = await sse.next()
        ready = await sse.next()
        assert replayed["id"] == second.id
        assert json.loads(replayed["data"]["p"])["seq"] == 2
        assert ready["data"] == {"resync": False, "reconnect": True}
        await ctx.__aexit__(None, None, None)


async def test_a_pruned_last_event_id_is_a_resync(served: Served) -> None:
    async with httpx.AsyncClient(timeout=None) as client:
        ctx, sse = await _open(client, served, "0" * 31 + "f")
        ready = await sse.next()
        assert ready["event"] == "ready"
        assert ready["data"]["resync"] is True
        await ctx.__aexit__(None, None, None)


async def test_a_long_run_of_filtered_events_is_a_resync(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hub_module, "REPLAY_MAX", 2)
    first = _message(1)
    others = [PrinterEvent(kind="printer.report", printer_id="p") for _ in range(3)]
    async with httpx.AsyncClient(timeout=None) as client:
        ctx, sse = await _open(client, served)
        await sse.next()
        _emit(served, first)
        assert (await sse.next())["id"] == first.id
        await ctx.__aexit__(None, None, None)
        for event in others:
            served.bus.publish(event)
        await _logged(served, others[-1].id)
        ctx, sse = await _open(client, served, first.id)
        ready = await sse.next()
        assert ready["data"] == {"resync": True, "reconnect": True}
        await ctx.__aexit__(None, None, None)


async def test_another_callers_credential_is_refused(served: Served) -> None:
    async with httpx.AsyncClient(timeout=PROMPTLY) as client:
        for caller in ("camera", "agent"):
            refused = await client.get(f"{served.url}{BASE}/changes", headers=served.token(caller))
            assert refused.status_code == 403
        fleet = await client.get(f"{served.url}{BASE}/areas", headers=served.token("fleet"))
        assert fleet.json()["areas"] == ["changes", "fleet"]


async def _status(
    client: httpx.AsyncClient, served: Served, caller: ServiceCaller, base: str
) -> int:
    """The status of one stream request, read and closed at once."""

    async def ask() -> int:
        async with client.stream(
            "GET", f"{served.url}{base}/changes", headers=served.token(caller)
        ) as response:
            return response.status_code

    return await asyncio.wait_for(ask(), PROMPTLY)


async def test_fleet_streams_have_a_budget_of_their_own(served: Served) -> None:
    """The cap counts the caller group, so one more stream is 429 and the agent's stay free."""
    async with httpx.AsyncClient(timeout=None) as client:
        opened = []
        try:
            for _ in range(crud_fleet.FLEET_STREAMS_MAX):
                opened.append(await _open(client, served))
            assert await _status(client, served, "fleet", BASE) == 429
            assert await _status(client, served, "agent", AGENT_BASE) == 200
        finally:
            for ctx, _ in opened:
                await ctx.__aexit__(None, None, None)


async def test_the_agent_stream_never_carries_fleet_kinds(served: Served) -> None:
    """A fleet.message names session ids, the fleet's only identity: the agent's stream,
    live and in replay, leaves it out."""
    agent_base = "/api/v1/internal/crud/agent"
    async with httpx.AsyncClient(timeout=None) as client:
        ctx = client.stream(
            "GET", f"{served.url}{agent_base}/changes", headers=served.token("agent")
        )
        response = await ctx.__aenter__()
        assert response.status_code == 200
        sse = Sse(response)
        assert (await sse.next())["event"] == "ready"
        hidden = _message()
        _emit(served, hidden)
        await _logged(served, hidden.id)
        shown = ModelEvent(kind="model.updated", slug="m")
        served.bus.publish(shown)
        got = await sse.next()
        assert got["id"] == shown.id
        await ctx.__aexit__(None, None, None)

        replay = client.stream(
            "GET",
            f"{served.url}{agent_base}/changes",
            headers={**served.token("agent"), "last-event-id": "0"},
        )
        response = await replay.__aenter__()
        sse = Sse(response)
        seen = []
        while True:
            frame = await sse.next()
            if frame["event"] == "ready":
                break
            seen.append(frame["id"])
        assert hidden.id not in seen
        assert shown.id in seen
        await replay.__aexit__(None, None, None)
