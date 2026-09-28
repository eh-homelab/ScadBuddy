"""The render queue's latency controls -- supersede, coalesce, deadline, leases --
against both job stores, and the metrics that show whether they hold."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
import pytest_asyncio

from scadbuddy.core.config import Config
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.paths import DataPaths
from scadbuddy.render.diagnostics import Diagnostic
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.job_store import LOST_WORKER_ERROR, JobBackend, QueueCounts, render_key
from scadbuddy.render.jobs import (
    SUPERSEDED_ERROR,
    Job,
    JobResult,
    JobStore,
    PartInfo,
    QueueFullError,
    RenderQueue,
    attempt_work_dir,
)
from scadbuddy.render.pg_store import (
    MIGRATIONS,
    QUEUE_CHANNEL,
    TWIN_QUEUED_ERROR,
    PostgresJobStore,
    QueueListener,
)
from scadbuddy.render.runner import OpenSCADError

CONFIG = Config(
    data_dir=Path("/unused"),
    render_concurrency=1,
    render_poll_interval=0.05,
    job_ttl=3600.0,
)


def _result() -> JobResult:
    return JobResult(
        model_3mf="jobs/x.work/model.3mf",
        preview_glb="jobs/x.work/preview.glb",
        parts=[PartInfo(name="Color 1", colour="#FF6AC1", extruder=1, watertight=True)],
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
    )


def _job(**params: int) -> Job:
    return Job(id=uuid.uuid4().hex, slug="demo", params=dict(params), created_at=datetime.now(UTC))


class Gate:
    """A render that holds its worker until released, so jobs behind it wait."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started: list[str] = []

    async def __call__(self, job: Job) -> tuple[JobResult, list[str]]:
        self.started.append(job.id)
        await self.release.wait()
        return _result(), []


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    data = DataPaths(tmp_path)
    data.ensure()
    return data


StoreFactory = Callable[[], JobBackend]


@pytest.fixture(params=["files", pytest.param("postgres", marks=pytest.mark.requires_postgres)])
def make_store(request: pytest.FixtureRequest, paths: DataPaths) -> StoreFactory:
    if request.param == "files":
        return lambda: JobStore(paths)
    conninfo: str = request.getfixturevalue("pg_conninfo")
    return lambda: PostgresJobStore(conninfo, paths, pool_size=4)


QueueFactory = Callable[..., Awaitable[RenderQueue]]


@pytest_asyncio.fixture
async def make_queue(paths: DataPaths, make_store: StoreFactory) -> AsyncIterator[QueueFactory]:
    queues: list[RenderQueue] = []

    async def make(render: Gate, config: Config = CONFIG) -> RenderQueue:
        queue = RenderQueue(config, paths, store=make_store(), render=render)
        await queue.start()
        queues.append(queue)
        return queue

    yield make
    for queue in queues:
        await queue.aclose()


async def _occupy_the_worker(queue: RenderQueue, gate: Gate) -> Job:
    running = await queue.submit("demo", {"n": 0})
    for _ in range(200):
        if gate.started:
            break
        await asyncio.sleep(0.01)
    assert gate.started == [running.id]
    return running


def _pending(queue: RenderQueue) -> int:
    return queue.store.counts().pending


def _sample(metrics: Metrics, name: str, **labels: str) -> float:
    value = metrics.registry.get_sample_value(name, labels)
    return 0.0 if value is None else value


async def test_an_identical_waiting_render_is_coalesced(make_queue: QueueFactory) -> None:
    gate = Gate()
    queue = await make_queue(gate)
    await _occupy_the_worker(queue, gate)

    first = await queue.submit("demo", {"n": 1, "label": "hi"})
    second = await queue.submit("demo", {"label": "hi", "n": 1})

    assert second.id == first.id
    assert _pending(queue) == 1
    assert _sample(queue.metrics, "scadbuddy_render_jobs_coalesced_total") == 1
    gate.release.set()
    await queue.join()
    assert queue.store.read(first.id).state == "done"


async def test_a_different_revision_is_not_the_same_render(make_queue: QueueFactory) -> None:
    gate = Gate()
    queue = await make_queue(gate)
    await _occupy_the_worker(queue, gate)

    live = await queue.submit("demo", {"n": 1})
    old = await queue.submit("demo", {"n": 1}, model_version="a" * 40)

    assert old.id != live.id
    gate.release.set()
    await queue.join()


async def test_a_running_render_is_never_coalesced(make_queue: QueueFactory) -> None:
    """It has already read its source; an edit since would be served stale."""
    gate = Gate()
    queue = await make_queue(gate)
    running = await _occupy_the_worker(queue, gate)

    again = await queue.submit("demo", {"n": 0})

    assert again.id != running.id
    gate.release.set()
    await queue.join()


async def test_a_superseded_waiting_job_is_dropped_unrendered(make_queue: QueueFactory) -> None:
    gate = Gate()
    queue = await make_queue(gate)
    await _occupy_the_worker(queue, gate)

    stale = await queue.submit("demo", {"n": 1})
    fresh = await queue.submit("demo", {"n": 2}, supersedes=stale.id)
    gate.release.set()
    await queue.join()

    dropped = queue.store.read(stale.id)
    assert dropped.state == "failed"
    assert dropped.error == SUPERSEDED_ERROR
    assert dropped.started_at is None
    assert stale.id not in gate.started
    assert queue.store.read(fresh.id).state == "done"
    metrics = queue.metrics
    assert _sample(metrics, "scadbuddy_render_jobs_finished_total", outcome="superseded") == 1


async def test_a_running_job_is_not_superseded(make_queue: QueueFactory) -> None:
    gate = Gate()
    queue = await make_queue(gate)
    running = await _occupy_the_worker(queue, gate)

    await queue.submit("demo", {"n": 1}, supersedes=running.id)
    gate.release.set()
    await queue.join()

    assert queue.store.read(running.id).state == "done"


async def test_superseding_a_shared_job_only_releases_this_claim(
    make_queue: QueueFactory,
) -> None:
    """Two tabs coalesced onto one job; one moving on must not cancel the other's."""
    gate = Gate()
    queue = await make_queue(gate)
    await _occupy_the_worker(queue, gate)

    shared = await queue.submit("demo", {"n": 1})
    assert (await queue.submit("demo", {"n": 1})).id == shared.id
    await queue.submit("demo", {"n": 2}, supersedes=shared.id)
    gate.release.set()
    await queue.join()

    assert queue.store.read(shared.id).state == "done"


async def test_resubmitting_the_render_it_supersedes_keeps_that_job(
    make_queue: QueueFactory,
) -> None:
    gate = Gate()
    queue = await make_queue(gate)
    await _occupy_the_worker(queue, gate)

    first = await queue.submit("demo", {"n": 1})
    again = await queue.submit("demo", {"n": 1}, supersedes=first.id)

    assert again.id == first.id
    gate.release.set()
    await queue.join()
    assert queue.store.read(first.id).state == "done"


async def test_a_job_of_another_model_is_never_superseded(make_queue: QueueFactory) -> None:
    """A stale job id from another model's page, or another client's, drops nothing."""
    gate = Gate()
    queue = await make_queue(gate)
    await _occupy_the_worker(queue, gate)

    theirs = await queue.submit("other", {"n": 1})
    await queue.submit("demo", {"n": 2}, supersedes=theirs.id)
    gate.release.set()
    await queue.join()

    assert queue.store.read(theirs.id).state == "done"
    assert _sample(queue.metrics, "scadbuddy_render_jobs_finished_total", outcome="superseded") == 0


async def test_every_render_is_accepted_however_deep_the_queue(
    make_queue: QueueFactory,
) -> None:
    """There is no admission limit: a busy server queues, it never refuses."""
    gate = Gate()
    queue = await make_queue(gate, replace(CONFIG, render_queue_depth_slo=2))
    await _occupy_the_worker(queue, gate)

    waiting = [await queue.submit("demo", {"n": n}) for n in range(1, 41)]

    assert _pending(queue) == 40
    queue.refresh_metrics()
    assert _sample(queue.metrics, "scadbuddy_render_queue_depth") == 40
    assert _sample(queue.metrics, "scadbuddy_render_queue_depth_slo") == 2
    assert _sample(queue.metrics, "scadbuddy_render_queue_oldest_seconds") > 0
    gate.release.set()
    await queue.join()
    assert all(queue.store.read(job.id).state == "done" for job in waiting)


async def test_a_configured_limit_refuses_with_a_retry_hint(make_queue: QueueFactory) -> None:
    gate = Gate()
    queue = await make_queue(gate, replace(CONFIG, render_queue_max=3))
    await _occupy_the_worker(queue, gate)
    for n in range(1, 4):
        await queue.submit("demo", {"n": n})

    with pytest.raises(QueueFullError) as refused:
        await queue.submit("demo", {"n": 4})

    assert refused.value.depth == 3
    assert refused.value.retry_after >= 1
    assert _pending(queue) == 3
    assert _sample(queue.metrics, "scadbuddy_render_jobs_rejected_total") == 1
    assert _sample(queue.metrics, "scadbuddy_render_queue_max") == 3
    # A request that coalesces takes no place, so it is still answered.
    assert (await queue.submit("demo", {"n": 3})).slug == "demo"
    gate.release.set()
    await queue.join()


async def test_superseding_frees_its_place_before_the_limit(make_queue: QueueFactory) -> None:
    """A slider drag is never refused for the room its own stale previews take up."""
    gate = Gate()
    queue = await make_queue(gate, replace(CONFIG, render_queue_max=3))
    await _occupy_the_worker(queue, gate)
    waiting = [await queue.submit("demo", {"n": n}) for n in range(1, 4)]

    fresh = await queue.submit("demo", {"n": 9}, supersedes=waiting[-1].id)

    assert _pending(queue) == 3
    gate.release.set()
    await queue.join()
    assert queue.store.read(fresh.id).state == "done"


async def test_a_refused_submit_supersedes_nothing(make_queue: QueueFactory) -> None:
    """A shared job keeps its place when one of its two claims moves on, so the
    submit is refused -- and the claim it tried to release is still there."""
    gate = Gate()
    queue = await make_queue(gate, replace(CONFIG, render_queue_max=2))
    await _occupy_the_worker(queue, gate)
    shared = await queue.submit("demo", {"n": 1})
    assert (await queue.submit("demo", {"n": 1})).id == shared.id
    await queue.submit("demo", {"n": 2})

    with pytest.raises(QueueFullError):
        await queue.submit("demo", {"n": 3}, supersedes=shared.id)
    # Had the refusal released a claim, this second release (by a submit that
    # coalesces, so is never refused) would be the last one and drop the job.
    await queue.submit("demo", {"n": 2}, supersedes=shared.id)
    gate.release.set()
    await queue.join()
    assert queue.store.read(shared.id).state == "done"


async def test_jobs_are_rendered_oldest_first(make_queue: QueueFactory) -> None:
    gate = Gate()
    queue = await make_queue(gate)
    running = await _occupy_the_worker(queue, gate)

    waiting = [await queue.submit("demo", {"n": n}) for n in range(1, 5)]
    gate.release.set()
    await queue.join()

    assert gate.started == [running.id, *(job.id for job in waiting)]


async def test_concurrency_is_the_number_of_jobs_rendered_at_once(
    make_queue: QueueFactory,
) -> None:
    in_flight = 0
    peak = 0
    release = asyncio.Event()

    class Counting(Gate):
        async def __call__(self, job: Job) -> tuple[JobResult, list[str]]:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await release.wait()
            in_flight -= 1
            return _result(), []

    queue = await make_queue(Counting(), replace(CONFIG, render_concurrency=3))
    for n in range(8):
        await queue.submit("demo", {"n": n})
    for _ in range(200):
        if peak == 3:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    assert peak == 3
    release.set()
    await queue.join()
    assert peak == 3


async def test_a_job_past_its_queue_deadline_is_failed_unrendered(
    make_queue: QueueFactory,
) -> None:
    gate = Gate()
    queue = await make_queue(gate, replace(CONFIG, render_queue_timeout=0.05))
    await _occupy_the_worker(queue, gate)

    late = await queue.submit("demo", {"n": 1})
    await asyncio.sleep(0.15)
    gate.release.set()
    await queue.join()

    expired = queue.store.read(late.id)
    assert expired.state == "failed"
    assert expired.error is not None
    assert "SCADBUDDY_RENDER_QUEUE_TIMEOUT" in expired.error
    assert late.id not in gate.started
    assert _sample(queue.metrics, "scadbuddy_render_jobs_finished_total", outcome="expired") == 1


async def test_the_queue_says_which_store_holds_it(make_queue: QueueFactory) -> None:
    queue = await make_queue(Gate())
    assert _sample(queue.metrics, "scadbuddy_render_store_info", backend=queue.store.backend) == 1
    assert queue.store.backend in ("files", "postgres")


async def test_the_queue_reports_its_latency(make_queue: QueueFactory) -> None:
    gate = Gate()
    gate.release.set()
    queue = await make_queue(gate, replace(CONFIG, render_latency_slo=45.0))

    await queue.submit("demo", {"n": 1})
    await queue.join()
    queue.refresh_metrics()

    metrics = queue.metrics
    assert _sample(metrics, "scadbuddy_render_jobs_submitted_total") == 1
    assert _sample(metrics, "scadbuddy_render_jobs_finished_total", outcome="done") == 1
    assert _sample(metrics, "scadbuddy_render_queue_wait_seconds_count") == 1
    assert _sample(metrics, "scadbuddy_render_duration_seconds_count", outcome="done") == 1
    assert _sample(metrics, "scadbuddy_render_job_latency_seconds_count", outcome="done") == 1
    assert _sample(metrics, "scadbuddy_render_jobs_running") == 0
    assert _sample(metrics, "scadbuddy_render_queue_depth") == 0
    assert _sample(metrics, "scadbuddy_render_queue_oldest_seconds") == 0
    assert _sample(metrics, "scadbuddy_render_workers") == 1
    assert _sample(metrics, "scadbuddy_render_latency_slo_seconds") == 45


def test_a_missing_job_is_a_lookup_error(make_store: StoreFactory) -> None:
    store = make_store()
    store.open()
    try:
        with pytest.raises(LookupError):
            store.read("f" * 32)
    finally:
        store.close()


# --- diagnostics where the job lives (#252) ------------------------------------------

WARNING = Diagnostic(severity="warning", message="unknown variable", file="model.scad", line=3)
ERROR = Diagnostic(severity="error", message="Parser error: syntax error", file="model.scad")


async def test_a_failed_render_s_diagnostics_are_kept_with_the_job(
    make_queue: QueueFactory,
) -> None:
    """A failed render has no result: its diagnostics must be on the record itself,
    in the job file or on the `render_jobs` row."""

    async def fail(job: Job) -> tuple[JobResult, list[str]]:
        raise OpenSCADError("openscad exited with 1", ["ERROR: x"], 1, [ERROR], 7)

    queue = await make_queue(fail)
    job = await queue.submit("demo", {"n": 1})
    await queue.join()

    stored = queue.store.read(job.id)
    assert stored.state == "failed"
    assert stored.diagnostics == [ERROR]
    assert stored.diagnostics_dropped == 7


async def test_a_done_render_s_diagnostics_are_kept_with_the_job(
    make_queue: QueueFactory,
) -> None:
    async def render(job: Job) -> tuple[JobResult, list[str]]:
        return _result().model_copy(update={"diagnostics": [WARNING]}), []

    queue = await make_queue(render)
    job = await queue.submit("demo", {"n": 1})
    await queue.join()

    stored = queue.store.read(job.id)
    assert stored.diagnostics == [WARNING]
    assert stored.result is not None and stored.result.diagnostics == [WARNING]


async def test_a_failed_render_s_warnings_are_kept_with_the_job(
    make_queue: QueueFactory,
) -> None:
    """#408: a failed render has no result, so its warnings -- say the picture it
    could not open, the likely reason it drew nothing -- live on the record too."""
    missing = "OpenSCAD could not open pic.svg"

    async def fail(job: Job) -> tuple[JobResult, list[str]]:
        raise OpenSCADError("the render produced no geometry", [], warnings=[missing])

    queue = await make_queue(fail)
    job = await queue.submit("demo", {"n": 1})
    await queue.join()

    stored = queue.store.read(job.id)
    assert stored.state == "failed"
    assert stored.warnings == [missing]


async def test_a_done_render_s_warnings_are_kept_with_the_job(make_queue: QueueFactory) -> None:
    async def render(job: Job) -> tuple[JobResult, list[str]]:
        return _result().model_copy(update={"warnings": ["a warning"]}), []

    queue = await make_queue(render)
    job = await queue.submit("demo", {"n": 1})
    await queue.join()

    assert queue.store.read(job.id).warnings == ["a warning"]


def test_the_latest_settled_render_is_the_one_that_finished_last(
    make_store: StoreFactory,
) -> None:
    store = make_store()
    store.open()
    try:
        settled: list[str] = []
        for n in (1, 2, 3):
            job = _job(n=n)
            store.submit(job, render_key("demo", job.params, None))
            claimed = store.claim()
            assert claimed is not None
            claimed.state = "failed" if n == 2 else "done"
            claimed.finished_at = datetime.now(UTC)
            claimed.diagnostics = [ERROR] if n == 2 else []
            assert store.finish(claimed)
            settled.append(claimed.id)
        waiting = _job(n=9)
        store.submit(waiting, render_key("demo", waiting.params, None))

        latest = store.latest_finished("demo")

        assert latest is not None and latest.id == settled[-1]
        assert store.latest_finished("another") is None
    finally:
        store.close()


# --- Postgres only -----------------------------------------------------------------


@pytest.mark.requires_postgres
def test_migrations_apply_once(pg_conninfo: str, paths: DataPaths) -> None:
    first = PostgresJobStore(pg_conninfo, paths, pool_size=2)
    first.open()
    first.close()
    second = PostgresJobStore(pg_conninfo, paths, pool_size=2)
    second.open()
    second.close()
    with psycopg.connect(pg_conninfo) as conn:
        ids = [row[0] for row in conn.execute("SELECT id FROM scadbuddy_migrations ORDER BY id")]
    assert ids == [m.id for m in MIGRATIONS]


@pytest.mark.requires_postgres
def test_accepted_jobs_survive_a_restart(pg_conninfo: str, paths: DataPaths) -> None:
    """The point of the table: a deploy mid-queue renders the queue after it."""
    before = PostgresJobStore(pg_conninfo, paths, pool_size=2)
    before.open()
    job = _job(n=1)
    before.submit(job, render_key("demo", job.params, None))
    before.close()

    after = PostgresJobStore(pg_conninfo, paths, pool_size=2)
    after.open()
    try:
        assert after.abandon_orphans() == []
        claimed = after.claim()
        assert claimed is not None and claimed.id == job.id
        assert claimed.state == "running"
    finally:
        after.close()


@pytest.mark.requires_postgres
def test_concurrent_identical_submits_make_one_job(pg_conninfo: str, paths: DataPaths) -> None:
    store = PostgresJobStore(pg_conninfo, paths, pool_size=8)
    store.open()
    ids: list[str] = []
    lock = threading.Lock()

    def submit() -> None:
        job = _job(n=7)
        answered = store.submit(job, render_key("demo", job.params, None)).job
        with lock:
            ids.append(answered.id)

    try:
        threads = [threading.Thread(target=submit) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(set(ids)) == 1
        assert store.counts().pending == 1
    finally:
        store.close()


@pytest.mark.requires_postgres
def test_a_lost_worker_s_job_is_requeued_then_failed(pg_conninfo: str, paths: DataPaths) -> None:
    store = PostgresJobStore(pg_conninfo, paths, pool_size=2)
    store.open()
    try:
        job = _job(n=1)
        store.submit(job, render_key("demo", job.params, None))
        assert store.claim() is not None  # ...and the worker dies without a heartbeat

        first = store.reap(lease=0.0001, max_attempts=2)
        assert [j.id for j in first.requeued] == [job.id]
        assert store.read(job.id).state == "pending"

        again = store.claim()
        assert again is not None
        second = store.reap(lease=0.0001, max_attempts=2)
        assert [j.id for j in second.failed] == [job.id]
        dead = store.read(job.id)
        assert dead.state == "failed"
        assert dead.error == LOST_WORKER_ERROR
    finally:
        store.close()


@pytest.mark.requires_postgres
def test_a_requeued_job_keeps_every_claim_on_it(pg_conninfo: str, paths: DataPaths) -> None:
    """Two tabs share one job; it loses its worker and is requeued. One tab moving
    on (a supersede naming it) must release only its own claim, not drop the
    render the other tab is still waiting on."""
    store = PostgresJobStore(pg_conninfo, paths, pool_size=2)
    store.open()
    try:
        shared = _job(n=1)
        key = render_key("demo", shared.params, None)
        store.submit(shared, key)
        assert store.submit(_job(n=1), key).coalesced  # the second tab
        assert store.claim() is not None  # ...and its worker dies
        assert [j.id for j in store.reap(lease=0.0001, max_attempts=2).requeued] == [shared.id]

        newer = _job(n=2)
        answer = store.submit(newer, render_key("demo", newer.params, None), supersedes=shared.id)

        assert answer.superseded is None
        assert store.read(shared.id).state == "pending"
    finally:
        store.close()


@pytest.mark.requires_postgres
def test_a_retry_s_wait_counts_from_the_submit(pg_conninfo: str, paths: DataPaths) -> None:
    """What the queue deadline measures (`started_at - created_at`): on a retry it
    still starts at the original submit, so the first attempt's time counts."""
    store = PostgresJobStore(pg_conninfo, paths, pool_size=2)
    store.open()
    try:
        job = _job(n=1)
        store.submit(job, render_key("demo", job.params, None))
        first = store.claim()
        assert first is not None and first.started_at is not None
        store.reap(lease=0.0001, max_attempts=2)
        retry = store.claim()
        assert retry is not None and retry.started_at is not None

        assert retry.created_at == first.created_at
        assert retry.started_at - retry.created_at >= first.started_at - first.created_at
    finally:
        store.close()


@pytest.mark.requires_postgres
def test_a_twin_in_the_queue_fails_one_row_not_the_whole_reap(
    pg_conninfo: str, paths: DataPaths
) -> None:
    """Requeueing a job whose identical render is already pending would violate the
    pending-key index: that row fails as interrupted, and the rest of the pass
    still requeues."""
    store = PostgresJobStore(pg_conninfo, paths, pool_size=2)
    store.open()
    try:
        twin_of, other = _job(n=1), _job(n=2)
        for job in (twin_of, other):
            store.submit(job, render_key("demo", job.params, None))
        assert store.claim() is not None
        assert store.claim() is not None  # both lose their worker
        twin = _job(n=1)
        store.submit(twin, render_key("demo", twin.params, None))

        reaped = store.reap(lease=0.0001, max_attempts=3)

        assert [j.id for j in reaped.requeued] == [other.id]
        assert [j.id for j in reaped.failed] == [twin_of.id]
        assert store.read(twin_of.id).error == TWIN_QUEUED_ERROR
        assert store.read(other.id).state == "pending"
    finally:
        store.close()


@pytest.mark.requires_postgres
def test_a_retry_keeps_only_its_own_diagnostics(pg_conninfo: str, paths: DataPaths) -> None:
    """The stale attempt's diagnostics never land; the retry's do."""
    store = PostgresJobStore(pg_conninfo, paths, pool_size=2)
    store.open()
    try:
        job = _job(n=1)
        store.submit(job, render_key("demo", job.params, None))
        stale = store.claim()
        assert stale is not None
        store.reap(lease=0.0001, max_attempts=3)
        assert store.read(job.id).diagnostics == []
        retry = store.claim()
        assert retry is not None

        stale.state, stale.diagnostics, stale.diagnostics_dropped = "failed", [ERROR], 4
        assert store.finish(stale) is False
        retry.state, retry.result, retry.diagnostics = "done", _result(), [WARNING]
        assert store.finish(retry) is True

        stored = store.read(job.id)
        assert (stored.diagnostics, stored.diagnostics_dropped) == ([WARNING], 0)
    finally:
        store.close()


@pytest.mark.requires_postgres
def test_a_reaped_worker_cannot_overwrite_the_retry(pg_conninfo: str, paths: DataPaths) -> None:
    store = PostgresJobStore(pg_conninfo, paths, pool_size=2)
    store.open()
    try:
        job = _job(n=1)
        store.submit(job, render_key("demo", job.params, None))
        stale = store.claim()
        assert stale is not None
        store.reap(lease=0.0001, max_attempts=3)
        retry = store.claim()
        assert retry is not None

        stale.state = "failed"
        stale.error = "late"
        assert store.finish(stale) is False
        retry.state = "done"
        retry.result = _result()
        assert store.finish(retry) is True
        assert store.read(job.id).state == "done"
    finally:
        store.close()


@pytest.mark.requires_postgres
async def test_a_heartbeating_job_is_not_reaped(pg_conninfo: str, paths: DataPaths) -> None:
    gate = Gate()
    config = replace(CONFIG, render_lease_timeout=0.3)
    queue = RenderQueue(
        config, paths, store=PostgresJobStore(pg_conninfo, paths, pool_size=4), render=gate
    )
    await queue.start()
    try:
        running = await _occupy_the_worker(queue, gate)
        await asyncio.sleep(1.0)  # three leases: the reaper has run several times
        assert queue.store.read(running.id).state == "running"
        assert _sample(queue.metrics, "scadbuddy_render_jobs_retried_total") == 0
        gate.release.set()
        await queue.join()
        assert queue.store.read(running.id).state == "done"
    finally:
        await queue.aclose()


# --- wake-ups: NOTIFY, and the fallback poll ----------------------------------------

#: Neither poll can explain a pickup well inside this: only a wake-up can.
SLOW_POLLS = replace(CONFIG, render_poll_interval=30.0, render_fallback_poll_interval=30.0)
#: "Well under the fallback interval".
PROMPTLY = 5.0


async def _until(predicate: Callable[[], bool], timeout: float = PROMPTLY) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


def _listening(queue: RenderQueue) -> bool:
    return _sample(queue.metrics, "scadbuddy_render_queue_listener_connected") == 1


@pytest.mark.requires_postgres
def test_only_a_committed_new_job_is_announced(pg_conninfo: str, paths: DataPaths) -> None:
    """NOTIFY goes out in the submit's transaction: a coalesced submit queued nothing
    and a refused one was rolled back, so neither wakes anybody."""
    store = PostgresJobStore(pg_conninfo, paths, pool_size=2)
    store.open()
    try:
        with psycopg.connect(pg_conninfo, autocommit=True) as listen:
            listen.execute(f"LISTEN {QUEUE_CHANNEL}".encode())
            key = render_key("demo", {"n": 1}, None)
            store.submit(_job(n=1), key)
            store.submit(_job(n=1), key)  # coalesced
            with pytest.raises(QueueFullError):
                store.submit(_job(n=2), render_key("demo", {"n": 2}, None), max_pending=1)
            announced = list(listen.notifies(timeout=0.5))
    finally:
        store.close()

    assert [n.channel for n in announced] == [QUEUE_CHANNEL]


@pytest.mark.requires_postgres
async def test_a_job_queued_by_one_pool_is_taken_at_once_by_another(
    pg_conninfo: str, paths: DataPaths
) -> None:
    gate_a, gate_b = Gate(), Gate()
    a = RenderQueue(
        CONFIG, paths, store=PostgresJobStore(pg_conninfo, paths, pool_size=4), render=gate_a
    )
    b = RenderQueue(
        SLOW_POLLS, paths, store=PostgresJobStore(pg_conninfo, paths, pool_size=4), render=gate_b
    )
    await a.start()
    try:
        await _occupy_the_worker(a, gate_a)  # A cannot take the next job itself
        await b.start()
        await _until(lambda: _listening(b))

        started = time.monotonic()
        job = await a.submit("demo", {"n": 1})
        await _until(lambda: gate_b.started == [job.id])

        assert time.monotonic() - started < PROMPTLY < SLOW_POLLS.render_fallback_poll_interval
    finally:
        gate_a.release.set()
        gate_b.release.set()
        await a.aclose()
        await b.aclose()


@pytest.mark.requires_postgres
async def test_a_dropped_listener_reconnects_and_work_continues(
    pg_conninfo: str, paths: DataPaths
) -> None:
    gate = Gate()
    gate.release.set()
    queue = RenderQueue(
        SLOW_POLLS, paths, store=PostgresJobStore(pg_conninfo, paths, pool_size=4), render=gate
    )
    elsewhere = PostgresJobStore(pg_conninfo, paths, pool_size=2)  # another replica
    elsewhere.open()
    await queue.start()
    try:
        await _until(lambda: _listening(queue))
        listener = queue.listener
        assert isinstance(listener, QueueListener)
        assert queue.idle_poll_interval == SLOW_POLLS.render_fallback_poll_interval
        dropped = listener.backend_pid
        with psycopg.connect(pg_conninfo, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (dropped,))

        reconnects = "scadbuddy_render_queue_listener_reconnects_total"
        await _until(lambda: _sample(queue.metrics, reconnects) == 1 and _listening(queue))
        assert listener.backend_pid not in (None, dropped)

        job = _job(n=1)
        elsewhere.submit(job, render_key("demo", {"n": 1}, None))
        await _until(lambda: gate.started == [job.id])
    finally:
        await queue.aclose()
        elsewhere.close()
    assert not _listening(queue)


async def test_the_file_store_wakes_its_own_workers_without_a_listener(
    paths: DataPaths,
) -> None:
    gate = Gate()
    gate.release.set()
    config = replace(SLOW_POLLS, render_poll_interval=20.0)
    queue = RenderQueue(config, paths, store=JobStore(paths), render=gate)
    await queue.start()
    try:
        assert queue.listener is None
        assert queue.idle_poll_interval == config.render_poll_interval  # unchanged
        await asyncio.sleep(0.05)  # the worker has found nothing and is idle
        job = await queue.submit("demo", {"n": 1})
        await _until(lambda: gate.started == [job.id])
        assert not _listening(queue)
        assert _sample(queue.metrics, "scadbuddy_render_queue_listener_reconnects_total") == 0
    finally:
        await queue.aclose()


# --- metrics and config --------------------------------------------------------------


def test_a_stage_is_timed_even_when_it_raises() -> None:
    metrics = Metrics()
    with pytest.raises(RuntimeError), metrics.stage("render"):
        raise RuntimeError("openscad died")
    assert _sample(metrics, "scadbuddy_render_stage_seconds_count", stage="render") == 1


def test_every_outcome_series_exists_before_the_first_job() -> None:
    exposition = Metrics().exposition().decode()
    for outcome in ("done", "failed", "expired", "superseded"):
        assert f'scadbuddy_render_jobs_finished_total{{outcome="{outcome}"}} 0.0' in exposition


@pytest.mark.parametrize(
    ("field", "value", "env"),
    [
        ("render_queue_max", -1, "SCADBUDDY_RENDER_QUEUE_MAX"),
        ("render_queue_timeout", -1.0, "SCADBUDDY_RENDER_QUEUE_TIMEOUT"),
        ("render_poll_interval", 0.0, "SCADBUDDY_RENDER_POLL_INTERVAL"),
        ("render_fallback_poll_interval", 0.0, "SCADBUDDY_RENDER_FALLBACK_POLL_INTERVAL"),
        ("render_lease_timeout", 0.0, "SCADBUDDY_RENDER_LEASE_TIMEOUT"),
        ("render_max_attempts", 0, "SCADBUDDY_RENDER_MAX_ATTEMPTS"),
        ("render_queue_depth_slo", -1, "SCADBUDDY_RENDER_QUEUE_DEPTH_SLO"),
        ("render_latency_slo", -1.0, "SCADBUDDY_RENDER_LATENCY_SLO"),
    ],
)
def test_queue_settings_are_validated_by_name(field: str, value: float, env: str) -> None:
    with pytest.raises(ValueError, match=env):
        Config(**{field: value})  # type: ignore[arg-type]


def test_a_retry_renders_into_a_directory_of_its_own(paths: DataPaths) -> None:
    """A lapsed lease does not prove the first worker died, so the retry must not
    write into the files it may still be writing."""
    job = _job(n=1)
    first = attempt_work_dir(paths, job.claimed(1))
    retry = attempt_work_dir(paths, job.claimed(2))

    assert first == paths.job_work_dir(job.id)
    assert retry != first
    assert retry.is_relative_to(paths.job_work_dir(job.id))  # deleted with the job


async def test_a_store_that_cannot_be_read_says_so(make_queue: QueueFactory) -> None:
    """The queue gauges keep their last values through an outage, so the outage
    needs a signal of its own for the stall alert to key off."""
    gate = Gate()
    gate.release.set()
    queue = await make_queue(gate)
    queue.refresh_metrics()
    assert _sample(queue.metrics, "scadbuddy_render_store_up") == 1

    def down() -> QueueCounts:
        raise ConnectionError("the database went away")

    queue.store.counts = down  # type: ignore[method-assign]
    queue.refresh_metrics()  # must not raise: the scrape still answers

    assert _sample(queue.metrics, "scadbuddy_render_store_up") == 0
    assert _sample(queue.metrics, "scadbuddy_render_store_errors_total", operation="read") == 1


async def test_a_failed_start_releases_the_store(paths: DataPaths) -> None:
    """Startup fails after the store opened (a first query, a prune): the store is
    closed again rather than left holding its pool for the life of the process."""
    closed: list[bool] = []

    class Failing(JobStore):
        def abandon_orphans(self) -> list[Job]:
            raise ConnectionError("the database went away mid-startup")

        def close(self) -> None:
            closed.append(True)

    queue = RenderQueue(CONFIG, paths, store=Failing(paths), render=Gate())
    with pytest.raises(ConnectionError):
        await queue.start()

    assert closed == [True]
    queue.close_thumbnails()


# ── the background lane (default-render previews) ─────────────────────────────


async def test_background_work_runs_behind_every_waiting_render(
    make_queue: QueueFactory,
) -> None:
    """A preview never starts ahead of a render someone asked for -- including one
    submitted after the preview was queued, and (Postgres) one any replica took."""
    gate = Gate()
    queue = await make_queue(gate)
    #: Which renders had started when the preview ran.
    ran_after: list[list[str]] = []

    async def background() -> bytes:
        ran_after.append(list(gate.started))
        return b"png"

    first = await _occupy_the_worker(queue, gate)
    preview = asyncio.create_task(queue.run_background(background))
    await asyncio.sleep(0.05)
    second = await queue.submit("demo", {"n": 1})  # submitted after, rendered before
    gate.release.set()

    assert await preview == b"png"
    await queue.join()
    assert ran_after == [[first.id, second.id]]


async def test_background_work_is_never_a_job(make_queue: QueueFactory) -> None:
    """Held in the process, never in the store: nothing to list, nothing a model's
    delete waits on, and a failure raises to its caller with the worker alive."""
    gate = Gate()
    gate.release.set()
    queue = await make_queue(gate)
    started = asyncio.Event()
    release = asyncio.Event()

    async def held() -> None:
        started.set()
        await release.wait()

    running = asyncio.create_task(queue.run_background(held))
    await asyncio.wait_for(started.wait(), 5)
    assert queue.store.list_jobs() == []
    assert not queue.store.has_unfinished("demo")
    assert queue.store.counts().pending == queue.store.counts().running == 0
    release.set()
    await running

    async def broken() -> None:
        raise RuntimeError("no geometry")

    with pytest.raises(RuntimeError, match="no geometry"):
        await queue.run_background(broken)
    job = await queue.submit("demo", {"n": 1})
    await queue.join()
    assert queue.store.read(job.id).state == "done"


async def test_background_work_is_not_admitted_or_counted(make_queue: QueueFactory) -> None:
    """Admission (`render_queue_max`) and the queue metrics see renders only: a
    waiting preview takes no place and moves no gauge or counter."""
    gate = Gate()
    queue = await make_queue(gate, replace(CONFIG, render_queue_max=1))
    await _occupy_the_worker(queue, gate)

    async def background() -> None:
        return None

    preview = asyncio.create_task(queue.run_background(background))
    await _until(lambda: len(queue._background) == 1)

    # The one place is the next render's, not the waiting preview's.
    waiting = await queue.submit("demo", {"n": 1})
    with pytest.raises(QueueFullError):
        await queue.submit("demo", {"n": 2})
    queue.refresh_metrics()
    assert _sample(queue.metrics, "scadbuddy_render_queue_depth") == 1
    assert _sample(queue.metrics, "scadbuddy_render_jobs_submitted_total") == 2

    gate.release.set()
    await preview
    await queue.join()
    assert queue.store.read(waiting.id).state == "done"
    assert _sample(queue.metrics, "scadbuddy_render_jobs_submitted_total") == 2
