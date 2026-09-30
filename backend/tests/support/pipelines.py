"""Fake activities, registered by name, for `TemplatePipeline` tests: a piece is "rendered"
by `cached_piece` from its params (width `w`, depth `d`), `pack` is the real one."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from scadbuddy.library.pipelines import (
    DEFAULT_PIPELINE_FILE,
    DEFAULT_PIPELINE_SOURCE,
    pipeline_version_of,
)
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.job_models import Job, JobResult, PipelineOutput
from scadbuddy.render.projection import workflow_id_for
from scadbuddy.workflows.models import (
    Failure,
    Layout,
    LoadedPipeline,
    LoadRequest,
    OutputRequest,
    PackRequest,
    PieceRequest,
    PieceResult,
    PlateSize,
    Projection,
)
from scadbuddy.workflows.pipeline_activities import pack_layout
from scadbuddy.workflows.pipelines import RenderPiece, TemplatePipeline


def a_job(**inputs: Any) -> Job:
    params = inputs.get("params", {})
    return Job(
        id=uuid.uuid4().hex,
        slug="demo",
        params=params,
        inputs={"v": 0, **inputs, "params": params},
        created_at=datetime.now(UTC),
    )


class FakeWorld:
    def __init__(self, source: str | None = None, *, fail: str | None = None) -> None:
        self.source = source
        self.fail = fail  # a file whose render fails
        self.pieces: list[PieceRequest] = []
        self.projections: list[Projection] = []
        self.outputs: list[OutputRequest] = []

    def activities(self) -> list[Callable[..., Any]]:
        return [self.load_pipeline, self.cached_piece, self.pack, self.write_output, self.project]

    @activity.defn(name="load_pipeline")
    async def load_pipeline(self, req: LoadRequest) -> LoadedPipeline:
        source = self.source or DEFAULT_PIPELINE_SOURCE
        return LoadedPipeline(
            source=source,
            file="pipeline/pipeline.py" if self.source else DEFAULT_PIPELINE_FILE,
            api=1,
            version=pipeline_version_of(source) if self.source else "default",
            inputs_version=1 if self.source else 0,
            plate=PlateSize(key="default", width=256, depth=256),
        )

    @activity.defn(name="cached_piece")
    async def cached_piece(self, req: PieceRequest) -> PieceResult | None:
        self.pieces.append(req)
        if req.file == self.fail:
            raise ApplicationError(
                "openscad exited with 1",
                Failure(error="openscad exited with 1", log_tail=["ERROR: boom"]),
                type="OpenSCADError",
                non_retryable=True,
            )
        w, d = float(req.params.get("w", 10)), float(req.params.get("d", 10))
        return PieceResult(
            result=JobResult(
                model_3mf=f"blobs/{req.piece_key}/model.3mf",
                preview_glb=f"blobs/{req.piece_key}/preview.glb",
                parts=[],
                colors=["#FF0000"],
                bbox_mm=BoundingBox(min=(0, 0, 0), max=(w, d, 5), size=(w, d, 5)),
            ),
            log_tail=[f"rendered {req.file}"],
        )

    @activity.defn(name="pack")
    async def pack(self, req: PackRequest) -> Layout:
        return pack_layout(req)

    @activity.defn(name="write_output")
    async def write_output(self, req: OutputRequest) -> PipelineOutput:
        self.outputs.append(req)
        return fake_output(req)

    @activity.defn(name="project")
    async def project(self, projection: Projection) -> None:
        self.projections.append(projection)

    def final(self) -> Projection:
        return self.projections[-1]


def fake_output(req: OutputRequest) -> PipelineOutput:
    """What `write_output` returns, without writing: an `own` layout is the piece itself."""
    first = req.layout.own or req.layout.plates[0].items[0].piece_key
    return PipelineOutput(
        name=req.name,
        bom=req.bom,
        files=sorted(req.files),
        record=req.record,
        blob_keys=[first] if req.layout.own else [f"output-{req.job_id}-{req.index}"],
        result=JobResult(
            model_3mf=f"blobs/{first}/model.3mf",
            preview_glb=f"blobs/{first}/preview.glb",
            parts=[],
            bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
        ),
    )


def activity_named(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    return activity.defn(name=name)(fn)


async def run_job(world: FakeWorld, job: Job, *, client: Client) -> None:
    queue = f"t-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[TemplatePipeline, RenderPiece],
        activities=world.activities(),
    ):
        await client.execute_workflow(
            TemplatePipeline.run, job, id=workflow_id_for(job.id), task_queue=queue
        )
