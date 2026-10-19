"""`start_command` on an execution whose workflow task is failing (#4156): still
accepting only while an ``Operation`` defers a kind its build does not register (#4139),
whose task goes on once a build with the kind is current. A task failing for any other
reason waits for nothing, and is refused as before #4139 rather than followed forever."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import cast

import pytest
from temporalio import workflow
from temporalio.api.enums.v1 import EventType, WorkflowTaskFailedCause
from temporalio.api.failure.v1 import ApplicationFailureInfo, Failure, TimeoutFailureInfo
from temporalio.api.history.v1 import (
    History,
    HistoryEvent,
    WorkflowTaskFailedEventAttributes,
)
from temporalio.api.workflowservice.v1 import (
    GetWorkflowExecutionHistoryReverseRequest,
    GetWorkflowExecutionHistoryReverseResponse,
)
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.service import RPCError
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from scadbuddy.workflows.commands import (
    DEFERRED_KIND,
    NOT_READY_EVENTS,
    NOT_READY_PAGES,
    CommandStillAcceptingError,
    TemporalRefusedError,
    deferred_kind,
    start_command,
)
from scadbuddy.workflows.operation import UnregisteredKindError
from tests.support.temporal import temporal_available, temporal_server


@workflow.defn(name="NotReadyDeferred")
class Deferred:
    @workflow.run
    async def run(self) -> None:
        raise UnregisteredKindError("this build does not register 'op.x.run'")

    @workflow.update
    async def accepted(self) -> str:
        return "accepted"


@workflow.defn(name="NotReadyBroken")
class Broken:
    @workflow.run
    async def run(self) -> None:
        raise RuntimeError("a bug in workflow code")

    @workflow.update
    async def accepted(self) -> str:
        return "accepted"


@pytest.fixture(scope="module")
def temporal_address() -> Iterator[str]:
    if not temporal_available():
        pytest.skip("no Temporal")
    with temporal_server() as address:
        yield address


async def _failing(client: Client, workflow_name: str, queue: str) -> str:
    """An execution whose workflow task has failed and is being retried."""
    id = f"not-ready-{uuid.uuid4().hex[:8]}"
    handle = await client.start_workflow(workflow_name, id=id, task_queue=queue)
    async with asyncio.timeout(30):
        while (await handle.describe()).raw_description.pending_workflow_task.attempt < 2:
            await asyncio.sleep(0.1)
    return id


@pytest.mark.requires_temporal
@pytest.mark.parametrize(
    ("workflow_name", "answer"),
    [("NotReadyDeferred", CommandStillAcceptingError), ("NotReadyBroken", TemporalRefusedError)],
)
async def test_only_a_deferred_kind_is_still_accepting(
    temporal_address: str, workflow_name: str, answer: type[Exception]
) -> None:
    client = await Client.connect(temporal_address, data_converter=pydantic_data_converter)
    queue = f"not-ready-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[Deferred, Broken],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        id = await _failing(client, workflow_name, queue)
        with pytest.raises(answer):
            await start_command(
                client,
                workflow_name,
                None,
                id=id,
                task_queue=queue,
                update="accepted",
                result_type=str,
                reuse=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            )
        await client.get_workflow_handle(id).terminate()


async def _start(client: Client, id: str, queue: str) -> None:
    await start_command(
        client,
        "NotReadyDeferred",
        None,
        id=id,
        task_queue=queue,
        update="accepted",
        result_type=str,
        reuse=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
    )


class _Pages:
    """A workflow service serving an execution's reverse history in fixed pages: the dev
    server returns it whole whatever the page size, so it cannot show the paging."""

    def __init__(self, *pages: list[HistoryEvent]) -> None:
        self.pages = pages
        self.tokens: list[bytes] = []

    async def get_workflow_execution_history_reverse(
        self, request: GetWorkflowExecutionHistoryReverseRequest, **kwargs: object
    ) -> GetWorkflowExecutionHistoryReverseResponse:
        self.tokens.append(request.next_page_token)
        n = int(request.next_page_token or b"0")
        more = str(n + 1).encode() if n + 1 < len(self.pages) else b""
        return GetWorkflowExecutionHistoryReverseResponse(
            history=History(events=self.pages[n]), next_page_token=more
        )


def _signals(n: int) -> list[HistoryEvent]:
    return [HistoryEvent(event_type=EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED)] * n


def _task_failed(kind: str) -> HistoryEvent:
    return HistoryEvent(
        event_type=EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED,
        workflow_task_failed_event_attributes=WorkflowTaskFailedEventAttributes(
            failure=Failure(application_failure_info=ApplicationFailureInfo(type=kind))
        ),
    )


async def test_a_deferral_under_a_page_of_signals_is_still_found() -> None:
    """With no worker to take the task (a deploy's gap), signals are written above its
    failure once it times out, more than one page of `NOT_READY_EVENTS`: the deferral
    is read on the next page. (A worker taking it again records a new failure above.)"""
    service = _Pages(_signals(NOT_READY_EVENTS), [*_signals(5), _task_failed(DEFERRED_KIND)])
    client = SimpleNamespace(namespace="default", workflow_service=service)
    assert await deferred_kind(cast(Client, client), "op-x")
    assert service.tokens == [b"", b"1"]


async def test_another_failure_is_no_deferral_and_says_what_it_was(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure that is not a deferral answers no, and the warning names its kind and
    cause (#4320), a cause newer than the protos included: its number, not a failed read."""
    timed_out = HistoryEvent(
        event_type=EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED,
        workflow_task_failed_event_attributes=WorkflowTaskFailedEventAttributes(
            failure=Failure(timeout_failure_info=TimeoutFailureInfo()),
            cause=WorkflowTaskFailedCause.WORKFLOW_TASK_FAILED_CAUSE_RESET_STICKY_TASK_QUEUE,
        ),
    )
    newer = HistoryEvent(
        event_type=EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED,
        workflow_task_failed_event_attributes=WorkflowTaskFailedEventAttributes(
            failure=Failure(application_failure_info=ApplicationFailureInfo(type="Other")),
            cause=cast(WorkflowTaskFailedCause.ValueType, 999),
        ),
    )
    caplog.set_level("WARNING", logger="scadbuddy.workflows.commands")
    for event in (timed_out, newer):
        client = SimpleNamespace(namespace="default", workflow_service=_Pages([event]))
        assert not await deferred_kind(cast(Client, client), "op-x")
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "command op-x: its newest workflow task failure is timeout_failure_info '', cause"
        " WORKFLOW_TASK_FAILED_CAUSE_RESET_STICKY_TASK_QUEUE, not a deferral",
        "command op-x: its newest workflow task failure is application_failure_info"
        " 'Other', cause 999, not a deferral",
    ]


async def test_a_history_of_only_signals_is_no_deferral() -> None:
    """The read stops after `NOT_READY_PAGES` pages and answers no."""
    service = _Pages(*[_signals(NOT_READY_EVENTS)] * (NOT_READY_PAGES + 2))
    client = SimpleNamespace(namespace="default", workflow_service=service)
    assert not await deferred_kind(cast(Client, client), "op-x")
    assert len(service.tokens) == NOT_READY_PAGES


@pytest.mark.requires_temporal
async def test_a_history_that_cannot_be_read_is_refused(
    temporal_address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deferral whose history read fails is not taken for one: refused, not followed."""
    client = await Client.connect(temporal_address, data_converter=pydantic_data_converter)
    queue = f"not-ready-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[Deferred],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        id = await _failing(client, "NotReadyDeferred", queue)

        async def unreadable(*args: object, **kwargs: object) -> object:
            raise RuntimeError("history unavailable")

        monkeypatch.setattr(
            client.workflow_service, "get_workflow_execution_history_reverse", unreadable
        )
        try:
            with pytest.raises(TemporalRefusedError):
                await _start(client, id, queue)
        finally:
            monkeypatch.undo()
            # An execution already closed must not hide the assertion's own failure.
            with contextlib.suppress(RPCError):
                await client.get_workflow_handle(id).terminate()
