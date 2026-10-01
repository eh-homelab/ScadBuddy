"""The render activities over the real stages, and the real workflows over them."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import shutil
import threading
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
import trimesh
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.core.paths import DataPaths
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.pg_listener import PgListener
from scadbuddy.library.assets import AssetStore, AssetUnavailableError, file_assets
from scadbuddy.library.libraries import CheckoutGate, LibraryNotInstalledError
from scadbuddy.render import jobs
from scadbuddy.render.diagnostics import Diagnostic
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.job_models import (
    Job,
    JobResult,
    OutputRecord,
    PartInfo,
    PipelineOutput,
    StepInfo,
    render_key,
)
from scadbuddy.render.jobs import RAW_RENDER_NAME
from scadbuddy.render.projection import CANCELLED_ERROR, JobProjection, workflow_id_for
from scadbuddy.render.runner import ProcessOutput
from scadbuddy.render.schema import CustomizerSchema, Parameter
from scadbuddy.store import BlobRefs
from scadbuddy.store.assets import RemoteAssets
from scadbuddy.worker import make_current_until_polled
from scadbuddy.workflows import activities
from scadbuddy.workflows import activities as activities_module
from scadbuddy.workflows.activities import (
    PIECE_NAME,
    RenderActivities,
    _heartbeating,
    _main_result,
    _process_output,
    _scope,
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
from scadbuddy.workflows.pipeline_activities import PipelineActivities
from scadbuddy.workflows.pipelines import TemplatePipeline
from tests.conftest import PgPool, write_openscad_3mf
from tests.support.activities import REVISION, demo_paths, piece_request, worker_deps
from tests.support.store import local_content, store_pool
from tests.support.temporal import temporal_client


async def test_a_pieces_scope_reads_model_json_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`template_title` reads the template's `model.json`: a file read, so not on the loop."""
    loop_thread = threading.get_ident()
    readers: list[int] = []

    def title(model_dir: Path, slug: str) -> str:
        readers.append(threading.get_ident())
        return "Demo"

    monkeypatch.setattr(activities, "template_title", title)
    prepared = PrepareResult(version=REVISION, scad=str(tmp_path / "model.scad"), schema_cache="")
    scope = await _scope(piece_request(), prepared)
    assert (scope.slug, scope.title) == ("demo", "Demo")
    assert readers and loop_thread not in readers


async def test_a_piece_in_a_subdirectory_takes_its_templates_title(tmp_path: Path) -> None:
    """`parts/roof.scad`'s folder is named from the template's `model.json`, not from
    `parts/` (which has none, so the slug would name it)."""
    (tmp_path / "model.json").write_text('{"name": "Dollhouse"}')
    (tmp_path / "parts").mkdir()
    req = piece_request().model_copy(update={"file": "parts/roof.scad"})
    prepared = PrepareResult(
        version=REVISION, scad=str(tmp_path / "parts" / "roof.scad"), schema_cache=""
    )
    assert (await _scope(req, prepared)).title == "Dollhouse"


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
    paths = demo_paths(tmp_path)
    gate = CheckoutGate()
    deps = replace(worker_deps(tmp_path, paths), checkouts=gate)
    acts = RenderActivities(deps)
    env = ActivityEnvironment()
    req = piece_request()
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
    paths = demo_paths(tmp_path)
    gate = CheckoutGate()
    deps = replace(worker_deps(tmp_path, paths), checkouts=gate)
    acts = RenderActivities(deps)
    env = ActivityEnvironment()
    req = piece_request()
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
    paths = demo_paths(tmp_path)
    deps = worker_deps(tmp_path, paths)
    acts = RenderActivities(deps)
    env = ActivityEnvironment()
    req = piece_request()

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
    assert piece.result.libraries == []


async def test_a_piece_records_the_library_pins_its_template_declares(tmp_path: Path) -> None:
    """#169: the pins `prepare` resolved cross the activities and land on the result,
    so the output saved from it names the exact library commits it was built from."""
    paths = demo_paths(tmp_path)
    checkout = _checkout(paths)
    commit = checkout.name
    pin = {"name": "bosl", "url": "https://example.invalid/bosl.git", "ref": "v2", "commit": commit}
    paths.model_meta("demo").write_text(json.dumps({"libraries": [pin]}), encoding="utf-8")
    acts = RenderActivities(worker_deps(tmp_path, paths))
    env = ActivityEnvironment()
    req = piece_request()

    prepared = await env.run(acts.prepare, req)
    assert prepared.library_path == [str(checkout)]
    main = await env.run(acts.render_main, req, prepared)
    await env.run(acts.render_solids, req, prepared, main)
    piece = await env.run(acts.finish_piece, req, prepared, main)

    assert [(p.name, p.ref, p.commit) for p in piece.result.libraries] == [("bosl", "v2", commit)]
    assert await env.run(acts.cached_piece, req) == piece


async def test_a_piece_without_a_revision_is_never_answered_from_its_blob(
    tmp_path: Path,
) -> None:
    """Its key names no revision, so the live source can change under it."""
    paths = demo_paths(tmp_path)
    deps = worker_deps(tmp_path, paths)
    req = piece_request(revision=None)
    _write_piece(deps.blobs.dir_for(req.piece_key), PieceResult(result=_result()))

    assert await ActivityEnvironment().run(RenderActivities(deps).cached_piece, req) is None


async def test_an_unreadable_piece_is_a_miss(tmp_path: Path) -> None:
    paths = demo_paths(tmp_path)
    deps = worker_deps(tmp_path, paths)
    req = piece_request()
    (deps.blobs.dir_for(req.piece_key) / PIECE_NAME).write_text('{"result": 1}')

    assert await ActivityEnvironment().run(RenderActivities(deps).cached_piece, req) is None


async def test_an_openscad_failure_is_a_non_retryable_application_error(tmp_path: Path) -> None:
    paths = demo_paths(tmp_path, "%%FAIL%%\n")
    acts = RenderActivities(worker_deps(tmp_path, paths))
    env = ActivityEnvironment()
    req = piece_request()

    # `prepare` derives the schema to check the parameters (phase 4), so a source that
    # does not parse fails there, before any render.
    with pytest.raises(ApplicationError) as raised:
        await env.run(acts.prepare, req)

    assert raised.value.type == "OpenSCADError"
    assert raised.value.non_retryable
    failure = raised.value.details[0]
    assert isinstance(failure, Failure)
    assert failure.error == raised.value.message
    assert failure.log_tail == ["ERROR: Parser error: syntax error"]


async def test_a_failed_preview_keeps_its_openscad_diagnostics(tmp_path: Path) -> None:
    paths = demo_paths(tmp_path, "%%FAIL%%\n")
    acts = RenderActivities(worker_deps(tmp_path, paths))

    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(acts.render_preview_png, piece_request().slug)

    assert raised.value.type == "OpenSCADError"
    assert raised.value.non_retryable
    failure = raised.value.details[0]
    assert isinstance(failure, Failure)
    assert failure.log_tail == ["ERROR: Parser error: syntax error"]


async def test_an_openscad_failure_in_render_main_carries_its_failure(tmp_path: Path) -> None:
    """The source parsed at `prepare`, then stopped parsing: `render_main` maps the
    render's OpenSCADError to a non-retryable failure with its log tail."""
    paths = demo_paths(tmp_path)
    acts = RenderActivities(worker_deps(tmp_path, paths))
    env = ActivityEnvironment()
    req = piece_request()
    prepared = await env.run(acts.prepare, req)
    paths.model_source("demo").write_text("%%FAIL%%\n", encoding="utf-8")

    with pytest.raises(ApplicationError) as raised:
        await env.run(acts.render_main, req, prepared)

    assert raised.value.type == "OpenSCADError" and raised.value.non_retryable
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


class _StoppedError(Exception):
    """Ends `render_main` once the checkout has been watched."""


async def test_render_main_heartbeats_while_its_checkout_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#674 gate: `checkout_fresh` waits on the key's lock, which another fetch of the
    same key may hold for a whole transfer; the activity heartbeats through it."""
    paths = demo_paths(tmp_path)
    deps = worker_deps(tmp_path, paths)
    beat = asyncio.Event()
    beats_in_checkout: list[bool] = []
    real = activities._heartbeating

    async def quick[T](work: asyncio.Task[T], every: float = 5.0) -> T:
        return await real(work, every=0.01)

    async def checkout_fresh(key: str) -> str | None:
        try:
            await asyncio.wait_for(beat.wait(), 5)
            beats_in_checkout.append(True)
        except TimeoutError:
            beats_in_checkout.append(False)
        raise _StoppedError

    monkeypatch.setattr(activities, "_heartbeating", quick)
    monkeypatch.setattr(deps.blobs, "checkout_fresh", checkout_fresh)
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *details: beat.set()
    prepared = PrepareResult(version=REVISION, scad=str(tmp_path / "model.scad"), schema_cache="")
    with pytest.raises(_StoppedError):
        await env.run(RenderActivities(deps).render_main, piece_request(), prepared)
    assert beats_in_checkout == [True]


# ── project ────────────────────────────────────────────────────────────────────


def _output(name: str, key: str) -> PipelineOutput:
    return PipelineOutput(
        name=name,
        result=_result(),
        blob_keys=[key],
        record=OutputRecord(
            revision="r",
            ui_api=None,
            pipeline_api=1,
            pipeline_version="v",
            inputs_v=0,
            plate_key="default",
            parts=[key],
        ),
    )


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
    paths = demo_paths(tmp_path)
    return (
        RenderActivities(worker_deps(tmp_path, paths, projection=projection, refs=refs)),
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
        blob_keys=["piece-key", "output-x-0"],
        outputs=[_output("house", "piece-key"), _output("garage", "output-x-0")],
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
    assert stored.outputs == done.outputs  # both, in order
    assert {"piece-key", "output-x-0"} <= refs.referenced()

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


async def _make_current(client: Client) -> None:
    """As the worker does: Temporal 1.28 takes the build only once it polls."""
    assert await make_current_until_polled(
        lambda: make_current(client, namespace=client.namespace, build_id="test"),
        build_id="test",
        backoff=(0.1,),
        every=0.2,
        deadline=30,
    )


@pytest.mark.requires_postgres
@pytest.mark.requires_temporal
async def test_a_job_renders_end_to_end_on_the_render_worker(
    tmp_path: Path, pg_conninfo: str, projection: JobProjection
) -> None:
    paths = demo_paths(tmp_path)
    refs = BlobRefs(projection.pool)
    deps = worker_deps(tmp_path, paths, projection=projection, refs=refs)
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
            pipeline=PipelineActivities(deps),
            build_id="test",
            max_concurrent_activities=2,
        ):
            # A versioned worker takes new workflows only once its version is current.
            await _make_current(client)
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
    paths = demo_paths(tmp_path)
    refs = BlobRefs(projection.pool)
    deps = worker_deps(tmp_path, paths, projection=projection, refs=refs)

    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        async with render_worker(
            client,
            queue,
            RenderActivities(deps),
            pipeline=PipelineActivities(deps),
            build_id="test",
            max_concurrent_activities=2,
        ):
            await _make_current(client)

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


@pytest.mark.parametrize("stage", ["render_main", "render_solids"])
async def test_an_upload_the_store_lacks_fails_the_stage_as_an_input_error(
    tmp_path: Path,
    pg_conninfo: str,
    pg_pool: PgPool,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    missing = "f" * 64

    async def refuses(*_: object, **__: object) -> None:
        # What `staged_assets` raises for a file parameter naming no upload.
        file_assets(
            CustomizerSchema(
                parameters=[Parameter(name="label", type="file", initial="", accept=["svg"])]
            ),
            {"label": missing},
            AssetStore(tmp_path / "empty", pg_pool),
            tmp_path,
        )

    monkeypatch.setattr(activities_module, "render_main", refuses)
    monkeypatch.setattr(activities_module, "render_solids_stage", refuses)
    # The fake openscad's schema has no file parameter; this is about the stage.
    monkeypatch.setattr(activities_module, "params_problem", lambda *_: None)
    paths = demo_paths(tmp_path)
    with store_pool(pg_conninfo) as pool:
        deps = dataclasses.replace(
            worker_deps(tmp_path, paths),
            assets=AssetStore(paths.assets, pg_pool),
            remote_assets=RemoteAssets(local_content(tmp_path / "remote", pool)),
        )
        acts = RenderActivities(deps)
        req = PieceRequest(
            slug="demo",
            revision=REVISION,
            params={"label": missing},
            piece_key=piece_key("demo", REVISION, "model.scad", {"label": missing}),
        )
        deps.blobs.dir_for(req.piece_key)  # render_solids continues a piece
        prepared = await ActivityEnvironment().run(acts.prepare, req)
        main = RenderMainResult()
        run = getattr(acts, stage)
        args = (req, prepared) if stage == "render_main" else (req, prepared, main)
        with pytest.raises(ApplicationError) as raised:
            await ActivityEnvironment().run(run, *args)
    assert raised.value.type == "AssetUnavailable" and raised.value.non_retryable
    assert missing in str(raised.value) and "not in the blob store" in str(raised.value)


async def test_an_upload_whose_local_copy_vanished_is_not_called_absent_from_the_store(
    tmp_path: Path, pg_conninfo: str, pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ensure` found the upload already local, then the worker's sweep took the copy
    before the render read it: a retry brings it back, so the stage is retried, and the
    error does not say the store lacks it."""
    paths = demo_paths(tmp_path)
    assets = AssetStore(paths.assets, pg_pool)
    meta = assets.put(b'<svg xmlns="http://www.w3.org/2000/svg"/>', "logo.svg")

    async def vanished(*_: object, **__: object) -> None:
        raise AssetUnavailableError("label", meta.id)

    monkeypatch.setattr(activities_module, "render_main", vanished)
    with store_pool(pg_conninfo) as pool:
        deps = dataclasses.replace(
            worker_deps(tmp_path, paths),
            assets=assets,
            remote_assets=RemoteAssets(local_content(tmp_path / "remote", pool)),
        )
        acts = RenderActivities(deps)
        params: dict[str, str | int | float | bool] = {"label": meta.id}
        req = PieceRequest(
            slug="demo",
            revision=REVISION,
            params=params,
            piece_key=piece_key("demo", REVISION, "model.scad", params),
        )
        prepared = await ActivityEnvironment().run(acts.prepare, req)
        with pytest.raises(ApplicationError) as raised:
            await ActivityEnvironment().run(acts.render_main, req, prepared)
    assert raised.value.type == "AssetUnavailable" and not raised.value.non_retryable
    assert meta.id in str(raised.value) and "not in the blob store" not in str(raised.value)
