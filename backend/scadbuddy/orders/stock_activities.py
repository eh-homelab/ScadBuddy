"""The Python steps of the orders' stock workflows (#6949 K1 Task 16, design §6.1a), on the
``library`` queue, whose worker runs in the API process: they write Python-owned rows, which
only the API may write (root CLAUDE.md #5295). Go's steps (`inventory.Take`,
`inventory.GiveBack`, `inventory.Recost`) run between them on the queue ``inventory``
(`workflows/order_stock.py`).

- `orders_fulfil_commit`: `FulfilOrderItem`'s first step. It reads what the change would
  take (`Lifecycle.stock_plan`), asks Go's estimate of each take with no lock held
  (``bad_quantity`` refuses as phase 2 did), then commits under the order's lock
  (`Lifecycle.stock_commit`), and reads again when a quantity changed meanwhile.
- `orders_parts_record`: its last step, Go's answers on the order (`Lifecycle.stock_record`).
- `orders_await_workflow`: the wait for another open workflow of the item, which heartbeats.
- `orders_recost_orders` and `orders_recost_record`: `RecostLot`'s first and last steps.

A refusal in a commit step is a non-retryable `ApplicationError` whose type is the problem's
code and whose first detail is the problem (`lifecycle_problem`): nothing was written, the
workflow fails with it, and the same id starts again once the cause is fixed (R-K1-32). Every
step is idempotent by the workflow id. `OrderStockSteps` holds the steps' work with no
Temporal in it."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import timedelta
from typing import Any, Protocol

from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError

from scadbuddy.core.problems import ApiError
from scadbuddy.inventory.models import RecostResult
from scadbuddy.orders.lifecycle import Lifecycle, LifecycleError, lifecycle_problem
from scadbuddy.orders.models import Actor

logger = logging.getLogger(__name__)

ORDERS_FULFIL_COMMIT = "orders_fulfil_commit"
ORDERS_PARTS_RECORD = "orders_parts_record"
ORDERS_AWAIT_WORKFLOW = "orders_await_workflow"
ORDERS_RECOST_ORDERS = "orders_recost_orders"
ORDERS_RECOST_RECORD = "orders_recost_record"

#: How often the first step reads the estimates again when the quantities moved meanwhile.
REREADS = 5
#: How often `orders_await_workflow` heartbeats while it waits.
HEARTBEAT_EVERY = timedelta(seconds=10)
#: The most moves the re-cost's read of a lot's orders asks Go for. An order it misses is
#: named by Go's `recost_orders_moved`, and the next round locks it too.
RECOST_MOVES_LIMIT = 1000


class Reads(Protocol):
    """What the steps read of Go (`InventoryCore.read`)."""

    async def read(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any: ...


def refusal_of(error: LifecycleError) -> ApplicationError:
    """The non-retryable failure of a commit step that refused (R-K1-32)."""
    problem = lifecycle_problem(error)
    return ApplicationError(str(error), problem, type=error.slug, non_retryable=True)


class OrderStockSteps:
    def __init__(self, lifecycle: Callable[[], Lifecycle], reads: Callable[[], Reads]) -> None:
        self._lifecycle = lifecycle
        self._reads = reads

    async def _estimate(self, calls: list[dict[str, Any]]) -> None:
        """Go's estimate of each take (`GET /items/{id}/estimate?qty=`), which refuses
        ``bad_quantity`` for a take whose amount a move cannot hold (R-P2-16). Its other
        refusals are the take's to answer; Go out of reach fails the step, and Temporal
        retries it."""
        for call in calls:
            try:
                await self._reads().read(
                    f"/items/{call['item_id']}/estimate", params={"qty": call["qty"]}
                )
            except ApiError as error:
                if error.extensions.get("code") == "bad_quantity":
                    raise LifecycleError(
                        "bad_quantity",
                        f"item {call['order_item_id']} uses {call['qty']} of part"
                        f" {call['item_id']}: more than one stock move holds at its cost",
                        items=[call["order_item_id"]],
                    ) from None
                if error.status >= 500:
                    raise

    async def fulfil_commit(self, input: Mapping[str, Any]) -> dict[str, Any]:
        lifecycle = self._lifecycle()
        by = Actor.model_validate(input["actor"])
        workflow_id, op, order_id, lines = (
            input["workflow_id"],
            input["op"],
            input["order_id"],
            input["lines"],
        )
        try:
            for _ in range(REREADS):
                plan = await asyncio.to_thread(
                    lifecycle.stock_plan, workflow_id, op, order_id, lines
                )
                if plan.done or plan.wait_for is not None:
                    return asdict(plan)
                if op != "unfulfil":
                    await self._estimate(plan.calls)
                step = await asyncio.to_thread(
                    lifecycle.stock_commit, workflow_id, op, order_id, lines, by, plan.calls
                )
                if not step.reread:
                    return asdict(step)
            raise LifecycleError(
                "order_item_changed",
                "the items' parts kept changing while the fulfil read their stock; send it again",
                items=[line["item_id"] for line in lines],
            )
        except LifecycleError as error:
            raise refusal_of(error) from None

    async def parts_record(self, input: Mapping[str, Any]) -> Any:
        lifecycle = self._lifecycle()
        return await asyncio.to_thread(
            lifecycle.stock_record,
            input["workflow_id"],
            input["op"],
            input["order_id"],
            input["item_ids"],
            input["attempts"],
            input["results"],
            Actor.model_validate(input["actor"]),
        )

    async def recost_orders(self, input: Mapping[str, Any]) -> list[str]:
        """The orders whose moves name the lot (Go's read), with the ones a refused round
        named (``also``), sorted."""
        found = await self._reads().read(
            "/moves", params={"lot_id": input["lot_id"], "limit": RECOST_MOVES_LIMIT}
        )
        named = {m["order_id"] for m in found["moves"] if m.get("order_id")}
        return sorted(named | set(input.get("also") or ()))

    async def recost_record(self, input: Mapping[str, Any]) -> Any:
        lifecycle = self._lifecycle()
        result = await asyncio.to_thread(
            lifecycle.recost_record,
            input["workflow_id"],
            input["lot_id"],
            RecostResult.model_validate(input["result"]),
            Actor.model_validate(input["actor"]),
        )
        return result.model_dump(mode="json")


def order_stock_activities(
    steps: Callable[[], OrderStockSteps], client: Callable[[], Client]
) -> list[Callable[..., Any]]:
    """The steps as the ``library`` worker's activities."""

    @activity.defn(name=ORDERS_FULFIL_COMMIT)
    async def orders_fulfil_commit(input: dict[str, Any]) -> dict[str, Any]:
        return await steps().fulfil_commit(input)

    @activity.defn(name=ORDERS_PARTS_RECORD)
    async def orders_parts_record(input: dict[str, Any]) -> Any:
        return await steps().parts_record(input)

    @activity.defn(name=ORDERS_RECOST_ORDERS)
    async def orders_recost_orders(input: dict[str, Any]) -> list[str]:
        return await steps().recost_orders(input)

    @activity.defn(name=ORDERS_RECOST_RECORD)
    async def orders_recost_record(input: dict[str, Any]) -> Any:
        return await steps().recost_record(input)

    @activity.defn(name=ORDERS_AWAIT_WORKFLOW)
    async def orders_await_workflow(workflow_id: str) -> None:
        """Waits for the item's other open workflow to end, heartbeating as it waits. A
        run that failed is an end too: the first step then reads the item again."""
        handle = client().get_workflow_handle(workflow_id)
        while True:
            activity.heartbeat()
            try:
                await asyncio.wait_for(handle.result(), HEARTBEAT_EVERY.total_seconds())
                return
            except TimeoutError:
                continue
            except WorkflowFailureError:
                return

    return [
        orders_fulfil_commit,
        orders_parts_record,
        orders_recost_orders,
        orders_recost_record,
        orders_await_workflow,
    ]
