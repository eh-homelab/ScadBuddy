"""The render activities over the real stages, and the real workflows over them."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
import trimesh
from temporalio.api.workflowservice.v1 import SetWorkerDeploymentCurrentVersionRequest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.pg_listener import PgListener
from scadbuddy.library.assets import AssetStore
from scadbuddy.render.diagnostics import Diagnostic
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.job_models import Job, JobResult, PartInfo, StepInfo
from scadbuddy.render.job_store import render_key
from scadbuddy.render.projection import JobProjection, workflow_id_for
from scadbuddy.store import BlobRefs
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.workflows.activities import RenderActivities, WorkerDeps, _heartbeating
from scadbuddy.workflows.client import DEPLOYMENT_NAME, render_worker
from scadbuddy.workflows.models import Failure, PieceRequest, Projection, piece_key
from scadbuddy.workflows.pipelines import TemplatePipeline
from tests.conftest import write_openscad_3mf
from tests.support.temporal import temporal_client

#: Writes a `.param`, copies the 3MF named in `fake-env.json` to every `.3mf` output,
#: and exits 1 on a source containing `%%FAIL%%`.
FAKE_OPENSCAD = """#!/usr/bin/env python3
import json
import pathlib
import shutil
import sys

args = sys.argv[1:]
settings = json.loads(pathlib.Path(sys.argv[0]).with_name("fake-env.json").read_text())
out = args[args.index("-o") + 1] if "-o" in args else None
source = pathlib.Path(args[-1])
if "%%FAIL%%" in source.read_text(encoding="utf-8"):
    print("ERROR: Parser error: syntax error", file=sys.stderr)
    raise SystemExit(1)
if out is not None and out.endswith(".param"):
    pathlib.Path(out).write_text(
        json.dumps({"parameters": [{"name": "width", "type": "number", "initial": 10}]})
    )
elif out is not None and out.endswith(".3mf"):
    shutil.copyfile(settings["FAKE_3MF"], out)
"""


def _paths(tmp_path: Path, source: str = "cube();\n") -> DataPaths:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text(source, encoding="utf-8")
    return paths


def _config(tmp_path: Path, paths: DataPaths) -> Config:
    binary = tmp_path / "bin" / "fake-openscad"
    binary.parent.mkdir()
    binary.write_text(FAKE_OPENSCAD, encoding="utf-8")
    binary.chmod(0o755)
    model = write_openscad_3mf(
        tmp_path / "bin" / "drawn.3mf",
        [("Color 1", "#0047BB00", trimesh.creation.box(extents=(10, 10, 2)))],
    )
    (binary.parent / "fake-env.json").write_text(json.dumps({"FAKE_3MF": str(model)}))
    return Config(openscad=str(binary), data_dir=paths.root)


def _deps(
    tmp_path: Path,
    paths: DataPaths,
    *,
    projection: JobProjection | None = None,
    refs: BlobRefs | None = None,
) -> WorkerDeps:
    return WorkerDeps(
        config=_config(tmp_path, paths),
        paths=paths,
        assets=AssetStore(paths.assets),
        blobs=LocalBlobStore(paths.blobs),
        refs=refs,  # type: ignore[arg-type]
        projection=projection,  # type: ignore[arg-type]
    )


def _request(params: dict[str, int] | None = None) -> PieceRequest:
    params = params or {"width": 12}
    return PieceRequest(
        slug="demo",
        revision=None,
        params=dict(params),
        piece_key=piece_key("demo", None, "model.scad", params),
    )


# ── the stage activities ───────────────────────────────────────────────────────


async def test_the_four_stages_render_into_the_piece_blob(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    deps = _deps(tmp_path, paths)
    acts = RenderActivities(deps)
    env = ActivityEnvironment()
    req = _request()

    prepared = await env.run(acts.prepare, req)
    main = await env.run(acts.render_main, req, prepared)
    await env.run(acts.render_solids, req, prepared, main)
    piece = await env.run(acts.finish_piece, req, prepared, main)

    assert deps.blobs.exists(req.piece_key)
    blob = deps.blobs.dir_for(req.piece_key)
    assert (paths.root / piece.result.model_3mf).is_file()
    assert (paths.root / piece.result.model_3mf).parent == blob
    assert piece.result.source_version == prepared.version
    assert piece.log_tail == main.log_tail
    assert main.returncode == 0


async def test_an_openscad_failure_is_a_non_retryable_application_error(tmp_path: Path) -> None:
    paths = _paths(tmp_path, "%%FAIL%%\n")
    acts = RenderActivities(_deps(tmp_path, paths))
    env = ActivityEnvironment()
    req = _request()
    prepared = await env.run(acts.prepare, req)

    with pytest.raises(ApplicationError) as raised:
        await env.run(acts.render_main, req, prepared)

    assert raised.value.type == "OpenSCADError"
    assert raised.value.non_retryable
    failure = raised.value.details[0]
    assert isinstance(failure, Failure)
    assert failure.error == raised.value.message
    assert failure.log_tail == ["ERROR: Parser error: syntax error"]


async def test_cancelling_a_heartbeating_activity_cancels_its_work() -> None:
    started = asyncio.Event()

    async def forever() -> None:
        started.set()
        await asyncio.Event().wait()

    inner: asyncio.Task[None] | None = None

    async def body() -> None:
        nonlocal inner
        inner = asyncio.create_task(forever())
        await _heartbeating(inner, every=0.01)

    outer = asyncio.create_task(ActivityEnvironment().run(body))
    await started.wait()
    await asyncio.sleep(0.05)  # a few heartbeats
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    assert inner is not None and inner.cancelled()


# ── project ────────────────────────────────────────────────────────────────────


@pytest.fixture
def projection(pg_conninfo: str) -> Iterator[JobProjection]:
    bus = PgNotifyEventBus(pg_conninfo, listener=PgListener(pg_conninfo))
    store = JobProjection(pg_conninfo, pool_size=2, events=bus)
    store.open()
    try:
        yield store
    finally:
        store.close()


def _job(**params: int) -> Job:
    return Job(
        id=uuid.uuid4().hex,
        slug="demo",
        params=dict(params),
        inputs={"params": dict(params)},
        created_at=datetime.now(UTC),
    )


def _result() -> JobResult:
    return JobResult(
        model_3mf="blobs/k/model.3mf",
        preview_glb="blobs/k/preview.glb",
        parts=[PartInfo(name="Color 1", colour="#FF0000", extruder=1, watertight=True)],
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
        warnings=["a warning"],
        diagnostics=[Diagnostic(severity="warning", message="unknown variable")],
        diagnostics_dropped=2,
    )


@pytest.fixture
def projecting(
    tmp_path: Path, projection: JobProjection
) -> tuple[RenderActivities, JobProjection, BlobRefs]:
    refs = BlobRefs(projection.pool)
    paths = _paths(tmp_path)
    return (
        RenderActivities(_deps(tmp_path, paths, projection=projection, refs=refs)),
        projection,
        refs,
    )


def _submitted(projection: JobProjection) -> Job:
    job = _job(width=1)
    projection.submit(job, render_key("demo", {"width": 1}, None))
    return job


@pytest.mark.requires_postgres
async def test_project_running_then_steps(
    projecting: tuple[RenderActivities, JobProjection, BlobRefs],
) -> None:
    acts, projection, _ = projecting
    job = _submitted(projection)

    await acts.project(Projection(job_id=job.id, slug="demo", state="running"))
    assert projection.read(job.id).state == "running"

    steps = [StepInfo(name="render", state="running", done=0, total=1)]
    await acts.project(Projection(job_id=job.id, slug="demo", steps=steps))
    stored = projection.read(job.id)
    assert stored.state == "running"
    assert stored.steps == steps


@pytest.mark.requires_postgres
async def test_project_done_copies_the_result_and_refs_the_blob(
    projecting: tuple[RenderActivities, JobProjection, BlobRefs],
) -> None:
    acts, projection, refs = projecting
    job = _submitted(projection)
    await acts.project(Projection(job_id=job.id, slug="demo", state="running"))

    done = Projection(
        job_id=job.id,
        slug="demo",
        state="done",
        result=_result(),
        log_tail=["fine"],
        steps=[StepInfo(name="render", state="done", done=1, total=1)],
        blob_key="piece-key",
    )
    await acts.project(done)

    stored = projection.read(job.id)
    assert stored.state == "done"
    assert stored.result == _result()
    assert stored.log_tail == ["fine"]
    assert stored.warnings == ["a warning"]
    assert stored.diagnostics == _result().diagnostics
    assert stored.diagnostics_dropped == 2
    assert stored.steps == done.steps
    assert "piece-key" in refs.referenced()

    # A second `done` (a retried activity) returns and changes nothing.
    await acts.project(done.model_copy(update={"log_tail": ["other"]}))
    assert projection.read(job.id) == stored


@pytest.mark.requires_postgres
async def test_project_failed_copies_the_failure(
    projecting: tuple[RenderActivities, JobProjection, BlobRefs],
) -> None:
    acts, projection, refs = projecting
    job = _submitted(projection)
    await acts.project(Projection(job_id=job.id, slug="demo", state="running"))

    failure = Failure(
        error="openscad exited with 1",
        log_tail=["ERROR: boom"],
        diagnostics=[Diagnostic(severity="error", message="Parser error")],
        diagnostics_dropped=1,
        warnings=["missing pic.svg"],
    )
    await acts.project(Projection(job_id=job.id, slug="demo", state="failed", failure=failure))

    stored = projection.read(job.id)
    assert stored.state == "failed"
    assert stored.error == "openscad exited with 1"
    assert stored.log_tail == ["ERROR: boom"]
    assert stored.diagnostics == failure.diagnostics
    assert stored.diagnostics_dropped == 1
    assert stored.warnings == ["missing pic.svg"]
    assert stored.result is None
    assert refs.referenced() == set()


@pytest.mark.requires_postgres
async def test_project_for_an_unknown_job_returns(
    projecting: tuple[RenderActivities, JobProjection, BlobRefs],
) -> None:
    acts, _, _ = projecting
    missing = uuid.uuid4().hex
    await acts.project(Projection(job_id=missing, slug="demo", state="running"))
    await acts.project(Projection(job_id=missing, slug="demo", state="done", result=_result()))
    await acts.project(
        Projection(job_id=missing, slug="demo", state="failed", failure=Failure(error="x"))
    )


# ── the real workflows over the real activities ────────────────────────────────


@pytest.mark.requires_postgres
@pytest.mark.requires_temporal
async def test_a_job_renders_end_to_end_on_the_render_worker(
    tmp_path: Path, pg_conninfo: str, projection: JobProjection
) -> None:
    paths = _paths(tmp_path)
    refs = BlobRefs(projection.pool)
    deps = _deps(tmp_path, paths, projection=projection, refs=refs)
    job = _submitted(projection)
    key = piece_key("demo", None, "model.scad", {"width": 1})

    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        async with render_worker(
            client,
            queue,
            RenderActivities(deps),
            build_id="test",
            max_concurrent_activities=2,
        ):
            # A versioned worker takes new workflows only once its version is current.
            await client.workflow_service.set_worker_deployment_current_version(
                SetWorkerDeploymentCurrentVersionRequest(
                    namespace=client.namespace,
                    deployment_name=DEPLOYMENT_NAME,
                    build_id="test",
                    ignore_missing_task_queues=True,
                    allow_no_pollers=True,
                )
            )
            await asyncio.wait_for(
                client.execute_workflow(
                    TemplatePipeline.run, job, id=workflow_id_for(job.id), task_queue=queue
                ),
                timeout=120,
            )

    stored = projection.read(job.id)
    assert stored.state == "done", stored.error
    assert stored.result is not None
    assert (paths.root / stored.result.model_3mf).is_file()
    assert deps.blobs.exists(key)
    assert key in refs.referenced()
    with psycopg.connect(pg_conninfo) as conn:
        kinds = [row[0] for row in conn.execute("SELECT kind FROM events ORDER BY seq")]
    assert kinds == ["job.pending", "job.running", "job.done"]
