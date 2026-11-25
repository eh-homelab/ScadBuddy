"""The agent's change stream (#5299 D4, step 11, area ``changes``, `api/crud_agent_changes.py`,
`agent_changes/hub.py`): only the ``agent`` credential opens it; every NOTIFY on the six channels
reaches every stream this process serves, from the process's one LISTEN connection; a reconnect
replays the event log after its ``Last-Event-ID``; a reader that falls behind loses its queue and
is told to replay, never holding up the listener or another stream; and the streams per
credential are capped.

The served tests run the real route behind the real guard on a local port, over the bus and
listener the API runs (`PgNotifyEventBus`, `PgListener`), so a NOTIFY goes through Postgres."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import psycopg
import pytest
import pytest_asyncio
from starlette.requests import Request

from scadbuddy.agent_changes import hub as hub_module
from scadbuddy.agent_changes.component import chain_signals
from scadbuddy.agent_changes.hub import (
    CHANNELS,
    GAP,
    START,
    Change,
    ChangeHub,
    ChangeStream,
    StreamsFullError,
    frames,
)
from scadbuddy.api import crud_agent_changes
from scadbuddy.auth.gate import CALLER_SCOPE_KEY
from scadbuddy.auth.models import Service
from scadbuddy.core.events import (
    PG_CHANNEL,
    AgentOperationEvent,
    ModelEvent,
    SessionBusEvent,
    encode_event,
)
from scadbuddy.core.pg_events import LoggedEvent, Replay
from tests.crud.crud_support import PROMPTLY, Served, Sse, served_changes

BASE = "/api/v1/internal/crud/agent"


def _model(slug: str) -> ModelEvent:
    return ModelEvent(kind="model.updated", slug=slug)


# -- the hub and its frames, with no database ---------------------------------------------


@dataclass
class FakeLog:
    """The event log: logged events in order, ``pruned`` up to a seq."""

    events: list[ModelEvent] = field(default_factory=list)
    pruned: int = 0

    async def newest(self) -> str | None:
        return self.events[-1].id if self.events else None

    async def position(self, event_id: str) -> int | None:
        for seq, event in enumerate(self.events, start=1):
            if event.id == event_id and seq > self.pruned:
                return seq
        return None

    async def replay(self, after: int, *, limit: int = 100) -> Replay:
        rows = [
            LoggedEvent(seq=seq, event=event)
            for seq, event in enumerate(self.events, start=1)
            if seq > max(after, self.pruned)
        ][:limit]
        return Replay(events=list(rows), gap=after < self.pruned)


def _parse(chunk: bytes) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for line in chunk.decode().splitlines():
        if line.startswith(":") or not line:
            continue
        name, _, value = line.partition(": ")
        fields[name] = json.loads(value) if name == "data" else value
    return fields


async def _take(source: Any, count: int) -> list[dict[str, Any]]:
    got: list[dict[str, Any]] = []
    while len(got) < count:
        chunk = await asyncio.wait_for(anext(source), PROMPTLY)
        if not chunk.startswith(b":"):
            got.append(_parse(chunk))
    return got


async def test_a_first_stream_starts_at_the_newest_logged_event() -> None:
    log = FakeLog([_model("a"), _model("b")])
    stream = ChangeStream("k")
    source = frames(stream, log, None)
    [ready] = await _take(source, 1)
    assert ready == {
        "id": log.events[-1].id,
        "event": "ready",
        "data": {"resync": False, "reconnect": False},
    }
    stream.offer(Change("scadbuddy_input", "req-1"))
    [change] = await _take(source, 1)
    # Not a logged event: no id, so the agent's place stays where it was.
    assert change == {"event": "change", "data": {"c": "scadbuddy_input", "p": "req-1"}}


async def test_an_empty_log_gives_the_start_as_the_place() -> None:
    source = frames(ChangeStream("k"), FakeLog(), None)
    [ready] = await _take(source, 1)
    assert ready["id"] == START


async def test_a_reconnect_replays_after_its_last_event_id_and_skips_the_live_copies() -> None:
    log = FakeLog([_model("a"), _model("b"), _model("c")])
    stream = ChangeStream("k")
    # The stream subscribed first: c's NOTIFY arrives while the log is replayed too.
    stream.offer(Change(PG_CHANNEL, encode_event(log.events[2]), log.events[2].id))
    source = frames(stream, log, log.events[0].id)
    got = await _take(source, 3)
    assert [g["event"] for g in got] == ["change", "change", "ready"]
    assert [json.loads(g["data"]["p"])["slug"] for g in got[:2]] == ["b", "c"]
    assert [g["id"] for g in got] == [log.events[1].id, log.events[2].id, log.events[2].id]
    assert got[2]["data"] == {"resync": False, "reconnect": True}
    later = _model("d")
    stream.offer(Change(PG_CHANNEL, encode_event(later), later.id))
    [change] = await _take(source, 1)
    # c's live copy was skipped: the next frame is d.
    assert change["id"] == later.id


@pytest.mark.parametrize("last", ["not an id", "", "9" * 32, "../x", "x" * 65, "1"])
async def test_a_place_it_cannot_find_is_a_resync_never_a_guess(last: str) -> None:
    log = FakeLog([_model("a")])
    [ready] = await _take(frames(ChangeStream("k"), log, last), 1)
    assert ready["event"] == "ready"
    assert ready["data"] == {"resync": True, "reconnect": True}
    assert ready["id"] == log.events[0].id


async def test_a_pruned_place_or_a_long_replay_is_a_resync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = FakeLog([_model(str(i)) for i in range(5)], pruned=2)
    [ready] = await _take(frames(ChangeStream("k"), log, START), 1)
    assert ready["data"]["resync"] is True
    monkeypatch.setattr(hub_module, "REPLAY_MAX", 2)
    log.pruned = 0
    [ready] = await _take(frames(ChangeStream("k"), log, log.events[0].id), 1)
    assert ready["data"]["resync"] is True
    assert ready["id"] == log.events[-1].id


async def test_a_payload_with_newlines_stays_one_frame() -> None:
    stream = ChangeStream("k")
    source = frames(stream, FakeLog(), None)
    await _take(source, 1)
    stream.offer(Change("scadbuddy_mcp", "a\nb\r\nid: x\n\ndata: y"))
    [change] = await _take(source, 1)
    assert change == {
        "event": "change",
        "data": {"c": "scadbuddy_mcp", "p": "a\nb\r\nid: x\n\ndata: y"},
    }


async def test_a_slow_reader_loses_its_queue_and_replays_without_holding_up_another() -> None:
    log = FakeLog([_model("a")])
    hub = ChangeHub(queue_max=3)
    slow, quick = hub.open("k1"), hub.open("k2")
    quick_frames = frames(quick, log, None)
    await _take(quick_frames, 1)
    slow_frames = frames(slow, log, None)
    await _take(slow_frames, 1)
    got = []
    for i in range(10):  # never waits on the slow reader
        hub.publish(Change("scadbuddy_pending", str(i)))
        got += await _take(quick_frames, 1)
    assert slow.overflows > 0
    assert [g["data"]["p"] for g in got] == [str(i) for i in range(10)]
    # The slow one's queue was dropped for a gap: it replays, then says `reconnected`.
    log.events.append(_model("b"))
    got = await _take(slow_frames, 2)
    assert got[0]["id"] == log.events[1].id
    assert got[1] == {"id": log.events[1].id, "event": "reconnected", "data": {"resync": False}}


async def test_a_listener_reconnect_marks_every_stream_with_one_gap() -> None:
    hub = ChangeHub()
    stream = hub.open("k")
    hub._connected(False)
    assert await stream.next(0.01) is None
    hub.publish(Change("scadbuddy_input", "x"))
    hub._connected(True)
    hub._connected(True)
    assert await stream.next(0.01) == Change("scadbuddy_input", "x")
    assert await stream.next(0.01) is GAP
    assert await stream.next(0.01) is None


def test_the_streams_are_capped_per_credential_and_in_all() -> None:
    hub = ChangeHub(per_credential=2, total=3)
    a = hub.open("one")
    hub.open("one")
    with pytest.raises(StreamsFullError):
        hub.open("one")
    hub.open("two")
    with pytest.raises(StreamsFullError):
        hub.open("three")
    hub.close(a)
    hub.open("three")


def test_one_credential_from_two_pods_is_two_keys() -> None:
    """Every agent process that reads the sealed credential from its view holds the same one;
    the cap counts each pod's streams, so a fifth process is not refused for good."""

    def key(host: str) -> str:
        caller = Service(caller="agent", scopes=frozenset(), credential_id="cred-1")
        scope = {"type": "http", "client": (host, 4000), CALLER_SCOPE_KEY: caller}
        return crud_agent_changes.stream_key(Request(scope))

    assert key("10.0.0.1") == key("10.0.0.1")
    assert key("10.0.0.1") != key("10.0.0.2")
    hub = ChangeHub(per_credential=1, total=8)
    hub.open(key("10.0.0.1"))
    hub.open(key("10.0.0.2"))
    with pytest.raises(StreamsFullError):
        hub.open(key("10.0.0.1"))


async def test_shutdown_ends_every_stream_and_opens_none() -> None:
    hub = ChangeHub()
    stream = hub.open("k")
    source = frames(stream, FakeLog(), None)
    await _take(source, 1)
    hub.shutdown()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(source), PROMPTLY)
    with pytest.raises(StreamsFullError):
        hub.open("k")


async def test_shutdown_ends_a_full_stream_too() -> None:
    """A reader stalled until its queue is full still ends at a shutdown, never a replay that
    waits on: an open stream holds uvicorn's drain."""
    hub = ChangeHub(queue_max=2)
    stream = hub.open("k")
    source = frames(stream, FakeLog(), None)
    await _take(source, 1)
    for i in range(2):
        stream.offer(Change("scadbuddy_input", f"p{i}"))
    hub.shutdown()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(source), PROMPTLY)


async def test_changes_heard_after_shutdown_never_push_the_end_out() -> None:
    """Turns keep publishing while uvicorn drains: a stalled stream told to end still ends."""
    hub = ChangeHub(queue_max=2)
    stream = hub.open("k")
    source = frames(stream, FakeLog(), None)
    await _take(source, 1)
    hub.shutdown()
    for i in range(5):
        hub.publish(Change("scadbuddy_input", f"late{i}"))
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(source), PROMPTLY)


def test_the_signal_ends_the_streams_then_runs_the_servers_handler() -> None:
    import signal as signal_module

    seen: list[int] = []
    previous = signal_module.signal(
        signal_module.SIGTERM, lambda signum, frame: seen.append(signum)
    )
    loop = asyncio.new_event_loop()
    try:
        hub = ChangeHub()
        replaced = chain_signals(hub, loop)
        signal_module.raise_signal(signal_module.SIGTERM)
        loop.run_until_complete(asyncio.sleep(0))
        assert seen == [signal_module.SIGTERM]
        with pytest.raises(StreamsFullError):
            hub.open("k")
        for sig, handler in replaced.items():
            signal_module.signal(sig, handler)
    finally:
        signal_module.signal(signal_module.SIGTERM, previous)
        loop.close()


def test_an_ignored_signal_ends_no_stream() -> None:
    import signal as signal_module

    previous = signal_module.signal(signal_module.SIGTERM, signal_module.SIG_IGN)
    loop = asyncio.new_event_loop()
    try:
        hub = ChangeHub()
        replaced = chain_signals(hub, loop)
        signal_module.raise_signal(signal_module.SIGTERM)
        loop.run_until_complete(asyncio.sleep(0))
        hub.open("k")  # the process goes on, and so do its streams
        for sig, handler in replaced.items():
            signal_module.signal(sig, handler)
    finally:
        signal_module.signal(signal_module.SIGTERM, previous)
        loop.close()


async def test_a_log_this_build_cannot_decode_is_a_resync() -> None:
    log = FakeLog([_model("a"), _model("b")])

    async def undecodable(after: int, *, limit: int = 100) -> Replay:
        raise ValueError("unknown event kind 'from.a.newer.build'")

    log.replay = undecodable  # type: ignore[method-assign]
    [ready] = await _take(frames(ChangeStream("k"), log, log.events[0].id), 1)
    assert ready["data"] == {"resync": True, "reconnect": True}
    assert ready["id"] == log.events[-1].id


async def test_a_fresh_stream_starts_where_it_subscribed_and_never_moves_back() -> None:
    log = FakeLog([_model("a")])
    start = log.events[0].id
    stream = ChangeStream("k")
    # Logged and heard after the place was read, before the frames began.
    later = _model("b")
    log.events.append(later)
    stream.offer(Change(PG_CHANNEL, encode_event(later), later.id))
    source = frames(stream, log, None, start=start)
    ready, change = await _take(source, 2)
    assert ready["id"] == start
    assert change["id"] == later.id


# -- served, over Postgres ----------------------------------------------------------------


@pytest_asyncio.fixture
async def served(pg_conninfo: str) -> AsyncIterator[Served]:
    async with served_changes(pg_conninfo, crud_agent_changes.router, per_credential=2) as it:
        yield it


async def _open(
    client: httpx.AsyncClient, served: Served, headers: dict[str, str] | None = None
) -> tuple[Any, Sse]:
    context = client.stream(
        "GET", f"{served.url}{BASE}/changes", headers={**served.token("agent"), **(headers or {})}
    )
    response = await context.__aenter__()
    assert response.status_code == 200, await response.aread()
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-scadbuddy-crud"] == "1"
    return context, Sse(response)


def _notify(conninfo: str, channel: str, payload: str) -> None:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute("SELECT pg_notify(%s, %s)", (channel, payload))


@pytest.mark.requires_postgres
async def test_only_the_agent_credential_opens_the_stream(served: Served) -> None:
    async with httpx.AsyncClient(timeout=PROMPTLY) as client:
        for caller in ("print", "projects"):
            refused = await client.get(f"{served.url}{BASE}/changes", headers=served.token(caller))
            assert refused.status_code == 403
            assert refused.headers["x-scadbuddy-crud"] == "1"
        assert (await client.get(f"{served.url}{BASE}/changes")).status_code == 401
        areas = await client.get(f"{served.url}{BASE}/areas", headers=served.token("agent"))
        assert "changes" in areas.json()["areas"]
    assert served.hub.streams == 0


@pytest.mark.requires_postgres
async def test_every_channel_reaches_two_streams_in_order(served: Served) -> None:
    async with httpx.AsyncClient(timeout=None) as client:
        a_ctx, a = await _open(client, served)
        b_ctx, b = await _open(client, served)
        for sse in (a, b):
            assert (await sse.next())["event"] == "ready"
        for channel in CHANNELS:
            await asyncio.to_thread(_notify, served.conninfo, channel, f"{channel}-payload")
        published = _model("m")
        served.bus.publish(published)
        for sse in (a, b):
            got = [await sse.next() for _ in range(len(CHANNELS) + 1)]
            assert [g["data"]["c"] for g in got] == [*CHANNELS, PG_CHANNEL]
            assert [g["data"]["p"] for g in got[:-1]] == [f"{c}-payload" for c in CHANNELS]
            assert got[-1]["id"] == published.id
            assert json.loads(got[-1]["data"]["p"])["slug"] == "m"
        await a_ctx.__aexit__(None, None, None)
        await b_ctx.__aexit__(None, None, None)


@pytest.mark.requires_postgres
async def test_a_reconnect_replays_the_log_after_its_last_event_id(served: Served) -> None:
    first, second, third = _model("1"), _model("2"), _model("3")
    async with httpx.AsyncClient(timeout=None) as client:
        ctx, sse = await _open(client, served)
        await sse.next()
        served.bus.publish(first)
        heard = await sse.next()
        assert heard["id"] == first.id
        await ctx.__aexit__(None, None, None)
        # Published while the agent had no stream: only the log has them.
        served.bus.publish(second)
        served.bus.publish(third)
        await asyncio.to_thread(_notify, served.conninfo, "scadbuddy_mcp", "lost")
        deadline = time.monotonic() + PROMPTLY
        while await served.bus.newest() != third.id:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.05)
        ctx, sse = await _open(client, served, {"last-event-id": first.id})
        got = [await sse.next() for _ in range(3)]
        assert [g.get("id") for g in got] == [second.id, third.id, third.id]
        assert got[2]["event"] == "ready"
        assert got[2]["data"] == {"resync": False, "reconnect": True}
        await ctx.__aexit__(None, None, None)


@pytest.mark.requires_postgres
async def test_the_agents_notify_only_events_carry_no_id_so_a_reconnect_still_replays(
    served: Served,
) -> None:
    logged, later = _model("logged"), _model("later")
    session = SessionBusEvent(kind="session.waiting", session_id="s1", seq=7, status="idle")
    operation = AgentOperationEvent(operation_id="op-1")
    async with httpx.AsyncClient(timeout=None) as client:
        ctx, sse = await _open(client, served)
        await sse.next()
        served.bus.publish(logged)
        assert (await sse.next())["id"] == logged.id
        # As the agent sends them: NOTIFY only, never in the log.
        for unlogged in (session, operation):
            await asyncio.to_thread(_notify, served.conninfo, PG_CHANNEL, encode_event(unlogged))
            heard = await sse.next()
            assert "id" not in heard
            assert json.loads(heard["data"]["p"])["id"] == unlogged.id
        await ctx.__aexit__(None, None, None)
        served.bus.publish(later)
        deadline = time.monotonic() + PROMPTLY
        while await served.bus.newest() != later.id:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.05)
        # The agent's place is still the logged event: a replay, not a resync.
        ctx, sse = await _open(client, served, {"last-event-id": logged.id})
        got = [await sse.next() for _ in range(2)]
        assert [g.get("id") for g in got] == [later.id, later.id]
        assert got[1]["event"] == "ready"
        assert got[1]["data"] == {"resync": False, "reconnect": True}
        await ctx.__aexit__(None, None, None)


@pytest.mark.requires_postgres
async def test_a_stream_nobody_reads_holds_up_neither_the_listener_nor_another(
    served: Served,
) -> None:
    async with httpx.AsyncClient(timeout=None) as client:
        stalled_ctx, stalled = await _open(client, served)
        await stalled.next()  # then never read again
        ctx, sse = await _open(client, served)
        await sse.next()
        for i in range(50):
            await asyncio.to_thread(_notify, served.conninfo, "scadbuddy_pending", str(i))
        got = [await sse.next() for _ in range(50)]
        assert [g["data"]["p"] for g in got] == [str(i) for i in range(50)]
        await ctx.__aexit__(None, None, None)
        await stalled_ctx.__aexit__(None, None, None)


@pytest.mark.requires_postgres
async def test_a_credential_holds_only_so_many_streams(served: Served) -> None:
    async with httpx.AsyncClient(timeout=None) as client:
        headers = served.token("agent")
        opened = []
        for _ in range(2):
            context = client.stream("GET", f"{served.url}{BASE}/changes", headers=headers)
            response = await context.__aenter__()
            assert response.status_code == 200
            opened.append(context)
        refused = await client.get(f"{served.url}{BASE}/changes", headers=headers)
        assert refused.status_code == 429
        assert refused.headers["x-scadbuddy-crud"] == "1"
        # Another credential has its own.
        other = client.stream("GET", f"{served.url}{BASE}/changes", headers=served.token("agent"))
        assert (await other.__aenter__()).status_code == 200
        opened.append(other)
        for context in opened:
            await context.__aexit__(None, None, None)
        deadline = time.monotonic() + PROMPTLY
        while served.hub.streams:
            assert time.monotonic() < deadline, "a closed stream stayed counted"
            await asyncio.sleep(0.05)
