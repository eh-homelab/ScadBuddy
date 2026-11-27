"""`TemporalStarter` (#6949 K1 Task 16, `orders/stock_start.py`) over a fake Temporal client:
a start that cannot reach Temporal is 503 `inventory_unavailable`, a run still open after
the wait is `WorkflowAccepted`, a run that failed with a refusal raises `WorkflowRefused`,
and one that succeeded before is read again. No Temporal server runs."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.core.problems import ApiError
from scadbuddy.inventory.core_client import Done
from scadbuddy.orders.stock_start import TemporalStarter, WorkflowAccepted, WorkflowRefused

HANG = object()


class FakeHandle:
    def __init__(self, answer: Any) -> None:
        self.answer = answer

    async def result(self) -> Any:
        if self.answer is HANG:
            await asyncio.Event().wait()
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class FakeClient:
    def __init__(self, answer: Any = None, *, start: BaseException | None = None) -> None:
        self.answer = answer
        self.start = start
        self.starts: list[tuple[str, Any, dict[str, Any]]] = []
        self.handles: list[str] = []

    async def start_workflow(self, workflow: str, arg: Any, **options: Any) -> FakeHandle:
        self.starts.append((workflow, arg, options))
        if self.start is not None:
            raise self.start
        return FakeHandle(self.answer)

    def get_workflow_handle(self, workflow_id: str) -> FakeHandle:
        self.handles.append(workflow_id)
        return FakeHandle(self.answer)


def _starter(client: FakeClient) -> TemporalStarter:
    return TemporalStarter(client, "library-test")  # type: ignore[arg-type]


def _failure(cause: BaseException) -> WorkflowFailureError:
    error = WorkflowFailureError(cause=cause)
    return error


async def test_a_start_answers_the_workflows_result() -> None:
    client = FakeClient({"ok": True})

    done = await _starter(client).astart_and_wait("FulfilOrderItem", {"a": 1}, id="w1")

    assert done == Done({"ok": True})
    [(workflow, arg, options)] = client.starts
    assert (workflow, arg) == ("FulfilOrderItem", {"a": 1, "inventory_queue": "inventory"})
    assert options["id"] == "w1" and options["task_queue"] == "library-test"
    assert options["id_conflict_policy"] == WorkflowIDConflictPolicy.USE_EXISTING
    assert options["id_reuse_policy"] == WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY


async def test_a_failed_start_is_503_and_writes_nothing() -> None:
    client = FakeClient(start=RPCError("refused", RPCStatusCode.UNAVAILABLE, b""))

    with pytest.raises(ApiError) as unavailable:
        await _starter(client).astart_and_wait("FulfilOrderItem", {}, id="w2")

    assert unavailable.value.status == 503
    assert unavailable.value.extensions["code"] == "inventory_unavailable"


async def test_a_slow_run_is_accepted() -> None:
    accepted = await _starter(FakeClient(HANG)).astart_and_wait(
        "FulfilOrderItem", {}, id="w3", wait=0.05
    )

    assert isinstance(accepted, WorkflowAccepted)
    assert (accepted.workflow_id, accepted.kind, accepted.repeated) == (
        "w3",
        "FulfilOrderItem",
        False,
    )


async def test_a_run_that_succeeded_before_is_read_again() -> None:
    client = FakeClient({"ok": 2}, start=WorkflowAlreadyStartedError("w4", "FulfilOrderItem"))

    done = await _starter(client).astart_and_wait("FulfilOrderItem", {}, id="w4")

    assert done == Done({"ok": 2})
    assert client.handles == ["w4"]


async def test_a_refused_run_raises_its_problem() -> None:
    problem = {"code": "order_on_hold", "status": 409, "detail": "on hold"}
    refused = ApplicationError("on hold", problem, type="order_on_hold", non_retryable=True)

    with pytest.raises(WorkflowRefused) as raised:
        await _starter(FakeClient(_failure(refused))).astart_and_wait(
            "FulfilOrderItem", {}, id="w5"
        )

    assert raised.value.problem == problem and raised.value.code == "order_on_hold"


async def test_any_other_failure_is_500() -> None:
    with pytest.raises(ApiError) as failed:
        await _starter(FakeClient(_failure(RuntimeError("boom")))).astart_and_wait(
            "FulfilOrderItem", {}, id="w6"
        )
    assert failed.value.status == 500


def test_an_unbound_starter_is_503() -> None:
    with pytest.raises(ApiError) as unavailable:
        _starter(FakeClient()).start_and_wait("FulfilOrderItem", {}, id="w7")
    assert unavailable.value.status == 503


async def test_a_bound_starter_runs_on_the_apps_loop() -> None:
    starter = _starter(FakeClient({"ok": 3}))
    starter.bind(asyncio.get_running_loop())

    done = await asyncio.to_thread(starter.start_and_wait, "FulfilOrderItem", {}, id="w8")

    assert done == Done({"ok": 3})
