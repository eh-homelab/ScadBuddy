"""The render activities over the real stages, and the real workflows over them."""

from __future__ import annotations

import asyncio
import shutil
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
import trimesh
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.pg_listener import PgListener
from scadbuddy.library.assets import AssetStore
from scadbuddy.library.libraries import CheckoutGate, LibraryNotInstalledError
from scadbuddy.render import jobs
from scadbuddy.render.diagnostics import Diagnostic
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.job_models import Job, JobResult, PartInfo, StepInfo, render_key
from scadbuddy.render.jobs import RAW_RENDER_NAME
from scadbuddy.render.projection import CANCELLED_ERROR, JobProjection, workflow_id_for
from scadbuddy.render.runner import ProcessOutput
from scadbuddy.store import BlobRefs
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.workflows import activities
from scadbuddy.workflows.activities import (
    PIECE_NAME,
    RenderActivities,
    WorkerDeps,
    _heartbeating,
    _main_result,
    _process_output,
    _write_piece,
)
from scadbuddy.workflows.client import make_current, render_worker
from scadbuddy.workflows.models import (
    Failure,
    PieceRequest,
    PieceResult,
    PrepareResult,
    Projection,
    RenderMainResult,
    piece_key,
)
from scadbuddy.workflows.pipelines import TemplatePipeline
from tests.conftest import fake_3mf_openscad, write_openscad_3mf
from tests.support.temporal import temporal_client

REVISION = "c0ffee0"


class _History:
    """A repository whose every template was last committed at `REVISION`."""

    available = True

    def last_commit(self, path: str) -> str:
        return REVISION


def _paths(tmp_path: Path, source: str = "cube();\n") -> DataPaths:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text(source, encoding="utf-8")
    return paths


def _config(tmp_path: Path, paths: DataPaths) -> Config:
    return Config(openscad=fake_3mf_openscad(tmp_path / "bin"), data_dir=paths.root)


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
        history=_History(),  # type: ignore[arg-type]
    )


def _request(revision: str | None = REVISION) -> PieceRequest:
    params = {"width": 12}
    return PieceRequest(
        slug="demo",
        revision=revision,
        params=dict(params),
        piece_key=piece_key("demo", revision, "model.scad", params),
    )


# ── the library lease, per activity ────────────────────────────────────────────


def _checkout(paths: DataPaths) -> Path:
    """A pinned library's checkout as `require_checkouts` finds it:
    ``libraries/<name>/<sha>/<name>/``."""
    checkout = paths.libraries / "bosl" / ("ab12cd3" + "0" * 33)
    (checkout / "bosl").mkdir(parents=True)
    (checkout / "bosl" / "std.scad").write_text("module bosl() {}\n", encoding="utf-8")
    return checkout


async def _prepared_with_library(
    acts: RenderActivities, env: ActivityEnvironment, req: PieceRequest, checkout: Path
) -> PrepareResult:
    prepared = await env.run(acts.prepare, req)
    # The template pins the library: `prepare_source` resolved its checkout.
    return prepared.model_copy(update={"library_path": [str(checkout)]})


async def test_each_stage_activity_holds_the_library_lease_for_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Held while the activity reads the checkouts, released when it returns: a lease
    cannot span activities, each one is its own unit of work."""
    paths = _paths(tmp_path)
    gate = CheckoutGate()
    deps = replace(_deps(tmp_path, paths), checkouts=gate)
    acts = RenderActivities(deps)
    env = ActivityEnvironment()
    req = _request()
    checkout = _checkout(paths)
    holder = f"piece:{req.piece_key}"
    seen: dict[str, list[str]] = {}

    real_main = jobs._render_main

    async def observed_main(*args: Any, **kwargs: Any) -> Any:
        seen["render_main"] = gate.leased(checkout)
        return await real_main(*args, **kwargs)

    real_finish = jobs.finish_piece_stage  # what `activities` imported

    async def observed_finish(*args: Any, **kwargs: Any) -> Any:
        seen["finish_piece"] = gate.leased(checkout)
        return await real_finish(*args, **kwargs)

    monkeypatch.setattr(jobs, "_render_main", observed_main)
    monkeypatch.setattr(activities, "finish_piece_stage", observed_finish)

    prepared = await _prepared_with_library(acts, env, req, checkout)
    main = await env.run(acts.render_main, req, prepared)
    assert seen["render_main"] == [holder]
    assert gate.leased(checkout) == []
    await env.run(acts.render_solids, req, prepared, main)
    assert gate.leased(checkout) == []
    await env.run(acts.finish_piece, req, prepared, main)
    assert seen["finish_piece"] == [holder]
    assert gate.leased(checkout) == []


@pytest.mark.parametrize("stage", ["render_main", "render_solids", "finish_piece"])
async def test_a_checkout_removed_between_activities_fails_the_next_one(
    tmp_path: Path, stage: str
) -> None:
    """The gap between two activities is open to a removal. The next activity's lease
    re-checks its checkouts, so it fails fast rather than reading what is gone; the
    fetcher restores the pin and the retry renders."""
    paths = _paths(tmp_path)
    gate = CheckoutGate()
    deps = replace(_deps(tmp_path, paths), checkouts=gate)
    acts = RenderActivities(deps)
    env = ActivityEnvironment()
    req = _request()
    checkout = _checkout(paths)
    prepared = await _prepared_with_library(acts, env, req, checkout)
    main = RenderMainResult()
    if stage != "render_main":
        main = await env.run(acts.render_main, req, prepared)
    if stage == "finish_piece":
        await env.run(acts.render_solids, req, prepared, main)

    async with gate.removing():
        shutil.rmtree(checkout)

    with pytest.raises(LibraryNotInstalledError, match="bosl"):
        if stage == "render_main":
            await env.run(acts.render_main, req, prepared)
        elif stage == "render_solids":
            await env.run(acts.render_solids, req, prepared, main)
        else:
            await env.run(acts.finish_piece, req, prepared, main)
    assert gate.leased(checkout) == []
    assert not (deps.blobs.dir_for(req.piece_key) / PIECE_NAME).exists()


# ── the stage activities ───────────────────────────────────────────────────────


def test_a_render_that_echoed_no_plates_carries_none_between_activities() -> None:
    """`RenderMainResult` mirrors `ProcessOutput`: no `echo(plates = N)` is None in
    both, never a count the template did not state."""
    output = ProcessOutput(returncode=0, log_tail=[], duration_s=0.0)
    assert output.plates is None
    main = _main_result(output)
    assert main.plates is None
    assert _process_output(RenderMainResult.model_validate_json(main.model_dump_json())) == output
    assert RenderMainResult().plates == output.plates


async def test_the_four_stages_render_into_the_piece_blob(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    deps = _deps(tmp_path, paths)
    acts = RenderActivities(deps)
    env = ActivityEnvironment()
    req = _request()

    assert await env.run(acts.cached_piece, req) is None
    prepared = await env.run(acts.prepare, req)
    main = await env.run(acts.render_main, req, prepared)
    await env.run(acts.render_solids, req, prepared, main)
    piece = await env.run(acts.finish_piece, req, prepared, main)

    blob = deps.blobs.dir_for(req.piece_key)
    assert (blob / PIECE_NAME).is_file()
    assert await env.run(acts.cached_piece, req) == piece
    assert (paths.root / piece.result.model_3mf).is_file()
    assert (paths.root / piece.result.model_3mf).parent == blob
    assert piece.result.source_version == prepared.version == REVISION
    assert piece.log_tail == main.log_tail
    assert main.returncode == 0


async def test_a_piece_without_a_revision_is_never_answered_from_its_blob(
    tmp_path: Path,
) -> None:
    """Its key names no revision, so the live source can change under it."""
    paths = _paths(tmp_path)
    deps = _deps(tmp_path, paths)
    req = _request(revision=None)
    _write_piece(deps.blobs.dir_for(req.piece_key), PieceResult(result=_result()))

    assert await ActivityEnvironment().run(RenderActivities(deps).cached_piece, req) is None


async def test_an_unreadable_piece_is_a_miss(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    deps = _deps(tmp_path, paths)
    req = _request()
    (deps.blobs.dir_for(req.piece_key) / PIECE_NAME).write_text('{"result": 1}')

    assert await ActivityEnvironment().run(RenderActivities(deps).cached_piece, req) is None


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


async def test_a_failed_preview_keeps_its_openscad_diagnostics(tmp_path: Path) -> None:
    paths = _paths(tmp_path, "%%FAIL%%\n")
    acts = RenderActivities(_deps(tmp_path, paths))

    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(acts.render_preview_png, _request().slug)

    assert raised.value.type == "OpenSCADError"
    assert raised.value.non_retryable
    failure = raised.value.details[0]
    assert isinstance(failure, Failure)
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


def _kinds(conninfo: str, job_id: str) -> list[str]:
    with psycopg.connect(conninfo) as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT kind FROM events WHERE payload->>'job_id' = %s ORDER BY seq", (job_id,)
            )
        ]


def _cancelled(job: Job) -> Projection:
    return Projection(
        job_id=job.id,
        slug="demo",
        state="cancelled",
        failure=Failure(error="cancelled"),
        log_tail=["partial"],
        steps=[StepInfo(name="render", state="running", done=0, total=1)],
    )


@pytest.mark.requires_postgres
async def test_project_cancelled_onto_a_running_job(
    pg_conninfo: str, projecting: tuple[RenderActivities, JobProjection, BlobRefs]
) -> None:
    acts, projection, _ = projecting
    job = _submitted(projection)
    await acts.project(Projection(job_id=job.id, slug="demo", state="running"))

    await acts.project(_cancelled(job))

    stored = projection.read(job.id)
    assert stored.state == "cancelled"
    assert stored.error == "cancelled"
    assert stored.steps == _cancelled(job).steps
    assert _kinds(pg_conninfo, job.id) == ["job.pending", "job.running", "job.superseded"]


@pytest.mark.requires_postgres
async def test_project_cancelled_onto_a_job_the_api_cancelled_keeps_its_error(
    pg_conninfo: str, projecting: tuple[RenderActivities, JobProjection, BlobRefs]
) -> None:
    acts, projection, _ = projecting
    job = _submitted(projection)
    assert projection.release_claim(job.id, slug="demo") is not None

    await acts.project(_cancelled(job))

    stored = projection.read(job.id)
    assert stored.state == "cancelled"
    assert stored.error == CANCELLED_ERROR
    assert stored.steps == _cancelled(job).steps
    assert _kinds(pg_conninfo, job.id) == ["job.pending", "job.superseded"]


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
    job, again = (_job(width=1).model_copy(update={"model_version": REVISION}) for _ in "ab")
    projection.submit(job, render_key("demo", {"width": 1}, REVISION))
    key = piece_key("demo", REVISION, "model.scad", {"width": 1})
    raw = deps.blobs.dir_for(key) / RAW_RENDER_NAME

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
            await make_current(client, namespace=client.namespace, build_id="test")
            await asyncio.wait_for(
                client.execute_workflow(
                    TemplatePipeline.run, job, id=workflow_id_for(job.id), task_queue=queue
                ),
                timeout=120,
            )
            rendered = raw.stat().st_mtime_ns
            # The same piece again, after the first one closed: answered from the blob.
            projection.submit(again, render_key("demo", {"width": 1}, REVISION))
            await asyncio.wait_for(
                client.execute_workflow(
                    TemplatePipeline.run, again, id=workflow_id_for(again.id), task_queue=queue
                ),
                timeout=120,
            )

    stored = projection.read(job.id)
    assert stored.state == "done", stored.error
    assert stored.result is not None
    assert (paths.root / stored.result.model_3mf).is_file()
    assert key in refs.referenced()
    assert _kinds(pg_conninfo, job.id) == ["job.pending", "job.running", "job.done"]
    repeat = projection.read(again.id)
    assert repeat.state == "done", repeat.error
    assert repeat.result == stored.result
    assert raw.stat().st_mtime_ns == rendered
    refs.drop_holder("job", job.id)
    assert key in refs.referenced()  # the repeat's own ref


@pytest.mark.requires_postgres
@pytest.mark.requires_temporal
async def test_a_revision_less_job_never_renders_over_another_jobs_files(
    tmp_path: Path, projection: JobProjection
) -> None:
    """#642: without a revision the key named only the slug and params, so a second job
    re-rendered a live source into the first job's blob directory, under its row."""
    paths = _paths(tmp_path)
    refs = BlobRefs(projection.pool)
    deps = _deps(tmp_path, paths, projection=projection, refs=refs)

    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        async with render_worker(
            client,
            queue,
            RenderActivities(deps),
            build_id="test",
            max_concurrent_activities=2,
        ):
            await make_current(client, namespace=client.namespace, build_id="test")

            async def rendered(job: Job) -> Path:
                projection.submit(job, render_key("demo", {"width": 1}, None))
                await asyncio.wait_for(
                    client.execute_workflow(
                        TemplatePipeline.run, job, id=workflow_id_for(job.id), task_queue=queue
                    ),
                    timeout=120,
                )
                done = projection.read(job.id)
                assert done.state == "done", done.error
                assert done.result is not None
                return paths.root / done.result.model_3mf

            first = await rendered(_job(width=1))
            before = first.read_bytes()
            # The author edits the template, which now draws a taller box.
            paths.model_source("demo").write_text("cube(20);\n", encoding="utf-8")
            write_openscad_3mf(
                tmp_path / "bin" / "drawn.3mf",
                [("Color 1", "#0047BB00", trimesh.creation.box(extents=(10, 10, 20)))],
            )
            second = await rendered(_job(width=1))

    assert first.read_bytes() == before
    assert second.parent != first.parent
    assert second.read_bytes() != before
