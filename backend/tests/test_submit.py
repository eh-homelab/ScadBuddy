"""RenderService: submit -> start_workflow -> reconcile, over a dev server and a real
projection, with the openscad activities faked."""

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

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from scadbuddy.core.config import load_config
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.pg_listener import PgListener
from scadbuddy.core.problems import ApiError
from scadbuddy.library.assets import AssetStore
from scadbuddy.render.job_models import Job, JobNotFoundError, now, render_key
from scadbuddy.render.jobs import SnapshotUnavailableError
from scadbuddy.render.projection import JobProjection, workflow_id_for
from scadbuddy.render.schema import ParamValue
from scadbuddy.render.submit import MAX_WORKFLOW_INPUT_BYTES, RenderService
from scadbuddy.store import BlobRefs
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.workflows.activities import RenderActivities, WorkerDeps
from scadbuddy.workflows.models import Projection
from scadbuddy.workflows.pipelines import RenderPreview
from tests.support.temporal import temporal_client
from tests.test_workflows import FakeActivities, _worker

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

SLUG = "demo"


class ProjectingActivities(FakeActivities):
    """The fakes, but `project` writes the row as the real activity does."""

    def __init__(self, deps: WorkerDeps, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._real = RenderActivities(deps)

    @activity.defn(name="project")
    async def project(self, projection: Projection) -> None:
        await super().project(projection)
        await self._real.project(projection)


def _sample(metrics: Metrics, name: str) -> float:
    return metrics.registry.get_sample_value(name) or 0.0


async def _settled(projection: JobProjection, job_id: str, timeout: float = 30) -> Job:
    async with asyncio.timeout(timeout):
        while True:
            job = await asyncio.to_thread(projection.read, job_id)
            if job.state in ("done", "failed", "cancelled"):
                return job
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
        return RenderService(
            projection=projection,
            client=client,
            task_queue=task_queue,
            config=deps.config,
            paths=deps.paths,
            metrics=Metrics(),
            **kwargs,
        )

    return make


async def test_a_submit_starts_the_workflow_its_row_names(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        async with _worker(client, queue, ProjectingActivities(deps)):
            job = await service.submit(SLUG, {"width": 1})
            assert job.inputs == {"params": {"width": 1}, "v": 0}
            # The workflow the row names runs to its end.
            await asyncio.wait_for(
                client.get_workflow_handle(workflow_id_for(job.id)).result(), timeout=30
            )
        await service.aclose()
        done = await asyncio.to_thread(projection.read, job.id)

    assert done.state == "done", done.error
    assert done.result is not None
    assert _sample(service.metrics, "scadbuddy_render_jobs_submitted_total") == 1


async def test_a_submit_whose_start_failed_is_started_by_the_reconciler(
    make_service: ServiceFactory,
    deps: WorkerDeps,
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue, reconcile_after=0.5, reconcile_interval=0.1)

        async def unavailable(*_: object, **__: object) -> None:
            raise RuntimeError("temporal is down")

        with monkeypatch.context() as patched:
            patched.setattr(client, "start_workflow", unavailable)
            job = await service.submit(SLUG, {"width": 2})
        # The row is committed and waits for its workflow.
        assert (await asyncio.to_thread(projection.read, job.id)).state == "pending"
        assert (
            service.metrics.registry.get_sample_value(
                "scadbuddy_render_store_errors_total", {"operation": "start_workflow"}
            )
            == 1
        )

        async with _worker(client, queue, ProjectingActivities(deps)):
            await service.start()
            try:
                done = await _settled(projection, job.id)
            finally:
                await service.aclose()

    assert done.state == "done", done.error


async def test_superseding_the_last_claim_cancels_the_workflow(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        gate = asyncio.Event()
        acts = ProjectingActivities(deps, block_solids=gate)
        async with _worker(client, queue, acts):
            first = await service.submit(SLUG, {"width": 3})
            async with asyncio.timeout(30):
                while "render_solids" not in acts.calls:
                    await asyncio.sleep(0.05)
            second = await service.submit(SLUG, {"width": 4}, supersedes=first.id)
            handle = client.get_workflow_handle(workflow_id_for(first.id))
            async with asyncio.timeout(30):
                while (await handle.describe()).status == WorkflowExecutionStatus.RUNNING:
                    await asyncio.sleep(0.05)
            gate.set()
            cancelled = await _settled(projection, first.id)
            done = await _settled(projection, second.id)
        described = await handle.describe()
        await service.aclose()

    assert cancelled.state == "cancelled"
    assert described.status == WorkflowExecutionStatus.CANCELED
    assert done.state == "done", done.error


async def test_an_identical_submit_coalesces_and_starts_nothing_new(
    make_service: ServiceFactory, projection: JobProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue, reconcile_after=0.0)
        started: list[str] = []
        start = client.start_workflow

        async def counting(*args: Any, **kwargs: Any) -> Any:
            started.append(kwargs["id"])
            return await start(*args, **kwargs)

        monkeypatch.setattr(client, "start_workflow", counting)
        # No worker: the first job stays pending, so the second is answered by it.
        first = await service.submit(SLUG, {"width": 5})
        second = await service.submit(SLUG, {"width": 5})
        submitted = list(started)
        # Pending, but its workflow is running (waiting for a worker): not restarted.
        reconciled = await service.reconcile_once()
        await service.aclose()

    assert second.id == first.id
    assert submitted == [workflow_id_for(first.id)]
    assert reconciled == 0
    assert _sample(service.metrics, "scadbuddy_render_jobs_coalesced_total") == 1
    assert (await asyncio.to_thread(projection.read, first.id)).claims == 2


async def test_a_row_whose_start_fails_does_not_stop_the_next_one(
    make_service: ServiceFactory, projection: JobProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue, reconcile_after=0.0)
        with monkeypatch.context() as patched:

            async def unavailable(*_: object, **__: object) -> None:
                raise RuntimeError("temporal is down")

            patched.setattr(client, "start_workflow", unavailable)
            first = await service.submit(SLUG, {"width": 6})
            second = await service.submit(SLUG, {"width": 7})

        start = client.start_workflow

        async def refuses_the_first(*args: Any, **kwargs: Any) -> Any:
            if kwargs["id"] == workflow_id_for(first.id):
                raise RuntimeError("this payload is refused")
            return await start(*args, **kwargs)

        monkeypatch.setattr(client, "start_workflow", refuses_the_first)
        # A failed first pass does not stop the service from starting.
        await service.start()
        await service.aclose()
        second_run = await client.get_workflow_handle(workflow_id_for(second.id)).describe()

    assert second_run.status == WorkflowExecutionStatus.RUNNING
    assert (
        service.metrics.registry.get_sample_value(
            "scadbuddy_render_store_errors_total", {"operation": "start_workflow"}
        )
        == 3
    )


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


async def test_a_cancel_that_fails_is_a_warning_and_the_supersede_still_succeeds(
    make_service: ServiceFactory,
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with temporal_client() as client:
        service = make_service(client, f"t-{uuid.uuid4().hex[:8]}")
        first = await service.submit(SLUG, {"width": 11})
        cancelled = _spy_cancel(
            monkeypatch, client, RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")
        )
        with caplog.at_level(logging.WARNING, logger="scadbuddy.render.submit"):
            second = await service.submit(SLUG, {"width": 12}, supersedes=first.id)
        await service.aclose()

    assert second.id != first.id and second.state == "pending"
    assert cancelled == [workflow_id_for(first.id)]
    assert (await asyncio.to_thread(projection.read, first.id)).state == "cancelled"
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warned and getattr(warned[0], "job_id", None) == first.id
    assert (
        service.metrics.registry.get_sample_value(
            "scadbuddy_render_store_errors_total", {"operation": "cancel_workflow"}
        )
        == 1
    )


async def test_a_cancel_that_fails_with_anything_else_never_fails_the_supersede(
    make_service: ServiceFactory,
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with temporal_client() as client:
        service = make_service(client, f"t-{uuid.uuid4().hex[:8]}")
        first = await service.submit(SLUG, {"width": 17})
        _spy_cancel(monkeypatch, client, ValueError("not an RPC error"))
        with caplog.at_level(logging.WARNING, logger="scadbuddy.render.submit"):
            second = await service.submit(SLUG, {"width": 18}, supersedes=first.id)
        await service.aclose()

    assert second.id != first.id and second.state == "pending"
    assert (await asyncio.to_thread(projection.read, first.id)).state == "cancelled"
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warned and getattr(warned[0], "job_id", None) == first.id
    assert getattr(warned[0], "error_type", None) == "ValueError"
    assert (
        service.metrics.registry.get_sample_value(
            "scadbuddy_render_store_errors_total", {"operation": "cancel_workflow"}
        )
        == 1
    )


async def test_a_cancel_of_a_workflow_that_never_started_is_not_an_error(
    make_service: ServiceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with temporal_client() as client:
        service = make_service(client, f"t-{uuid.uuid4().hex[:8]}")
        first = await service.submit(SLUG, {"width": 13})
        _spy_cancel(monkeypatch, client, RPCError("not found", RPCStatusCode.NOT_FOUND, b""))
        await service.submit(SLUG, {"width": 14}, supersedes=first.id)
        await service.aclose()

    assert not service.metrics.registry.get_sample_value(
        "scadbuddy_render_store_errors_total", {"operation": "cancel_workflow"}
    )


async def test_cancelling_the_last_claim_cancels_the_workflow(
    make_service: ServiceFactory, projection: JobProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with temporal_client() as client:
        service = make_service(client, f"t-{uuid.uuid4().hex[:8]}")
        job = await service.submit(SLUG, {"width": 15})
        cancelled = _spy_cancel(monkeypatch, client)
        withdrawn = await service.cancel(job.id, slug=SLUG)
        await service.aclose()

    assert withdrawn is not None and withdrawn.id == job.id
    assert withdrawn.state == "cancelled"
    assert cancelled == [workflow_id_for(job.id)]


async def test_cancelling_one_of_two_claims_leaves_the_workflow_running(
    make_service: ServiceFactory, projection: JobProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with temporal_client() as client:
        service = make_service(client, f"t-{uuid.uuid4().hex[:8]}")
        job = await service.submit(SLUG, {"width": 16})
        assert (await service.submit(SLUG, {"width": 16})).id == job.id
        cancelled = _spy_cancel(monkeypatch, client)
        withdrawn = await service.cancel(job.id, slug=SLUG)
        await service.aclose()

    assert withdrawn is None
    assert cancelled == []
    stored = await asyncio.to_thread(projection.read, job.id)
    assert (stored.state, stored.claims) == ("pending", 1)


async def test_cancelling_an_unknown_job_touches_nothing(
    make_service: ServiceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with temporal_client() as client:
        service = make_service(client, f"t-{uuid.uuid4().hex[:8]}")
        cancelled = _spy_cancel(monkeypatch, client)
        withdrawn = await service.cancel(uuid.uuid4().hex, slug=SLUG)
        await service.aclose()

    assert withdrawn is None
    assert cancelled == []


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


async def test_a_start_that_can_never_succeed_fails_the_job_and_is_not_retried(
    make_service: ServiceFactory, projection: JobProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with temporal_client() as client:
        service = make_service(client, f"t-{uuid.uuid4().hex[:8]}", reconcile_after=0.0)
        attempts: list[str] = []

        async def refused(*_: object, **kwargs: Any) -> None:
            attempts.append(kwargs["id"])
            raise RPCError("Blob data size exceeds limit", RPCStatusCode.INVALID_ARGUMENT, b"")

        monkeypatch.setattr(client, "start_workflow", refused)
        job = await service.submit(SLUG, {"width": 8})
        reconciled = await service.reconcile_once()
        await service.aclose()

    stored = await asyncio.to_thread(projection.read, job.id)
    assert stored.state == "failed"
    assert stored.error is not None and "Blob data size exceeds limit" in stored.error
    assert attempts == [workflow_id_for(job.id)]
    assert reconciled == 0


async def test_a_reconciled_start_that_can_never_succeed_fails_the_job(
    make_service: ServiceFactory, projection: JobProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with temporal_client() as client:
        service = make_service(client, f"t-{uuid.uuid4().hex[:8]}", reconcile_after=0.0)
        with monkeypatch.context() as patched:
            # Transient, both: FAILED_PRECONDITION is how Temporal answers a namespace
            # that is not active (yet), so the row waits for the reconciler.
            statuses = iter([RPCStatusCode.UNAVAILABLE, RPCStatusCode.FAILED_PRECONDITION])

            async def transient(*_: object, **__: object) -> None:
                raise RPCError("not now", next(statuses), b"")

            patched.setattr(client, "start_workflow", transient)
            job = await service.submit(SLUG, {"width": 9})
            assert await service.reconcile_once() == 0
        assert (await asyncio.to_thread(projection.read, job.id)).state == "pending"

        async def no_namespace(*_: object, **__: object) -> None:
            raise RPCError("Namespace scadbuddy is not found.", RPCStatusCode.NOT_FOUND, b"")

        monkeypatch.setattr(client, "start_workflow", no_namespace)
        await service.reconcile_once()
        await service.aclose()

    stored = await asyncio.to_thread(projection.read, job.id)
    assert stored.state == "failed"
    assert stored.error is not None and "not found" in stored.error


async def test_settled_jobs_past_their_ttl_are_pruned_without_a_restart(
    projection: JobProjection, deps: WorkerDeps
) -> None:
    old = _job_row(finished_ago=timedelta(days=2))
    projection.submit(old, render_key(old.slug, old.params, None))
    old.state, old.finished_at = "done", now() - timedelta(days=2)
    assert projection.finish(old)
    fresh = _job_row(finished_ago=timedelta(0))
    projection.submit(fresh, render_key(fresh.slug, fresh.params, None))

    async with temporal_client() as client:
        service = RenderService(
            projection=projection,
            client=client,
            task_queue=f"t-{uuid.uuid4().hex[:8]}",
            config=replace(deps.config, job_ttl=86400.0),
            paths=deps.paths,
            metrics=Metrics(),
            reconcile_after=3600.0,
            reconcile_interval=0.05,
            prune_interval=0.0,
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

    assert (await asyncio.to_thread(projection.read, fresh.id)).state == "pending"


def _job_row(*, finished_ago: timedelta) -> Job:
    params: dict[str, ParamValue] = {"width": uuid.uuid4().int % 1000}
    return Job(
        id=uuid.uuid4().hex,
        slug=SLUG,
        params=params,
        inputs={"params": params},
        created_at=now() - finished_ago,
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
    make_service: ServiceFactory, projection: JobProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with temporal_client() as client:
        service = make_service(client, f"t-{uuid.uuid4().hex[:8]}")
        pinned = _PinnedSnapshots("f" * 40)
        service.snapshots = pinned  # type: ignore[assignment]

        async def unavailable(*_: object, **__: object) -> None:
            raise RuntimeError("no workflow in this test")

        with monkeypatch.context() as patched:
            patched.setattr(client, "start_workflow", unavailable)
            job = await service.submit(SLUG, {"width": 3})
        await service.aclose()
    assert pinned.asked == [(SLUG, None)]
    assert job.model_version == "f" * 40
    assert (await asyncio.to_thread(projection.read, job.id)).model_version == "f" * 40
