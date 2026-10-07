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
from typing import Any, NoReturn

import psycopg
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from temporalio import activity
from temporalio.api.enums.v1 import IndexedValueType
from temporalio.api.operatorservice.v1 import AddSearchAttributesRequest
from temporalio.client import (
    Client,
    WorkflowExecutionStatus,
    WorkflowFailureError,
    WorkflowUpdateFailedError,
)
from temporalio.exceptions import ApplicationError, TimeoutType
from temporalio.exceptions import TimeoutError as TemporalTimeoutError
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
    run_closed,
    workflow_id_for,
    workflow_id_for_key,
)
from scadbuddy.render.schema import ParamValue
from scadbuddy.render.submit import MAX_WORKFLOW_INPUT_BYTES, RPC_TIMEOUT, RenderService
from scadbuddy.store import BlobRefs
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.workflows import commands as commands_module
from scadbuddy.workflows.activities import RenderActivities, WorkerDeps
from scadbuddy.workflows.commands import (
    CommandClosedError,
    CommandStillAcceptingError,
    TemporalBusyError,
    TemporalRefusedError,
    TemporalUnavailableError,
    temporal_failure,
)
from scadbuddy.workflows.models import (
    ACCEPT_ACTIVITY,
    CLAIMS_ACTIVITY,
    RELEASE_UPDATE,
    RENDER_UNSTARTABLE,
    AcceptRender,
    MigrateRequest,
    MigrateResult,
    Projection,
    ReleaseAnswer,
    RenderAnswer,
)
from scadbuddy.workflows.pipelines import (
    ACCEPT_RETRY,
    KIND,
    MIGRATE_EXECUTION_TIMEOUT,
    STATUS,
    SUBJECT,
    RenderPiece,
    RenderPreview,
    TemplatePipeline,
)
from tests.support.pipelines import FakeWorld
from tests.support.renders import legacy_row, namespace_not_found
from tests.support.temporal import temporal_client
from tests.test_template_pipeline import NEVER_YIELDS
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
    async def project(self, projection: Projection) -> bool:
        if projection.state == "running" and self.hold_running is not None:
            await self.hold_running.wait()
        await super().project(projection)
        return await self._real.project(projection)


#: The render series of a counter labelled by job kind.
RENDER = {"kind": "render"}


def _sample(metrics: Metrics, name: str, labels: dict[str, str] | None = None) -> float:
    return metrics.registry.get_sample_value(name, labels) or 0.0


def _settle_failed(metrics: Metrics, settle_pass: str) -> float:
    sample = "scadbuddy_render_settle_failed_total"
    return metrics.registry.get_sample_value(sample, {"pass": settle_pass}) or 0.0


def _settle_errors(metrics: Metrics, settle_pass: str) -> float:
    sample = "scadbuddy_render_settle_errors_total"
    return metrics.registry.get_sample_value(sample, {"pass": settle_pass}) or 0.0


def _failed_jobs(metrics: Metrics) -> float:
    sample = "scadbuddy_render_jobs_finished_total"
    return metrics.registry.get_sample_value(sample, {"outcome": "failed"}) or 0.0


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
    assert _sample(service.metrics, "scadbuddy_render_jobs_submitted_total", RENDER) == 1


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
    assert _sample(service.metrics, "scadbuddy_render_jobs_coalesced_total", RENDER) == 1


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
    assert _sample(service.metrics, "scadbuddy_render_jobs_submitted_total", RENDER) == 2


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


async def test_a_release_blocked_in_the_workflow_does_not_hold_the_submit(
    make_service: ServiceFactory,
    deps: WorkerDeps,
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last release waits in the workflow for the cancelled job's projection. Held
    there, the submit still answers with the new job inside its bound, below Envoy's 15 s
    route timeout (review #1066 (8) 1). `rpc_timeout` bounds each poll, not the Update
    (the SDK polls again), so a long one stands in for a server that keeps answering
    polls with no outcome."""
    monkeypatch.setattr(submit_module, "RPC_TIMEOUT", timedelta(seconds=60))
    monkeypatch.setattr(submit_module, "RELEASE_BOUND", 1.0, raising=False)
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        gate, held = asyncio.Event(), asyncio.Event()
        acts = ProjectingActivities(deps, block_main=gate, block_cancelled=held)
        async with _worker(client, queue, acts):
            old = await service.submit(SLUG, {"width": _w()})
            began = time.monotonic()
            async with asyncio.timeout(30):
                new = await service.submit(
                    SLUG, {"width": _w()}, supersedes=old.id, request_id=uuid.uuid4().hex
                )
            took = time.monotonic() - began
            held.set()
            gate.set()
            await _settled(projection, old.id)
            await _settled(projection, new.id)
        await service.aclose()

    assert new.id != old.id
    print("TOOK", took)
    assert took < 5


def _classified(error: RPCError, id: str) -> NoReturn:
    """Raise ``error`` as `start_command` does: classified, the RPC error its cause."""
    failure = temporal_failure(error, id)
    assert failure is not None
    raise failure from error


class _Described:
    """A client whose `describe` finds the execution, or answers NOT_FOUND."""

    def __init__(self, *, exists: bool) -> None:
        self.exists = exists

    def get_workflow_handle(self, *_: object, **__: object) -> _Described:
        return self

    async def describe(self, **__: object) -> None:
        if not self.exists:
            raise RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b"")


@pytest.mark.parametrize(
    ("exists", "raised", "counted"),
    [
        (True, CommandStillAcceptingError, "scadbuddy_render_accept_pending_total"),
        (False, TemporalUnavailableError, "scadbuddy_render_store_errors_total"),
    ],
)
async def test_a_submit_is_answered_within_one_bound(
    make_service: ServiceFactory,
    monkeypatch: pytest.MonkeyPatch,
    exists: bool,
    raised: type[Exception],
    counted: str,
) -> None:
    """One deadline covers every Temporal call a submit makes (review #1066 (8) 1).
    Past it the request is still accepting when its execution exists, and Temporal
    unavailable when it cannot say: a client that never connected queued nothing."""

    async def hanging(*_: object, **__: Any) -> RenderAnswer:
        await asyncio.Event().wait()
        raise AssertionError("never answered")

    monkeypatch.setattr(submit_module, "start_command", hanging)
    monkeypatch.setattr(submit_module, "SUBMIT_DEADLINE", 0.5)
    service = make_service(_Described(exists=exists), f"t-{uuid.uuid4().hex[:8]}")
    async with asyncio.timeout(30):
        with pytest.raises(raised):
            await service.submit(SLUG, {"width": _w()})
    await service.aclose()

    assert (
        service.metrics.registry.get_sample_value(
            counted, {"operation": "start_workflow"} if not exists else None
        )
        == 1
    )


@pytest.mark.parametrize("code", [RPCStatusCode.DEADLINE_EXCEEDED, RPCStatusCode.CANCELLED])
@pytest.mark.parametrize(
    ("exists", "raised"),
    [(True, CommandStillAcceptingError), (False, TemporalUnavailableError)],
)
async def test_a_start_that_grpc_ended_is_answered_as_a_late_one(
    make_service: ServiceFactory,
    monkeypatch: pytest.MonkeyPatch,
    code: RPCStatusCode,
    exists: bool,
    raised: type[Exception],
) -> None:
    """gRPC ending the call says nothing of the start, which may have reached Temporal:
    still accepting when the execution exists, never "nothing was done" (review #1066
    (8) 2)."""

    async def ended(*_: object, **kwargs: Any) -> RenderAnswer:
        _classified(RPCError("ended", code, b""), kwargs["id"])

    monkeypatch.setattr(submit_module, "start_command", ended)
    service = make_service(_Described(exists=exists), "unused")
    with pytest.raises(raised) as error:
        await service.submit(SLUG, {"width": _w()})
    await service.aclose()

    assert isinstance(error.value.__cause__, RPCError)


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


class _BrokenAccept(ProjectingActivities):
    """`render_accept` fails every attempt, as a SQL error the code does not expect."""

    @activity.defn(name=ACCEPT_ACTIVITY)
    async def render_accept(self, accept: AcceptRender) -> Job:
        self.accepts += 1
        raise RuntimeError("relation render_jobs has no column x")


async def test_an_accept_that_keeps_failing_is_answered_unstartable_within_the_deadline(
    make_service: ServiceFactory, deps: WorkerDeps
) -> None:
    """The first step's retries are bounded: past them the run completes and its
    Update answers the refusal, so the request gets an answer rather than looping on
    `command-still-accepting` with no row (review #1066 (10) 1)."""
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        acts = _BrokenAccept(deps)
        params: dict[str, ParamValue] = {"width": _w()}
        async with _worker(client, queue, acts):
            began = time.monotonic()
            with pytest.raises(WorkflowUpdateFailedError) as refused:
                await service.submit(SLUG, params)
            took = time.monotonic() - began
            handle = client.get_workflow_handle(workflow_id_for_key(render_key(SLUG, params, None)))
            await asyncio.wait_for(handle.result(), timeout=30)
            described = await handle.describe()
        await service.aclose()

    assert took < submit_module.SUBMIT_DEADLINE
    assert described.status == WorkflowExecutionStatus.COMPLETED
    cause = refused.value.cause
    assert isinstance(cause, ApplicationError) and cause.type == RENDER_UNSTARTABLE
    assert acts.accepts == ACCEPT_RETRY.maximum_attempts


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
            # Another job waits, so the queue is full without the one superseded.
            _legacy(projection)
            with pytest.raises(QueueFullError) as refused:
                await service.submit(SLUG, {"width": _w()}, supersedes=first.id)
            waiting = await asyncio.to_thread(projection.read, first.id)
            hold.set()
            await _settled(projection, first.id)
        await service.aclose()

    # No render has finished yet: the initial estimate (#603).
    assert refused.value.depth == 1 and refused.value.retry_after == 10
    assert waiting.state == "pending" and waiting.claims == 1
    assert _sample(service.metrics, "scadbuddy_render_jobs_rejected_total", RENDER) == 1


async def test_a_supersede_never_needs_a_slot_held_by_the_job_it_replaces(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    """With the queue full of the caller's own stale preview, the request replacing it
    is accepted, and that preview is superseded (review #1066 (9) 3)."""
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue, config=replace(deps.config, render_queue_max=1))
        gate = asyncio.Event()
        acts = ProjectingActivities(deps, block_main=gate)
        async with _worker(client, queue, acts, max_concurrent_activities=1):
            # A running job holds the worker's one activity slot, so the next one's
            # `running` write waits for it, and that job stays pending.
            busy = await service.submit(SLUG, {"width": _w()})
            await _until(acts, "render_main")
            first = await service.submit(SLUG, {"width": _w()})
            second = await service.submit(SLUG, {"width": _w()}, supersedes=first.id)
            dropped = await _settled(projection, first.id)
            gate.set()
            done = await _settled(projection, second.id)
            await _settled(projection, busy.id)
        await service.aclose()

    assert (dropped.state, dropped.error) == ("cancelled", SUPERSEDED_ERROR)
    assert done.state == "done", done.error
    assert _sample(service.metrics, "scadbuddy_render_jobs_rejected_total", RENDER) == 0


async def test_a_request_answered_closing_and_resent_after_the_close_gets_a_fresh_job(
    make_service: ServiceFactory,
    deps: WorkerDeps,
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slider dragged from A to B and back: A's request reaches A's run while B's
    supersede is releasing it, and is answered still-accepting. Sent again with its key
    once the run has closed, it starts a fresh render of A, rather than being answered
    from the closed run (review #1066 (7) 1)."""
    monkeypatch.setattr(submit_module, "CLOSING_WAIT", 0.5)
    a = {"width": _w()}
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        gate, projecting = asyncio.Event(), asyncio.Event()
        acts = ProjectingActivities(deps, block_main=gate, block_cancelled=projecting)
        async with _worker(client, queue, acts):
            first = await service.submit(SLUG, a, request_id=uuid.uuid4().hex)
            await _until(acts, "render_main")
            superseding = asyncio.create_task(
                service.submit(
                    SLUG, {"width": _w()}, supersedes=first.id, request_id=uuid.uuid4().hex
                )
            )
            # The release waits for the cancelled job to project (held here).
            async with asyncio.timeout(30):
                while not [
                    p for p in acts.projections if p.state == "cancelled" and p.job_id == first.id
                ]:
                    await asyncio.sleep(0.01)
            request = uuid.uuid4().hex
            with pytest.raises(CommandStillAcceptingError):
                await service.submit(SLUG, a, request_id=request)
            projecting.set()
            second = await asyncio.wait_for(superseding, timeout=30)
            assert first.workflow_id is not None
            await asyncio.wait_for(
                client.get_workflow_handle(
                    first.workflow_id, run_id=first.workflow_run_id
                ).result(),
                timeout=30,
            )
            again = await service.submit(SLUG, a, request_id=request)
            gate.set()
            await _settled(projection, again.id)
            await _settled(projection, second.id)
        await service.aclose()

    assert again.id != first.id and again.workflow_run_id != first.workflow_run_id
    assert (await asyncio.to_thread(projection.read, first.id)).state == "cancelled"


async def test_an_update_aborted_by_a_closing_execution_starts_again(
    make_service: ServiceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = Job(id=uuid.uuid4().hex, slug=SLUG, created_at=now())
    calls: list[str] = []

    async def answering(*_: object, **kwargs: Any) -> RenderAnswer:
        calls.append(str(kwargs["id"]))
        if len(calls) == 1:
            _classified(
                RPCError(
                    "workflow update was aborted by closing workflow",
                    RPCStatusCode.NOT_FOUND,
                    b"",
                ),
                kwargs["id"],
            )
        return RenderAnswer(job=job)

    monkeypatch.setattr(submit_module, "start_command", answering)
    async with temporal_client() as client:
        service = make_service(client, f"t-{uuid.uuid4().hex[:8]}")
        submitted = await service.submit(SLUG, {"width": _w()})
        await service.aclose()

    assert submitted.id == job.id and len(calls) == 2


async def test_an_update_aborted_twice_by_closing_executions_is_still_accepting(
    make_service: ServiceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Losing the closing race on both attempts is the other outcome of the same race
    as a `CLOSING` rejection: 503 still-accepting, never a configuration error (review
    #1066 (11) 1)."""
    monkeypatch.setattr(submit_module, "CLOSING_WAIT", 0.1)
    calls: list[str] = []

    async def aborting(*_: object, **kwargs: Any) -> RenderAnswer:
        calls.append(str(kwargs["id"]))
        _classified(
            RPCError(
                "workflow update was aborted by closing workflow", RPCStatusCode.NOT_FOUND, b""
            ),
            kwargs["id"],
        )

    monkeypatch.setattr(submit_module, "start_command", aborting)
    async with temporal_client() as client:
        service = make_service(client, f"t-{uuid.uuid4().hex[:8]}")
        with pytest.raises(CommandStillAcceptingError):
            await service.submit(SLUG, {"width": _w()})
        await service.aclose()

    assert len(calls) == 2
    assert _sample(service.metrics, "scadbuddy_render_accept_pending_total") == 1


class FakePreview:
    """`render_preview_png` by name, holding every call until released."""

    def __init__(self) -> None:
        self.calls = 0
        self.revisions: list[str | None] = []
        self.priorities: list[int | None] = []
        self.release = asyncio.Event()

    @activity.defn(name="render_preview_png")
    async def render_preview_png(self, slug: str, revision: str | None = None) -> bytes:
        self.calls += 1
        self.revisions.append(revision)
        self.priorities.append(activity.info().priority.priority_key)
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
    # #603 (M5): previews share the render queue, so a boot-time pass of them must not
    # hold user renders back. They run at a lower priority than a render's default.
    assert fake.priorities == [submit_module.PREVIEW_PRIORITY]
    assert submit_module.PREVIEW_PRIORITY > 3  # Temporal's default; higher numbers wait


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


async def _register_search_attributes(client: Client) -> None:
    """§4.2's keywords, as the cluster's operator registers them."""
    request = AddSearchAttributesRequest(
        namespace=client.namespace,
        search_attributes={
            key.name: IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD for key in (KIND, SUBJECT, STATUS)
        },
    )
    with suppress(RPCError):  # registered already, on a shared server
        await client.operator_service.add_search_attributes(request)


class _HeldDone(ProjectingActivities):
    """The `done` projection waits for ``release`` once it has written the row."""

    def __init__(self, deps: WorkerDeps, *, release: asyncio.Event, **kwargs: Any):
        super().__init__(deps, **kwargs)
        self.release = release

    @activity.defn(name="project")
    async def project(self, projection: Projection) -> bool:
        open_row = await super().project(projection)
        if projection.state == "done":
            await self.release.wait()
        return open_row


async def test_a_release_after_the_render_finished_cancels_nothing(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    """The last release reaching a job whose `done` is written (in flight, or already
    back) cancels nothing and says so (review #1066 2.3)."""
    async with temporal_client() as client:
        await _register_search_attributes(client)
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue, search_attributes=True)
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
            described = await handle.describe()
        await service.aclose()

    assert done.state == "done"
    assert answer.cancelled is None
    # Visibility says what the row says (review #1066 (9) 2).
    assert described.typed_search_attributes.get(STATUS) == "settled"
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
    deadline = defaults["deadline"]
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
            # Only that submit is meant to outlive its deadline: on a loaded machine a
            # fresh start's accept can take over the 1 s, and the supersede below must
            # not be answered still-accepting.
            defaults["deadline"] = deadline
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


async def test_a_request_resent_after_its_render_closed_is_answered_with_its_job(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    """The key is the `accepted` Update's id. Sent again once ``render-<key>`` has
    closed, update-with-start answers it with the closed run's outcome rather than a
    new run: the same job, not a second one (review #1066 (6) 2)."""
    width = _w()
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        acts = ProjectingActivities(deps)
        async with _worker(client, queue, acts):
            request = uuid.uuid4().hex
            first = await service.submit(SLUG, {"width": width}, request_id=request)
            await _settled(projection, first.id)
            assert first.workflow_id is not None
            await asyncio.wait_for(
                client.get_workflow_handle(first.workflow_id).result(), timeout=30
            )
            again = await service.submit(SLUG, {"width": width}, request_id=request)
        await service.aclose()

    assert (again.id, again.workflow_run_id) == (first.id, first.workflow_run_id)
    assert acts.accepts == 1


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


@pytest.mark.parametrize(("fail_main", "state"), [(False, "done"), (True, "failed")])
async def test_an_older_builds_start_renders_its_row_on_this_worker(
    deps: WorkerDeps, projection: JobProjection, fail_main: bool, state: str
) -> None:
    """During a rolling deploy an older API inserts its row and starts `render-<job id>`
    with the `Job` itself: this build's worker decodes it (`RenderStart | Job`), settles
    the row and completes the run (review #1066 (9) 1)."""
    job = _legacy(projection)
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        async with _worker(client, queue, ProjectingActivities(deps, fail_main=fail_main)):
            handle = await client.start_workflow(
                "TemplatePipeline",
                job.model_dump(mode="json"),
                id=workflow_id_for(job.id),
                task_queue=queue,
            )
            await asyncio.wait_for(handle.result(), timeout=60)
            described = await handle.describe()

    stored = await asyncio.to_thread(projection.read, job.id)
    assert stored.state == state, stored.error
    assert described.status == WorkflowExecutionStatus.COMPLETED


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
    # Counted, so an operator can alert on it (review #1066 (9) 5).
    assert _settle_failed(service.metrics, "legacy") == 3
    assert _failed_jobs(service.metrics) == 3
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


async def test_a_database_outage_at_the_accept_is_still_accepting_not_a_500(
    make_service: ServiceFactory,
    deps: WorkerDeps,
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Postgres out of reach past the accept's retries is transient, as an older build's
    row is: the request is still accepting, not a 500 render-unstartable that tells
    clients not to send it again (review #1066 (11), the same class as 2)."""

    def unreachable(*_: object, **__: object) -> Job:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(projection, "accept", unreachable)
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        acts = ProjectingActivities(deps)
        async with _worker(client, queue, acts):
            with pytest.raises(CommandStillAcceptingError):
                await service.submit(SLUG, {"width": _w()}, request_id=uuid.uuid4().hex)
        await service.aclose()

    assert acts.accepts >= ACCEPT_RETRY.maximum_attempts


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


async def test_a_legacy_row_whose_workflow_runs_answers_still_accepting_past_the_retries(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    """A legacy row on the key whose older build's workflow still runs (a draining
    worker's) outlives `ACCEPT_RETRY`: the request is still accepting, re-sent with its
    key, never a 500 `render-unstartable` (review #1066 (11) 2)."""
    params: dict[str, ParamValue] = {"width": _w()}
    old = legacy_row(
        projection, Job(id=uuid.uuid4().hex, slug=SLUG, params=params, created_at=now())
    )
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        # The old build's execution: on a queue no worker polls, so it stays running.
        legacy = await client.start_workflow(
            "TemplatePipeline", id=workflow_id_for(old.id), task_queue=f"{queue}-old"
        )
        async with _worker(client, queue, ProjectingActivities(deps)):
            with pytest.raises(CommandStillAcceptingError):
                await service.submit(SLUG, params, request_id=uuid.uuid4().hex)
        await legacy.terminate()
        await service.aclose()

    assert _sample(service.metrics, "scadbuddy_render_accept_pending_total") == 1
    assert (await asyncio.to_thread(projection.read, old.id)).state == "pending"


async def test_rows_nothing_will_settle_are_failed_without_a_restart(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    """The housekeeping prune also fails a row whose execution closed without settling
    it (terminated by hand), and a legacy row an older API inserted after this one
    booted (review #1066 1.2). A row whose execution runs is left alone."""
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        await service.start()
        held = asyncio.Event()
        try:
            async with _worker(client, queue, ProjectingActivities(deps, hold_running=held)):
                job = await service.submit(SLUG, {"width": _w()})
                late = _legacy(projection)
                _aged(projection, late.id)
                await service.prune()
                running = await asyncio.to_thread(projection.read, job.id)
                assert job.workflow_id is not None
                await client.get_workflow_handle(
                    job.workflow_id, run_id=job.workflow_run_id
                ).terminate()
                # Past the grace a run's listing in Visibility may trail its start by.
                _aged(projection, job.id)
                await service.prune()
                closed = await _settled(projection, job.id, timeout=10)
                orphan = await _settled(projection, late.id, timeout=10)
                held.set()
        finally:
            await service.aclose()

    assert running.state == "pending"
    assert (closed.state, closed.error) == ("failed", CLOSED_ERROR)
    assert (orphan.state, orphan.error) == ("failed", LEGACY_UNSTARTED_ERROR)


async def test_a_settle_pass_asks_only_about_rows_whose_run_is_not_open(
    make_service: ServiceFactory,
    deps: WorkerDeps,
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One Visibility listing of the open runs, then a describe only for a row whose run
    it does not list: an open run's row costs no call of its own (review #1066 (9) 4)."""
    described: list[str] = []

    async def spy(client: Client, job: Job, **kwargs: Any) -> bool:
        described.append(job.id)
        return await run_closed(client, job, **kwargs)

    monkeypatch.setattr(submit_module, "run_closed", spy)
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue)
        held = asyncio.Event()
        async with _worker(client, queue, ProjectingActivities(deps, hold_running=held)):
            job = await service.submit(SLUG, {"width": _w()})
            _aged(projection, job.id)
            assert job.workflow_id is not None
            handle = client.get_workflow_handle(job.workflow_id, run_id=job.workflow_run_id)
            # Until Visibility lists the run: it may trail the start.
            async with asyncio.timeout(30):
                while not [
                    execution
                    async for execution in client.list_workflows(
                        f"WorkflowId = '{job.workflow_id}' AND ExecutionStatus = 'Running'"
                    )
                ]:
                    await asyncio.sleep(0.1)
            open_pass = await service.settle_closed()
            asked = list(described)
            await handle.terminate()
            async with asyncio.timeout(30):
                while not await service.settle_closed():
                    await asyncio.sleep(0.1)
            held.set()
        await service.aclose()

    assert open_pass == [] and asked == []
    assert described[-1] == job.id
    stored = await asyncio.to_thread(projection.read, job.id)
    assert (stored.state, stored.error) == ("failed", CLOSED_ERROR)
    assert _settle_failed(service.metrics, "closed") == 1
    assert _failed_jobs(service.metrics) == 1


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


async def test_a_missing_namespace_is_not_taken_for_a_closing_execution(
    make_service: ServiceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its NOT_FOUND is a configuration error, raised at once, never waited on and sent
    again (review #1066 3.1)."""
    calls: list[str] = []

    async def refusing(*_: object, **kwargs: Any) -> RenderAnswer:
        calls.append(str(kwargs["id"]))
        _classified(namespace_not_found(), kwargs["id"])

    monkeypatch.setattr(submit_module, "start_command", refusing)
    async with temporal_client() as client:
        service = make_service(client, f"t-{uuid.uuid4().hex[:8]}")
        with pytest.raises(TemporalBusyError):
            await service.submit(SLUG, {"width": _w()})
        await service.aclose()

    assert len(calls) == 1


class _Refusing:
    """A client whose every Update and cancel raises ``error``."""

    def __init__(self, error: RPCError) -> None:
        self.error = error

    def get_workflow_handle(self, *_: object, **__: object) -> _Refusing:
        return self

    async def execute_update(self, *_: object, **__: object) -> None:
        raise self.error


async def test_a_release_to_a_missing_namespace_is_not_taken_for_a_closed_run(
    make_service: ServiceFactory,
) -> None:
    """Its NOT_FOUND is configuration: the release fails (a warning in the supersede),
    never "closed, nothing to release" (review #1066 (7) 2)."""
    service = make_service(_Refusing(namespace_not_found()), "unused")
    job = Job(
        id=uuid.uuid4().hex,
        slug=SLUG,
        created_at=now(),
        workflow_id="render-x",
        workflow_run_id="run-1",
    )
    with pytest.raises(TemporalUnavailableError):
        await service._release(job, None)
    await service.aclose()


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


async def test_settled_jobs_past_their_ttl_are_pruned(
    projection: JobProjection, deps: WorkerDeps
) -> None:
    """The housekeeping Schedule's prune (#1054) calls this every interval."""
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
        )
        await service.prune()

    with pytest.raises(JobNotFoundError):
        await asyncio.to_thread(projection.read, old.id)
    assert (await asyncio.to_thread(projection.read, fresh.id)).state == "done"


async def test_a_prune_that_fails_still_settles_the_rows_nothing_will_settle(
    projection: JobProjection, deps: WorkerDeps, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #1095b 1: a prune whose delete fails (a Postgres error, a read-only
    volume) still fails the rows whose run closed, then fails its activity."""
    orphan = _accepted(projection, finished_ago=timedelta(0))  # its run never existed
    # Past the grace a run's listing in Visibility may trail its start by.
    _aged(projection, orphan.id)

    def failing(ttl: float, **_: object) -> list[str]:
        raise OSError("read-only file system")

    monkeypatch.setattr(projection, "prune", failing)
    async with temporal_client() as client:
        service = RenderService(
            projection=projection,
            client=client,
            task_queue=f"t-{uuid.uuid4().hex[:8]}",
            config=deps.config,
            paths=deps.paths,
            metrics=Metrics(),
        )
        with pytest.raises(OSError, match="read-only"):
            await service.prune()

    settled = await asyncio.to_thread(projection.read, orphan.id)
    assert (settled.state, settled.error) == ("failed", CLOSED_ERROR)


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
    """An aged row of a ``render-<render_key>`` run and an aged legacy row: each pass
    asks Temporal about one."""
    unsettled, stale = _legacy(projection), _legacy(projection)
    with psycopg.connect(projection.conninfo) as conn:
        conn.execute(
            "UPDATE render_jobs SET workflow_run_id = %s WHERE id = %s",
            (uuid.uuid4().hex, unsettled.id),
        )
    _aged(projection, unsettled.id, stale.id)


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
    for settle_pass in ("closed", "legacy"):
        assert _settle_errors(service.metrics, settle_pass) == 1


@pytest.mark.parametrize(
    "refusal",
    [
        TemporalUnavailableError("render-x"),
        TemporalRefusedError("render-x"),
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


class _ProjectingWorld(FakeWorld):
    """FakeWorld, but `project` writes the row, as `ProjectingActivities` does: without
    it the row never leaves `pending` and `settle_closed` never sees it."""

    def __init__(self, source: str, deps: WorkerDeps) -> None:
        super().__init__(source)
        self._real = RenderActivities(deps)

    @activity.defn(name="project")
    async def project(self, projection: Projection) -> bool:
        await super().project(projection)
        return await self._real.project(projection)

    @activity.defn(name=ACCEPT_ACTIVITY)
    async def render_accept(self, accept: AcceptRender) -> Job:
        return await self._real.render_accept(accept)

    @activity.defn(name=CLAIMS_ACTIVITY)
    async def render_claims(self, job_id: str, claims: int) -> None:
        await self._real.render_claims(job_id, claims)

    def activities(self) -> list[Callable[..., Any]]:
        return [*super().activities(), self.render_accept, self.render_claims]


async def test_a_timed_out_pipeline_is_failed_by_the_settle_pass(
    make_service: ServiceFactory,
    deps: WorkerDeps,
    projection: JobProjection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pipeline that never yields times out (`pipeline_timeout`), and the pass that
    fails rows whose run closed without settling them fails its row."""
    monkeypatch.setattr(submit_module, "VISIBILITY_GRACE", timedelta(0))
    world = _ProjectingWorld(NEVER_YIELDS, deps)
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(
            client, queue, config=replace(deps.config, template_activity_max_timeout=2.0)
        )
        assert service.config.pipeline_timeout == 8.0
        async with Worker(
            client,
            task_queue=queue,
            workflows=[TemplatePipeline, RenderPiece],
            activities=world.activities(),
        ):
            job = await service.submit(
                "demo", {}, model_version=None, supersedes=None, inputs={"params": {}, "v": 0}
            )
            async with asyncio.timeout(60):
                while (await asyncio.to_thread(projection.read, job.id)).state not in (
                    "done",
                    "failed",
                ):
                    await service.settle_closed()
                    await asyncio.sleep(1)
    stored = await asyncio.to_thread(projection.read, job.id)
    assert stored.state == "failed"
    # The settle pass's message; the only other way to fail is the pipeline's own
    # raise, should a machine ever finish the loop inside 8 s (N3).
    assert stored.error == CLOSED_ERROR or (stored.error or "").startswith("pipeline/pipeline.py:")


class _MigrationClient:
    """A client whose `execute_workflow` records its calls and answers or raises."""

    def __init__(self, outcome: BaseException | None = None) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def execute_workflow(self, _run: object, req: MigrateRequest, **kwargs: Any) -> Any:
        self.calls.append({"req": req, **kwargs})
        if self.outcome is not None:
            raise self.outcome
        return MigrateResult(inputs=req.inputs, from_version=0, to_version=0)


class _RecordingSnapshots:
    def __init__(self) -> None:
        self.pinned: list[tuple[str, str | None]] = []

    async def pin(self, slug: str, revision: str | None) -> str:
        self.pinned.append((slug, revision))
        return revision or "f" * 40


@pytest.mark.parametrize("version", ["a" * 40, None])
async def test_a_migration_on_the_bambuddy_store_pins_its_revision(
    make_service: ServiceFactory, version: str | None
) -> None:
    client = _MigrationClient()
    service = make_service(client, "q")
    snapshots = _RecordingSnapshots()
    service.snapshots = snapshots  # type: ignore[assignment]
    try:
        await service.migrate_inputs(SLUG, {"params": {}, "v": 0}, version=version)
    finally:
        await service.aclose()
    assert snapshots.pinned == [(SLUG, version)]
    assert client.calls[0]["req"].revision == (version or "f" * 40)
    assert client.calls[0]["rpc_timeout"] == RPC_TIMEOUT
    assert client.calls[0]["execution_timeout"] == MIGRATE_EXECUTION_TIMEOUT


async def test_inputs_too_large_to_migrate_are_refused_before_any_workflow(
    make_service: ServiceFactory,
) -> None:
    client = _MigrationClient()
    service = make_service(client, "q")
    # On the bambuddy store: refused before a snapshot is uploaded for it.
    snapshots = _RecordingSnapshots()
    service.snapshots = snapshots  # type: ignore[assignment]
    try:
        with pytest.raises(ApiError) as raised:
            await service.migrate_inputs(
                SLUG, {"blob": "x" * (MAX_WORKFLOW_INPUT_BYTES + 1)}, version=None
            )
    finally:
        await service.aclose()
    assert raised.value.status == 413
    assert client.calls == []
    assert snapshots.pinned == []


@pytest.mark.parametrize(
    ("outcome", "status", "message"),
    [
        (
            RPCError("connection refused", RPCStatusCode.UNAVAILABLE, b""),
            503,
            "the render service is unavailable",
        ),
        (
            WorkflowFailureError(
                cause=TemporalTimeoutError(
                    "timed out", type=TimeoutType.START_TO_CLOSE, last_heartbeat_details=[]
                )
            ),
            504,
            "migrating the inputs timed out",
        ),
    ],
)
async def test_a_migration_the_service_cannot_finish_is_a_server_error(
    make_service: ServiceFactory, outcome: BaseException, status: int, message: str
) -> None:
    service = make_service(_MigrationClient(outcome), "q")
    try:
        with pytest.raises(ApiError) as raised:
            await service.migrate_inputs(SLUG, {"params": {}, "v": 0}, version=None)
    finally:
        await service.aclose()
    assert raised.value.status == status
    assert message in str(raised.value.detail)


async def test_retry_after_is_how_long_renders_take_now(
    make_service: ServiceFactory, projection: JobProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#603: a full queue's `Retry-After` follows the recent renders' median, rounded up,
    and falls back to the initial estimate until one has finished."""
    async with temporal_client() as client:
        service = make_service(client, f"t-{uuid.uuid4().hex[:8]}")
        monkeypatch.setattr(projection, "recent_render_seconds", lambda: None)
        assert await service.retry_after() == 10
        monkeypatch.setattr(projection, "recent_render_seconds", lambda: 42.2)
        assert await service.retry_after() == 43
        monkeypatch.setattr(projection, "recent_render_seconds", lambda: 0.2)
        assert await service.retry_after() == 1
        await service.aclose()
