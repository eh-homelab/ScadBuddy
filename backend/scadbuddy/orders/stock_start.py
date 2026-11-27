"""How a Python write that moves stock starts its workflow (#6949 K1 Task 16, design §6.1a).

A change that crosses the Python-Go line (an order item's fulfil, accept or unfulfil, a
lot's re-cost, a printer merge) starts its workflow on the ``library`` queue before it
commits anything; only the workflow's own activities commit (`workflows/order_stock.py`).
`StockStarter.start_and_wait` starts it with Use Existing and Allow Duplicate Failed Only,
so the same id attaches to a run that is still open, starts again after a run that failed,
and reads the answer of a run that succeeded. It waits up to ``wait`` seconds for the end:

- the workflow's answer is `Done`;
- a run still open is `WorkflowAccepted`, which the route answers with 202;
- a run that failed with a refusal (a non-retryable `ApplicationError` whose first detail
  is the problem) raises `WorkflowRefused`, and nothing was written;
- a start that cannot reach Temporal is 503 ``inventory_unavailable``, and nothing was
  written; any other failure is a 500.

The stores call it from a worker thread (`asyncio.to_thread`, a route's thread pool), so
it runs the start on the app's own event loop (`bind`) with
`asyncio.run_coroutine_threadsafe`."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.service import RPCError

from scadbuddy.core.components import Components, Core
from scadbuddy.core.problems import ApiError
from scadbuddy.inventory.core_client import Done, unavailable
from scadbuddy.inventory.problems import PROBLEMS

logger = logging.getLogger(__name__)

#: How long a person's request waits for its workflow before it answers 202.
WAIT = 15.0

FULFIL_ORDER_ITEM = "FulfilOrderItem"
RECOST_LOT = "RecostLot"
MERGE_PRINTER = "MergePrinter"


class WorkflowAccepted(BaseModel):
    """A stock workflow still running after the wait: it ends by itself."""

    workflow_id: str
    kind: str
    started_at: datetime
    #: True when the start attached to a run that already succeeded.
    repeated: bool = False


class WorkflowRefused(Exception):  # noqa: N818
    """The workflow failed with a refusal: ``problem`` is its problem document (``code``,
    ``status``, ``detail`` and the refusal's other members). Nothing was written."""

    def __init__(self, problem: Mapping[str, Any]) -> None:
        super().__init__(str(problem.get("detail", "")))
        self.problem = dict(problem)

    @property
    def code(self) -> str:
        return str(self.problem.get("code", ""))


def refusal(code: str, detail: str, *, status_code: int, **members: Any) -> ApplicationError:
    """The non-retryable `ApplicationError` a commit step fails with (R-K1-32): its type is
    the problem's code, and its first detail the problem."""
    problem = {"code": code, "status": status_code, "detail": detail, **members}
    return ApplicationError(detail, problem, type=code, non_retryable=True)


class StockStarter(Protocol):
    def bind(self, loop: asyncio.AbstractEventLoop) -> None: ...

    def start_and_wait(
        self, workflow: str, arg: Mapping[str, Any], *, id: str, wait: float | None = WAIT
    ) -> Done | WorkflowAccepted: ...


def _refused_problem(failure: WorkflowFailureError) -> Mapping[str, Any] | None:
    cause = failure.cause
    if isinstance(cause, ApplicationError) and cause.non_retryable and cause.details:
        first = cause.details[0]
        if isinstance(first, Mapping) and "code" in first:
            return first
    return None


class TemporalStarter:
    """`StockStarter` over the API's lazy Temporal client, on the ``library`` queue."""

    def __init__(
        self,
        client: Client | Callable[[], Client],
        queue: str,
        inventory_queue: str = "inventory",
        *,
        wait_cap: float | None = None,
    ) -> None:
        self._client = client
        self.queue = queue
        #: Where the workflow runs Go's activities (``SCADBUDDY_TEMPORAL_TASK_QUEUE_INVENTORY``):
        #: each start's input names it (``inventory_queue``).
        self.inventory_queue = inventory_queue
        #: The longest any wait lasts (a test's short wait); None keeps each caller's.
        self.wait_cap = wait_cap
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """The app's loop, which every start runs on."""
        self._loop = loop

    @property
    def client(self) -> Client:
        return self._client() if callable(self._client) else self._client

    def start_and_wait(
        self, workflow: str, arg: Mapping[str, Any], *, id: str, wait: float | None = WAIT
    ) -> Done | WorkflowAccepted:
        if self._loop is None:
            raise unavailable("ScadBuddy's background service is not set up here.")
        future = asyncio.run_coroutine_threadsafe(
            self.astart_and_wait(workflow, arg, id=id, wait=wait), self._loop
        )
        return future.result()

    async def astart_and_wait(
        self, workflow: str, arg: Mapping[str, Any], *, id: str, wait: float | None = WAIT
    ) -> Done | WorkflowAccepted:
        if self.wait_cap is not None:
            wait = self.wait_cap if wait is None else min(wait, self.wait_cap)
        client = self.client
        repeated = False
        started_at = datetime.now(UTC)
        try:
            handle = await client.start_workflow(
                workflow,
                {**arg, "inventory_queue": self.inventory_queue},
                id=id,
                task_queue=self.queue,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            )
        except WorkflowAlreadyStartedError:
            # It succeeded before: its stored answer is the answer.
            handle = client.get_workflow_handle(id)
            repeated = True
        except (RPCError, RuntimeError, OSError) as error:
            logger.warning("could not start %s %s: %s", workflow, id, error)
            raise unavailable(
                "ScadBuddy's background service did not answer. Nothing was changed; try again."
            ) from None
        try:
            # Not shielded: the wait's end stops only this poll; the workflow runs on.
            answer = await asyncio.wait_for(handle.result(), wait)
        except (TimeoutError, RPCError):
            return WorkflowAccepted(
                workflow_id=id, kind=workflow, started_at=started_at, repeated=repeated
            )
        except WorkflowFailureError as failed:
            problem = _refused_problem(failed)
            if problem is not None:
                raise WorkflowRefused(problem) from None
            raise ApiError(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"the workflow {id} failed: {failed.cause or failed}",
                type_=PROBLEMS + "inventory_activity_failed",
                code="inventory_activity_failed",
            ) from None
        return Done(answer)


def accepted_response(accepted: WorkflowAccepted) -> JSONResponse:
    """The route's 202 for a workflow still running."""
    return JSONResponse(accepted.model_dump(mode="json"), status_code=status.HTTP_202_ACCEPTED)


def make_starter(core: Core, components: Components | None) -> StockStarter:
    """The app's starter: the API's lazy Temporal client, on the ``library`` queue. Each
    component that starts a stock workflow builds one and binds it to the app's loop when
    it runs. (The tests put a starter that runs the steps in line here.)"""
    del components
    return TemporalStarter(
        core.print_runs.client,
        core.settings.temporal_task_queue_library,
        core.settings.temporal_task_queue_inventory,
    )
