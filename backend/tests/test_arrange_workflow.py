"""Arrange runs as a `TemplatePipeline` of kind `arrange` on its own render_jobs row
(spec 2026-09-27 §3.4, §7)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from temporalio.worker import Worker

from scadbuddy.core.config import Config
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.paths import DataPaths
from scadbuddy.render import submit as submit_module
from scadbuddy.render.inputs import arrange_key
from scadbuddy.render.job_models import Job, now
from scadbuddy.render.projection import workflow_id_for_key
from scadbuddy.render.submit import RenderService
from scadbuddy.workflows.models import ArrangeInputs, PackItem, PlateSize, RenderAnswer, RenderStart
from scadbuddy.workflows.pipelines import TemplatePipeline
from tests.support.pipelines import FakeWorld
from tests.support.temporal import temporal_client
from tests.test_arrange_packing import part

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
            client, task_queue=queue, workflows=[TemplatePipeline], activities=world.activities()
        ):
            await client.execute_workflow(
                TemplatePipeline.run, job, id=f"render-{job.id}", task_queue=queue
            )


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


class _Starts:
    """`start_command`, recorded: each start answers a job, coalesced when its id was
    started before."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, RenderStart, str]] = []
        self.jobs: dict[str, Job] = {}

    async def __call__(
        self, _client: object, workflow: str, start: RenderStart, **kwargs: Any
    ) -> RenderAnswer:
        self.calls.append((workflow, start, kwargs["id"]))
        coalesced = kwargs["id"] in self.jobs
        if not coalesced:
            self.jobs[kwargs["id"]] = Job(
                id=uuid.uuid4().hex,
                slug=start.slug,
                kind=start.kind,
                inputs=start.inputs,
                created_at=now(),
            )
        return RenderAnswer(job=self.jobs[kwargs["id"]], coalesced=coalesced)


def _service(tmp_path: Path) -> RenderService:
    return RenderService(
        projection=MagicMock(),
        client=MagicMock(),
        task_queue="q",
        config=Config(data_dir=tmp_path),
        paths=DataPaths(tmp_path),
        metrics=Metrics(),
    )


async def test_an_arrange_starts_a_template_pipeline_of_kind_arrange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    starts = _Starts()
    monkeypatch.setattr(submit_module, "start_command", starts)
    svc = _service(tmp_path)
    job = await svc.arrange("demo", _inputs())
    [(workflow, start, workflow_id)] = starts.calls
    assert workflow == "TemplatePipeline" and start.kind == "arrange" and job.kind == "arrange"
    payload = _inputs().model_dump(mode="json")
    assert workflow_id == workflow_id_for_key(arrange_key("demo", payload))
    assert start.inputs == payload


async def test_an_identical_arrange_coalesces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(submit_module, "start_command", _Starts())
    svc = _service(tmp_path)
    first = await svc.arrange("demo", _inputs())
    second = await svc.arrange("demo", _inputs())
    third = await svc.arrange("demo", _inputs("by_colour"))
    assert first.id == second.id != third.id
    assert first.kind == "arrange"
    # Counted as arranges, not renders (final review M4).
    sample = svc.metrics.registry.get_sample_value
    assert sample("scadbuddy_render_jobs_submitted_total", {"kind": "arrange"}) == 2
    assert sample("scadbuddy_render_jobs_coalesced_total", {"kind": "arrange"}) == 1
    assert sample("scadbuddy_render_jobs_submitted_total", {"kind": "render"}) == 0
