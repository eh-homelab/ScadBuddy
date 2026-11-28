"""The orders' stock workflows run in line, for the Python tests that need no Temporal
(#6949 K1 Task 16). `InlineStarter` is a `StockStarter` that runs each workflow's steps in
the workflow's own order (`workflows/order_stock.py`): the Python steps are the real ones
(`OrderStockSteps`, `printers/merge_activities.py` `merge_commit`); Go's activities are
`GoStock`, phase 2's Python stock writes (`tests/support/python_stock.py`) behind Go's
`{status, body}` answers, with Go's idempotency ledger by key. The real workflows on a real
Temporal are `tests/workflows/test_order_stock.py`'s (CI)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

from temporalio.exceptions import ApplicationError

from scadbuddy.core.problems import ApiError
from scadbuddy.entities.registry import ENSURE_ENTITY_SQL, EntityStore
from scadbuddy.inventory.core_client import Done
from scadbuddy.inventory.stock import BadQuantity, LotNotFound, estimate_unit_cost
from scadbuddy.orders.models import Actor
from scadbuddy.orders.stock_activities import OrderStockSteps
from scadbuddy.orders.stock_start import (
    FULFIL_ORDER_ITEM,
    MERGE_PRINTER,
    RECOST_LOT,
    WAIT,
    WorkflowAccepted,
    WorkflowRefused,
)
from scadbuddy.printers.merge_activities import merge_commit
from scadbuddy.printers.store import PrinterStore
from tests.support.python_stock import StockStore

INVENTORY_PROBLEMS = "https://scadbuddy.dev/problems/inventory/"


def _refused(status: int, code: str, detail: str, **members: Any) -> dict[str, Any]:
    return {
        "status": status,
        "body": {
            "type": INVENTORY_PROBLEMS + code,
            "title": "Refused",
            "status": status,
            "detail": detail,
            "code": code,
            **members,
        },
    }


@dataclass
class GoCall:
    name: str
    arg: dict[str, Any]


class GoStock:
    """Go's `inventory.Take`, `GiveBack`, `Recost` and `Repoint`, and its estimate and move
    reads, over phase 2's stock writes. `answers` maps an activity name to answers given
    before the real one, in turn: a `{status, body}` dict, or an exception it raises."""

    def __init__(self, pool: Any, shop_currency: Callable[[], str] = lambda: "USD") -> None:
        self.pool = pool
        self.stock = StockStore(pool, EntityStore(pool), shop_currency)
        self.calls: list[GoCall] = []
        self.answers: dict[str, list[Any]] = {}
        self._ledger: dict[str, dict[str, Any]] = {}
        self._writing = threading.Lock()

    def named(self, name: str) -> list[GoCall]:
        return [c for c in self.calls if c.name == name]

    def run(self, name: str, arg: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(GoCall(name, arg))
        queued = self.answers.get(name)
        if queued:
            answer = queued.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return dict(answer)
        key = arg["key"]
        # One write per key, as Go's `crud_requests` row makes it: two runs of one key at
        # once (a terminated run's activity still running beside its new run's) wait here.
        with self._writing:
            if key in self._ledger:
                return self._ledger[key]
            made: dict[str, Any] = getattr(self, "_" + name.removeprefix("inventory."))(arg)
            self._ledger[key] = made
            return made

    def _Take(self, arg: dict[str, Any]) -> dict[str, Any]:  # noqa: N802
        by = Actor.model_validate(arg["actor"])
        item = UUID(arg["params"]["item_id"])
        body = arg["body"]
        try:
            with self.pool.connection() as conn, conn.transaction():
                self.stock.lock_items(conn, [item])
                moves = self.stock.take(
                    conn,
                    item,
                    Decimal(body["qty"]),
                    by,
                    order_id=body.get("order_id"),
                    order_item_id=body.get("order_item_id"),
                )
        except BadQuantity as error:
            return _refused(422, "bad_quantity", f"bad quantity {error}")
        return {"status": 200, "body": {"moves": [m.model_dump(mode="json") for m in moves]}}

    def _GiveBack(self, arg: dict[str, Any]) -> dict[str, Any]:  # noqa: N802
        by = Actor.model_validate(arg["actor"])
        item = UUID(arg["params"]["item_id"])
        try:
            with self.pool.connection() as conn, conn.transaction():
                moves = self.stock.give_back(
                    conn, order_item_id=arg["body"]["order_item_id"], item_id=item, by=by
                )
        except BadQuantity as error:
            return _refused(422, "bad_quantity", f"bad quantity {error}")
        return {"status": 200, "body": {"moves": [m.model_dump(mode="json") for m in moves]}}

    def _Recost(self, arg: dict[str, Any]) -> dict[str, Any]:  # noqa: N802
        lot = UUID(arg["params"]["lot_id"])
        locked = set(arg["body"]["orders"])
        try:
            with self.pool.connection() as conn, conn.transaction():
                named = conn.execute(
                    "SELECT DISTINCT order_id FROM stock_moves WHERE lot_id = %s"
                    " AND order_id IS NOT NULL ORDER BY order_id",
                    (lot,),
                ).fetchall()
                other = [r["order_id"] for r in named if r["order_id"] not in locked]
                if other:
                    return _refused(
                        409,
                        "recost_orders_moved",
                        "a move of the lot names an order the re-cost did not lock",
                        orders=other,
                    )
                result = self.stock.recost_lot(conn, lot)
        except BadQuantity as error:
            return _refused(422, "bad_quantity", f"bad quantity {error}")
        except LotNotFound:
            return _refused(404, "lot_not_found", f"no lot '{lot}'")
        return {"status": 200, "body": result.model_dump(mode="json")}

    def _Repoint(self, arg: dict[str, Any]) -> dict[str, Any]:  # noqa: N802
        body = arg["body"]
        gone, kept, only = body["gone"], body["kept"], body.get("only")
        with self.pool.connection() as conn, conn.transaction():
            gone_id = conn.execute(
                "SELECT id FROM entities WHERE kind = 'printer' AND ref = %s", (gone,)
            ).fetchone()
            moves: list[str] = []
            if gone_id is not None:
                kept_id = conn.execute(ENSURE_ENTITY_SQL, ("printer", kept)).fetchone()
                assert kept_id is not None
                rows = conn.execute(
                    "UPDATE stock_moves SET entity_id = %s WHERE entity_id = %s"
                    " AND (%s::uuid[] IS NULL OR id = ANY(%s::uuid[])) RETURNING id",
                    (
                        kept_id["id"],
                        gone_id["id"],
                        None if only is None else only["moves"],
                        None if only is None else only["moves"],
                    ),
                ).fetchall()
                moves = sorted(str(r["id"]) for r in rows)
            units = conn.execute(
                "UPDATE stock_units SET printer_id = %s WHERE printer_id = %s"
                " AND (%s::uuid[] IS NULL OR id = ANY(%s::uuid[])) RETURNING id",
                (
                    kept,
                    gone,
                    None if only is None else only["units"],
                    None if only is None else only["units"],
                ),
            ).fetchall()
        return {
            "status": 200,
            "body": {"moved": {"moves": moves, "units": sorted(str(r["id"]) for r in units)}},
        }

    async def read(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        """Go's reads the steps make: an item's estimate, and the moves of a lot."""
        params = dict(params or {})
        if path.endswith("/estimate"):
            item = UUID(path.split("/")[2])
            try:
                with self.pool.connection() as conn:
                    cost, unknown = estimate_unit_cost(conn, item, Decimal(str(params["qty"])))
            except BadQuantity:
                raise ApiError(422, "bad quantity", code="bad_quantity") from None
            return {"unit_cost_minor": str(cost), "cost_unknown": unknown}
        if path == "/moves":
            moves = self.stock.moves(lot_id=UUID(params["lot_id"]), limit=int(params["limit"]))
            return {"moves": [m.model_dump(mode="json") for m in moves]}
        raise AssertionError(f"no read {path}")


@dataclass
class Started:
    workflow: str
    id: str
    arg: dict[str, Any]


@dataclass
class InlineStarter:
    """A `StockStarter` that runs the workflow's steps in line and answers `Done`, or
    raises `WorkflowRefused` as a failed run would. ``accept`` makes every start answer
    `WorkflowAccepted` without running anything (a workflow still running)."""

    steps: Callable[[], OrderStockSteps] | None
    go: GoStock
    printers: Callable[[], PrinterStore] | None = None
    started: list[Started] = field(default_factory=list)
    accept: bool = False

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        pass

    def start_and_wait(
        self, workflow: str, arg: Mapping[str, Any], *, id: str, wait: float | None = WAIT
    ) -> Done | WorkflowAccepted:
        from datetime import UTC, datetime

        self.started.append(Started(workflow, id, dict(arg)))
        if self.accept:
            return WorkflowAccepted(workflow_id=id, kind=workflow, started_at=datetime.now(UTC))
        try:
            # A thread of its own: the caller may be on a running loop (an async test).
            with ThreadPoolExecutor(1) as pool:
                body = pool.submit(asyncio.run, self._run(workflow, dict(arg), id)).result()
            return Done(body)
        except ApplicationError as refused:
            raise WorkflowRefused(refused.details[0]) from None

    async def _run(self, workflow: str, arg: dict[str, Any], workflow_id: str) -> Any:
        if workflow == FULFIL_ORDER_ITEM:
            return await self._fulfil(arg, workflow_id)
        if workflow == RECOST_LOT:
            return await self._recost(arg, workflow_id)
        assert workflow == MERGE_PRINTER
        return await self._merge(arg)

    async def _fulfil(self, arg: dict[str, Any], workflow_id: str) -> Any:
        assert self.steps is not None
        steps = self.steps()
        step = await steps.fulfil_commit({**arg, "workflow_id": workflow_id})
        assert step["wait_for"] is None, "in line, no other workflow of the item is open"
        unfulfil = arg["op"] == "unfulfil"
        results = []
        for call in step["calls"]:
            if unfulfil:
                answer = self.go.run(
                    "inventory.GiveBack",
                    {
                        "key": call["key"],
                        "actor": arg["actor"],
                        "params": {"item_id": call["item_id"]},
                        "body": {"order_item_id": call["order_item_id"]},
                    },
                )
            else:
                answer = self.go.run(
                    "inventory.Take",
                    {
                        "key": call["key"],
                        "actor": arg["actor"],
                        "params": {"item_id": call["item_id"]},
                        "body": {
                            "qty": call["qty"],
                            "order_id": arg["order_id"],
                            "order_item_id": call["order_item_id"],
                        },
                    },
                )
            results.append({**call, "status": answer["status"], "body": answer["body"]})
        return await steps.parts_record(
            {
                "workflow_id": workflow_id,
                "op": arg["op"],
                "order_id": arg["order_id"],
                "item_ids": [line["item_id"] for line in arg["lines"]],
                "attempts": step["attempts"],
                "results": results,
                "actor": arg["actor"],
            }
        )

    async def _recost(self, arg: dict[str, Any], workflow_id: str) -> Any:
        assert self.steps is not None
        steps = self.steps()
        also: list[str] = []
        answer: dict[str, Any] = {}
        attempt = 0
        while True:
            attempt += 1
            orders = await steps.recost_orders({"lot_id": arg["lot_id"], "also": also})
            answer = self.go.run(
                "inventory.Recost",
                {
                    "key": f"{workflow_id}:{attempt}",
                    "actor": arg["actor"],
                    "params": {"lot_id": arg["lot_id"]},
                    "body": {"orders": orders},
                },
            )
            if answer["status"] < 400:
                return await steps.recost_record(
                    {
                        "workflow_id": workflow_id,
                        "lot_id": arg["lot_id"],
                        "result": answer["body"],
                        "actor": arg["actor"],
                    }
                )
            if answer["body"].get("code") != "recost_orders_moved":
                break
            named = set(answer["body"].get("orders") or ())
            also = sorted(set(also) | named)
            # `RecostLot`: three rounds, then on while Go names an order not locked.
            if attempt >= 3 and not named - set(orders):
                break
            if attempt >= 50:  # `RECOST_MAX_ROUNDS`
                break
        problem = {**answer["body"], "status": answer["status"]}
        raise ApplicationError(problem["detail"], problem, non_retryable=True)

    async def _merge(self, arg: dict[str, Any]) -> Any:
        assert self.printers is not None
        store = self.printers()
        gone, kept = arg["gone"], arg["kept"]
        moved: list[dict[str, Any]] = []
        for round_ in range(1, 6):
            key = f"entity_merge:printer:{gone}:{kept}:inline:{round_}"
            answer = self.go.run(
                "inventory.Repoint",
                {
                    "key": key,
                    "actor": arg["actor"],
                    "params": {},
                    "body": {"kind": "printer", "gone": gone, "kept": kept},
                },
            )
            moved.append(answer["body"]["moved"])
            try:
                step = await merge_commit(store, {**arg})
            except ApplicationError:
                only = {
                    "moves": sorted({i for m in moved for i in m["moves"]}),
                    "units": sorted({i for m in moved for i in m["units"]}),
                }
                self.go.run(
                    "inventory.Repoint",
                    {
                        "key": f"{key}:back",
                        "actor": arg["actor"],
                        "params": {},
                        "body": {"kind": "printer", "gone": kept, "kept": gone, "only": only},
                    },
                )
                raise
            if not step.get("again"):
                return step
        raise AssertionError("in line, a merge commits within five rounds")


def inline_starter(core: Any, components: Any) -> InlineStarter:
    """`stock_start.make_starter` for an app under test: the steps over its own orders
    and printers, and Go's stock over its rows pool."""
    from scadbuddy.orders.component import ORDERS
    from scadbuddy.printers.component import PRINTERS
    from scadbuddy.rows.component import ROWS

    assert components is not None
    go = GoStock(
        components.get(ROWS).pool, lambda: core.settings_store.snapshot().runtime.orders_currency
    )
    return InlineStarter(
        lambda: OrderStockSteps(lambda: components.get(ORDERS).lifecycle, lambda: go),
        go,
        lambda: components.get(PRINTERS).store,
    )


def run_in_line(lifecycle: Any, pool: Any) -> GoStock:
    """Gives a test's own `Lifecycle` an `InlineStarter` over Go's stock on ``pool``; the
    `GoStock`, whose ``calls`` and ``stock`` the test reads."""
    go = GoStock(pool, lambda: lifecycle.rules().shop_currency)
    lifecycle.starter = InlineStarter(lambda: OrderStockSteps(lambda: lifecycle, lambda: go), go)
    return go


def app_go(client: Any) -> GoStock:
    """The `GoStock` of the app under test (`TestClient`): its orders' starter's."""
    from scadbuddy.api.deps import STATE_ATTR
    from scadbuddy.orders.component import ORDERS

    state = getattr(client.app.state, STATE_ATTR)
    starter = state.components.get(ORDERS).lifecycle.starter
    assert isinstance(starter, InlineStarter)
    return starter.go
