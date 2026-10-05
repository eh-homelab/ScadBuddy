"""RenderService: submit as update-with-start (#1053), supersede as the `release`
Update, over a dev server and a real projection, with the openscad activities faked."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import replace
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from google.protobuf.any_pb2 import Any as Any_
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from temporalio import activity
from temporalio.api.common.v1 import GrpcStatus
from temporalio.api.errordetails.v1 import NamespaceNotFoundFailure
from temporalio.client import (
    Client,
    WorkflowExecutionStatus,
)
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from scadbuddy.core.config import load_config
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.pg_listener import PgListener
from scadbuddy.core.problems import ApiError
from scadbuddy.core.tracing import use_traceparent
from scadbuddy.library.assets import AssetStore
from scadbuddy.render import submit as submit_module
from scadbuddy.render.job_models import (
    SUPERSEDED_ERROR,
    Job,
    JobNotFoundError,
    QueueFullError,
    now,
    render_key,
)
from scadbuddy.render.jobs import SnapshotUnavailableError
from scadbuddy.render.projection import (
    CLOSED_ERROR,
    LEGACY_UNSTARTED_ERROR,
    JobProjection,
    workflow_id_for,
    workflow_id_for_key,
)
from scadbuddy.render.schema import ParamValue
from scadbuddy.render.submit import MAX_WORKFLOW_INPUT_BYTES, RenderService
from scadbuddy.store import BlobRefs
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.workflows import commands as commands_module
from scadbuddy.workflows.activities import RenderActivities, WorkerDeps
from scadbuddy.workflows.commands import (
    CommandClosedError,
    CommandStillAcceptingError,
    TemporalUnavailableError,
)
from scadbuddy.workflows.models import (
    ACCEPT_ACTIVITY,
    CLAIMS_ACTIVITY,
    RELEASE_UPDATE,
    AcceptRender,
    Projection,
    ReleaseAnswer,
    RenderAnswer,
)
from scadbuddy.workflows.pipelines import RenderPreview, TemplatePipeline
from tests.support.renders import legacy_row
from tests.support.temporal import temporal_client
from tests.test_workflows import FakeActivities, _worker

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

SLUG = "demo"


class ProjectingActivities(FakeActivities):
    """The fakes, but the row's writes are the real activities': `render_accept`,
    `render_claims` and `project`. ``hold_running`` keeps a job pending (its `running`
    projection waits for it)."""

    def __init__(
        self, deps: WorkerDeps, *, hold_running: asyncio.Event | None = None, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self._real = RenderActivities(deps)
        self.hold_running = hold_running

    @activity.defn(name=ACCEPT_ACTIVITY)
    async def render_accept(self, accept: AcceptRender) -> Job:
        self.accepts += 1
        return await self._real.render_accept(accept)

    @activity.defn(name=CLAIMS_ACTIVITY)
    async def render_claims(self, job_id: str, claims: int) -> None:
        self.claims.append(claims)
        await self._real.render_claims(job_id, claims)

    @activity.defn(name="project")
    async def project(self, projection: Projection) -> None:
        if projection.state == "running" and self.hold_running is not None:
            await self.hold_running.wait()
        await super().project(projection)
        await self._real.project(projection)


def _sample(metrics: Metrics, name: str) -> float:
    return metrics.registry.get_sample_value(name) or 0.0


def _w() -> int:
    """A width no other test renders: every test shares the session's Temporal, where
    `render-<render_key>` names one execution."""
    return uuid.uuid4().int % 10**9


async def _settled(projection: JobProjection, job_id: str, timeout: float = 30) -> Job:
    async with asyncio.timeout(timeout):
        while True:
            job = await asyncio.to_thread(projection.read, job_id)
            if job.state in ("done", "failed", "cancelled"):
                return job
            await asyncio.sleep(0.05)


async def _until(acts: FakeActivities, call: str) -> None:
    async with asyncio.timeout(30):
        while call not in acts.calls:
            await asyncio.sleep(0.05)


@pytest.fixture
async def projection(pg_conninfo: str) -> AsyncIterator[JobProjection]:
    bus = PgNotifyEventBus(pg_conninfo, listener=PgListener(pg_conninfo))
    store = JobProjection(pg_conninfo, pool_size=4, events=bus)
    await asyncio.to_thread(store.open)
    try:
        yield store
    finally:
        await asyncio.to_thread(store.close)


@pytest.fixture
def deps(projection: JobProjection, paths: DataPaths) -> WorkerDeps:
    config = replace(load_config({}), data_dir=paths.root)
    return WorkerDeps(
        config=config,
        paths=paths,
        assets=AssetStore(paths.assets),
        blobs=LocalBlobStore(paths.blobs),
        refs=BlobRefs(projection.pool),
        projection=projection,
    )


ServiceFactory = Callable[..., RenderService]


@pytest.fixture
def make_service(projection: JobProjection, deps: WorkerDeps) -> ServiceFactory:
    def make(client: Client, task_queue: str, **kwargs: Any) -> RenderService:
        config = kwargs.pop("config", deps.config)
        return RenderService(
            projection=projection,
            client=client,
            task_queue=task_queue,
            config=config,
            paths=deps.paths,
            metrics=Metrics(),
            **kwargs,
        )

    return make


async def test_a_submit_runs_as_render_of_its_key(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    width = _w()
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        async with _worker(client, queue, ProjectingActivities(deps)):
            job = await service.submit(SLUG, {"width": width})
            assert job.inputs == {"params": {"width": width}, "v": 0}
            key = render_key(SLUG, {"width": width}, None)
            await asyncio.wait_for(
                client.get_workflow_handle(workflow_id_for_key(key)).result(), timeout=30
            )
        await service.aclose()
        done = await asyncio.to_thread(projection.read, job.id)

    assert done.state == "done", done.error
    assert done.result is not None
    assert done.workflow_id == workflow_id_for_key(key) and done.workflow_run_id
    assert _sample(service.metrics, "scadbuddy_render_jobs_submitted_total") == 1


async def test_an_identical_submit_joins_the_running_job_with_a_claim(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    width = _w()
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        gate = asyncio.Event()
        acts = ProjectingActivities(deps, block_main=gate)
        async with _worker(client, queue, acts):
            first = await service.submit(SLUG, {"width": width})
            await _until(acts, "render_main")
            # Running, not only pending: the open execution answers it (§4.5).
            second = await service.submit(SLUG, {"width": width})
            claims = (await asyncio.to_thread(projection.read, first.id)).claims
            gate.set()
            await _settled(projection, first.id)
        await service.aclose()

    assert second.id == first.id
    assert second.claims == 2 and claims == 2
    assert acts.accepts == 1
    assert _sample(service.metrics, "scadbuddy_render_jobs_coalesced_total") == 1


async def test_a_submit_that_coalesces_keeps_the_first_submitters_inputs(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    """The render is keyed on `params`, so a second submit with other UI state joins the
    open job, and the row keeps the inputs of the submission that made it (the route
    answers each caller with its own)."""
    width = _w()
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        gate = asyncio.Event()
        acts = ProjectingActivities(deps, block_main=gate)
        lid = {"params": {"width": width}, "ui": {"tab": "lid"}, "v": 0}
        base = {"params": {"width": width}, "ui": {"tab": "base"}, "v": 0}
        async with _worker(client, queue, acts):
            first = await service.submit(SLUG, {"width": width}, inputs=lid)
            second = await service.submit(SLUG, {"width": width}, inputs=base)
            gate.set()
            await _settled(projection, first.id)
        await service.aclose()

    assert second.id == first.id
    assert (await asyncio.to_thread(projection.read, first.id)).inputs == lid


async def test_superseding_releases_the_old_execution_after_the_new_one_starts(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        gate = asyncio.Event()
        acts = ProjectingActivities(deps, block_solids=gate)
        async with _worker(client, queue, acts):
            first = await service.submit(SLUG, {"width": _w()})
            await _until(acts, "render_solids")
            second = await service.submit(SLUG, {"width": _w()}, supersedes=first.id)
            # The release answers once the cancelled job is projected.
            cancelled = await asyncio.to_thread(projection.read, first.id)
            assert first.workflow_id is not None
            old = client.get_workflow_handle(first.workflow_id, run_id=first.workflow_run_id)
            await asyncio.wait_for(old.result(), timeout=30)
            gate.set()
            done = await _settled(projection, second.id)
        closed = (await old.describe()).status
        await service.aclose()

    assert cancelled.state == "cancelled" and cancelled.error == SUPERSEDED_ERROR
    assert closed == WorkflowExecutionStatus.COMPLETED
    assert done.state == "done", done.error
    assert _sample(service.metrics, "scadbuddy_render_jobs_submitted_total") == 2


async def test_a_resent_supersede_releases_the_old_job_once(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    """A submit re-sent with its key after a lost answer is the same request: the job it
    supersedes loses one claim, not two (review #1066 finding 1)."""
    width = _w()
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        gate = asyncio.Event()
        async with _worker(client, queue, ProjectingActivities(deps, block_main=gate)):
            old = await service.submit(SLUG, {"width": width})
            assert (await service.submit(SLUG, {"width": width})).id == old.id
            request, params = uuid.uuid4().hex, {"width": _w()}
            first = await service.submit(SLUG, params, supersedes=old.id, request_id=request)
            again = await service.submit(SLUG, params, supersedes=old.id, request_id=request)
            kept = await asyncio.to_thread(projection.read, old.id)
            gate.set()
            await _settled(projection, old.id)
            await _settled(projection, first.id)
        await service.aclose()

    assert again.id == first.id
    assert kept.state in ("pending", "running") and kept.claims == 1


async def test_superseding_the_same_render_answers_it_without_a_claim(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    width = _w()
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        gate = asyncio.Event()
        acts = ProjectingActivities(deps, block_main=gate)
        async with _worker(client, queue, acts):
            first = await service.submit(SLUG, {"width": width})
            again = await service.submit(SLUG, {"width": width}, supersedes=first.id)
            gate.set()
            await _settled(projection, first.id)
        await service.aclose()

    assert again.id == first.id
    assert acts.claims == []
    assert (await asyncio.to_thread(projection.read, first.id)).state == "done"


async def test_superseding_a_finished_job_still_submits(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        async with _worker(client, queue, ProjectingActivities(deps)):
            first = await service.submit(SLUG, {"width": _w()})
            await _settled(projection, first.id)
            second = await service.submit(SLUG, {"width": _w()}, supersedes=first.id)
            done = await _settled(projection, second.id)
        await service.aclose()

    assert done.state == "done", done.error
    assert (await asyncio.to_thread(projection.read, first.id)).state == "done"


async def test_a_refused_submit_supersedes_nothing(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue, config=replace(deps.config, render_queue_max=1))
        hold = asyncio.Event()
        acts = ProjectingActivities(deps, hold_running=hold)
        async with _worker(client, queue, acts):
            first = await service.submit(SLUG, {"width": _w()})
            with pytest.raises(QueueFullError) as refused:
                await service.submit(SLUG, {"width": _w()}, supersedes=first.id)
            waiting = await asyncio.to_thread(projection.read, first.id)
            hold.set()
            await _settled(projection, first.id)
        await service.aclose()

    assert refused.value.depth == 1 and refused.value.retry_after >= 1
    assert waiting.state == "pending" and waiting.claims == 1
    assert _sample(service.metrics, "scadbuddy_render_jobs_rejected_total") == 1


async def test_a_submit_after_the_last_release_waits_for_close_and_starts_again(
    make_service: ServiceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = Job(id=uuid.uuid4().hex, slug=SLUG, created_at=now())
    answers = [RenderAnswer(closing=True), RenderAnswer(job=job)]
    calls: list[str] = []

    async def answering(*_: object, **kwargs: Any) -> RenderAnswer:
        calls.append(str(kwargs["id"]))
        return answers.pop(0)

    monkeypatch.setattr(submit_module, "start_command", answering)
    async with temporal_client() as client:
        service = make_service(client, f"t-{uuid.uuid4().hex[:8]}")
        submitted = await service.submit(SLUG, {"width": _w()})
        await service.aclose()

    assert submitted.id == job.id
    assert len(calls) == 2 and calls[0] == calls[1]


async def test_an_update_aborted_by_a_closing_execution_starts_again(
    make_service: ServiceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = Job(id=uuid.uuid4().hex, slug=SLUG, created_at=now())
    calls: list[str] = []

    async def answering(*_: object, **kwargs: Any) -> RenderAnswer:
        calls.append(str(kwargs["id"]))
        if len(calls) == 1:
            raise RPCError(
                "workflow update was aborted by closing workflow", RPCStatusCode.NOT_FOUND, b""
            )
        return RenderAnswer(job=job)

    monkeypatch.setattr(submit_module, "start_command", answering)
    async with temporal_client() as client:
        service = make_service(client, f"t-{uuid.uuid4().hex[:8]}")
        submitted = await service.submit(SLUG, {"width": _w()})
        await service.aclose()

    assert submitted.id == job.id and len(calls) == 2


class FakePreview:
    """`render_preview_png` by name, holding every call until released."""

    def __init__(self) -> None:
        self.calls = 0
        self.revisions: list[str | None] = []
        self.release = asyncio.Event()

    @activity.defn(name="render_preview_png")
    async def render_preview_png(self, slug: str, revision: str | None = None) -> bytes:
        self.calls += 1
        self.revisions.append(revision)
        await self.release.wait()
        return PNG + slug.encode()


PNG = b"\x89PNG\r\n\x1a\n"


async def test_a_preview_renders_on_the_worker_and_one_slug_runs_once(
    make_service: ServiceFactory, deps: WorkerDeps
) -> None:
    registered = {
        getattr(fn, "__temporal_activity_definition").name for fn in RenderActivities(deps).all()
    }
    assert "render_preview_png" in registered

    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        fake = FakePreview()
        async with Worker(
            client,
            task_queue=queue,
            workflows=[RenderPreview],
            activities=[fake.render_preview_png],
        ):
            both = asyncio.gather(
                service.render_preview(SLUG, 30.0), service.render_preview(SLUG, 30.0)
            )
            async with asyncio.timeout(30):
                while fake.calls == 0:
                    await asyncio.sleep(0.05)
            await asyncio.sleep(0.2)
            fake.release.set()
            pngs = await both
        await service.aclose()

    assert list(pngs) == [PNG + SLUG.encode()] * 2
    assert fake.calls == 1


async def test_a_local_preview_after_a_source_edit_does_not_join_the_older_run(
    make_service: ServiceFactory, paths: DataPaths
) -> None:
    """#903: on the local store the run's id names the source key, so a preview asked
    for after an edit starts its own run rather than taking the pre-edit image."""
    paths.model_dir(SLUG).mkdir(parents=True, exist_ok=True)
    paths.model_source(SLUG).write_text("cube(1);", encoding="utf-8")
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        fake = FakePreview()
        async with Worker(
            client,
            task_queue=queue,
            workflows=[RenderPreview],
            activities=[fake.render_preview_png],
        ):
            before = asyncio.create_task(service.render_preview(SLUG, 30.0))
            try:
                async with asyncio.timeout(30):
                    while fake.calls == 0:
                        await asyncio.sleep(0.05)
                paths.model_source(SLUG).write_text("cube(2);", encoding="utf-8")
                after = asyncio.create_task(service.render_preview(SLUG, 30.0))
                with suppress(TimeoutError):
                    async with asyncio.timeout(5):
                        while fake.calls < 2:
                            await asyncio.sleep(0.05)
                calls = fake.calls
            finally:
                fake.release.set()
            await asyncio.gather(before, after)
        await service.aclose()

    assert calls == 2


class _Pinning:
    """`SnapshotStore.pin` as the API's: the slug's last commit, stored."""

    def __init__(self) -> None:
        self.pinned: list[tuple[str, str | None]] = []

    async def pin(self, slug: str, revision: str | None) -> str | None:
        self.pinned.append((slug, revision))
        return "b" * 40


async def test_a_preview_on_the_bambuddy_store_pins_the_last_commit_for_the_worker(
    make_service: ServiceFactory,
) -> None:
    """Final review C1: the worker renders the snapshot of the revision the API pinned,
    not a live source it does not have."""
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        pinning = _Pinning()
        service.snapshots = pinning  # type: ignore[assignment]
        fake = FakePreview()
        fake.release.set()
        async with Worker(
            client,
            task_queue=queue,
            workflows=[RenderPreview],
            activities=[fake.render_preview_png],
        ):
            png = await service.render_preview(SLUG, 30.0)
        await service.aclose()

    assert png == PNG + SLUG.encode()
    assert pinning.pinned == [(SLUG, None)]
    assert fake.revisions == ["b" * 40]


class _Revisions(_Pinning):
    """`SnapshotStore.pin` whose last commit moves: each call answers the next revision."""

    def __init__(self, *revisions: str) -> None:
        super().__init__()
        self.revisions = list(revisions)

    async def pin(self, slug: str, revision: str | None) -> str | None:
        self.pinned.append((slug, revision))
        return self.revisions.pop(0)


async def test_a_preview_joins_only_a_run_of_the_same_revision(
    make_service: ServiceFactory,
) -> None:
    """#674 gate: a preview for a newer commit must not join a run still rendering an
    older one, or the scheduler stores the old image under the new source's key. Two
    calls at one revision still share one run."""
    old, new = "a" * 40, "c" * 40
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        service.snapshots = _Revisions(old, new, new)  # type: ignore[assignment]
        fake = FakePreview()
        async with Worker(
            client,
            task_queue=queue,
            workflows=[RenderPreview],
            activities=[fake.render_preview_png],
        ):
            first = asyncio.create_task(service.render_preview(SLUG, 30.0))
            async with asyncio.timeout(30):
                while fake.calls == 0:
                    await asyncio.sleep(0.05)
            later = asyncio.gather(
                service.render_preview(SLUG, 30.0), service.render_preview(SLUG, 30.0)
            )
            async with asyncio.timeout(30):
                while fake.calls < 2:
                    await asyncio.sleep(0.05)
            await asyncio.sleep(0.2)
            fake.release.set()
            await first
            await later
            ids = {
                (await client.get_workflow_handle(f"preview-{SLUG}-{revision[:12]}").describe()).id
                for revision in (old, new)
            }
        await service.aclose()

    assert fake.calls == 2
    assert fake.revisions == [old, new]
    assert len(ids) == 2


class _NoCommit(_Pinning):
    """`SnapshotStore.pin` with no history, or a template with no commit yet."""

    async def pin(self, slug: str, revision: str | None) -> str | None:
        self.pinned.append((slug, revision))
        return None


async def test_a_render_on_the_bambuddy_store_with_no_commit_to_pin_is_refused(
    make_service: ServiceFactory, projection: JobProjection
) -> None:
    """As for a preview: refused at submit, before a row or a workflow exists."""
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        service.snapshots = _NoCommit()  # type: ignore[assignment]
        with pytest.raises(SnapshotUnavailableError, match=SLUG):
            await service.submit(SLUG, {"size": 1})
        await service.aclose()

    # No row, so no workflow: `submit` starts one only for the row it recorded.
    assert await asyncio.to_thread(projection.list_jobs) == []


async def test_a_preview_on_the_bambuddy_store_with_no_commit_to_pin_is_refused(
    make_service: ServiceFactory,
) -> None:
    """The worker has no live source to fall back to: a clear error, and no run."""
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        service.snapshots = _NoCommit()  # type: ignore[assignment]
        fake = FakePreview()
        fake.release.set()
        async with Worker(
            client,
            task_queue=queue,
            workflows=[RenderPreview],
            activities=[fake.render_preview_png],
        ):
            with pytest.raises(SnapshotUnavailableError, match=SLUG):
                await service.render_preview(SLUG, 30.0)
        await service.aclose()

    assert fake.calls == 0


async def test_a_preview_past_its_timeout_stops_waiting_and_leaves_the_shared_run(
    make_service: ServiceFactory,
) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        fake = FakePreview()
        handle = client.get_workflow_handle(f"preview-{SLUG}")
        async with Worker(
            client,
            task_queue=queue,
            workflows=[RenderPreview],
            activities=[fake.render_preview_png],
        ):
            try:
                began = time.monotonic()
                with pytest.raises(TimeoutError):
                    await service.render_preview(SLUG, 0.5)
                waited = time.monotonic() - began
                # The caller gave up; the run it shared bounds itself (its memo'd
                # `preview_timeout`) and is still rendering for anyone who joins it.
                await asyncio.sleep(1.0)
                still = (await handle.describe()).status
                later = asyncio.create_task(service.render_preview(SLUG, 30.0))
                await asyncio.sleep(0.2)
                fake.release.set()
                png = await later
            finally:
                fake.release.set()
                with suppress(RPCError):
                    await handle.cancel()
        await service.aclose()

    assert waited < 5.0
    assert still == WorkflowExecutionStatus.RUNNING
    assert png == PNG + SLUG.encode()
    assert fake.calls == 1


class _Handle:
    """`get_workflow_handle`'s result, with `cancel` recorded or refused."""

    def __init__(self, workflow_id: str, cancelled: list[str], error: Exception | None) -> None:
        self.workflow_id, self.cancelled, self.error = workflow_id, cancelled, error

    async def cancel(self, **_: object) -> None:
        self.cancelled.append(self.workflow_id)
        if self.error is not None:
            raise self.error


def _spy_cancel(
    monkeypatch: pytest.MonkeyPatch, client: Client, error: Exception | None = None
) -> list[str]:
    cancelled: list[str] = []
    monkeypatch.setattr(
        client,
        "get_workflow_handle",
        lambda workflow_id, **_: _Handle(workflow_id, cancelled, error),
    )
    return cancelled


def _legacy(projection: JobProjection) -> Job:
    """A pending row an older release inserted: its workflow `render-<id>`."""
    params: dict[str, ParamValue] = {"width": _w()}
    job = Job(id=uuid.uuid4().hex, slug=SLUG, params=params, created_at=now())
    return legacy_row(projection, job)


async def test_superseding_a_legacy_row_cancels_its_workflow_the_old_way(
    make_service: ServiceFactory,
    deps: WorkerDeps,
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        old = _legacy(projection)
        cancelled = _spy_cancel(monkeypatch, client)
        async with _worker(client, queue, ProjectingActivities(deps)):
            second = await service.submit(SLUG, {"width": _w()}, supersedes=old.id)
        await service.aclose()

    assert second.id != old.id
    assert cancelled == [workflow_id_for(old.id)]
    dropped = await asyncio.to_thread(projection.read, old.id)
    assert dropped.state == "cancelled" and dropped.error == SUPERSEDED_ERROR


async def test_a_legacy_cancel_that_fails_is_a_warning_and_the_supersede_still_succeeds(
    make_service: ServiceFactory,
    deps: WorkerDeps,
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        old = _legacy(projection)
        _spy_cancel(monkeypatch, client, RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b""))
        async with _worker(client, queue, ProjectingActivities(deps)):
            with caplog.at_level(logging.WARNING, logger="scadbuddy.render.submit"):
                second = await service.submit(SLUG, {"width": _w()}, supersedes=old.id)
        await service.aclose()

    assert second.id != old.id
    assert (await asyncio.to_thread(projection.read, old.id)).state == "cancelled"
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warned and getattr(warned[0], "job_id", None) == old.id
    assert (
        service.metrics.registry.get_sample_value(
            "scadbuddy_render_store_errors_total", {"operation": "cancel_workflow"}
        )
        == 1
    )


async def test_a_legacy_cancel_that_fails_with_anything_else_never_fails_the_supersede(
    make_service: ServiceFactory,
    deps: WorkerDeps,
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        old = _legacy(projection)
        _spy_cancel(monkeypatch, client, ValueError("not an RPC error"))
        async with _worker(client, queue, ProjectingActivities(deps)):
            with caplog.at_level(logging.WARNING, logger="scadbuddy.render.submit"):
                await service.submit(SLUG, {"width": _w()}, supersedes=old.id)
        await service.aclose()

    assert (await asyncio.to_thread(projection.read, old.id)).state == "cancelled"
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warned and getattr(warned[0], "error_type", None) == "ValueError"


async def test_a_legacy_cancel_of_a_workflow_that_never_started_is_not_an_error(
    make_service: ServiceFactory,
    deps: WorkerDeps,
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        old = _legacy(projection)
        _spy_cancel(monkeypatch, client, RPCError("not found", RPCStatusCode.NOT_FOUND, b""))
        async with _worker(client, queue, ProjectingActivities(deps)):
            await service.submit(SLUG, {"width": _w()}, supersedes=old.id)
        await service.aclose()

    assert not service.metrics.registry.get_sample_value(
        "scadbuddy_render_store_errors_total", {"operation": "cancel_workflow"}
    )


class _HeldDone(ProjectingActivities):
    """The `done` projection waits for ``release`` once it has written the row."""

    def __init__(self, deps: WorkerDeps, *, release: asyncio.Event, **kwargs: Any):
        super().__init__(deps, **kwargs)
        self.release = release

    @activity.defn(name="project")
    async def project(self, projection: Projection) -> None:
        await super().project(projection)
        if projection.state == "done":
            await self.release.wait()


async def test_a_release_after_the_render_finished_cancels_nothing(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    """The last release reaching a job whose `done` is written (in flight, or already
    back) cancels nothing and says so (review #1066 2.3)."""
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        held = asyncio.Event()
        async with _worker(client, queue, _HeldDone(deps, release=held)):
            job = await service.submit(SLUG, {"width": _w()})
            done = await _settled(projection, job.id)
            assert job.workflow_id is not None
            handle = client.get_workflow_handle(job.workflow_id, run_id=job.workflow_run_id)
            last = asyncio.create_task(
                handle.execute_update(RELEASE_UPDATE, "cancelled", result_type=ReleaseAnswer)
            )
            await asyncio.sleep(1)
            held.set()
            answer = await asyncio.wait_for(last, timeout=30)
            await asyncio.wait_for(handle.result(), timeout=30)
        await service.aclose()

    assert done.state == "done"
    assert answer.cancelled is None
    assert (await asyncio.to_thread(projection.read, job.id)).state == "done"


async def test_a_resent_request_is_one_claim_so_a_supersede_still_cancels(
    make_service: ServiceFactory,
    deps: WorkerDeps,
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request answered `command-still-accepting` and sent again with its key joins
    its own claim, not a second one, so the supersede that follows cancels the render
    (review #1066 2.1)."""
    defaults = commands_module.start_command.__kwdefaults__
    assert defaults is not None
    monkeypatch.setitem(defaults, "deadline", timedelta(seconds=1))
    width = _w()
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        accepting, gate = asyncio.Event(), asyncio.Event()
        acts = _SlowAccept(deps, accepting=accepting, block_main=gate)
        async with _worker(client, queue, acts):
            request = uuid.uuid4().hex
            with pytest.raises(CommandStillAcceptingError):
                await service.submit(SLUG, {"width": width}, request_id=request)
            accepting.set()
            first = await service.submit(SLUG, {"width": width}, request_id=request)
            again = await service.submit(SLUG, {"width": width}, request_id=request)
            second = await service.submit(SLUG, {"width": _w()}, supersedes=first.id)
            dropped = await _settled(projection, first.id)
            gate.set()
            await _settled(projection, second.id)
        await service.aclose()

    assert again.id == first.id and first.claims == again.claims == 1
    assert acts.claims == []
    assert (dropped.state, dropped.error) == ("cancelled", SUPERSEDED_ERROR)


class _SlowAccept(ProjectingActivities):
    """`render_accept` waits for ``accepting``: an Update that outlives its deadline."""

    def __init__(self, deps: WorkerDeps, *, accepting: asyncio.Event, **kwargs: Any):
        super().__init__(deps, **kwargs)
        self.accepting = accepting

    @activity.defn(name=ACCEPT_ACTIVITY)
    async def render_accept(self, accept: AcceptRender) -> Job:
        await self.accepting.wait()
        return await super().render_accept(accept)


def _aged(projection: JobProjection, *job_ids: str) -> None:
    """Older than `LEGACY_GRACE`: past any older API's insert-then-start."""
    with psycopg.connect(projection.conninfo) as conn:
        conn.execute(
            "UPDATE render_jobs SET created_at = now() - interval '1 hour' WHERE id = ANY(%s)",
            (list(job_ids),),
        )


async def test_settle_legacy_fails_only_old_rows_no_workflow_will_run(
    make_service: ServiceFactory, projection: JobProjection
) -> None:
    """Past `LEGACY_GRACE`, a row naming no workflow, or one Temporal does not have
    running, is orphaned. A younger row may be between the older API's insert and its
    start (review #1066 1.2), and a running one is that build's. A row that build moved
    to running and whose workflow then closed is failed too (review #1066 (5) 2.1)."""
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        orphan, draining, ancient, fresh, stalled = (_legacy(projection) for _ in range(5))
        with psycopg.connect(projection.conninfo) as conn:
            conn.execute("UPDATE render_jobs SET workflow_id = NULL WHERE id = %s", (ancient.id,))
            conn.execute("UPDATE render_jobs SET state = 'running' WHERE id = %s", (stalled.id,))
        _aged(projection, orphan.id, draining.id, ancient.id, stalled.id)
        # The old build's execution of `draining`: no worker here, so it stays running.
        await client.start_workflow(
            "TemplatePipeline", id=workflow_id_for(draining.id), task_queue=queue
        )
        try:
            failed = await service.settle_legacy()
        finally:
            await client.get_workflow_handle(workflow_id_for(draining.id)).terminate()
        await service.aclose()

    assert sorted(failed) == sorted([orphan.id, ancient.id, stalled.id])
    for job_id in (orphan.id, ancient.id):
        stored = await asyncio.to_thread(projection.read, job_id)
        assert (stored.state, stored.error) == ("failed", LEGACY_UNSTARTED_ERROR)
    stored = await asyncio.to_thread(projection.read, stalled.id)
    assert (stored.state, stored.error) == ("failed", CLOSED_ERROR)
    for job_id in (draining.id, fresh.id):
        assert (await asyncio.to_thread(projection.read, job_id)).state == "pending"


async def test_an_old_legacy_row_whose_workflow_never_ran_does_not_block_its_key(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    """The pending key holds one row per render key (expand/contract): a legacy row
    past `LEGACY_GRACE` whose workflow Temporal does not have is failed by the next
    render of its key, which then runs (lead follow-up to review #1066 1.1)."""
    params: dict[str, ParamValue] = {"width": _w()}
    old = legacy_row(
        projection, Job(id=uuid.uuid4().hex, slug=SLUG, params=params, created_at=now())
    )
    _aged(projection, old.id)
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        async with _worker(client, queue, ProjectingActivities(deps)):
            job = await service.submit(SLUG, params)
            done = await _settled(projection, job.id)
        await service.aclose()

    assert done.state == "done", done.error
    stored = await asyncio.to_thread(projection.read, old.id)
    assert (stored.state, stored.error) == ("failed", LEGACY_UNSTARTED_ERROR)


async def test_an_old_legacy_row_whose_workflow_runs_keeps_its_key_until_it_closes(
    make_service: ServiceFactory,
    deps: WorkerDeps,
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While the older build's workflow of a legacy row on the key still runs,
    `render_accept` retries rather than failing that row; once it closes without
    settling it, the next attempt fails the row and inserts its own (review #1066 4.1)."""
    defaults = commands_module.start_command.__kwdefaults__
    assert defaults is not None
    monkeypatch.setitem(defaults, "deadline", timedelta(seconds=1))
    params: dict[str, ParamValue] = {"width": _w()}
    old = legacy_row(
        projection, Job(id=uuid.uuid4().hex, slug=SLUG, params=params, created_at=now())
    )
    _aged(projection, old.id)
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        # The old build's execution: on a queue no worker polls, so it stays running.
        legacy = await client.start_workflow(
            "TemplatePipeline", id=workflow_id_for(old.id), task_queue=f"{queue}-old"
        )
        acts = ProjectingActivities(deps)
        async with _worker(client, queue, acts):
            with pytest.raises(CommandStillAcceptingError):
                await service.submit(SLUG, params)
            # The 1 s deadline can lapse before a loaded worker runs the first
            # `render_accept`; wait for that attempt rather than racing it.
            async with asyncio.timeout(60):
                while acts.accepts < 1:
                    await asyncio.sleep(0.1)
            waiting = await asyncio.to_thread(projection.read, old.id)
            tries = acts.accepts
            await legacy.terminate()
            async with asyncio.timeout(60):
                while (await asyncio.to_thread(projection.read, old.id)).state == "pending":
                    await asyncio.sleep(0.1)
            jobs = await asyncio.to_thread(projection.list_jobs)
            new = next(job for job in jobs if job.id != old.id)
            done = await _settled(projection, new.id)
        await service.aclose()

    assert waiting.state == "pending" and tries >= 1
    stored = await asyncio.to_thread(projection.read, old.id)
    assert (stored.state, stored.error) == ("failed", LEGACY_UNSTARTED_ERROR)
    assert done.state == "done", done.error


async def test_rows_nothing_will_settle_are_failed_without_a_restart(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    """The pruner's pass also fails a row whose execution closed without settling it
    (terminated by hand), and a legacy row an older API inserted after this one booted
    (review #1066 1.2). A row whose execution runs is left alone."""
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue, prune_interval=0.05)
        await service.start()
        held = asyncio.Event()
        try:
            async with _worker(client, queue, ProjectingActivities(deps, hold_running=held)):
                job = await service.submit(SLUG, {"width": _w()})
                late = _legacy(projection)
                _aged(projection, late.id)
                await asyncio.sleep(0.5)
                running = await asyncio.to_thread(projection.read, job.id)
                assert job.workflow_id is not None
                await client.get_workflow_handle(
                    job.workflow_id, run_id=job.workflow_run_id
                ).terminate()
                closed = await _settled(projection, job.id, timeout=10)
                orphan = await _settled(projection, late.id, timeout=10)
                held.set()
        finally:
            await service.aclose()

    assert running.state == "pending"
    assert (closed.state, closed.error) == ("failed", CLOSED_ERROR)
    assert (orphan.state, orphan.error) == ("failed", LEGACY_UNSTARTED_ERROR)


class _SlowFirstClaim(ProjectingActivities):
    """The projection of the second claim lands after the third's, unless they are
    serialised."""

    @activity.defn(name=CLAIMS_ACTIVITY)
    async def render_claims(self, job_id: str, claims: int) -> None:
        if claims == 2:
            await asyncio.sleep(1)
        await super().render_claims(job_id, claims)


async def test_concurrent_claims_project_the_workflows_count(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    """Two requests joining at once: the row ends at the workflow's count, never at the
    one a slower projection carried (review #1066 2.1)."""
    params: dict[str, ParamValue] = {"width": _w()}
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        held = asyncio.Event()
        acts = _SlowFirstClaim(deps, hold_running=held)
        async with _worker(client, queue, acts):
            job = await service.submit(SLUG, params)
            await asyncio.gather(service.submit(SLUG, params), service.submit(SLUG, params))
            async with asyncio.timeout(10):
                while len(acts.claims) < 2:
                    await asyncio.sleep(0.05)
            await asyncio.sleep(1.5)
            claimed = await asyncio.to_thread(projection.read, job.id)
            held.set()
            await _settled(projection, job.id)
        await service.aclose()

    assert claimed.claims == 3


async def test_a_release_while_an_input_problems_failure_waits_to_be_written_cancels_it(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    """The last release landing while the `failed` projection of an input problem is
    scheduled (no worker takes it) still leaves the row terminal (review #1066 2.2)."""
    acts = ProjectingActivities(deps)
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        async with Worker(
            client,
            task_queue=queue,
            workflows=[TemplatePipeline],
            activities=[acts.project, acts.render_accept, acts.render_claims],
            no_remote_activities=True,
        ):
            job = await service.submit("Not A Slug", {"width": _w()})
            assert job.workflow_id is not None
            handle = client.get_workflow_handle(job.workflow_id, run_id=job.workflow_run_id)
            answer = await asyncio.wait_for(
                handle.execute_update(RELEASE_UPDATE, "superseded", result_type=ReleaseAnswer),
                timeout=30,
            )
            await asyncio.wait_for(handle.result(), timeout=30)
        await service.aclose()

    stored = await asyncio.to_thread(projection.read, job.id)
    assert (stored.state, stored.error) == ("cancelled", SUPERSEDED_ERROR)
    assert answer.cancelled is not None and answer.cancelled.id == job.id


def _namespace_not_found() -> RPCError:
    status = GrpcStatus(
        code=RPCStatusCode.NOT_FOUND,
        message="Namespace nope is not found.",
        details=[
            Any_(type_url=f"type.googleapis.com/{NamespaceNotFoundFailure.DESCRIPTOR.full_name}")
        ],
    )
    return RPCError(status.message, RPCStatusCode.NOT_FOUND, status.SerializeToString())


async def test_a_missing_namespace_is_not_taken_for_a_closing_execution(
    make_service: ServiceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its NOT_FOUND is a configuration error, raised at once, never waited on and sent
    again (review #1066 3.1)."""
    calls: list[str] = []

    async def refusing(*_: object, **kwargs: Any) -> RenderAnswer:
        calls.append(str(kwargs["id"]))
        raise _namespace_not_found()

    monkeypatch.setattr(submit_module, "start_command", refusing)
    async with temporal_client() as client:
        service = make_service(client, f"t-{uuid.uuid4().hex[:8]}")
        with pytest.raises(RPCError):
            await service.submit(SLUG, {"width": _w()})
        await service.aclose()

    assert len(calls) == 1


# ── inputs Temporal can never take (final review I2) ────────────────────────────


async def test_a_submit_too_large_for_a_workflow_input_is_a_413_and_no_row(
    make_service: ServiceFactory, projection: JobProjection
) -> None:
    async with temporal_client() as client:
        service = make_service(client, f"t-{uuid.uuid4().hex[:8]}")
        with pytest.raises(ApiError) as refused:
            await service.submit(SLUG, {"text": "x" * MAX_WORKFLOW_INPUT_BYTES})
        await service.aclose()

    assert refused.value.status == 413
    assert await asyncio.to_thread(projection.list_jobs) == []


async def test_settled_jobs_past_their_ttl_are_pruned_without_a_restart(
    projection: JobProjection, deps: WorkerDeps
) -> None:
    old = _accepted(projection, finished_ago=timedelta(days=2))
    old.state, old.finished_at = "done", now() - timedelta(days=2)
    assert projection.finish(old)
    fresh = _accepted(projection, finished_ago=timedelta(0))
    fresh.state, fresh.finished_at = "done", now()
    assert projection.finish(fresh)

    async with temporal_client() as client:
        service = RenderService(
            projection=projection,
            client=client,
            task_queue=f"t-{uuid.uuid4().hex[:8]}",
            config=replace(deps.config, job_ttl=86400.0),
            paths=deps.paths,
            metrics=Metrics(),
            prune_interval=0.05,
        )
        await service.start()
        try:
            async with asyncio.timeout(10):
                while True:
                    try:
                        await asyncio.to_thread(projection.read, old.id)
                    except JobNotFoundError:
                        break
                    await asyncio.sleep(0.05)
        finally:
            await service.aclose()

    assert (await asyncio.to_thread(projection.read, fresh.id)).state == "done"


def _accepted(projection: JobProjection, *, finished_ago: timedelta) -> Job:
    params: dict[str, ParamValue] = {"width": _w()}
    job = Job(
        id=uuid.uuid4().hex,
        slug=SLUG,
        params=params,
        inputs={"params": params},
        created_at=now() - finished_ago,
    )
    key = render_key(SLUG, params, None)
    return projection.accept(
        job, key, workflow_id=workflow_id_for_key(key), run_id=uuid.uuid4().hex
    )


class _PinnedSnapshots:
    """The bambuddy store's `SnapshotStore.pin`, recorded: every job names a revision."""

    def __init__(self, revision: str) -> None:
        self.revision = revision
        self.asked: list[tuple[str, str | None]] = []

    async def pin(self, slug: str, revision: str | None) -> str | None:
        self.asked.append((slug, revision))
        return self.revision


async def test_with_a_snapshot_store_a_submit_names_the_pinned_revision(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    revision = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        pinned = _PinnedSnapshots(revision)
        service.snapshots = pinned  # type: ignore[assignment]
        hold = asyncio.Event()
        async with _worker(client, queue, ProjectingActivities(deps, hold_running=hold)):
            job = await service.submit(SLUG, {"width": _w()})
            stored = await asyncio.to_thread(projection.read, job.id)
            hold.set()
        await service.aclose()
    assert pinned.asked == [(SLUG, None)]
    assert job.model_version == revision
    assert stored.model_version == revision


async def test_a_coalesced_submit_links_to_the_render_it_joined(
    make_service: ServiceFactory,
    deps: WorkerDeps,
    projection: JobProjection,
    spans: InMemorySpanExporter,
) -> None:
    width = _w()
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        hold = asyncio.Event()
        async with _worker(client, queue, ProjectingActivities(deps, hold_running=hold)):
            # Held pending, so the second joins the first's execution.
            first = await service.submit(SLUG, {"width": width})
            second = await service.submit(SLUG, {"width": width})
            hold.set()
            await _settled(projection, first.id)
        await service.aclose()
    assert second.id == first.id
    submits = [s for s in spans.get_finished_spans() if s.name == "render.submit"]
    assert len(submits) == 2
    opened, joined = submits
    assert (joined.attributes or {})["scadbuddy.coalesced"] is True
    assert (opened.attributes or {})["scadbuddy.coalesced"] is False
    assert [link.context.span_id for link in joined.links] == [opened.context.span_id]


async def test_with_no_valid_span_rows_carry_no_traceparent(
    make_service: ServiceFactory,
    deps: WorkerDeps,
    projection: JobProjection,
    spans: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review 2 of #1064: no stub of current_traceparent. Spec §4: the column is NULL
    # when the sampler dropped the first request (a `traceparent: ...-00` parent) or the
    # SDK is off (its no-op tracer gives no valid context). Outside any span the submit
    # span is a sampled root of its own, so that row does carry one: the control that
    # shows the two NULLs are not NULL by accident.
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        async with _worker(client, queue, ProjectingActivities(deps)):
            with use_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00"):
                unsampled = await service.submit(SLUG, {"width": _w()})
            with monkeypatch.context() as off:
                off.setattr(trace, "get_tracer", lambda *_args, **_kwargs: trace.NoOpTracer())
                disabled = await service.submit(SLUG, {"width": _w()})
            root = await service.submit(SLUG, {"width": _w()})
            for job in (unsampled, disabled, root):
                await _settled(projection, job.id)
        await service.aclose()
    assert (await asyncio.to_thread(projection.read, unsampled.id)).traceparent is None
    assert (await asyncio.to_thread(projection.read, disabled.id)).traceparent is None
    stored = (await asyncio.to_thread(projection.read, root.id)).traceparent
    (submit_span,) = [
        s
        for s in spans.get_finished_spans()
        if s.name == "render.submit" and (s.attributes or {}).get("scadbuddy.job_id") == root.id
    ]
    assert submit_span.parent is None
    assert stored is not None
    assert stored.split("-")[1] == f"{submit_span.context.trace_id:032x}"


def _unsettled_and_stale(projection: JobProjection) -> None:
    """A row of a ``render-<render_key>`` run and an aged legacy row: one describe each."""
    unsettled, stale = _legacy(projection), _legacy(projection)
    with psycopg.connect(projection.conninfo) as conn:
        conn.execute(
            "UPDATE render_jobs SET workflow_run_id = %s WHERE id = %s",
            (uuid.uuid4().hex, unsettled.id),
        )
    _aged(projection, stale.id)


async def test_start_does_not_wait_for_the_settle_pass_while_temporal_is_unreachable(
    make_service: ServiceFactory, projection: JobProjection
) -> None:
    """The lazy client's first connect retries for minutes: the boot pass runs in the
    background, so the lifespan finishes at once (review #1066 1.1, 6.1)."""
    # Here, not at the top: the workflow sandbox re-imports this module.
    from scadbuddy.workflows.client import connect_lazily

    _unsettled_and_stale(projection)
    service = make_service(connect_lazily("127.0.0.1:1", "default"), "unused")
    began = time.monotonic()
    try:
        async with asyncio.timeout(10):
            await service.start()
    finally:
        await service.aclose()
    assert time.monotonic() - began < 10


async def test_a_settle_pass_while_temporal_is_unreachable_is_bounded(
    make_service: ServiceFactory, projection: JobProjection
) -> None:
    """Each describe is bounded, and the pass stops at the first that Temporal does not
    answer: the rows wait for the next pass (review #1066 1.1)."""
    from scadbuddy.workflows.client import connect_lazily

    _unsettled_and_stale(projection)
    service = make_service(connect_lazily("127.0.0.1:1", "default"), "unused")
    began = time.monotonic()
    async with asyncio.timeout(60):
        await service.settle()
    assert time.monotonic() - began < 30
    assert all(job.state == "pending" for job in await asyncio.to_thread(projection.list_jobs))


@pytest.mark.parametrize(
    "refusal",
    [
        TemporalUnavailableError("render-x"),
        RPCError("denied", RPCStatusCode.PERMISSION_DENIED, b""),
    ],
)
async def test_a_render_start_that_fails_counts_as_a_start_workflow_error(
    make_service: ServiceFactory, monkeypatch: pytest.MonkeyPatch, refusal: Exception
) -> None:
    """Every refused start is counted, not only logged (review #1066 4.1)."""

    async def refusing(*_: object, **__: Any) -> RenderAnswer:
        raise refusal

    monkeypatch.setattr(submit_module, "start_command", refusing)
    async with temporal_client() as client:
        service = make_service(client, f"t-{uuid.uuid4().hex[:8]}")
        with pytest.raises(type(refusal)):
            await service.submit(SLUG, {"width": _w()})
        await service.aclose()

    errors = service.metrics.registry.get_sample_value(
        "scadbuddy_render_store_errors_total", {"operation": "start_workflow"}
    )
    assert errors == 1


@pytest.mark.parametrize(
    "pending", [CommandStillAcceptingError("render-x"), CommandClosedError("render-x")]
)
async def test_a_start_still_accepting_counts_as_pending_not_as_an_error(
    make_service: ServiceFactory, monkeypatch: pytest.MonkeyPatch, pending: Exception
) -> None:
    """The client sends the same request again: nothing failed, so the start_workflow
    errors stay an alert on Temporal itself (review #1066 (5) 3.1)."""

    async def accepting(*_: object, **__: Any) -> RenderAnswer:
        raise pending

    monkeypatch.setattr(submit_module, "start_command", accepting)
    async with temporal_client() as client:
        service = make_service(client, f"t-{uuid.uuid4().hex[:8]}")
        with pytest.raises(type(pending)):
            await service.submit(SLUG, {"width": _w()})
        await service.aclose()

    sample = service.metrics.registry.get_sample_value
    assert sample("scadbuddy_render_store_errors_total", {"operation": "start_workflow"}) == 0
    assert sample("scadbuddy_render_accept_pending_total") == 1
