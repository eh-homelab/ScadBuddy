"""The Postgres event bus (spec §7; #264, #266): NOTIFY delivery across replicas,
the payload cap, resync after a dropped listener, the replay log and its pruning,
job events through it, and the fallback without a database."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
import pytest_asyncio

from scadbuddy.api.deps import build_state
from scadbuddy.core import pg_events
from scadbuddy.core.config import Config
from scadbuddy.core.events import (
    PG_CHANNEL,
    BusResync,
    Event,
    InProcessEventBus,
    JobEvent,
    ModelEvent,
    Subscription,
    encode_event,
)
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.pg_events import (
    EVENT_LOG_LOCK,
    MAX_PAYLOAD_BYTES,
    POSTGRES_NOTIFY_LIMIT,
    EventLogMissingError,
    EventLogRetention,
    PgNotifyEventBus,
)
from scadbuddy.core.pg_listener import PgListener
from scadbuddy.core.settings import Settings
from scadbuddy.render.job_store import JobStore, render_key
from scadbuddy.render.jobs import Job, JobResult, QueueFullError, RenderQueue
from scadbuddy.render.pg_store import PostgresJobStore, migrate

#: Well inside this, or it is not "at once".
PROMPTLY = 5.0
#: Long enough for a NOTIFY that was going to arrive to have arrived.
QUIET = 0.5


def _model(slug: str) -> ModelEvent:
    return ModelEvent(kind="model.updated", slug=slug)


def _sample(metrics: Metrics, name: str, **labels: str) -> float:
    value = metrics.registry.get_sample_value(name, labels)
    return 0.0 if value is None else value


async def _until(predicate: Callable[[], bool], timeout: float = PROMPTLY) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def _next(subscription: Subscription, timeout: float = PROMPTLY) -> Event:
    return await asyncio.wait_for(subscription.get(), timeout)


async def _quiet(subscription: Subscription) -> list[Event]:
    """What else arrives within `QUIET`."""
    await asyncio.sleep(QUIET)
    seen: list[Event] = []
    while (event := subscription.get_nowait()) is not None:
        seen.append(event)
    return seen


def _migrated(conninfo: str) -> str:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        migrate(conn)
    return conninfo


BusFactory = Callable[..., Awaitable[PgNotifyEventBus]]


@pytest_asyncio.fixture
async def make_bus(pg_conninfo: str) -> AsyncIterator[BusFactory]:
    """Started buses on one migrated schema: each its own "replica"."""
    _migrated(pg_conninfo)
    buses: list[PgNotifyEventBus] = []

    async def make(retention: EventLogRetention | None = None) -> PgNotifyEventBus:
        listener = PgListener(pg_conninfo, check_interval=1.0, backoff=0.05, max_backoff=0.2)
        bus = PgNotifyEventBus(pg_conninfo, listener=listener, retention=retention)
        await bus.start()
        buses.append(bus)
        await _until(lambda: listener.backend_pid is not None)
        return bus

    yield make
    for bus in buses:
        await bus.aclose()


def _slugs(events: list[Event]) -> list[str]:
    return [event.slug for event in events if isinstance(event, ModelEvent)]


# --- delivery ------------------------------------------------------------------------


@pytest.mark.requires_postgres
async def test_each_replica_hears_each_event_exactly_once(
    make_bus: BusFactory,
) -> None:
    a, b = await make_bus(), await make_bus()
    on_a, on_b = a.subscribe(), b.subscribe()

    a.publish(_model("from-a"))
    b.publish(_model("from-b"))
    # Not delivered locally: only what comes back over NOTIFY is.
    assert on_a.get_nowait() is None

    for subscription in (on_a, on_b):
        heard = [await _next(subscription), await _next(subscription)]
        heard += await _quiet(subscription)
        assert sorted(_slugs(heard)) == ["from-a", "from-b"]
    assert _sample(a.metrics, "scadbuddy_events_published_total") == 1
    assert _sample(a.metrics, "scadbuddy_events_received_total") == 2


@pytest.mark.requires_postgres
async def test_publishing_from_a_worker_thread_never_blocks_the_caller(
    make_bus: BusFactory,
) -> None:
    bus = await make_bus()
    subscription = bus.subscribe()

    await asyncio.to_thread(bus.publish, _model("threaded"))

    assert _slugs([await _next(subscription)]) == ["threaded"]


@pytest.mark.requires_postgres
async def test_events_published_before_start_are_sent_on_start(pg_conninfo: str) -> None:
    _migrated(pg_conninfo)
    listener = PgListener(pg_conninfo, check_interval=1.0)
    early = PgNotifyEventBus(pg_conninfo, listener=listener)
    early.publish(_model("early"))  # the lifespan's built-in sync commits before start
    hears = PgNotifyEventBus(pg_conninfo, listener=PgListener(pg_conninfo, check_interval=1.0))
    await hears.start()
    try:
        await _until(lambda: hears.listener.backend_pid is not None)
        subscription = hears.subscribe()
        await early.start()
        assert _slugs([await _next(subscription)]) == ["early"]
    finally:
        await early.aclose()
        await hears.aclose()


@pytest.mark.requires_postgres
async def test_starting_before_the_store_migrated_says_so(pg_conninfo: str) -> None:
    """The ordering the lifespan relies on, asserted: an unmigrated database is a
    clear error at start, not `relation "events" does not exist` in every write."""
    bus = PgNotifyEventBus(pg_conninfo, listener=PgListener(pg_conninfo))

    with pytest.raises(EventLogMissingError, match="open the job store"):
        await bus.start()

    assert bus._pool.closed  # a failed start released what it opened
    await bus.aclose()  # and closing after it is still safe


@pytest.mark.requires_postgres
async def test_each_event_is_encoded_once_whichever_path_publishes_it(
    make_bus: BusFactory, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The payload `publish` size-checks is the one the outbox writes: no second
    serialisation in the drain, as `publish_in` never had one."""
    bus = await make_bus()
    subscription = bus.subscribe()
    encoded: list[str] = []

    def counting(event: Event) -> str:
        encoded.append(event.id)
        return encode_event(event)

    monkeypatch.setattr(pg_events, "encode_event", counting)
    published = [_model(f"m{n}") for n in range(3)]
    for event in published:
        bus.publish(event)
    in_transaction = _model("in-transaction")
    with psycopg.connect(pg_conninfo) as conn, conn.transaction():
        bus.publish_in(conn, in_transaction)

    heard = [await _next(subscription) for _ in range(4)]
    assert sorted(_slugs(heard)) == ["in-transaction", "m0", "m1", "m2"]
    assert sorted(encoded) == sorted(event.id for event in [*published, in_transaction])
    # And what was logged is that payload, byte for byte decodable.
    replay = await bus.replay(0)
    assert {logged.event.id for logged in replay.events} == set(encoded)


# --- the payload cap -----------------------------------------------------------------


def test_the_cap_is_well_under_postgres_notify_limit() -> None:
    assert MAX_PAYLOAD_BYTES <= POSTGRES_NOTIFY_LIMIT // 2
    # Ids only: a realistic event is a small fraction of the cap.
    assert len(encode_event(_model("a" * 100)).encode()) < MAX_PAYLOAD_BYTES // 5


@pytest.mark.requires_postgres
async def test_an_oversize_event_is_refused_logged_and_counted(
    make_bus: BusFactory, pg_conninfo: str, caplog: pytest.LogCaptureFixture
) -> None:
    bus = await make_bus()
    subscription = bus.subscribe()
    huge = _model("x" * (MAX_PAYLOAD_BYTES + 1))

    with caplog.at_level(logging.ERROR):
        bus.publish(huge)
        with psycopg.connect(pg_conninfo) as conn:
            bus.publish_in(conn, huge)
    bus.publish(_model("small"))  # the bus goes on

    assert _slugs([await _next(subscription)]) == ["small"]
    assert await _quiet(subscription) == []
    assert "refused to publish an event over the NOTIFY payload cap" in caplog.text
    assert _sample(bus.metrics, "scadbuddy_events_dropped_total", reason="oversize") == 2
    assert await bus.position(huge.id) is None


# --- transactions: heard on commit, readable when heard, in log order -----------------


@pytest.mark.requires_postgres
async def test_an_event_is_readable_in_the_log_when_it_is_heard(
    make_bus: BusFactory,
) -> None:
    bus = await make_bus()
    subscription = bus.subscribe()
    positions: list[int] = []

    for n in range(5):
        bus.publish(_model(f"m{n}"))
    for _ in range(5):
        event = await _next(subscription)
        # Asked the moment it is heard: the NOTIFY left on the log row's commit.
        seq = await bus.position(event.id)
        assert seq is not None
        positions.append(seq)

    assert positions == sorted(positions)


@pytest.mark.requires_postgres
async def test_an_event_in_a_transaction_is_heard_on_commit_never_on_rollback(
    make_bus: BusFactory, pg_conninfo: str
) -> None:
    bus = await make_bus()
    subscription = bus.subscribe()
    kept, dropped = _model("kept"), _model("rolled-back")

    with psycopg.connect(pg_conninfo) as conn, conn.transaction():
        bus.publish_in(conn, dropped)
        raise psycopg.Rollback
    with psycopg.connect(pg_conninfo) as conn, conn.transaction():
        bus.publish_in(conn, kept)
        assert await _quiet(subscription) == []  # not yet committed

    assert _slugs([await _next(subscription)]) == ["kept"]
    assert await bus.position(dropped.id) is None
    assert await bus.position(kept.id) is not None


@pytest.mark.requires_postgres
async def test_log_order_is_commit_order(make_bus: BusFactory, pg_conninfo: str) -> None:
    """The log lock is held to commit: a publish that starts while another's
    transaction is open waits for it, so no reader of `seq` N can later find a
    smaller one appear."""
    bus = await make_bus()
    subscription = bus.subscribe()
    first, second = _model("first"), _model("second")
    second_done = threading.Event()

    def publish_second() -> None:
        with psycopg.connect(pg_conninfo) as conn, conn.transaction():
            bus.publish_in(conn, second)
        second_done.set()

    with psycopg.connect(pg_conninfo) as conn, conn.transaction():
        bus.publish_in(conn, first)
        worker = threading.Thread(target=publish_second)
        worker.start()
        await asyncio.sleep(QUIET)
        assert not second_done.is_set()  # waiting on the lock
    await asyncio.to_thread(worker.join, PROMPTLY)

    heard = [await _next(subscription), await _next(subscription)]
    assert _slugs(heard) == ["first", "second"]
    first_seq, second_seq = await bus.position(first.id), await bus.position(second.id)
    assert first_seq is not None and second_seq is not None and first_seq < second_seq


# --- resync after a dropped listener ------------------------------------------------


@pytest.mark.requires_postgres
async def test_a_dropped_listener_resyncs_its_subscribers_and_events_keep_flowing(
    make_bus: BusFactory, pg_conninfo: str
) -> None:
    bus = await make_bus()
    publisher = await make_bus()
    # A filtered subscriber still gets the marker: it may have missed its own kind.
    subscription = bus.subscribe(kinds={"model.updated"})
    before = _model("before")
    publisher.publish(before)
    assert _slugs([await _next(subscription)]) == ["before"]

    dropped = bus.listener.backend_pid
    with psycopg.connect(pg_conninfo, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (dropped,))

    marker = await _next(subscription)
    assert isinstance(marker, BusResync)
    assert marker.kind == "bus.resync"
    assert marker.last_event_id == before.id
    assert bus.listener.backend_pid not in (None, dropped)
    assert _sample(bus.metrics, "scadbuddy_events_resyncs_total") == 1
    # The first connect sent none, and the publisher's own listener saw no gap.
    assert _sample(publisher.metrics, "scadbuddy_events_resyncs_total") == 0

    publisher.publish(_model("after"))
    assert _slugs([await _next(subscription)]) == ["after"]
    # What the marker names is where to replay from.
    seq = await bus.position(before.id)
    assert seq is not None
    replay = await bus.replay(seq)
    assert _slugs([logged.event for logged in replay.events]) == ["after"]


# --- the replay log -------------------------------------------------------------------


@pytest.mark.requires_postgres
async def test_the_log_replays_after_a_seq_in_pages(make_bus: BusFactory) -> None:
    bus = await make_bus()
    subscription = bus.subscribe()
    for n in range(5):
        bus.publish(_model(f"m{n}"))
    for _ in range(5):
        await _next(subscription)

    page = await bus.replay(0, limit=2)
    assert _slugs([logged.event for logged in page.events]) == ["m0", "m1"]
    assert not page.gap
    rest = await bus.replay(page.events[-1].seq, limit=100)
    assert _slugs([logged.event for logged in rest.events]) == ["m2", "m3", "m4"]
    assert [logged.seq for logged in rest.events] == sorted(logged.seq for logged in rest.events)
    assert (await bus.replay(rest.events[-1].seq)).events == []
    with pytest.raises(ValueError, match="limit"):
        await bus.replay(0, limit=0)


@pytest.mark.requires_postgres
async def test_pruning_keeps_the_newest_rows_and_drops_old_ones(
    make_bus: BusFactory, pg_conninfo: str
) -> None:
    bus = await make_bus(retention=EventLogRetention(seconds=3600, rows=3))
    subscription = bus.subscribe()
    for n in range(5):
        bus.publish(_model(f"m{n}"))
    for _ in range(5):
        await _next(subscription)

    assert await bus.prune_log() == 2  # by rows
    replay = await bus.replay(0)
    assert _slugs([logged.event for logged in replay.events]) == ["m2", "m3", "m4"]
    assert replay.gap  # m0 and m1 are gone: a client resuming from 0 must resync
    assert not (await bus.replay(replay.events[0].seq - 1)).gap

    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(
            "UPDATE events SET logged_at = now() - interval '2 hours' WHERE seq = %s",
            (replay.events[0].seq,),
        )
    assert await bus.prune_log() == 1  # by age
    assert _slugs([logged.event for logged in (await bus.replay(0)).events]) == ["m3", "m4"]
    assert _sample(bus.metrics, "scadbuddy_event_log_pruned_total") == 3


@pytest.mark.requires_postgres
async def test_an_emptied_log_still_knows_what_was_pruned(
    make_bus: BusFactory, pg_conninfo: str
) -> None:
    bus = await make_bus()
    subscription = bus.subscribe()
    bus.publish(_model("gone"))
    await _next(subscription)
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute("DELETE FROM events")

    assert (await bus.replay(0)).gap


# --- job events ----------------------------------------------------------------------

CONFIG = Config(
    data_dir=Path("/unused"),
    render_concurrency=1,
    render_poll_interval=0.05,
    job_ttl=3600.0,
)


def _result() -> JobResult:
    return JobResult.model_validate(
        {
            "model_3mf": "a",
            "preview_glb": "b",
            "parts": [],
            "bbox_mm": {"min": [0, 0, 0], "max": [1, 1, 1], "size": [1, 1, 1]},
        }
    )


def _jobs(events: list[Event]) -> list[tuple[str, str]]:
    return [(e.kind, e.job_id) for e in events if isinstance(e, JobEvent)]


@pytest.mark.requires_postgres
async def test_job_events_go_through_the_bus_and_commit_with_the_job(
    make_bus: BusFactory, pg_conninfo: str, tmp_path: Path
) -> None:
    bus = await make_bus()
    heard: list[Event] = []
    bus.add_listener(heard.append)
    release = asyncio.Event()

    async def render(job: Job) -> tuple[JobResult, list[str]]:
        await release.wait()
        return _result(), []

    paths = DataPaths(tmp_path)
    store = PostgresJobStore(pg_conninfo, paths, pool_size=4)
    store.events = bus
    config = replace(CONFIG, render_queue_max=2)
    queue = RenderQueue(config, paths, store=store, render=render, events=bus)
    await queue.start()
    try:
        assert store.announces_jobs
        running = await queue.submit("demo", {"n": 0})
        await _until(lambda: ("job.running", running.id) in _jobs(heard))
        first = await queue.submit("demo", {"n": 1})
        second = await queue.submit("demo", {"n": 2}, supersedes=first.id)
        again = await queue.submit("demo", {"n": 2})  # coalesced: nothing new
        third = await queue.submit("demo", {"n": 3})
        with pytest.raises(QueueFullError):  # rolled back: announces nothing
            await queue.submit("demo", {"n": 4})
        release.set()
        await queue.join()
        await _until(lambda: ("job.done", third.id) in _jobs(heard))
        await asyncio.sleep(QUIET)
    finally:
        release.set()
        await queue.aclose()

    assert again.id == second.id
    assert _jobs(heard) == [
        ("job.pending", running.id),
        ("job.running", running.id),
        ("job.pending", first.id),
        ("job.superseded", first.id),
        ("job.pending", second.id),
        ("job.pending", third.id),
        ("job.done", running.id),
        ("job.running", second.id),
        ("job.done", second.id),
        ("job.running", third.id),
        ("job.done", third.id),
    ]
    # Every one of them is in the log, in the order it was heard.
    replay = await bus.replay(0, limit=100)
    assert _jobs([logged.event for logged in replay.events]) == _jobs(heard)


@pytest.mark.requires_postgres
async def test_a_mass_reap_never_holds_the_event_log_lock_across_jobs(
    make_bus: BusFactory, pg_conninfo: str, tmp_path: Path
) -> None:
    """`EVENT_LOG_LOCK` is held to commit. A reap of many lost workers commits each
    job with its event, so between two reaped jobs any replica can publish; one
    transaction for the whole pass would hold every publisher up for all of them."""
    bus = await make_bus()
    heard: list[Event] = []
    bus.add_listener(heard.append)
    published: list[str] = []
    free_between_jobs: list[bool] = []

    class Probe:
        """The bus, but before each job's event after the first it asks, from
        another connection, whether the log lock is free right now."""

        def publish_in(self, conn: psycopg.Connection[Any], event: Event) -> None:
            if published:
                with psycopg.connect(pg_conninfo) as other, other.transaction():
                    row = other.execute(
                        "SELECT pg_try_advisory_xact_lock(%s)", (EVENT_LOG_LOCK,)
                    ).fetchone()
                    assert row is not None
                    free_between_jobs.append(bool(row[0]))
            bus.publish_in(conn, event)
            published.append(event.id)

    store = PostgresJobStore(pg_conninfo, DataPaths(tmp_path), pool_size=2)
    store.open()
    try:
        jobs = [_stale_job(store, n) for n in range(4)]
        store.events = Probe()
        reaped = await asyncio.to_thread(store.reap, lease=0.0001, max_attempts=2)
    finally:
        store.close()

    assert sorted(job.id for job in reaped.requeued) == sorted(job.id for job in jobs)
    assert free_between_jobs == [True, True, True]
    await _until(lambda: len(_jobs(heard)) == 4)
    assert sorted(_jobs(heard)) == sorted(("job.pending", job.id) for job in jobs)


def _stale_job(store: PostgresJobStore, n: int) -> Job:
    """A job whose worker took it and died: claimed, never heartbeated."""
    job = Job(id=f"{n:032x}", slug="demo", params={"n": n}, created_at=datetime.now(UTC))
    store.submit(job, render_key("demo", job.params, None))
    assert store.claim() is not None
    return job


# --- selection and the fallback ------------------------------------------------------


@pytest.mark.requires_postgres
def test_a_database_url_selects_the_postgres_bus_on_the_queue_s_listener(
    tmp_path: Path, pg_conninfo: str
) -> None:
    state = build_state(
        Settings(
            data_dir=tmp_path,
            database_url=pg_conninfo,
            event_log_retention_seconds=60,
            event_log_retention_rows=10,
        )
    )
    assert isinstance(state.events, PgNotifyEventBus)
    store = state.queue.store
    assert isinstance(store, PostgresJobStore)
    assert store.events is state.events
    assert state.events.listener is store.pg_listener
    assert state.events.retention == EventLogRetention(seconds=60, rows=10)
    assert state.queue.events is state.events
    assert PG_CHANNEL in store.pg_listener.channels


@pytest.mark.parametrize("field", ["event_log_retention_seconds", "event_log_retention_rows"])
def test_retention_settings_are_validated_by_name(field: str) -> None:
    with pytest.raises(ValueError, match=f"SCADBUDDY_{field.upper()} must be at least 0"):
        Settings.model_validate({field: -1})


def test_retention_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCADBUDDY_EVENT_LOG_RETENTION_SECONDS", "120")
    monkeypatch.setenv("SCADBUDDY_EVENT_LOG_RETENTION_ROWS", "0")
    settings = Settings()
    assert settings.event_log_retention_seconds == 120
    assert settings.event_log_retention_rows == 0


def test_without_a_database_the_bus_stays_in_process(tmp_path: Path) -> None:
    """#266's "without the database": no Postgres, the UI's bus is the in-process one
    and the render queue publishes every job event itself, as before."""
    state = build_state(Settings(data_dir=tmp_path))
    assert isinstance(state.events, InProcessEventBus)
    assert isinstance(state.queue.store, JobStore)
    assert not state.queue.store.announces_jobs
    assert state.queue.events is state.events
