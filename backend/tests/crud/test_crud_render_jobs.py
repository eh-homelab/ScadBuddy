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


@pytest.fixture
async def jobs(projection: JobProjection) -> AsyncIterator[RemoteJobProjection]:
    async with served_render_jobs(projection, unavailable_budget=0.5) as remote:
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
    job = await _accept(jobs, _job())
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
    job = await _accept(jobs, _job())
    done = job.model_copy(update={"state": "done", "finished_at": datetime.now(UTC)})
    assert await asyncio.to_thread(jobs.finish, done)
    assert not await asyncio.to_thread(jobs.finish, done)
    assert _events(projection, job.id) == ["job.pending", "job.done"]


async def test_a_finish_to_an_unfinished_state_is_a_422(
    jobs: RemoteJobProjection, projection: JobProjection
) -> None:
    job = await _accept(jobs, _job())
    with pytest.raises(CrudRefusedError) as refused:
        await asyncio.to_thread(jobs.finish, job.model_copy(update={"state": "running"}))
    assert refused.value.status == 422
    assert projection.read(job.id).state == "pending"


async def test_a_500_from_the_accept_route_is_not_unreachable(
    jobs: RemoteJobProjection, projection: JobProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error the route does not expect fails the accept fast, as a 5xx, which the
    worker does not take for an API out of reach (`render_accept`)."""

    def broken(*_: object, **__: object) -> Job:
        raise RuntimeError("relation render_jobs has no column x")

    monkeypatch.setattr(projection, "accept", broken)
    started = time.monotonic()
    with pytest.raises(CrudUnavailableError) as failed:
        await _accept(jobs, _job())
    assert time.monotonic() - started < 3
    assert failed.value.outcome == "unavailable" and not failed.value.unreachable


async def test_claims_steps_and_reads_reach_the_row(
    jobs: RemoteJobProjection, projection: JobProjection
) -> None:
    job = await _accept(jobs, _job())
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
    job = await _accept(jobs, _job())

    def slow(*_: object) -> None:
        time.sleep(6)

    monkeypatch.setattr(projection, "set_claims", slow)
    started = time.monotonic()
    with pytest.raises(CrudUnavailableError):
        await asyncio.to_thread(jobs.set_claims, job.id, 2)
    assert time.monotonic() - started < PROJECT_TIMEOUT.total_seconds()
