from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
import trimesh

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.jobs import (
    UNCOLOURED_WARNING,
    Job,
    JobResult,
    JobStore,
    PartInfo,
    RenderQueue,
    solid_parts,
)
from scadbuddy.render.runner import OpenSCADError
from scadbuddy.render.schema import CustomizerSchema
from scadbuddy.render.split import ColourPart

CONFIG = Config(data_dir=Path("/unused"), render_concurrency=2, job_ttl=3600.0)


def _result() -> JobResult:
    return JobResult(
        model_3mf="jobs/x.work/model.3mf",
        preview_glb="jobs/x.work/preview.glb",
        source_version="sha256:test",
        parts=[PartInfo(name="Color 1", colour="#FF6AC1", extruder=1, watertight=True)],
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
    )


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    data = DataPaths(tmp_path)
    data.ensure()
    return data


def _job(job_id: str, **kwargs: object) -> Job:
    return Job(id=job_id, slug="demo", created_at=datetime.now(UTC), **kwargs)  # type: ignore[arg-type]


def test_store_round_trip(paths: DataPaths) -> None:
    store = JobStore(paths)
    job = _job("a")
    store.write(job)
    assert store.read("a") == job
    assert [j.id for j in store.list_jobs()] == ["a"]


def test_fail_unfinished_marks_pending_and_running_jobs(paths: DataPaths) -> None:
    store = JobStore(paths)
    store.write(_job("pending"))
    store.write(_job("running", state="running"))
    store.write(_job("done", state="done"))

    assert sorted(j.id for j in store.fail_unfinished()) == ["pending", "running"]
    assert store.read("pending").state == "failed"
    assert store.read("pending").error == "interrupted by a restart"
    assert store.read("done").state == "done"


def test_prune_removes_expired_jobs_and_their_work_dirs(paths: DataPaths) -> None:
    store = JobStore(paths)
    old = _job("old", state="done", finished_at=datetime.now(UTC) - timedelta(hours=3))
    store.write(old)
    fresh = _job("fresh", state="done", finished_at=datetime.now(UTC))
    store.write(fresh)
    work = paths.job_work_dir("old")
    work.mkdir()
    (work / "model.3mf").write_bytes(b"x")

    assert store.prune(3600.0) == ["old"]
    assert not work.exists()
    assert [j.id for j in store.list_jobs()] == ["fresh"]


@pytest_asyncio.fixture
async def queue(paths: DataPaths) -> AsyncIterator[RenderQueue]:
    queue = RenderQueue(CONFIG, paths, render=lambda job: _fake_render(job))
    await queue.start()
    yield queue
    await queue.aclose()


async def _fake_render(job: Job) -> tuple[JobResult, list[str]]:
    if job.params.get("mode") == "boom":
        raise OpenSCADError("openscad exited with 1", ["ERROR: something"], 1)
    if job.params.get("mode") == "bug":
        raise KeyError("unexpected")
    return _result(), ["Total rendering time: 0:00:00.065"]


async def test_a_successful_job_records_its_result(queue: RenderQueue) -> None:
    job = await queue.submit("demo", {"name": "Reagan"})
    assert queue.store.read(job.id).state == "pending"
    await queue.join()

    done = queue.store.read(job.id)
    assert done.state == "done"
    assert done.result == _result()
    assert done.log_tail == ["Total rendering time: 0:00:00.065"]
    assert done.started_at is not None and done.finished_at is not None


async def test_an_openscad_failure_keeps_the_log_tail(queue: RenderQueue) -> None:
    job = await queue.submit("demo", {"mode": "boom"})
    await queue.join()

    failed = queue.store.read(job.id)
    assert failed.state == "failed"
    assert failed.error == "openscad exited with 1"
    assert failed.log_tail == ["ERROR: something"]
    assert failed.result is None


async def test_an_unexpected_error_fails_the_job_and_the_worker_lives_on(
    queue: RenderQueue,
) -> None:
    broken = await queue.submit("demo", {"mode": "bug"})
    good = await queue.submit("demo", {})
    await queue.join()

    assert queue.store.read(broken.id).state == "failed"
    assert queue.store.read(broken.id).error == "KeyError: 'unexpected'"
    assert queue.store.read(good.id).state == "done"


async def test_concurrency_is_capped_by_the_config(paths: DataPaths) -> None:
    in_flight = 0
    peak = 0
    release = asyncio.Event()

    async def slow(job: Job) -> tuple[JobResult, list[str]]:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await release.wait()
        in_flight -= 1
        return _result(), []

    queue = RenderQueue(replace(CONFIG, render_concurrency=2), paths, render=slow)
    await queue.start()
    try:
        for _ in range(4):
            await queue.submit("demo", {})
        await asyncio.sleep(0.05)
        assert peak == 2
        release.set()
        await queue.join()
    finally:
        await queue.aclose()
    assert peak == 2


async def test_start_fails_jobs_left_behind_by_a_restart(paths: DataPaths) -> None:
    JobStore(paths).write(_job("stale", state="running"))
    queue = RenderQueue(CONFIG, paths, render=_fake_render)
    await queue.start()
    try:
        assert queue.store.read("stale").state == "failed"
    finally:
        await queue.aclose()


async def test_uncoloured_geometry_falls_back_to_the_split_parts() -> None:
    preview = [
        ColourPart(0, "Default", "#F9D72C", trimesh.creation.box()),
        ColourPart(1, "Color 1", "#FF6AC1", trimesh.creation.box()),
    ]
    parts, warnings = await solid_parts(
        Path("/nonexistent/model.scad"),
        CustomizerSchema(),
        {},
        preview,
        Path("/nonexistent"),
        config=Config(openscad="/nonexistent/openscad"),
    )
    assert parts == preview
    assert warnings == [UNCOLOURED_WARNING]
