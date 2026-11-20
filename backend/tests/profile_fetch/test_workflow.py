"""``ProfileFetch`` (#6625 Task 6) on a real Temporal, its activities faked by name: the
export then the import; a fetch that cannot finish posts ``failed``; no worker to take the
export fails the run (Review Focus 5); and a run the API already ended ends the workflow
quietly (Decision 25)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from scadbuddy.filament.profile_fetch import FetchStarter, PollerCache, TemporalQueueProbe
from scadbuddy.profile_fetch import workflow as fetch_workflow
from scadbuddy.profile_fetch.wire import (
    EXPORT_ACTIVITY,
    FAILED_ACTIVITY,
    IMPORT_ACTIVITY,
    NOT_RUNNING,
    ExportFailure,
    ExportReport,
    ExportTarget,
    FetchAsk,
    ImportOutcome,
    fetch_workflow_id,
)
from scadbuddy.profile_fetch.workflow import (
    EXPORT_HEARTBEAT,
    EXPORT_RETRY,
    EXPORT_TIMEOUT,
    NOT_STARTED_IN_TIME,
    ProfileFetch,
)
from tests.support.temporal import temporal_client, terminate_open_workflows

pytestmark = pytest.mark.requires_temporal

REPORT = ExportReport(
    exported=3, failures=[ExportFailure(printer_model="H2C", nozzle="0.2", reason="no 0.2")]
)
OUTCOME = ImportOutcome(status="partial", presets=3, k_values=1, missing=[], notes=[])


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with temporal_client() as connected:
        yield connected


@pytest.fixture
def queue() -> str:
    return f"profile-fetch-{uuid.uuid4().hex[:8]}"


def _ask() -> FetchAsk:
    return FetchAsk(
        run_id=str(uuid.uuid4()),
        type_id=str(uuid.uuid4()),
        tdfp_id=30173,
        targets=[ExportTarget(printer_model="H2C", nozzles=["0.2", "0.4"])],
    )


class Fakes:
    def __init__(
        self,
        *,
        export_error: Callable[[], BaseException] | None = None,
        import_error: Callable[[], BaseException] | None = None,
    ) -> None:
        self.export_error = export_error
        self.import_error = import_error
        self.exports: list[FetchAsk] = []
        self.export_infos: list[activity.Info] = []
        self.imports: list[tuple[str, ExportReport]] = []
        self.failed: list[tuple[str, str]] = []

    def activities(self) -> list[Callable[..., Any]]:
        @activity.defn(name=EXPORT_ACTIVITY)
        async def export(ask: FetchAsk) -> ExportReport:
            self.exports.append(ask)
            self.export_infos.append(activity.info())
            if self.export_error is not None:
                raise self.export_error()
            return REPORT

        @activity.defn(name=IMPORT_ACTIVITY)
        async def import_(run_id: str, report: ExportReport) -> ImportOutcome:
            self.imports.append((run_id, report))
            if self.import_error is not None:
                raise self.import_error()
            return OUTCOME

        @activity.defn(name=FAILED_ACTIVITY)
        async def failed(run_id: str, reason: str) -> None:
            self.failed.append((run_id, reason))

        return [export, import_, failed]


async def _result(client: Client, ask: FetchAsk) -> ImportOutcome | None:
    # A union is a valid result type for the pydantic converter, though not for the stub.
    handle = client.get_workflow_handle(
        fetch_workflow_id(ask.run_id),
        result_type=ImportOutcome | None,  # type: ignore[arg-type]
    )
    result: ImportOutcome | None = await asyncio.wait_for(handle.result(), 60)
    return result


async def _run(client: Client, queue: str, fakes: Fakes, ask: FetchAsk) -> ImportOutcome | None:
    try:
        async with Worker(
            client, task_queue=queue, workflows=[ProfileFetch], activities=fakes.activities()
        ):
            await FetchStarter(client, queue).start(ask)
            return await _result(client, ask)
    finally:
        await terminate_open_workflows(client, queue)


async def test_the_workflow_imports_after_export(client: Client, queue: str) -> None:
    fakes, ask = Fakes(), _ask()
    assert await _run(client, queue, fakes, ask) == OUTCOME
    assert fakes.exports == [ask]
    assert fakes.imports == [(ask.run_id, REPORT)]
    assert fakes.failed == []
    [info] = fakes.export_infos
    assert info.start_to_close_timeout == EXPORT_TIMEOUT == timedelta(minutes=20)
    assert info.heartbeat_timeout == EXPORT_HEARTBEAT == timedelta(minutes=2)
    assert info.retry_policy is not None
    assert info.retry_policy.maximum_attempts == EXPORT_RETRY.maximum_attempts == 3
    assert info.retry_policy.initial_interval == timedelta(minutes=1)
    # The queue wait caps queued work, not a missing worker (build Decision 62).
    history = await client.get_workflow_handle(fetch_workflow_id(ask.run_id)).fetch_history()
    [scheduled] = [
        event.activity_task_scheduled_event_attributes
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
        and event.activity_task_scheduled_event_attributes.activity_type.name == EXPORT_ACTIVITY
    ]
    assert scheduled.schedule_to_start_timeout.ToTimedelta() == timedelta(hours=6)


async def test_a_non_retryable_failure_posts_failed(client: Client, queue: str) -> None:
    """A refused login is not tried again: ``failed`` posts the activity's message."""
    fakes, ask = (
        Fakes(
            export_error=lambda: ApplicationError(
                "3dfilamentprofiles.com refused the login", type="login_failed", non_retryable=True
            )
        ),
        _ask(),
    )
    outcome = await _run(client, queue, fakes, ask)
    assert outcome is not None and outcome.status == "failed"
    assert len(fakes.exports) == 1
    assert fakes.imports == []
    assert fakes.failed == [(ask.run_id, "3dfilamentprofiles.com refused the login")]


async def test_an_import_that_fails_posts_failed(client: Client, queue: str) -> None:
    fakes, ask = (
        Fakes(import_error=lambda: ApplicationError("boom", type="Refused", non_retryable=True)),
        _ask(),
    )
    outcome = await _run(client, queue, fakes, ask)
    assert outcome is not None and outcome.status == "failed"
    assert fakes.failed == [(ask.run_id, "boom")]


@pytest.mark.parametrize("step", ["export", "import"])
async def test_a_run_the_api_ended_ends_the_workflow_quietly(
    client: Client, queue: str, step: str
) -> None:
    """Decision 25: a CRUD call answers 409 ``not_running`` (the API ended the run). The
    workflow ends: no retry, and no ``failed``, which would only be refused again."""

    def ended() -> BaseException:
        return ApplicationError("the run is not running", type=NOT_RUNNING, non_retryable=True)

    fakes = Fakes(export_error=ended) if step == "export" else Fakes(import_error=ended)
    ask = _ask()
    assert await _run(client, queue, fakes, ask) is None
    assert len(fakes.exports) == 1
    assert len(fakes.imports) == (1 if step == "import" else 0)
    assert fakes.failed == []


async def test_no_worker_fails_the_run(
    client: Client, queue: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review Focus 5: no worker takes the export within its ``schedule_to_start``. The
    workflow does not wait on: ``failed`` posts why, once a worker is there to post it.
    The export is never tried (Temporal does not retry a schedule-to-start timeout)."""
    monkeypatch.setattr(fetch_workflow, "EXPORT_SCHEDULE_TO_START", timedelta(seconds=2))
    fakes, ask = Fakes(), _ask()
    try:
        # Only the workflow: nothing polls the queue's activities yet.
        async with Worker(
            client,
            task_queue=queue,
            workflows=[ProfileFetch],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            await FetchStarter(client, queue).start(ask)
            await asyncio.sleep(4)
            async with Worker(client, task_queue=queue, activities=fakes.activities()):
                outcome = await _result(client, ask)
    finally:
        await terminate_open_workflows(client, queue)
    assert outcome is not None and outcome.status == "failed"
    assert fakes.exports == []
    assert fakes.failed == [(ask.run_id, NOT_STARTED_IN_TIME)]
    # R-F6: the worker may be down, or alive and busy with other fetches; both are named.
    assert NOT_STARTED_IN_TIME == (
        "no profile-fetch worker started the fetch within 6 h:"
        " none is running, or it is busy with other fetches"
    )


async def test_the_queue_probe_sees_a_polling_worker(client: Client, queue: str) -> None:
    """`TemporalQueueProbe` reads DescribeTaskQueue for the workflow and activity task
    queues: none polled a new queue, and a running worker does (build Decision 62)."""
    # No cache: the second read must see the worker that started after the first.
    probe = TemporalQueueProbe(client, queue, PollerCache(seconds=0))
    assert await probe.has_pollers() is False
    async with Worker(client, task_queue=queue, workflows=[ProfileFetch], activities=[]):
        deadline = asyncio.get_running_loop().time() + 10
        while not await probe.has_pollers():
            assert asyncio.get_running_loop().time() < deadline, "no poller seen in 10 s"
            await asyncio.sleep(0.2)
