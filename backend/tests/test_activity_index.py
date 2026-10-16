"""Activity (spec 2026-10-09): the record, its event and topic."""

import threading
import time

import pytest
from psycopg import Connection, errors

from scadbuddy.activity.index import ActivityIndex
from scadbuddy.activity.models import ActivityRow
from scadbuddy.api.realtime import topics_of, valid_topic
from scadbuddy.core.events import (
    ActivityEvent,
    AgentOperationEvent,
    JobEvent,
    decode_event,
    encode_event,
)
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.pg_listener import PgListener
from tests.conftest import PgPool


def test_activity_event_round_trips_and_routes() -> None:
    event = ActivityEvent(activity_id="render:abc", principal="browser")
    assert decode_event(encode_event(event)) == event
    assert topics_of(event) == ["activity", "activity:render:abc"]


def test_activity_topics_are_valid() -> None:
    assert valid_topic("activity")
    assert valid_topic("activity:render:abc123")
    assert valid_topic("activity:agent_operation:3f2c9a10-1b2c-4d5e-8f90-123456789abc")
    assert not valid_topic("activity:nope:x")
    assert not valid_topic("activity:render:")


def test_agent_operation_event_decodes_and_goes_nowhere() -> None:
    payload = (
        '{"id":"e1","at":"2026-10-09T00:00:00Z","kind":"ai_operation.changed","operation_id":"x"}'
    )
    event = decode_event(payload)
    assert isinstance(event, AgentOperationEvent)
    assert topics_of(event) == []


class Events:
    def __init__(self) -> None:
        self.published: list[object] = []

    def publish_in(self, conn: Connection[object], event: object) -> None:
        self.published.append(event)


def row(**over: object) -> ActivityRow:
    base = dict(
        id="render:j1",
        kind="render",
        status="running",
        title="Render keychain",
        source="render_jobs",
        source_id="j1",
        subject="keychain",
    )
    return ActivityRow.model_validate({**base, **over})


@pytest.mark.requires_postgres
async def test_upsert_then_attribute_keeps_principal_across_status_writes(pg_pool: PgPool) -> None:
    events = Events()
    index = ActivityIndex(pg_pool, events=events)
    with pg_pool.connection() as conn:
        index.upsert_in(conn, row())
    await index.attribute("render:j1", principal="token:t1", session="s1", via_tool="render_model")
    with pg_pool.connection() as conn:
        index.upsert_in(conn, row(status="succeeded"))
    got = await index.get("render:j1")
    assert got is not None
    assert (got.status, got.principal, got.session, got.via_tool) == (
        "succeeded",
        "token:t1",
        "s1",
        "render_model",
    )
    assert got.finished_at is not None
    assert len(events.published) == 3
    assert [getattr(e, "principal", None) for e in events.published] == [
        None,
        "token:t1",
        "token:t1",
    ]


@pytest.mark.requires_postgres
async def test_a_late_write_never_reopens_a_finished_row(pg_pool: PgPool) -> None:
    index = ActivityIndex(pg_pool, events=None)
    with pg_pool.connection() as conn:
        assert index.upsert_in(conn, row(status="succeeded"))
        assert not index.upsert_in(conn, row(status="running"))
        # A finished row still takes a finished write (a detail the source settles late).
        assert index.upsert_in(conn, row(status="failed"))
    got = await index.get("render:j1")
    assert got is not None
    assert got.status == "failed" and got.finished_at is not None


@pytest.mark.requires_postgres
async def test_attribute_before_upsert_is_kept(pg_pool: PgPool) -> None:
    index = ActivityIndex(pg_pool, events=None)
    await index.attribute("render:j2", principal="browser", session=None, via_tool=None)
    assert await index.get("render:j2") is None  # a placeholder is never shown
    with pg_pool.connection() as conn:
        index.upsert_in(conn, row(id="render:j2", source_id="j2"))
    got = await index.get("render:j2")
    assert got is not None and got.principal == "browser" and got.title == "Render keychain"


@pytest.mark.requires_postgres
async def test_first_attribution_wins(pg_pool: PgPool) -> None:
    index = ActivityIndex(pg_pool, events=None)
    await index.attribute("render:j3", principal="token:a", session=None, via_tool=None)
    await index.attribute("render:j3", principal="token:b", session="s", via_tool="t")
    with pg_pool.connection() as conn:
        index.upsert_in(conn, row(id="render:j3", source_id="j3"))
    got = await index.get("render:j3")
    assert got is not None and got.principal == "token:a"


@pytest.mark.requires_postgres
async def test_an_attribution_that_changes_nothing_announces_nothing(pg_pool: PgPool) -> None:
    """#3566: a client that keeps following a finished Update attributes it each time;
    only a write that fills something in publishes an ActivityEvent."""
    events = Events()
    index = ActivityIndex(pg_pool, events=events)
    for _ in range(3):
        await index.attribute("render:j4", principal="token:a", session=None, via_tool=None)
    assert len(events.published) == 1
    await index.attribute("render:j4", principal="token:a", session="s", via_tool=None)
    assert len(events.published) == 2


@pytest.mark.requires_postgres
async def test_upsert_with_attribution_publishes_principal(pg_pool: PgPool) -> None:
    from scadbuddy.activity.index import Attribution

    events = Events()
    index = ActivityIndex(pg_pool, events=events)
    with pg_pool.connection() as conn:
        index.upsert_in(
            conn,
            row(id="render:j4", source_id="j4"),
            attribution=Attribution("browser", None, None),
        )
    got = await index.get("render:j4")
    assert got is not None and got.principal == "browser"
    assert [getattr(e, "principal", None) for e in events.published] == ["browser"]


@pytest.mark.requires_postgres
def test_upsert_failure_does_not_abort_source(pg_pool: PgPool) -> None:
    metrics = Metrics()
    index = ActivityIndex(pg_pool, events=None, metrics=metrics)
    with pg_pool.connection() as conn, conn.transaction():
        conn.execute("CREATE TEMP TABLE src (x int)")
        conn.execute("INSERT INTO src VALUES (1)")
        index.upsert_in(
            conn, ActivityRow.model_construct(**{**row().model_dump(), "status": "bogus"})
        )
        index.upsert_in(conn, lambda: dict[str, ActivityRow]()["missing"])  # a mapping that raises
        conn.execute("INSERT INTO src VALUES (2)")
        assert conn.execute("SELECT count(*) AS n FROM src").fetchone() == {"n": 2}
    assert metrics.registry.get_sample_value("scadbuddy_activity_write_failures_total") == 2


@pytest.mark.requires_postgres
def test_migration_checks_reject_bad_status_and_inconsistent_finished_at(pg_pool: PgPool) -> None:
    insert = (
        "INSERT INTO activity (id, kind, status, title, source, source_id, finished_at)"
        " VALUES (%s, 'render', %s, 't', 'render_jobs', 'x', {})"
    )
    for activity_id, status, finished in (
        ("c1", "bogus", "now()"),
        ("c2", "succeeded", "NULL"),
        ("c3", "running", "now()"),
    ):
        with pg_pool.connection() as conn, pytest.raises(errors.CheckViolation):
            conn.execute(insert.replace("{}", finished), (activity_id, status))


@pytest.mark.requires_postgres
def test_writers_of_one_activity_row_never_deadlock_on_the_event_log(
    pg_pool: PgPool, pg_conninfo: str
) -> None:
    """The render projection publishes its job event, taking the event log's lock to
    commit, and then writes the job's activity row; every other activity write did the
    row first and then the lock. Two such transactions on one row deadlocked, and
    Postgres aborted one: its event or its activity write was lost (#3221)."""
    metrics = Metrics()
    bus = PgNotifyEventBus(pg_conninfo, listener=PgListener(pg_conninfo), metrics=metrics)
    index = ActivityIndex(pg_pool, events=bus, metrics=metrics)
    with pg_pool.connection() as conn:
        assert index.upsert_in(conn, row())
    other_pid: list[int] = []
    other_wrote: list[bool] = []

    def other() -> None:
        with pg_pool.connection() as conn, conn.transaction():
            other_pid.append(conn.info.backend_pid)
            other_wrote.append(index.upsert_in(conn, row(title="Render keychain again")))

    with pg_pool.connection() as conn, conn.transaction():
        # As render/projection.py `_announce` does: the event first, holding the lock...
        bus.publish_in(conn, JobEvent(kind="job.running", job_id="j1", slug="keychain"))
        thread = threading.Thread(target=other)
        thread.start()
        # The other writer waits on the event log's lock, seen in Postgres itself: with
        # the fix before the row, without it after taking the row, so this order meets it.
        deadline = time.monotonic() + 10
        with pg_pool.connection() as probe:
            while not (
                other_pid
                and probe.execute(
                    "SELECT 1 FROM pg_locks WHERE pid = %s AND locktype = 'advisory'"
                    " AND NOT granted",
                    (other_pid[0],),
                ).fetchone()
            ):
                assert time.monotonic() < deadline, "the other writer never waited on the lock"
                time.sleep(0.02)
        # ...then the activity row.
        assert index.upsert_in(conn, row(title="Render keychain, running"))
    thread.join(15)

    assert not thread.is_alive(), "the other writer is still waiting"
    assert other_wrote == [True]
    assert metrics.registry.get_sample_value("scadbuddy_activity_write_failures_total") == 0
    dropped = metrics.registry.get_sample_value(
        "scadbuddy_events_dropped_total", {"reason": "error"}
    )
    assert not dropped
