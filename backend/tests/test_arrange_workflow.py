"""Arrange runs as a workflow on its own render_jobs row (spec 2026-09-27 §3.4, §7)."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from temporalio.worker import Worker

from scadbuddy.core.config import Config
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.paths import DataPaths
from scadbuddy.render.job_models import Job, now
from scadbuddy.render.projection import JobProjection
from scadbuddy.render.submit import RenderService
from scadbuddy.workflows.models import ArrangeInputs, PackItem, PlateSize
from scadbuddy.workflows.pipelines import Arrange
from tests.support.pipelines import FakeWorld
from tests.support.temporal import temporal_client
from tests.test_arrange_packing import part
from tests.test_submit import projection  # noqa: F401  (the fixture)

PLATE = PlateSize(key="default", width=256.0, depth=256.0)


def _inputs(goal: str = "fewest_plates") -> ArrangeInputs:
    return ArrangeInputs(
        items=[
            PackItem(part=part("a", 40, 40), count=3),
            PackItem(part=part("b", 30, 30, "#FFFFFF")),
        ],
        goal=goal,
        plate=PLATE,
        colours=["#FF0000", "#FFFFFF"],
        name="together",
        sources=["o1"],
    )


def _job(inputs: ArrangeInputs) -> Job:
    return Job(
        id=uuid.uuid4().hex,
        slug="demo",
        kind="arrange",
        inputs=inputs.model_dump(mode="json"),
        created_at=now(),
    )


async def _run(world: FakeWorld, job: Job) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        async with Worker(
            client, task_queue=queue, workflows=[Arrange], activities=world.activities()
        ):
            await client.execute_workflow(Arrange.run, job, id=f"render-{job.id}", task_queue=queue)


@pytest.mark.requires_temporal
async def test_an_arrange_job_packs_and_writes_one_output_with_no_render() -> None:
    world = FakeWorld(None)
    job = _job(_inputs())
    await _run(world, job)
    assert world.pieces == []  # nothing rendered
    [req] = world.outputs
    assert req.colours == ["#FF0000", "#FFFFFF"] and req.name == "together"
    assert sum(len(p.items) for p in req.layout.plates) == 4
    assert req.record.pipeline_version == "arrange" and req.record.plate_key == "default"
    done = world.projections[-1]
    assert done.state == "done" and len(done.outputs) == 1
    assert {"a", "b"} <= set(done.blob_keys)


@pytest.mark.requires_temporal
async def test_an_arrange_that_cannot_pack_fails_with_the_reason() -> None:
    world = FakeWorld(None)
    inputs = _inputs()
    inputs.items.append(PackItem(part=part("huge", 400, 10)))
    await _run(world, _job(inputs))
    failed = world.projections[-1]
    assert failed.state == "failed" and failed.failure is not None
    assert "does not fit the plate" in failed.failure.error
    assert world.outputs == []


@pytest.mark.requires_postgres
async def test_the_reconciler_starts_an_arrange_row_as_arrange(
    projection: JobProjection,  # noqa: F811
    tmp_path: Path,
) -> None:
    client = MagicMock()
    client.start_workflow = AsyncMock(side_effect=RuntimeError("temporal is down"))
    svc = RenderService(
        projection=projection,
        client=client,
        task_queue="q",
        config=Config(data_dir=tmp_path),
        paths=DataPaths(tmp_path),
        metrics=Metrics(),
        reconcile_after=0.0,
    )
    job = await svc.arrange("demo", _inputs())
    assert client.start_workflow.await_args.args[0] == Arrange.run
    client.start_workflow.side_effect = None
    client.start_workflow.reset_mock()
    assert await svc.reconcile_once() == 1
    assert client.start_workflow.await_args.args[0] == Arrange.run
    assert client.start_workflow.await_args.kwargs["id"] == f"render-{job.id}"


@pytest.mark.requires_postgres
async def test_an_identical_arrange_coalesces(
    projection: JobProjection,  # noqa: F811
    tmp_path: Path,
) -> None:
    client = MagicMock()
    client.start_workflow = AsyncMock()
    svc = RenderService(
        projection=projection,
        client=client,
        task_queue="q",
        config=Config(data_dir=tmp_path),
        paths=DataPaths(tmp_path),
        metrics=Metrics(),
    )
    first = await svc.arrange("demo", _inputs())
    second = await svc.arrange("demo", _inputs())
    third = await svc.arrange("demo", _inputs("by_colour"))
    assert first.id == second.id != third.id
    assert first.kind == "arrange"
    assert client.start_workflow.await_count == 2
