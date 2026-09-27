"""The render queue's latency controls: supersede, coalesce, admission, deadline --
and the metrics that show whether they hold."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio

from scadbuddy.core.config import Config
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.paths import DataPaths
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.jobs import (
    SUPERSEDED_ERROR,
    Job,
    JobResult,
    PartInfo,
    QueueFullError,
    RenderQueue,
)

CONFIG = Config(
    data_dir=Path("/unused"),
    render_concurrency=1,
    render_queue_max=3,
    render_queue_timeout=0.0,
    job_ttl=3600.0,
)


def _result() -> JobResult:
    return JobResult(
        model_3mf="jobs/x.work/model.3mf",
        preview_glb="jobs/x.work/preview.glb",
        parts=[PartInfo(name="Color 1", colour="#FF6AC1", extruder=1, watertight=True)],
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
    )


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


QueueFactory = Callable[..., Awaitable[RenderQueue]]


@pytest_asyncio.fixture
async def make_queue(paths: DataPaths) -> AsyncIterator[QueueFactory]:
    queues: list[RenderQueue] = []

    async def make(render: Gate, config: Config = CONFIG) -> RenderQueue:
        queue = RenderQueue(config, paths, render=render)
        await queue.start()
        queues.append(queue)
        return queue

    yield make
    for queue in queues:
        await queue.aclose()


async def _occupy_the_worker(queue: RenderQueue, gate: Gate) -> Job:
    running = await queue.submit("demo", {"n": 0})
    for _ in range(100):
        if gate.started:
            break
        await asyncio.sleep(0.01)
    assert gate.started == [running.id]
    return running


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
    assert queue.depth == 1
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


async def test_a_full_queue_refuses_with_a_retry_hint(make_queue: QueueFactory) -> None:
    gate = Gate()
    queue = await make_queue(gate)
    await _occupy_the_worker(queue, gate)
    for n in range(1, 4):
        await queue.submit("demo", {"n": n})

    with pytest.raises(QueueFullError) as refused:
        await queue.submit("demo", {"n": 4})

    assert refused.value.depth == 3
    assert refused.value.retry_after >= 1
    assert _sample(queue.metrics, "scadbuddy_render_jobs_rejected_total") == 1
    assert _sample(queue.metrics, "scadbuddy_render_queue_depth") == 3
    # A coalesced request takes no room, so it is still answered.
    assert (await queue.submit("demo", {"n": 3})).slug == "demo"
    gate.release.set()
    await queue.join()


async def test_superseding_makes_room_before_admission(make_queue: QueueFactory) -> None:
    """A slider drag is never refused for the room its own stale previews take up."""
    gate = Gate()
    queue = await make_queue(gate)
    await _occupy_the_worker(queue, gate)
    waiting = [await queue.submit("demo", {"n": n}) for n in range(1, 4)]

    fresh = await queue.submit("demo", {"n": 9}, supersedes=waiting[-1].id)

    assert queue.depth == 3
    gate.release.set()
    await queue.join()
    assert queue.store.read(fresh.id).state == "done"


async def test_a_job_past_its_queue_deadline_is_failed_unrendered(
    make_queue: QueueFactory,
) -> None:
    gate = Gate()
    queue = await make_queue(gate, replace(CONFIG, render_queue_timeout=0.05))
    await _occupy_the_worker(queue, gate)

    late = await queue.submit("demo", {"n": 1})
    await asyncio.sleep(0.1)
    gate.release.set()
    await queue.join()

    expired = queue.store.read(late.id)
    assert expired.state == "failed"
    assert expired.error is not None
    assert "SCADBUDDY_RENDER_QUEUE_TIMEOUT" in expired.error
    assert late.id not in gate.started
    assert _sample(queue.metrics, "scadbuddy_render_jobs_finished_total", outcome="expired") == 1


async def test_the_queue_reports_its_latency(make_queue: QueueFactory) -> None:
    gate = Gate()
    gate.release.set()
    queue = await make_queue(gate)

    await queue.submit("demo", {"n": 1})
    await queue.join()

    metrics = queue.metrics
    assert _sample(metrics, "scadbuddy_render_jobs_submitted_total") == 1
    assert _sample(metrics, "scadbuddy_render_jobs_finished_total", outcome="done") == 1
    assert _sample(metrics, "scadbuddy_render_queue_wait_seconds_count") == 1
    assert _sample(metrics, "scadbuddy_render_duration_seconds_count", outcome="done") == 1
    assert _sample(metrics, "scadbuddy_render_job_latency_seconds_count", outcome="done") == 1
    assert _sample(metrics, "scadbuddy_render_jobs_running") == 0
    assert _sample(metrics, "scadbuddy_render_queue_depth") == 0
    assert _sample(metrics, "scadbuddy_render_workers") == 1
    assert _sample(metrics, "scadbuddy_render_queue_capacity") == 3


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
    ("field", "value"), [("render_queue_max", 0), ("render_queue_timeout", -1.0)]
)
def test_queue_limits_are_validated_by_name(field: str, value: float) -> None:
    with pytest.raises(ValueError, match="SCADBUDDY_RENDER_QUEUE_"):
        Config(**{field: value})  # type: ignore[arg-type]
