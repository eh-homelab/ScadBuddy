"""The orders' stock workflows on a real Temporal (#6949 K1 Task 16,
`workflows/order_stock.py`): `FulfilOrderItem`, `RecostLot` and `MergePrinter` with their
real Python steps, on a library queue of the test's own, and fakes of Go's activities
(`inventory.Take`, `GiveBack`, `Recost`, `Repoint`) on an inventory queue of its own. Each
fake counts its runs, can block until the test releases it, and writes as Go would
(`tests/support/order_stock.py` `GoStock`). The starts go through the real
`TemporalStarter`, as the routes' do, with a short wait.

`SCADBUDDY_RECORD_HISTORIES=1` also writes the histories `tests/test_order_stock_replay.py`
replays (`tests/fixtures/order_stock_histories/`)."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from scadbuddy.orders.lifecycle import FulfilLine, FulfilmentInput, LifecycleError
from scadbuddy.orders.models import OrderItem
from scadbuddy.orders.parts import ItemPart
from scadbuddy.orders.stock_activities import OrderStockSteps, order_stock_activities
from scadbuddy.orders.stock_start import TemporalStarter, WorkflowAccepted, WorkflowRefused
from scadbuddy.orders.store import OrderStore
from scadbuddy.printers.merge_activities import merge_activities
from scadbuddy.printers.models import PrinterCreate
from scadbuddy.printers.store import BambuddyLinkTakenError, MergeRunningError, PrinterStore
from scadbuddy.store.refs import BlobRefs
from scadbuddy.workflows.order_stock import (
    RECOST_MAX_ROUNDS,
    FulfilOrderItem,
    MergePrinter,
    RecostLot,
)
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop
from tests.orders.test_parts_lifecycle import _item
from tests.support.order_stock import GoStock
from tests.support.printers import MERGED_BY
from tests.support.temporal import temporal_client, terminate_open_workflows

pytestmark = [
    pytest.mark.requires_temporal,
    pytest.mark.requires_postgres,
    pytest.mark.real_stock_workflows,
]

HISTORIES = Path(__file__).parents[1] / "fixtures" / "order_stock_histories"
RECORD = os.environ.get("SCADBUDDY_RECORD_HISTORIES") == "1"
#: How long a start waits here before it answers `WorkflowAccepted`.
WAIT = 2.0
SERIAL = "01P00A000016949"


@dataclass
class FakeGo:
    """Go's stock activities over `GoStock`: ``runs`` counts each name's runs, ``gates``
    holds a name until its event is set, and ``fail_first`` fails that many first runs."""

    go: GoStock
    runs: Counter[str] = field(default_factory=Counter)
    gates: dict[str, asyncio.Event] = field(default_factory=dict)
    #: Names whose activity fails (not answers) with a non-retryable error.
    failing: set[str] = field(default_factory=set)
    after: dict[str, Callable[[dict[str, Any]], None]] = field(default_factory=dict)

    def activities(self) -> list[Callable[..., Any]]:
        def make(name: str) -> Callable[..., Any]:
            @activity.defn(name=f"inventory.{name}")
            async def run(input: dict[str, Any]) -> dict[str, Any]:
                self.runs[name] += 1
                if name in self.failing:
                    raise ApplicationError("scadbuddy-core is gone", non_retryable=True)
                gate = self.gates.get(name)
                if gate is not None:
                    await gate.wait()
                answer = await asyncio.to_thread(self.go.run, f"inventory.{name}", input)
                hook = self.after.get(name)
                if hook is not None:
                    await asyncio.to_thread(hook, input)
                return answer

            return run

        return [make(n) for n in ("Take", "GiveBack", "Recost", "Repoint")]


@dataclass
class World:
    client: Client
    shop: Shop
    printers: PrinterStore
    go: FakeGo
    starter: TemporalStarter
    library: str
    record_fails: int = 0


@pytest.fixture
async def world(pg_pool: PgPool) -> AsyncIterator[World]:
    pool, rows = pg_pool, OrderStore(pg_pool)
    async with temporal_client() as client:
        library = f"library-{uuid.uuid4().hex[:8]}"
        inventory = f"inventory-{uuid.uuid4().hex[:8]}"
        shop = Shop(rows, BlobRefs(pool))
        printers = PrinterStore(pool, None)
        starter = TemporalStarter(client, library, inventory, wait_cap=WAIT)
        starter.bind(asyncio.get_running_loop())
        shop.lifecycle.starter = starter
        printers.merges = starter
        fake = FakeGo(shop.go)
        world = World(client, shop, printers, fake, starter, library)
        steps = OrderStockSteps(lambda: shop.lifecycle, lambda: shop.go)
        python = order_stock_activities(lambda: steps, lambda: client)
        record = next(a for a in python if a.__name__ == "orders_parts_record")

        @activity.defn(name="orders_parts_record")
        async def orders_parts_record(input: dict[str, Any]) -> Any:
            if world.record_fails:
                world.record_fails -= 1
                raise RuntimeError("the database is away")
            return await record(input)

        activities = [a for a in python if a is not record]
        activities += [orders_parts_record, *merge_activities(lambda: printers)]
        async with (
            Worker(
                client,
                task_queue=library,
                workflows=[FulfilOrderItem, RecostLot, MergePrinter],
                activities=activities,
            ),
            Worker(client, task_queue=inventory, activities=fake.activities()),
        ):
            try:
                yield world
            finally:
                for gate in fake.gates.values():
                    gate.set()
                await terminate_open_workflows(client, library)


async def _call(work: Callable[[], Any]) -> Any:
    """A lifecycle call as a route makes it: off the loop the starter runs on."""
    return await asyncio.to_thread(work)


def _with_part(shop: Shop, quantity: int = 1, per_unit: int = 2) -> tuple[Any, OrderItem, str]:
    hook = _item(shop, "Silver hook", on_hand=10, cost="5")
    order, item = shop.accepted(quantity=quantity)
    shop.lifecycle.set_item_parts(
        order.id, item.id, [ItemPart(item_id=hook, per_unit=per_unit)], BY
    )
    return order, shop.get_item(item), hook


async def _accept(world: World, order: Any, item: OrderItem) -> tuple[Any, Any]:
    attempt = world.shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=item.quantity, outcome="succeeded"), BY
    )
    answer = await _call(
        lambda: world.shop.lifecycle.accept_fulfilment(order.id, item.id, attempt.id, BY)
    )
    return attempt, answer


async def _record(world: World, workflow_id: str, name: str) -> None:
    if not RECORD:
        return
    history = await world.client.get_workflow_handle(workflow_id).fetch_history()
    HISTORIES.mkdir(exist_ok=True)
    (HISTORIES / f"{name}.json").write_text(history.to_json())


async def _until(check: Callable[[], bool], seconds: float = 20.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "it never happened"
        await asyncio.sleep(0.1)


async def test_fulfil_takes_stock_through_the_workflow(world: World) -> None:
    shop = world.shop
    order, item, hook = _with_part(shop, per_unit=2)

    _, accepted = await _accept(world, order, item)

    assert not isinstance(accepted, WorkflowAccepted)
    assert shop.get_item(item).status == "fulfilled"
    [take] = shop.go.named("inventory.Take")
    assert take.arg["key"] == f"order_item:{item.id}:parts:1:{hook}"
    assert take.arg["body"]["qty"] == "2"
    [used] = [e for e in shop.rows.events(order.id) if e.kind == "parts.used"]
    assert used.detail["parts"][0]["amount_minor"] == "10"
    assert shop.get_item(item).parts_settling is None
    await _record(world, f"order_item:{item.id}:parts:1", "fulfil")


def _hold_after_the_pre_check(shop: Shop, order_id: str, monkeypatch: Any) -> None:
    """The order goes on hold once, right after the route's pre-check passed."""
    checked = shop.lifecycle._checked
    held: list[bool] = []

    def hold_after(conn: Any, order_row: Any, op: Any, lines: Any) -> Any:
        found = checked(conn, order_row, op, lines)
        if not held:
            held.append(True)
            shop.lifecycle.hold(order_id, BY)
        return found

    monkeypatch.setattr(shop.lifecycle, "_checked", hold_after)


async def test_a_refused_fulfil_can_be_sent_again(world: World, monkeypatch: Any) -> None:
    """R-K1-32: the run fails with the refusal, and once the cause is gone the same
    request starts a new run of the same id."""
    shop = world.shop
    order, item, _ = _with_part(shop)
    attempt = shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
    )
    _hold_after_the_pre_check(shop, order.id, monkeypatch)
    with pytest.raises(LifecycleError) as refused:
        await _call(lambda: shop.lifecycle.accept_fulfilment(order.id, item.id, attempt.id, BY))
    assert refused.value.slug == "order_on_hold"
    monkeypatch.undo()
    shop.lifecycle.release(order.id, BY)

    await _call(lambda: shop.lifecycle.accept_fulfilment(order.id, item.id, attempt.id, BY))

    assert shop.get_item(item).status == "fulfilled"
    assert world.go.runs["Take"] == 1
    assert shop.kinds(order).count("parts.used") == 1


async def test_a_refusal_in_step_1_answers_4xx(world: World, monkeypatch: Any) -> None:
    """The order goes on hold between the pre-check and step 1: the run fails with
    `order_on_hold` (not a completed one), the route answers it, and nothing is written."""
    shop = world.shop
    order, item, _ = _with_part(shop)
    attempt = shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
    )
    _hold_after_the_pre_check(shop, order.id, monkeypatch)
    with pytest.raises(LifecycleError) as refused:
        await _call(lambda: shop.lifecycle.accept_fulfilment(order.id, item.id, attempt.id, BY))

    assert refused.value.slug == "order_on_hold"
    described = await world.client.get_workflow_handle(f"order_item:{item.id}:parts:1").describe()
    assert described.status is not None and described.status.name == "FAILED"
    assert shop.get_item(item).status == "approved"
    assert world.go.runs["Take"] == 0
    await _record(world, f"order_item:{item.id}:parts:1", "refused")


async def test_a_slow_take_answers_202_and_settles_by_itself(world: World) -> None:
    shop = world.shop
    order, item, _ = _with_part(shop)
    world.go.gates["Take"] = asyncio.Event()

    _, accepted = await _accept(world, order, item)

    assert isinstance(accepted, WorkflowAccepted)
    assert accepted.workflow_id == f"order_item:{item.id}:parts:1"
    settling = shop.get_item(item)
    assert settling.status == "fulfilled" and settling.parts_settling is not None
    assert "parts.used" not in shop.kinds(order)
    world.go.gates["Take"].set()
    await _until(lambda: shop.get_item(item).parts_settling is None)
    assert shop.kinds(order).count("parts.used") == 1
    assert world.go.runs["Take"] == 1
    await _record(world, accepted.workflow_id, "slow_take")


async def test_a_retry_after_a_202_attaches(world: World) -> None:
    shop = world.shop
    order, item, _ = _with_part(shop)
    world.go.gates["Take"] = asyncio.Event()
    attempt, first = await _accept(world, order, item)
    assert isinstance(first, WorkflowAccepted)

    again = await _call(lambda: shop.lifecycle.accept_fulfilment(order.id, item.id, attempt.id, BY))

    assert isinstance(again, WorkflowAccepted) and again.workflow_id == first.workflow_id
    world.go.gates["Take"].set()
    await _until(lambda: shop.get_item(item).parts_settling is None)
    assert shop.kinds(order).count("parts.used") == 1
    with pytest.raises(LifecycleError) as refused:
        await _call(lambda: shop.lifecycle.accept_fulfilment(order.id, item.id, attempt.id, BY))
    assert refused.value.slug == "fulfilment_accepted"


async def test_a_failed_record_is_retried_without_a_second_take(world: World) -> None:
    shop = world.shop
    order, item, _ = _with_part(shop)
    world.record_fails = 1

    await _accept(world, order, item)

    assert shop.kinds(order).count("parts.used") == 1
    assert world.go.runs["Take"] == 1
    await _record(world, f"order_item:{item.id}:parts:1", "record_retry")


async def test_unfulfil_waits_for_the_fulfil(world: World) -> None:
    shop = world.shop
    order, item, hook = _with_part(shop)
    world.go.gates["Take"] = asyncio.Event()
    await _accept(world, order, item)

    waiting = await _call(lambda: shop.lifecycle.unfulfil_item(order.id, item.id, BY))

    assert isinstance(waiting, WorkflowAccepted)
    assert "item.unfulfilled" not in shop.kinds(order)
    world.go.gates["Take"].set()
    await _until(lambda: "parts.returned" in shop.kinds(order), 40.0)
    kinds = shop.kinds(order)
    assert kinds.index("parts.used") < kinds.index("item.unfulfilled")
    [give] = shop.go.named("inventory.GiveBack")
    assert give.arg["key"] == f"order_item:{item.id}:parts:1:return:{hook}"
    assert kinds.count("parts.returned") == 1
    await _record(world, waiting.workflow_id, "unfulfil_waits")


async def test_bulk_fulfil_is_one_workflow(world: World) -> None:
    shop = world.shop
    order = shop.order()
    first, second = shop.item(order), shop.item(order)
    for line in (first, second):
        shop.quoted(order, line)
    shop.lifecycle.accept_order(order.id, BY)
    lines = [FulfilLine(item_id=first.id, quantity=1), FulfilLine(item_id=second.id, quantity=1)]

    attempts = await _call(
        lambda: shop.lifecycle.fulfil_items(order.id, lines, BY, request_key="key-1")
    )

    assert not isinstance(attempts, WorkflowAccepted) and len(attempts) == 2
    described = await world.client.get_workflow_handle(f"order:{order.id}:fulfil:key-1").describe()
    assert described.status is not None and described.status.name == "COMPLETED"


async def test_a_refused_take_is_recorded(world: World) -> None:
    shop = world.shop
    order, item, _ = _with_part(shop)
    shop.go.answers["inventory.Take"] = [
        {"status": 422, "body": {"code": "bad_quantity", "status": 422, "detail": "too much"}}
    ]

    await _accept(world, order, item)

    kinds = shop.kinds(order)
    assert kinds.count("parts.stock_refused") == 1 and "parts.used" not in kinds


async def test_recost_retries_when_another_order_moved(world: World) -> None:
    shop = world.shop
    order, item, hook = _with_part(shop)
    await _accept(world, order, item)
    [lot] = shop.go.stock.lots(UUID(hook))
    shop.go.stock.set_lot_cost(lot.id, Decimal(6), BY)
    moved = {"status": 409, "body": {"code": "recost_orders_moved", "status": 409, "detail": "x"}}
    shop.go.answers["inventory.Recost"] = [dict(moved)]

    result = await _call(lambda: shop.lifecycle.recost_lot(lot.id, BY, request_key="r1"))

    assert not isinstance(result, WorkflowAccepted) and result.moves == 1
    assert world.go.runs["Recost"] == 2
    assert shop.kinds(order).count("item.parts_recosted") == 1
    await _record(world, f"lot:{lot.id}:recost:r1", "recost_retry")
    shop.go.answers["inventory.Recost"] = [dict(moved) for _ in range(3)]
    with pytest.raises(WorkflowRefused) as refused:
        await _call(lambda: shop.lifecycle.recost_lot(lot.id, BY, request_key="r2"))
    assert refused.value.code == "recost_orders_moved"
    assert shop.kinds(order).count("item.parts_recosted") == 1


def _unit(pool: PgPool, printer_id: str) -> UUID:
    with pool.connection() as conn:
        item = conn.execute(
            # A name of its own: `items_kind_name` holds one H2C (a test adds two units).
            "INSERT INTO items (kind_id, name) VALUES ('printer', 'H2C ' || gen_random_uuid())"
            " RETURNING id"
        ).fetchone()
        assert item is not None
        unit = conn.execute(
            "INSERT INTO stock_units (item_id, printer_id, cost_minor, origin, created_by)"
            " VALUES (%s, %s, 0, 'manual', '{}') RETURNING id",
            (item["id"], printer_id),
        ).fetchone()
    assert unit is not None
    return UUID(str(unit["id"]))


def _unit_printer(pool: PgPool, unit: UUID) -> str:
    with pool.connection() as conn:
        row = conn.execute("SELECT printer_id FROM stock_units WHERE id = %s", (unit,)).fetchone()
    assert row is not None
    return str(row["printer_id"])


async def test_a_merge_repoints_then_merges(world: World, pg_pool: PgPool) -> None:
    printers = world.printers
    synthetic = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C", by=MERGED_BY)
    unit = _unit(pg_pool, synthetic.id)
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))

    linked = await _call(lambda: printers.link_bambuddy(mine.id, 4, MERGED_BY))

    assert linked.id == mine.id and printers.get(synthetic.id) is None
    workflow_id = f"entity_merge:printer:{synthetic.id}:{mine.id}"
    described = await world.client.get_workflow_handle(workflow_id).describe()
    [repoint] = world.go.go.named("inventory.Repoint")
    assert repoint.arg["key"] == (
        f"entity_merge:printer:{synthetic.id}:{mine.id}:{described.run_id}:1"
    )
    assert _unit_printer(pg_pool, unit) == mine.id
    await _record(world, workflow_id, "merge")


async def test_a_slow_merge_answers_202_and_commits_by_itself(
    world: World, pg_pool: PgPool
) -> None:
    printers = world.printers
    synthetic = printers.ensure_bambuddy(5, serial=None, name="H2C", model="H2C", by=MERGED_BY)
    _unit(pg_pool, synthetic.id)
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))
    world.go.gates["Repoint"] = asyncio.Event()

    with pytest.raises(MergeRunningError) as running:
        await _call(lambda: printers.link_bambuddy(mine.id, 5, MERGED_BY))

    assert running.value.accepted.workflow_id == f"entity_merge:printer:{synthetic.id}:{mine.id}"
    assert printers.get(synthetic.id) is not None
    world.go.gates["Repoint"].set()
    await _until(lambda: printers.get(synthetic.id) is None)


async def test_a_stock_row_added_after_step_1_runs_both_again(
    world: World, pg_pool: PgPool
) -> None:
    printers = world.printers
    synthetic = printers.ensure_bambuddy(6, serial=None, name="H2C", model="H2C", by=MERGED_BY)
    _unit(pg_pool, synthetic.id)
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))
    added: list[UUID] = []

    def add_once(_: dict[str, Any]) -> None:
        if not added:
            added.append(_unit(pg_pool, synthetic.id))

    world.go.after["Repoint"] = add_once

    await _call(lambda: printers.link_bambuddy(mine.id, 6, MERGED_BY))

    assert printers.get(synthetic.id) is None
    assert world.go.runs["Repoint"] == 2
    assert _unit_printer(pg_pool, added[0]) == mine.id


async def test_a_refused_merge_moves_stock_back(world: World, pg_pool: PgPool) -> None:
    printers = world.printers
    synthetic = printers.ensure_bambuddy(7, serial=None, name="H2C", model="H2C", by=MERGED_BY)
    unit = _unit(pg_pool, synthetic.id)
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))

    def conflict(input: dict[str, Any]) -> None:
        if "only" not in input["body"]:
            with pg_pool.connection() as conn:
                conn.execute(
                    "UPDATE printers SET bambuddy_printer_id = 9 WHERE id = %s", (mine.id,)
                )

    world.go.after["Repoint"] = conflict

    with pytest.raises(BambuddyLinkTakenError):
        await _call(lambda: printers.link_bambuddy(mine.id, 7, MERGED_BY))

    assert world.go.runs["Repoint"] == 2
    assert _unit_printer(pg_pool, unit) == synthetic.id
    assert printers.get(synthetic.id) is not None
    await _record(world, f"entity_merge:printer:{synthetic.id}:{mine.id}", "merge_refused")
    # The conflict gone, the same link again: a new run of the same id merges.
    with pg_pool.connection() as conn:
        conn.execute("UPDATE printers SET bambuddy_printer_id = NULL WHERE id = %s", (mine.id,))
    world.go.after.clear()
    await _call(lambda: printers.link_bambuddy(mine.id, 7, MERGED_BY))
    assert printers.get(synthetic.id) is None


async def test_the_worker_ensure_waits_for_the_merge(world: World, pg_pool: PgPool) -> None:
    """The print worker's ensure waits with no cap of its own: the answer is the kept row
    once the slow Repoint ends, not a 202."""
    printers = world.printers
    printers.merges = TemporalStarter(world.client, world.library, world.starter.inventory_queue)
    printers.merges.bind(asyncio.get_running_loop())
    synthetic = printers.ensure_bambuddy(8, serial=None, name="H2C", model="H2C", by=MERGED_BY)
    _unit(pg_pool, synthetic.id)
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))
    world.go.gates["Repoint"] = asyncio.Event()

    async def release() -> None:
        await asyncio.sleep(WAIT * 2)
        world.go.gates["Repoint"].set()

    releasing = asyncio.create_task(release())
    row, wrote = await _call(
        lambda: printers.ensure_bambuddy_written(
            8, serial=SERIAL, name="H2C", model="H2C", by=MERGED_BY, wait=None
        )
    )
    await releasing

    assert row.id == mine.id and wrote
    assert printers.get(synthetic.id) is None


# ── fix round 1 (I1) ─────────────────────────────────────────────────────────────────


async def test_a_terminated_fulfil_is_taken_over_by_the_next_change(world: World) -> None:
    """A run that committed and then was terminated still holds its item. The unfulfil
    that waits for it starts it again; its first step finds its own commit, its Take runs
    once (the key's ledger), and it records. Then the unfulfil runs."""
    shop = world.shop
    order, item, _ = _with_part(shop)
    world.go.gates["Take"] = asyncio.Event()
    _, accepted = await _accept(world, order, item)
    assert isinstance(accepted, WorkflowAccepted)
    await world.client.get_workflow_handle(accepted.workflow_id).terminate("by hand")

    waiting = await _call(lambda: shop.lifecycle.unfulfil_item(order.id, item.id, BY))
    assert isinstance(waiting, WorkflowAccepted)
    world.go.gates["Take"].set()

    await _until(lambda: "parts.returned" in shop.kinds(order), 60.0)
    kinds = shop.kinds(order)
    assert kinds.count("parts.used") == 1
    assert kinds.index("parts.used") < kinds.index("parts.returned")
    assert len([m for m in shop.go.stock.moves(order_item_id=item.id) if m.kind == "use"]) == 1
    assert shop.get_item(item).parts_settling is None
    await _record(world, waiting.workflow_id, "takeover")


async def test_a_failed_take_is_recorded_and_frees_the_item(world: World) -> None:
    """A Go step that fails (past its schedule-to-close, or non-retryable) is recorded as
    `parts.stock_refused`, and the item no longer settles."""
    shop = world.shop
    order, item, _ = _with_part(shop)
    world.go.failing.add("Take")

    await _accept(world, order, item)

    await _until(lambda: shop.get_item(item).parts_settling is None)
    [refused] = [e for e in shop.rows.events(order.id) if e.kind == "parts.stock_refused"]
    assert refused.detail["status"] == 503
    assert refused.detail["problem"]["code"] == "inventory_step_failed"
    assert "parts.used" not in shop.kinds(order)
    await _record(world, f"order_item:{item.id}:parts:1", "take_failed")


async def _unfulfil_while_go_is_slow(world: World) -> tuple[Any, OrderItem]:
    """A fulfilled item's unfulfil, past its commit, with Go's `GiveBack` held: it answers
    202, and the item is `approved` again while its stock still settles."""
    shop = world.shop
    order, item, _ = _with_part(shop)
    await _accept(world, order, item)
    world.go.gates["GiveBack"] = asyncio.Event()
    waiting = await _call(lambda: shop.lifecycle.unfulfil_item(order.id, item.id, BY))
    assert isinstance(waiting, WorkflowAccepted)
    await _until(lambda: shop.get_item(item).status == "approved")
    assert shop.get_item(item).parts_settling is not None
    return order, shop.get_item(item)


async def test_an_accept_while_the_unfulfil_settles_is_the_next_cycle(world: World) -> None:
    """Not a replay of cycle 1's completed `parts:1` run: the accept is `parts:2`, it waits
    for the unfulfil, and then it takes the stock again."""
    shop = world.shop
    order, item = await _unfulfil_while_go_is_slow(world)

    attempt, accepted = await _accept(world, order, item)

    assert isinstance(accepted, WorkflowAccepted)
    assert accepted.workflow_id == f"order_item:{item.id}:parts:2"
    world.go.gates["GiveBack"].set()
    await _until(lambda: shop.kinds(order).count("parts.used") == 2, 40.0)
    assert shop.get_item(item).status == "fulfilled"
    assert shop.rows.fulfilment(attempt.id).accepted_at is not None  # type: ignore[union-attr]


async def test_a_fulfil_while_the_unfulfil_settles_is_not_lost(world: World) -> None:
    """The fulfil waits for the unfulfil and then commits, rather than failing
    `order_item_changed` with nobody told."""
    shop = world.shop
    order, item = await _unfulfil_while_go_is_slow(world)

    answer = await _call(
        lambda: shop.lifecycle.fulfil_items(
            order.id, [FulfilLine(item_id=item.id, quantity=1)], BY, request_key="w2"
        )
    )

    assert isinstance(answer, WorkflowAccepted)
    world.go.gates["GiveBack"].set()
    await _until(lambda: shop.kinds(order).count("parts.used") == 2, 40.0)
    assert shop.get_item(item).status == "fulfilled"
    result = await world.client.get_workflow_handle(answer.workflow_id).result()
    assert len(result) == 1


async def test_recost_goes_on_while_go_names_new_orders(world: World) -> None:
    """Past `RECOST_ROUNDS`, a re-cost runs again as long as each refusal names an order
    the round did not lock: a lot that many orders used re-costs in the end (PR #8323)."""
    shop = world.shop
    order, item, hook = _with_part(shop)
    await _accept(world, order, item)
    [lot] = shop.go.stock.lots(UUID(hook))
    shop.go.stock.set_lot_cost(lot.id, Decimal(6), BY)
    shop.go.answers["inventory.Recost"] = [
        {
            "status": 409,
            "body": {
                "code": "recost_orders_moved",
                "status": 409,
                "detail": "x",
                "orders": [f"order-unseen-{n}"],
            },
        }
        for n in range(4)
    ]

    result = await _call(lambda: shop.lifecycle.recost_lot(lot.id, BY, request_key="r3"))

    assert not isinstance(result, WorkflowAccepted) and result.moves == 1
    assert world.go.runs["Recost"] == 5
    [last] = shop.go.named("inventory.Recost")[-1:]
    assert set(last.arg["body"]["orders"]) >= {f"order-unseen-{n}" for n in range(4)}
    assert shop.kinds(order).count("item.parts_recosted") == 1
    # The `recost-until-no-new-orders` patch's scene, for `test_order_stock_replay.py`.
    await _record(world, f"lot:{lot.id}:recost:r3", "recost_rounds")


async def test_recost_stops_at_its_round_cap(world: World) -> None:
    """A lot whose orders keep moving: the re-cost fails with Go's last
    ``recost_orders_moved`` after `RECOST_MAX_ROUNDS`, and nothing is recorded."""
    shop = world.shop
    order, item, hook = _with_part(shop)
    await _accept(world, order, item)
    [lot] = shop.go.stock.lots(UUID(hook))
    shop.go.stock.set_lot_cost(lot.id, Decimal(6), BY)
    shop.go.answers["inventory.Recost"] = [
        {
            "status": 409,
            "body": {
                "code": "recost_orders_moved",
                "status": 409,
                "detail": "x",
                "orders": [f"order-moving-{n}"],
            },
        }
        for n in range(RECOST_MAX_ROUNDS + 5)
    ]

    try:
        answer = await _call(lambda: shop.lifecycle.recost_lot(lot.id, BY, request_key="r4"))
    except WorkflowRefused as refused:
        problem = refused.problem
    else:
        assert isinstance(answer, WorkflowAccepted)
        with pytest.raises(WorkflowFailureError) as failed:
            await world.client.get_workflow_handle(answer.workflow_id).result()
        cause = failed.value.cause
        assert isinstance(cause, ApplicationError)
        problem = cause.details[0]

    assert problem["code"] == "recost_orders_moved"
    assert f"after {RECOST_MAX_ROUNDS} rounds" in problem["detail"]
    assert world.go.runs["Recost"] == RECOST_MAX_ROUNDS
    assert shop.kinds(order).count("item.parts_recosted") == 0
