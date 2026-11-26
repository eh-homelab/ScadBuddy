"""The render worker's job rows through the CRUD routes (#5298 Task 14, inventory R3-R10):
`RemoteJobProjection` over `api/crud_render.py`, on the API's own `JobProjection`. Each
write is natural, its event and activity row commit with it, and a `project` write ends
inside its 10 s attempt."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import psycopg
import pytest

from scadbuddy.activity.index import ActivityIndex
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.pg_listener import PgListener
from scadbuddy.crud.client import CrudRefusedError, CrudUnavailableError
from scadbuddy.crud.render import BASE, RemoteJobProjection
from scadbuddy.render.job_models import Job, JobNotFoundError, QueueFullError, StepInfo
from scadbuddy.render.projection import JobProjection
from scadbuddy.workflows.pipelines import PROJECT_TIMEOUT
from tests.crud.crud_support import served_render_jobs

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def projection(pg_conninfo: str) -> Iterator[JobProjection]:
    """The API's: its events on the bus, its activity rows in the index."""
    bus = PgNotifyEventBus(pg_conninfo, listener=PgListener(pg_conninfo))
    store = JobProjection(pg_conninfo, pool_size=4, events=bus)
    store.open()
    store.activity = ActivityIndex(store.pool, events=bus)
    try:
        yield store
    finally:
        store.close()


class _Clock:
    """The client's clock: the real one, moved on by `end_budget`."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.offset

    def end_budget(self) -> None:
        """Spend the current call's retry budget, so the try in flight is its last."""
        self.offset += 3600


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
async def jobs(projection: JobProjection, clock: _Clock) -> AsyncIterator[RemoteJobProjection]:
    async with served_render_jobs(projection, unavailable_budget=0.5, clock=clock) as remote:
        yield remote


def _job(slug: str = "demo") -> Job:
    return Job(
        id=uuid.uuid4().hex,
        slug=slug,
        params={"w": 12},
        inputs={"params": {"w": 12}},
        model_version="abc123",
        created_at=datetime.now(UTC),
    )


def _events(projection: JobProjection, job_id: str) -> list[str]:
    with projection.pool.connection() as conn:
        rows = conn.execute(
            "SELECT kind FROM events WHERE payload->>'job_id' = %s ORDER BY seq", (job_id,)
        ).fetchall()
    return [row["kind"] for row in rows]


def _count(projection: JobProjection, query: str, *args: object) -> int:
    with projection.pool.connection() as conn:
        row = conn.execute(query, args).fetchone()
    assert row is not None
    return int(row["n"])


async def _accept(remote: RemoteJobProjection, job: Job, **kwargs: object) -> Job:
    return await asyncio.to_thread(
        lambda: remote.accept(job, "key-1", workflow_id="render-key-1", run_id="run-1", **kwargs)  # type: ignore[arg-type]
    )


async def _seed(projection: JobProjection, job: Job) -> Job:
    """A test's job row, written by the API's own projection: a test of another route
    does not pay an accept's 2 s read bound in its setup (#7402)."""
    return await asyncio.to_thread(
        lambda: projection.accept(job, "key-1", workflow_id="render-key-1", run_id="run-1")
    )


async def test_an_accept_sent_twice_writes_one_row_one_event_one_activity_row(
    jobs: RemoteJobProjection, projection: JobProjection
) -> None:
    first = await _accept(jobs, _job())
    # A retried activity mints a new job id for the same execution: it finds the row.
    again = await _accept(jobs, _job())
    assert again.id == first.id and again.state == "pending"
    assert _count(projection, "SELECT count(*) AS n FROM render_jobs") == 1
    assert _events(projection, first.id) == ["job.pending"]
    assert (
        _count(projection, "SELECT count(*) AS n FROM activity WHERE id = %s", f"render:{first.id}")
        == 1
    )


async def test_a_full_queue_is_a_409_that_writes_nothing(
    jobs: RemoteJobProjection, projection: JobProjection
) -> None:
    projection.accept(_job("other"), "key-0", workflow_id="render-key-0", run_id="run-0")
    events = _count(projection, "SELECT count(*) AS n FROM events")
    with pytest.raises(QueueFullError) as refused:
        await _accept(jobs, _job(), max_pending=1)
    assert refused.value.depth == 1
    assert isinstance(refused.value.__cause__, CrudRefusedError)
    assert refused.value.__cause__.status == 409
    assert _count(projection, "SELECT count(*) AS n FROM render_jobs") == 1
    assert _count(projection, "SELECT count(*) AS n FROM events") == events
    assert _count(projection, "SELECT count(*) AS n FROM activity") == 1


async def test_an_accept_naming_another_executions_ids_answers_as_a_missing_row(
    jobs: RemoteJobProjection, projection: JobProjection
) -> None:
    theirs = projection.accept(_job("theirs"), "key-1", workflow_id="render-key-1", run_id="run-1")
    with pytest.raises(CrudRefusedError) as refused:
        await _accept(jobs, _job("mine"))
    assert refused.value.status == 404
    assert "theirs" not in str(refused.value.problem)
    assert projection.read(theirs.id).slug == "theirs"
    assert _count(projection, "SELECT count(*) AS n FROM render_jobs") == 1


def _finish(remote: RemoteJobProjection, job: Job, key: str) -> dict[str, bool]:
    """The route's own answer, as the worker's client sends it with ``key``."""
    response = remote._client.request(
        "POST",
        f"{BASE}/jobs/{job.id}/finish",
        json=job.model_dump(mode="json"),
        idempotency_key=key,
    )
    answer: dict[str, bool] = response.json()
    return answer


async def test_a_finish_sent_twice_answers_true_twice_and_announces_once(
    jobs: RemoteJobProjection, projection: JobProjection
) -> None:
    job = await _seed(projection, _job())
    started = await asyncio.to_thread(jobs.mark_started, job.id)
    assert started is not None and started.state == "running"
    assert await asyncio.to_thread(jobs.mark_started, job.id) is None
    done = started.model_copy(update={"state": "done", "finished_at": datetime.now(UTC)})
    # One call's resend (one key) replays that it moved the row; a later call's does not.
    moved = {"settled": True, "moved": True}
    assert await asyncio.to_thread(_finish, jobs, done, "call-1") == moved
    assert await asyncio.to_thread(_finish, jobs, done, "call-1") == moved
    assert await asyncio.to_thread(_finish, jobs, done, "call-2") == {
        "settled": True,
        "moved": False,
    }
    assert _events(projection, job.id) == ["job.pending", "job.running", "job.done"]
    # Another settle of the done row is not this one's resend.
    failed = done.model_copy(update={"state": "failed", "error": "late"})
    assert await asyncio.to_thread(_finish, jobs, failed, "call-3") == {
        "settled": False,
        "moved": False,
    }
    with projection.pool.connection() as conn:
        status = conn.execute(
            "SELECT status FROM activity WHERE id = %s", (f"render:{job.id}",)
        ).fetchone()
    assert status == {"status": "succeeded"}


async def test_the_workers_finish_is_true_only_for_the_call_that_moved_the_row(
    jobs: RemoteJobProjection, projection: JobProjection
) -> None:
    """A retried activity's finish answers False, so the worker counts a settle once."""
    job = await _seed(projection, _job())
    done = job.model_copy(update={"state": "done", "finished_at": datetime.now(UTC)})
    assert await asyncio.to_thread(jobs.finish, done)
    assert not await asyncio.to_thread(jobs.finish, done)
    assert _events(projection, job.id) == ["job.pending", "job.done"]


async def test_a_finish_to_an_unfinished_state_is_a_422(
    jobs: RemoteJobProjection, projection: JobProjection
) -> None:
    job = await _seed(projection, _job())
    with pytest.raises(CrudRefusedError) as refused:
        await asyncio.to_thread(jobs.finish, job.model_copy(update={"state": "running"}))
    assert refused.value.status == 422
    assert projection.read(job.id).state == "pending"


async def test_a_500_from_the_accept_route_does_not_pass(
    jobs: RemoteJobProjection,
    projection: JobProjection,
    clock: _Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error the route does not expect fails the accept fast, as a 5xx, which the
    worker does not take for a state that passes (`render_accept`). The route's answer
    ends the budget: the error names the last try's failure, and a retry that a loaded
    runner stalls past `ACCEPT_TIMEOUT` would be a read timeout, `transport` (#7525).
    The route is reached (counted, not timed): a loaded runner made one round trip take
    3.5 s (#7885). A loaded runner can also stall the first try past `ACCEPT_TIMEOUT`
    before the route runs, and the budget then ends with that timeout (#8075). The route's
    answer is what this test checks, so it accepts again until the client has seen it."""
    tries: list[int] = []

    def broken(*_: object, **__: object) -> Job:
        tries.append(1)
        clock.end_budget()
        raise RuntimeError("relation render_jobs has no column x")

    monkeypatch.setattr(projection, "accept", broken)
    accepts = 0
    while accepts < 5:
        accepts += 1
        with pytest.raises(CrudUnavailableError) as failed:
            await _accept(jobs, _job())
        if failed.value.failure != "transport":
            break
    # A try an earlier accept gave up on can still reach the route later, so only that
    # the route ran is counted; its answer ending the budget is the failure's name.
    assert tries, (accepts, failed.value.failure)
    assert failed.value.failure == "unavailable", (accepts, len(tries), failed.value.failure)
    assert not failed.value.passes


async def test_claims_steps_and_reads_reach_the_row(
    jobs: RemoteJobProjection, projection: JobProjection
) -> None:
    job = await _seed(projection, _job())
    await asyncio.to_thread(jobs.set_claims, job.id, 3)
    steps = [StepInfo(name="render", state="running", done=1, total=2)]
    await asyncio.to_thread(jobs.set_steps, job.id, steps)
    read = await asyncio.to_thread(jobs.read, job.id, pool_timeout=5.0)
    assert (read.claims, read.steps) == (3, steps)
    with pytest.raises(JobNotFoundError):
        await asyncio.to_thread(jobs.read, "absent")


async def test_a_slow_route_makes_a_project_write_raise_inside_its_attempt(
    jobs: RemoteJobProjection, projection: JobProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A route that answers in 6 s: the write's 5 s request timeout ends it, and no try
    starts after its budget, so it raises before `PROJECT_TIMEOUT` (10 s) cuts the
    attempt short."""
    job = await _seed(projection, _job())

    def slow(*_: object) -> None:
        time.sleep(6)

    monkeypatch.setattr(projection, "set_claims", slow)
    started = time.monotonic()
    with pytest.raises(CrudUnavailableError):
        await asyncio.to_thread(jobs.set_claims, job.id, 2)
    assert time.monotonic() - started < PROJECT_TIMEOUT.total_seconds()


async def test_a_database_out_of_reach_at_the_accept_route_passes(
    jobs: RemoteJobProjection,
    projection: JobProjection,
    clock: _Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As a 500: the route's answer ends the budget, so its 503 is the last try's."""

    def unreachable(*_: object, **__: object) -> Job:
        clock.end_budget()
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(projection, "accept", unreachable)
    with pytest.raises(CrudUnavailableError) as failed:
        await _accept(jobs, _job())
    assert failed.value.failure == "database_unavailable" and failed.value.passes
