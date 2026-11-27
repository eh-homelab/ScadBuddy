"""The Python steps of the orders' stock workflows (#6949 K1 Task 16,
`orders/stock_activities.py`): each idempotent by the workflow id, a refusal a
non-retryable `ApplicationError` that wrote nothing (R-K1-32), and a step that waits for
another open workflow of the item. The steps run here with no Temporal; the workflows on a
real Temporal are `tests/workflows/test_order_stock.py`'s."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, TerminatedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import ActivityEnvironment

from scadbuddy.orders.lifecycle import FulfilLine, FulfilmentInput, LifecycleError
from scadbuddy.orders.models import Order, OrderItem
from scadbuddy.orders.parts import ItemPart
from scadbuddy.orders.stock_activities import OrderStockSteps, order_stock_activities
from scadbuddy.orders.stock_start import FULFIL_ORDER_ITEM, WorkflowAccepted
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop
from tests.orders.test_parts_lifecycle import _item
from tests.support.order_stock import InlineStarter

pytestmark = pytest.mark.requires_postgres

ACTOR = BY.model_dump(mode="json")


@pytest.fixture(name="shop")
def _shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


@pytest.fixture
def steps(shop: Shop) -> OrderStockSteps:
    return OrderStockSteps(lambda: shop.lifecycle, lambda: shop.go)


def _with_part(shop: Shop, quantity: int = 1, per_unit: int = 2) -> tuple[Order, OrderItem, str]:
    hook = _item(shop, "Silver hook", on_hand=10, cost="5")
    order, item = shop.accepted(quantity=quantity)
    shop.lifecycle.set_item_parts(
        order.id, item.id, [ItemPart(item_id=hook, per_unit=per_unit)], BY
    )
    return order, shop.get_item(item), hook


def _fulfil_input(order: Order, item: OrderItem, workflow_id: str, n: int = 1) -> dict[str, Any]:
    return {
        "workflow_id": workflow_id,
        "order_id": order.id,
        "op": "fulfil",
        "lines": [
            {
                "item_id": item.id,
                "quantity": item.quantity,
                "key": f"order_item:{item.id}:parts:{n}",
            }
        ],
        "actor": ACTOR,
    }


async def test_the_commit_answers_the_stock_calls(shop: Shop, steps: OrderStockSteps) -> None:
    order, item, hook = _with_part(shop, quantity=3)
    step = await steps.fulfil_commit(_fulfil_input(order, item, "order:o:fulfil:k1"))

    assert step["calls"] == [
        {
            "key": f"order_item:{item.id}:parts:1:{hook}",
            "item_id": hook,
            "qty": "6",
            "order_item_id": item.id,
        }
    ]
    assert (step["wait_for"], step["done"]) == (None, False)
    fulfilled = shop.get_item(item)
    assert fulfilled.status == "fulfilled"
    assert fulfilled.parts_settling is not None
    assert fulfilled.parts_settling.workflow_id == "order:o:fulfil:k1"
    [row] = shop.lifecycle.parts.item_parts([item.id])[item.id]
    assert row.used == 6
    # No stock moved yet: that is Go's step.
    assert shop.go.stock.moves(order_item_id=item.id) == []


async def test_step_1_is_idempotent(shop: Shop, steps: OrderStockSteps) -> None:
    """A forced retry after its commit: one attempt row, one audit row, the same calls."""
    order, item, _ = _with_part(shop)
    first = await steps.fulfil_commit(_fulfil_input(order, item, "order:o:fulfil:k2"))
    again = await steps.fulfil_commit(_fulfil_input(order, item, "order:o:fulfil:k2"))

    assert again["calls"] == first["calls"] and again["done"]
    assert again["attempts"] == first["attempts"]
    assert len(shop.rows.fulfilments(item.id)) == 1
    assert shop.kinds(order).count("fulfilment.accepted") == 1


async def test_a_refusal_in_step_1_writes_nothing(shop: Shop, steps: OrderStockSteps) -> None:
    order, item, _ = _with_part(shop)
    shop.lifecycle.hold(order.id, BY)
    before = shop.kinds(order)

    with pytest.raises(ApplicationError) as refused:
        await steps.fulfil_commit(_fulfil_input(order, item, "order:o:fulfil:k3"))

    assert refused.value.non_retryable and refused.value.type == "order_on_hold"
    assert refused.value.details[0]["status"] == 409
    assert shop.kinds(order) == before
    assert shop.get_item(item).status == "approved"
    assert shop.rows.fulfilments(item.id) == []


async def test_a_wrong_n_is_order_item_changed(shop: Shop, steps: OrderStockSteps) -> None:
    order, item, _ = _with_part(shop)
    with pytest.raises(ApplicationError) as refused:
        await steps.fulfil_commit(_fulfil_input(order, item, "order:o:fulfil:k4", n=2))
    assert refused.value.type == "order_item_changed"
    assert shop.get_item(item).status == "approved"


async def test_bad_quantity_refuses_in_the_commit(shop: Shop, steps: OrderStockSteps) -> None:
    """Go's estimate refuses the take: 409 `bad_quantity`, the order not changed."""
    hook = _item(shop, "Gold hook", on_hand=1, cost="90000000000000")
    order, item = shop.accepted(quantity=2_000_000)
    shop.lifecycle.set_item_parts(order.id, item.id, [ItemPart(item_id=hook, per_unit=1000)], BY)
    item = shop.get_item(item)

    with pytest.raises(ApplicationError) as refused:
        await steps.fulfil_commit(_fulfil_input(order, item, "order:o:fulfil:k5"))

    assert refused.value.type == "bad_quantity"
    assert refused.value.details[0]["items"] == [item.id]
    assert shop.get_item(item).status == "approved"
    assert shop.go.named("inventory.Take") == []


async def test_a_step_waits_for_the_items_other_workflow(
    shop: Shop, steps: OrderStockSteps
) -> None:
    order, item, _ = _with_part(shop)
    await steps.fulfil_commit(_fulfil_input(order, item, "order:o:fulfil:k6"))
    unfulfil = {
        "workflow_id": f"order_item:{item.id}:parts:1:return",
        "order_id": order.id,
        "op": "unfulfil",
        "lines": [{"item_id": item.id, "key": f"order_item:{item.id}:parts:1:return"}],
        "actor": ACTOR,
    }

    step = await steps.fulfil_commit(unfulfil)

    assert step["wait_for"] == "order:o:fulfil:k6"
    assert shop.get_item(item).status == "fulfilled"
    assert "item.unfulfilled" not in shop.kinds(order)


async def test_the_record_writes_once(shop: Shop, steps: OrderStockSteps) -> None:
    order, item, hook = _with_part(shop)
    step = await steps.fulfil_commit(_fulfil_input(order, item, "order:o:fulfil:k7"))
    [call] = step["calls"]
    answer = shop.go.run(
        "inventory.Take",
        {
            "key": call["key"],
            "actor": ACTOR,
            "params": {"item_id": hook},
            "body": {"qty": call["qty"], "order_id": order.id, "order_item_id": item.id},
        },
    )
    record = {
        "workflow_id": "order:o:fulfil:k7",
        "op": "fulfil",
        "order_id": order.id,
        "item_ids": [item.id],
        "attempts": step["attempts"],
        "results": [{**call, **answer}],
        "actor": ACTOR,
    }

    first = await steps.parts_record(record)
    again = await steps.parts_record(record)

    assert first == again and first[0]["id"] == step["attempts"][0]
    assert shop.kinds(order).count("parts.used") == 1
    [used] = [e for e in shop.rows.events(order.id) if e.kind == "parts.used"]
    assert used.detail["parts"] == [
        {"item_id": hook, "count": 2, "amount_minor": "10", "cost_unknown": False}
    ]
    assert used.detail["workflow"] == "order:o:fulfil:k7"
    assert shop.get_item(item).parts_settling is None


async def test_a_refused_take_is_recorded(shop: Shop, steps: OrderStockSteps) -> None:
    order, item, hook = _with_part(shop)
    step = await steps.fulfil_commit(_fulfil_input(order, item, "order:o:fulfil:k8"))
    [call] = step["calls"]
    refused = {"status": 422, "body": {"code": "bad_quantity", "status": 422, "detail": "x"}}

    await steps.parts_record(
        {
            "workflow_id": "order:o:fulfil:k8",
            "op": "fulfil",
            "order_id": order.id,
            "item_ids": [item.id],
            "attempts": step["attempts"],
            "results": [{**call, **refused}],
            "actor": ACTOR,
        }
    )

    kinds = shop.kinds(order)
    assert "parts.used" not in kinds and kinds.count("parts.stock_refused") == 1
    [event] = [e for e in shop.rows.events(order.id) if e.kind == "parts.stock_refused"]
    assert (event.detail["part_item_id"], event.detail["status"]) == (hook, 422)
    assert shop.get_item(item).parts_settling is None


async def test_the_recost_record_writes_once(shop: Shop, steps: OrderStockSteps) -> None:
    order, item, hook = _with_part(shop)
    shop.fulfilled(order, item)
    [lot] = shop.go.stock.lots(UUID(hook))
    shop.go.stock.set_lot_cost(lot.id, Decimal(6), BY)
    orders = await steps.recost_orders({"lot_id": str(lot.id)})
    assert orders == [order.id]
    answer = shop.go.run(
        "inventory.Recost",
        {
            "key": "lot:x:recost:k:1",
            "actor": ACTOR,
            "params": {"lot_id": str(lot.id)},
            "body": {"orders": orders},
        },
    )
    record = {
        "workflow_id": "lot:x:recost:k",
        "lot_id": str(lot.id),
        "result": answer["body"],
        "actor": ACTOR,
    }

    await steps.recost_record(record)
    await steps.recost_record(record)

    assert shop.kinds(order).count("item.parts_recosted") == 1


def test_a_fulfil_that_takes_longer_answers_accepted(shop: Shop) -> None:
    """A workflow still running after the wait: the route answers 202 with it."""
    order, item, _ = _with_part(shop)
    shop.lifecycle.starter = InlineStarter(None, shop.go, accept=True)

    answer = shop.lifecycle.fulfil_items(
        order.id,
        [FulfilLine(item_id=item.id, quantity=1)],
        BY,
        request_key="k9",
    )

    assert isinstance(answer, WorkflowAccepted)
    assert answer.workflow_id == f"order:{order.id}:fulfil:k9"


def test_a_wrong_state_is_a_4xx_at_once(shop: Shop) -> None:
    """The pre-check refuses with no lock, and nothing starts."""
    order, item, _ = _with_part(shop)
    shop.fulfilled(order, item)
    started = shop.lifecycle.starter
    assert isinstance(started, InlineStarter)
    seen = len(started.started)

    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.fulfil_items(
            order.id,
            [FulfilLine(item_id=item.id, quantity=1)],
            BY,
        )

    assert refused.value.slug == "item_not_open"
    assert len(started.started) == seen


def test_an_accept_that_does_not_fulfil_runs_inline(shop: Shop) -> None:
    order, item, _ = _with_part(shop, quantity=3)
    attempt = shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
    )
    started = shop.lifecycle.starter
    assert isinstance(started, InlineStarter)

    accepted = shop.lifecycle.accept_fulfilment(order.id, item.id, attempt.id, BY)

    assert not isinstance(accepted, WorkflowAccepted) and accepted.accepted_at is not None
    assert started.started == []
    assert shop.get_item(item).status == "approved"


def test_unfulfil_then_fulfil_uses_new_keys(shop: Shop) -> None:
    order, item, hook = _with_part(shop)
    shop.fulfilled(order, item)
    shop.lifecycle.unfulfil_item(order.id, item.id, BY)
    shop.fulfilled(order, shop.get_item(item))
    started = shop.lifecycle.starter
    assert isinstance(started, InlineStarter)

    ids = [s.id for s in started.started]
    assert ids[1:] == [
        f"order_item:{item.id}:parts:1:return",
        f"order_item:{item.id}:parts:2",
    ]
    assert [c.arg["key"] for c in shop.go.calls] == [
        f"order_item:{item.id}:parts:1:{hook}",
        f"order_item:{item.id}:parts:1:return:{hook}",
        f"order_item:{item.id}:parts:2:{hook}",
    ]
    assert shop.kinds(order).count("parts.returned") == 1


# ── fix round 1 (I1): a closed run that still holds its item is taken over ──────────────


async def test_the_takeover_input_rebuilds_a_committed_run(
    shop: Shop, steps: OrderStockSteps
) -> None:
    """A run that committed and then closed (terminated, timed out, past retention) still
    names its items. Its input comes back from the rows it wrote, so a new run of the same
    id finds its own commit and goes on to Go's steps and the record."""
    order, item, _ = _with_part(shop)
    await steps.fulfil_commit(_fulfil_input(order, item, "order:o:fulfil:t1"))

    again = shop.lifecycle.stock_takeover(order.id, "order:o:fulfil:t1")

    assert again is not None
    assert (again["order_id"], again["op"]) == (order.id, "fulfil")
    assert again["lines"] == [{"item_id": item.id, "key": f"order_item:{item.id}:parts:1"}]
    assert again["actor"] == ACTOR
    step = await steps.fulfil_commit({**again, "workflow_id": "order:o:fulfil:t1"})
    assert step["done"] and step["wait_for"] is None


async def test_no_takeover_once_the_run_recorded(shop: Shop, steps: OrderStockSteps) -> None:
    order, item, _ = _with_part(shop)
    step = await steps.fulfil_commit(_fulfil_input(order, item, "order:o:fulfil:t2"))
    await steps.parts_record(
        {
            "workflow_id": "order:o:fulfil:t2",
            "op": "fulfil",
            "order_id": order.id,
            "item_ids": [item.id],
            "attempts": step["attempts"],
            "results": [],
            "actor": ACTOR,
        }
    )

    assert shop.lifecycle.stock_takeover(order.id, "order:o:fulfil:t2") is None
    assert shop.lifecycle.stock_takeover(order.id, "order:o:fulfil:never") is None


async def test_an_unfulfil_takeover_keeps_its_op(shop: Shop, steps: OrderStockSteps) -> None:
    order, item, _ = _with_part(shop)
    shop.fulfilled(order, item)
    wid = f"order_item:{item.id}:parts:1:return"
    await steps.fulfil_commit(
        {
            "workflow_id": wid,
            "order_id": order.id,
            "op": "unfulfil",
            "lines": [{"item_id": item.id, "key": wid}],
            "actor": ACTOR,
        }
    )

    again = shop.lifecycle.stock_takeover(order.id, wid)

    assert again is not None and again["op"] == "unfulfil"
    assert again["lines"] == [{"item_id": item.id, "key": wid}]


class _Handle:
    def __init__(self, outcome: BaseException | None) -> None:
        self.outcome = outcome

    async def result(self) -> Any:
        if self.outcome is not None:
            raise self.outcome
        return {"ok": True}


class _Client:
    """A Temporal client whose runs of one id end with each ``outcomes`` in turn."""

    def __init__(self, *outcomes: BaseException | None) -> None:
        self.outcomes = list(outcomes)
        self.started: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def get_workflow_handle(self, workflow_id: str) -> _Handle:
        return _Handle(self.outcomes.pop(0))

    async def start_workflow(self, workflow: str, arg: dict[str, Any], **options: Any) -> _Handle:
        self.started.append((workflow, arg, options))
        return _Handle(None)


def _await(steps: OrderStockSteps, client: _Client) -> Callable[..., Any]:
    [found] = [
        a
        for a in order_stock_activities(lambda: steps, lambda: client)  # type: ignore[arg-type,return-value]
        if a.__name__ == "orders_await_workflow"
    ]
    return found


def _closed() -> WorkflowFailureError:
    return WorkflowFailureError(cause=TerminatedError("terminated by hand"))


async def test_a_closed_run_that_holds_the_item_is_started_again(
    shop: Shop, steps: OrderStockSteps
) -> None:
    order, item, _ = _with_part(shop)
    await steps.fulfil_commit(_fulfil_input(order, item, "order:o:fulfil:a1"))
    client = _Client(_closed(), None)

    outcome = await ActivityEnvironment().run(
        _await(steps, client),
        {"workflow_id": "order:o:fulfil:a1", "order_id": order.id, "inventory_queue": "inv"},
    )

    assert outcome == "taken_over"
    [(workflow, arg, options)] = client.started
    assert workflow == FULFIL_ORDER_ITEM and options["id"] == "order:o:fulfil:a1"
    assert options["task_queue"] == "test"
    assert options["id_reuse_policy"] == WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY
    assert arg["inventory_queue"] == "inv" and arg["op"] == "fulfil"


async def test_a_run_past_retention_counts_as_closed(shop: Shop, steps: OrderStockSteps) -> None:
    order, item, _ = _with_part(shop)
    await steps.fulfil_commit(_fulfil_input(order, item, "order:o:fulfil:a2"))
    client = _Client(RPCError("not found", RPCStatusCode.NOT_FOUND, b""), None)

    outcome = await ActivityEnvironment().run(
        _await(steps, client), {"workflow_id": "order:o:fulfil:a2", "order_id": order.id}
    )

    assert outcome == "taken_over" and len(client.started) == 1


async def test_a_closed_run_that_holds_nothing_is_left_closed(
    shop: Shop, steps: OrderStockSteps
) -> None:
    order, _, _ = _with_part(shop)
    client = _Client(_closed())

    outcome = await ActivityEnvironment().run(
        _await(steps, client), {"workflow_id": "order:o:fulfil:a3", "order_id": order.id}
    )

    assert outcome == "closed" and client.started == []


async def test_a_completed_run_ends_the_wait(shop: Shop, steps: OrderStockSteps) -> None:
    order, _, _ = _with_part(shop)
    client = _Client(None)

    outcome = await ActivityEnvironment().run(
        _await(steps, client), {"workflow_id": "order:o:fulfil:a4", "order_id": order.id}
    )

    assert outcome == "completed" and client.started == []


# ── fix round (PR #8323): a change sent while an unfulfil still settles ─────────────────


async def _unfulfil_committed(
    shop: Shop, steps: OrderStockSteps, order: Order, item: OrderItem
) -> str:
    """The item's unfulfil past its commit and before its record: Go's `GiveBack` is
    slow. The item is `approved` again, and its `parts_workflow_id` names the unfulfil."""
    wid = f"order_item:{item.id}:parts:1:return"
    await steps.fulfil_commit(
        {
            "workflow_id": wid,
            "order_id": order.id,
            "op": "unfulfil",
            "lines": [{"item_id": item.id, "key": wid}],
            "actor": ACTOR,
        }
    )
    settling = shop.get_item(item)
    assert settling.status == "approved" and settling.parts_settling is not None
    return wid


async def _unfulfil_recorded(
    steps: OrderStockSteps, order: Order, item: OrderItem, wid: str
) -> None:
    await steps.parts_record(
        {
            "workflow_id": wid,
            "op": "unfulfil",
            "order_id": order.id,
            "item_ids": [item.id],
            "attempts": [],
            "results": [],
            "actor": ACTOR,
        }
    )


async def test_an_accept_while_the_unfulfil_settles_starts_the_next_cycle(
    shop: Shop, steps: OrderStockSteps
) -> None:
    """The first cycle's accept is the completed run `parts:1`. An accept sent while the
    unfulfil settles is cycle 2's (`parts:2`), never a replay of `parts:1`'s answer; it
    commits once the unfulfil records. A resend of the unfulfil still attaches to its own
    run."""
    order, item, _ = _with_part(shop)
    shop.fulfilled(order, item)
    wid = await _unfulfil_committed(shop, steps, order, item)
    attempt = shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
    )
    started = shop.lifecycle.starter
    assert isinstance(started, InlineStarter)
    started.accept = True

    accepted = shop.lifecycle.accept_fulfilment(order.id, item.id, attempt.id, BY)
    again = shop.lifecycle.unfulfil_item(order.id, item.id, BY)

    assert isinstance(accepted, WorkflowAccepted)
    assert accepted.workflow_id == f"order_item:{item.id}:parts:2"
    assert isinstance(again, WorkflowAccepted) and again.workflow_id == wid
    await _unfulfil_recorded(steps, order, item, wid)
    run = next(s for s in started.started if s.id == accepted.workflow_id)
    step = await steps.fulfil_commit({**run.arg, "workflow_id": run.id})
    assert step["wait_for"] is None and not step["done"]
    assert shop.get_item(item).status == "fulfilled"


async def test_a_fulfil_while_the_unfulfil_settles_commits_after_it(
    shop: Shop, steps: OrderStockSteps
) -> None:
    """A fulfil sent while the unfulfil settles takes cycle 2's key, so its run commits
    once the unfulfil records, rather than failing `order_item_changed` unseen."""
    order, item, hook = _with_part(shop)
    shop.fulfilled(order, item)
    wid = await _unfulfil_committed(shop, steps, order, item)
    started = shop.lifecycle.starter
    assert isinstance(started, InlineStarter)
    started.accept = True

    answer = shop.lifecycle.fulfil_items(
        order.id, [FulfilLine(item_id=item.id, quantity=1)], BY, request_key="w1"
    )

    assert isinstance(answer, WorkflowAccepted)
    run = next(s for s in started.started if s.id == answer.workflow_id)
    assert run.arg["lines"][0]["key"] == f"order_item:{item.id}:parts:2"
    await _unfulfil_recorded(steps, order, item, wid)
    step = await steps.fulfil_commit({**run.arg, "workflow_id": run.id})
    assert step["wait_for"] is None
    assert [c["key"] for c in step["calls"]] == [f"order_item:{item.id}:parts:2:{hook}"]
    assert shop.get_item(item).status == "fulfilled"
