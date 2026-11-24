"""Lots, units and moves (#7067, spec 6.4 and 6.6): FIFO by `(received_at, id)` unless a
lot is named (D2), a shortfall on the newest lot, cost snapshots (D14), reversals at the
use's own cost, and "Re-cost past uses"."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from scadbuddy.entities.refs import EntityRef
from scadbuddy.entities.registry import EntityStore
from scadbuddy.inventory.items import ItemNotFound, ItemStore
from scadbuddy.inventory.kinds import KindStore
from scadbuddy.inventory.models import (
    AdjustInput,
    ItemInput,
    KindInput,
    Move,
    ReceiveLot,
    ReceiveUnits,
    RecostedItem,
    UnitStatusInput,
    UseInput,
)
from scadbuddy.inventory.stock import (
    BadQuantity,
    Conn,
    ItemNotCounted,
    LocationNotFound,
    LotNotFound,
    SerialExists,
    StockStore,
    UnitInstalled,
    UnitNotFound,
    WrongTracking,
)
from scadbuddy.orders.models import Actor
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

BY = Actor(kind="browser")


@pytest.fixture
def stock(pg_pool: PgPool) -> StockStore:
    return StockStore(pg_pool, EntityStore(pg_pool), lambda: "USD")


def _item(items: ItemStore, kind: str = "hardware", name: str = "M3 bolt") -> UUID:
    return items.create(ItemInput(kind_id=kind, name=name), BY).id


def _day(day: int) -> datetime:
    return datetime(2026, 11, day, 12, tzinfo=UTC)


def _two_lots(stock: StockStore, item: UUID) -> tuple[UUID, UUID]:
    """10 @ 5.00 on day 1 and 10 @ 7.00 on day 2 (minor units)."""
    first = stock.receive_lot(
        item, ReceiveLot(qty=Decimal(10), unit_cost_minor=Decimal(500), received_at=_day(1)), BY
    )
    second = stock.receive_lot(
        item, ReceiveLot(qty=Decimal(10), unit_cost_minor=Decimal(700), received_at=_day(2)), BY
    )
    return first.id, second.id


def _take(pg_pool: PgPool, stock: StockStore, item: UUID, qty: int, **kw: Any) -> list[Move]:
    with pg_pool.connection() as conn, conn.transaction():
        return stock.take(conn, item, Decimal(qty), BY, **kw)


def _shape(moves: list[Move]) -> list[tuple[object, ...]]:
    return [_row(m.qty, m.unit_cost_minor) for m in moves]


def _row(*values: object) -> tuple[object, ...]:
    """A tuple to compare with ints and strings (a Decimal equals its int)."""
    return values


def _count(pg_pool: PgPool, sql: str) -> int:
    with pg_pool.connection() as conn:
        row = conn.execute(sql.encode()).fetchone()
    assert row is not None
    return int(row["n"])


def _on_hand_is_the_sum_of_moves(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        bad = conn.execute(
            "SELECT l.id FROM stock_lots l WHERE l.on_hand <>"
            " (SELECT coalesce(sum(qty), 0) FROM stock_moves m WHERE m.lot_id = l.id)"
        ).fetchall()
    assert bad == []


def test_fifo_takes_the_oldest_lot_first(
    stock: StockStore, items: ItemStore, pg_pool: PgPool
) -> None:
    item = _item(items)
    first, second = _two_lots(stock, item)

    moves = _take(pg_pool, stock, item, 15, order_id="o1", order_item_id="oi1")

    assert _shape(moves) == [(-10, 500), (-5, 700)]
    assert [m.lot_id for m in moves] == [first, second]
    assert [m.amount_minor for m in moves] == [-5000, -3500]
    assert all(m.kind == "use" and m.order_item_id == "oi1" for m in moves)
    assert not any(m.needs_review for m in moves)
    assert [lot.on_hand for lot in stock.lots(item)] == [0, 5]
    _on_hand_is_the_sum_of_moves(pg_pool)


def test_a_named_lot_is_taken_first(stock: StockStore, items: ItemStore, pg_pool: PgPool) -> None:
    item = _item(items)
    first, second = _two_lots(stock, item)

    moves = _take(pg_pool, stock, item, 12, lot_id=second)

    assert [_row(m.lot_id, m.qty) for m in moves] == [(second, -10), (first, -2)]
    assert [lot.on_hand for lot in stock.lots(item)] == [8, 0]


def test_a_lot_of_another_item_is_not_found(
    stock: StockStore, items: ItemStore, pg_pool: PgPool
) -> None:
    item, other = _item(items), _item(items, name="M4 bolt")
    _, second = _two_lots(stock, item)

    with pytest.raises(LotNotFound):
        _take(pg_pool, stock, other, 1, lot_id=second)


def test_a_shortfall_goes_negative_on_the_newest_lot(
    stock: StockStore, items: ItemStore, pg_pool: PgPool
) -> None:
    item = _item(items)
    _, second = _two_lots(stock, item)

    moves = _take(pg_pool, stock, item, 25)

    assert _shape(moves) == [(-10, 500), (-10, 700), (-5, 700)]
    assert [m.needs_review for m in moves] == [False, False, True]
    assert moves[2].lot_id == second
    assert [lot.on_hand for lot in stock.lots(item)] == [0, -5]
    found = items.get(item)
    assert found is not None and found.on_hand == -5
    _on_hand_is_the_sum_of_moves(pg_pool)


def test_an_item_with_no_lot_opens_a_cost_unknown_lot(
    stock: StockStore, items: ItemStore, pg_pool: PgPool
) -> None:
    item = _item(items)

    (move,) = _take(pg_pool, stock, item, 3)

    (lot,) = stock.lots(item)
    assert _row(lot.origin, lot.unit_cost_minor, lot.cost_unknown, lot.on_hand) == (
        "adjustment",
        0,
        True,
        -3,
    )
    assert _row(move.lot_id, move.qty, move.cost_unknown, move.needs_review) == (
        lot.id,
        -3,
        True,
        True,
    )
    _on_hand_is_the_sum_of_moves(pg_pool)


def test_give_back_reverses_each_use_at_its_own_cost(
    stock: StockStore, items: ItemStore, pg_pool: PgPool
) -> None:
    """Review Focus 2: a lot cost edited after the use does not reach the return."""
    item = _item(items)
    first, second = _two_lots(stock, item)
    uses = _take(pg_pool, stock, item, 15, order_id="o1", order_item_id="oi1")
    stock.set_lot_cost(second, Decimal(900), BY)

    with pg_pool.connection() as conn, conn.transaction():
        back = stock.give_back(conn, order_item_id="oi1", item_id=item, by=BY)

    assert [_row(m.kind, m.lot_id, m.qty, m.unit_cost_minor) for m in back] == [
        ("return", first, 10, 500),
        ("return", second, 5, 700),
    ]
    assert [m.reverses_move_id for m in back] == [m.id for m in uses]
    assert all(m.order_id == "o1" and m.order_item_id == "oi1" for m in back)
    assert sum(m.amount_minor for m in uses + back) == 0
    assert [lot.on_hand for lot in stock.lots(item)] == [10, 10]
    with pg_pool.connection() as conn, conn.transaction():
        assert stock.give_back(conn, order_item_id="oi1", item_id=item, by=BY) == []
    _on_hand_is_the_sum_of_moves(pg_pool)


def test_give_back_reverses_a_shortfall_too(
    stock: StockStore, items: ItemStore, pg_pool: PgPool
) -> None:
    item = _item(items)
    _two_lots(stock, item)
    uses = _take(pg_pool, stock, item, 25, order_item_id="oi1")

    with pg_pool.connection() as conn, conn.transaction():
        back = stock.give_back(conn, order_item_id="oi1", item_id=item, by=BY)

    assert sum(m.amount_minor for m in uses + back) == 0
    assert [lot.on_hand for lot in stock.lots(item)] == [10, 10]


def test_set_lot_cost_moves_no_past_move(
    stock: StockStore, items: ItemStore, pg_pool: PgPool
) -> None:
    item = _item(items)
    _, second = _two_lots(stock, item)
    (use,) = _take(pg_pool, stock, item, 5, lot_id=second, order_item_id="oi1")

    lot = stock.set_lot_cost(second, Decimal(900), BY)

    assert _row(lot.unit_cost_minor, lot.cost_unknown) == (900, False)
    (again,) = stock.moves(order_item_id="oi1")
    assert _row(again.id, again.unit_cost_minor, again.amount_minor) == (use.id, 700, -3500)


def test_recost_resnapshots_and_names_the_order_items(
    stock: StockStore, items: ItemStore, pg_pool: PgPool
) -> None:
    item = _item(items)
    _, second = _two_lots(stock, item)
    _take(pg_pool, stock, item, 5, lot_id=second, order_id="o1", order_item_id="oi1")
    stock.set_lot_cost(second, Decimal(900), BY)

    with pg_pool.connection() as conn, conn.transaction():
        result = stock.recost_lot(conn, second)

    assert result.moves == 1
    assert result.orders == [
        RecostedItem(
            order_id="o1",
            order_item_id="oi1",
            before_minor=Decimal(3500),
            after_minor=Decimal(4500),
        )
    ]
    (use,) = stock.moves(order_item_id="oi1")
    assert _row(use.unit_cost_minor, use.amount_minor) == (900, -4500)
    # The receive keeps its own snapshot.
    receive = [m for m in stock.moves(lot_id=second) if m.kind == "receive"]
    assert [m.unit_cost_minor for m in receive] == [700]
    with pg_pool.connection() as conn, conn.transaction():
        assert stock.recost_lot(conn, second).moves == 0


def test_recost_keeps_a_use_and_its_return_netting_to_zero(
    stock: StockStore, items: ItemStore, pg_pool: PgPool
) -> None:
    item = _item(items)
    _, second = _two_lots(stock, item)
    _take(pg_pool, stock, item, 5, lot_id=second, order_id="o1", order_item_id="oi1")
    with pg_pool.connection() as conn, conn.transaction():
        stock.give_back(conn, order_item_id="oi1", item_id=item, by=BY)
    stock.set_lot_cost(second, Decimal(900), BY)

    with pg_pool.connection() as conn, conn.transaction():
        result = stock.recost_lot(conn, second)

    assert result.moves == 2
    assert result.orders == []
    assert sum(m.amount_minor for m in stock.moves(order_item_id="oi1")) == 0


def test_a_whole_number_uom_refuses_a_fraction(
    stock: StockStore, items: ItemStore, kinds: KindStore, pg_pool: PgPool
) -> None:
    bolt = _item(items)
    with pytest.raises(BadQuantity):
        stock.receive_lot(bolt, ReceiveLot(qty=Decimal("1.5")), BY)
    with pytest.raises(BadQuantity):
        stock.use(bolt, UseInput(qty=Decimal("1.5")), BY)
    with pytest.raises(BadQuantity):
        _take(pg_pool, stock, bolt, 0)
    assert stock.lots(bolt) == []

    grease = _item(items, kind="lubricant", name="Grease")
    stock.receive_lot(grease, ReceiveLot(qty=Decimal(10), unit_cost_minor=Decimal("0.333333")), BY)
    (move,) = stock.use(grease, UseInput(qty=Decimal("1.5")), BY)
    # round(-1.5 * 0.333333, 6), half away from zero as Postgres rounds.
    assert (move.qty, move.amount_minor) == (Decimal("-1.5"), Decimal("-0.500000"))


def test_a_lot_is_costed_from_its_total(stock: StockStore, items: ItemStore) -> None:
    item = _item(items)

    lot = stock.receive_lot(item, ReceiveLot(qty=Decimal(3), total_cost_minor=Decimal(1000)), BY)

    assert (lot.unit_cost_minor, lot.cost_unknown, lot.cost_currency) == (
        Decimal("333.333333"),
        False,
        None,
    )
    (receive,) = stock.moves(lot_id=lot.id)
    assert _row(receive.kind, receive.qty, receive.amount_minor) == (
        "receive",
        3,
        Decimal("999.999999"),
    )
    with pytest.raises(ValidationError):
        ReceiveLot(qty=Decimal(1), unit_cost_minor=Decimal(1), total_cost_minor=Decimal(1))


def test_a_lot_in_another_currency_opens_cost_unknown(stock: StockStore, items: ItemStore) -> None:
    """R-7067-11, D11: the shop is USD."""
    item = _item(items)

    lot = stock.receive_lot(
        item, ReceiveLot(qty=Decimal(4), unit_cost_minor=Decimal(250), currency="EUR"), BY
    )
    shop = stock.receive_lot(
        item, ReceiveLot(qty=Decimal(4), unit_cost_minor=Decimal(250), currency="USD"), BY
    )
    blank = stock.receive_lot(item, ReceiveLot(qty=Decimal(1)), BY)

    assert _row(lot.unit_cost_minor, lot.cost_unknown, lot.cost_currency) == (0, True, "EUR")
    assert _row(shop.unit_cost_minor, shop.cost_unknown, shop.cost_currency) == (250, False, None)
    assert _row(blank.unit_cost_minor, blank.cost_unknown) == (0, True)
    (receive,) = stock.moves(lot_id=lot.id)
    assert receive.cost_unknown is True


def test_a_lot_at_an_unknown_location_is_refused(stock: StockStore, items: ItemStore) -> None:
    item = _item(items)
    with pytest.raises(LocationNotFound):
        stock.receive_lot(item, ReceiveLot(qty=Decimal(1), location_id=uuid4()), BY)
    assert stock.lots(item) == []


def test_adjust_lot_writes_the_difference(
    stock: StockStore, items: ItemStore, pg_pool: PgPool
) -> None:
    item = _item(items)
    first, _ = _two_lots(stock, item)

    lot = stock.adjust_lot(first, AdjustInput(on_hand=Decimal(7), note="Shelf count"), BY)
    same = stock.adjust_lot(first, AdjustInput(on_hand=Decimal(7), note="Again"), BY)

    assert lot.on_hand == 7 and same.on_hand == 7
    adjusts = [m for m in stock.moves(lot_id=first) if m.kind == "adjust"]
    assert [_row(m.qty, m.unit_cost_minor, m.reason) for m in adjusts] == [(-3, 500, "Shelf count")]
    with pytest.raises(ValidationError):
        AdjustInput(on_hand=Decimal(1), note="  ")
    with pytest.raises(LotNotFound):
        stock.adjust_lot(uuid4(), AdjustInput(on_hand=Decimal(1), note="x"), BY)
    _on_hand_is_the_sum_of_moves(pg_pool)


def test_a_count_takes_fifo_down_and_adds_to_the_newest_lot_up(
    stock: StockStore, items: ItemStore, pg_pool: PgPool
) -> None:
    item = _item(items)
    first, second = _two_lots(stock, item)

    down = stock.count_item(item, Decimal(5), "Year end", BY)
    assert [_row(m.kind, m.lot_id, m.qty, m.reason) for m in down] == [
        ("adjust", first, -10, "Year end"),
        ("adjust", second, -5, "Year end"),
    ]
    up = stock.count_item(item, Decimal(8), "Found some", BY)
    assert [_row(m.kind, m.lot_id, m.qty, m.unit_cost_minor) for m in up] == [
        ("adjust", second, 3, 700)
    ]
    assert stock.count_item(item, Decimal(8), "Same", BY) == []
    assert [lot.on_hand for lot in stock.lots(item)] == [0, 8]

    empty = _item(items, name="Washer")
    (opened,) = stock.count_item(empty, Decimal(4), "First count", BY)
    (lot,) = stock.lots(empty)
    assert _row(lot.origin, lot.cost_unknown, lot.on_hand, opened.cost_unknown) == (
        "adjustment",
        True,
        4,
        True,
    )
    _on_hand_is_the_sum_of_moves(pg_pool)


def test_unit_status_writes_its_move(stock: StockStore, items: ItemStore) -> None:
    nozzle = _item(items, kind="nozzle", name="0.4 hardened")
    one, two = stock.receive_units(
        nozzle, ReceiveUnits(count=2, cost_minor=Decimal(3000), serials=["A1", " "]), BY
    )
    assert _row(one.serial, two.serial, one.status, one.cost_minor) == (
        "A1",
        None,
        "in_stock",
        3000,
    )
    assert [m.kind for m in stock.moves(item_id=nozzle)] == ["receive", "receive"]

    used = stock.set_unit_status(one.id, UnitStatusInput(status="in_service"), BY)
    assert used.status == "in_service" and used.in_service_at is not None
    retired = stock.set_unit_status(two.id, UnitStatusInput(status="retired", note="Worn"), BY)
    assert retired.retired_at is not None

    one_moves = stock.moves(unit_id=one.id)
    assert [_row(m.kind, m.qty, m.unit_cost_minor) for m in one_moves] == [
        ("use", -1, 3000),
        ("receive", 1, 3000),
    ]
    retire = stock.moves(unit_id=two.id)[0]
    assert _row(retire.kind, retire.qty, retire.reason) == ("retire", -1, "Worn")
    # Out of stock to out of stock moves no stock: the unit already left.
    sold = stock.set_unit_status(one.id, UnitStatusInput(status="sold"), BY)
    assert sold.status == "sold"
    assert len(stock.moves(unit_id=one.id)) == 2
    back = stock.set_unit_status(one.id, UnitStatusInput(status="in_stock"), BY)
    assert back.status == "in_stock"
    assert stock.moves(unit_id=one.id)[0].kind == "return"
    assert sum(m.qty for m in stock.moves(unit_id=one.id)) == 1
    found = items.get(nozzle)
    assert found is not None and found.on_hand == 1
    with pytest.raises(UnitNotFound):
        stock.set_unit_status(uuid4(), UnitStatusInput(status="sold"), BY)


def test_a_unit_status_names_its_entity(
    stock: StockStore, items: ItemStore, pg_pool: PgPool
) -> None:
    nozzle = _item(items, kind="nozzle", name="0.4")
    (unit,) = stock.receive_units(nozzle, ReceiveUnits(count=1), BY)
    other = _item(items, kind="hotend", name="Hotend")

    stock.set_unit_status(unit.id, UnitStatusInput(status="in_service", entity=f"item:{other}"), BY)

    (move, _) = stock.moves(unit_id=unit.id)
    with pg_pool.connection() as conn:
        assert move.entity_id == EntityStore(pg_pool).find(conn, EntityRef("item", str(other)))


def test_a_repeated_serial_is_refused(stock: StockStore, items: ItemStore) -> None:
    nozzle = _item(items, kind="nozzle", name="0.4")
    stock.receive_units(nozzle, ReceiveUnits(count=1, serials=["S1"]), BY)

    with pytest.raises(SerialExists):
        stock.receive_units(nozzle, ReceiveUnits(count=2, serials=["S2", "S1"]), BY)
    assert len(stock.units(nozzle)) == 1
    with pytest.raises(ValidationError):
        ReceiveUnits(count=2, serials=["only one"])


def test_an_installed_unit_is_refused(stock: StockStore, items: ItemStore, pg_pool: PgPool) -> None:
    """#7278 owns install and remove."""
    nozzle = _item(items, kind="nozzle", name="0.4")
    host, part = stock.receive_units(nozzle, ReceiveUnits(count=2), BY)
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE stock_units SET installed_in_unit_id = %s WHERE id = %s", (host.id, part.id)
        )

    with pytest.raises(UnitInstalled):
        stock.set_unit_status(part.id, UnitStatusInput(status="retired"), BY)


def test_lot_calls_on_a_unit_item_are_refused(
    stock: StockStore, items: ItemStore, pg_pool: PgPool
) -> None:
    nozzle = _item(items, kind="nozzle", name="0.4")
    bolt = _item(items)

    with pytest.raises(WrongTracking):
        stock.receive_lot(nozzle, ReceiveLot(qty=Decimal(1)), BY)
    with pytest.raises(WrongTracking):
        _take(pg_pool, stock, nozzle, 1)
    with pytest.raises(WrongTracking):
        stock.count_item(nozzle, Decimal(1), "x", BY)
    with pytest.raises(WrongTracking):
        stock.lots(nozzle)
    with pytest.raises(WrongTracking):
        stock.receive_units(bolt, ReceiveUnits(count=1), BY)
    with pytest.raises(WrongTracking):
        stock.units(bolt)
    with pytest.raises(ItemNotFound):
        stock.receive_lot(uuid4(), ReceiveLot(qty=Decimal(1)), BY)


def test_a_not_counted_item_takes_no_stock(
    stock: StockStore, items: ItemStore, pg_pool: PgPool
) -> None:
    """Ruling 1: a `none` kind has offers and prices, never lots, units or moves."""
    service = _item(items, kind="service", name="Laser cutting")
    printer = _item(items, kind="nozzle", name="A printer stand-in")

    calls: list[Callable[[], object]] = [
        lambda: stock.receive_lot(service, ReceiveLot(qty=Decimal(1)), BY),
        lambda: stock.receive_units(service, ReceiveUnits(count=1), BY),
        lambda: _take(pg_pool, stock, service, 1),
        lambda: stock.use(service, UseInput(qty=Decimal(1), entity=f"item:{printer}"), BY),
        lambda: stock.count_item(service, Decimal(1), "x", BY),
        lambda: stock.lots(service),
        lambda: stock.units(service),
    ]
    for call in calls:
        with pytest.raises(ItemNotCounted):
            call()

    assert _count(pg_pool, "SELECT count(*) AS n FROM stock_lots") == 0
    assert _count(pg_pool, "SELECT count(*) AS n FROM stock_units") == 0
    assert _count(pg_pool, "SELECT count(*) AS n FROM stock_moves") == 0
    assert _count(pg_pool, "SELECT count(*) AS n FROM entities") == 0


def test_use_names_its_entity(stock: StockStore, items: ItemStore, pg_pool: PgPool) -> None:
    grease = _item(items, kind="lubricant", name="Grease")
    target = _item(items, kind="nozzle", name="Target")
    stock.receive_lot(grease, ReceiveLot(qty=Decimal(10), unit_cost_minor=Decimal(5)), BY)

    (move,) = stock.use(
        grease, UseInput(qty=Decimal(2), entity=f"item:{target}", reason="Rails"), BY
    )

    with pg_pool.connection() as conn:
        assert move.entity_id == EntityStore(pg_pool).find(conn, EntityRef("item", str(target)))
    assert (move.kind, move.reason) == ("use", "Rails")


def test_estimate_matches_take(stock: StockStore, items: ItemStore, pg_pool: PgPool) -> None:
    item = _item(items)
    _two_lots(stock, item)
    moves_before = _count(pg_pool, "SELECT count(*) AS n FROM stock_moves")

    with pg_pool.connection() as conn:
        assert stock.estimate_unit_cost(conn, item, Decimal(15)) == (Decimal("566.666667"), False)
        # Past the stock on hand, at the newest lot's cost (Review Focus 1).
        assert stock.estimate_unit_cost(conn, item, Decimal(25)) == (Decimal(620), False)

    assert _count(pg_pool, "SELECT count(*) AS n FROM stock_moves") == moves_before
    assert [lot.on_hand for lot in stock.lots(item)] == [10, 10]
    moves = _take(pg_pool, stock, item, 15)
    assert sum(-m.amount_minor for m in moves) / 15 == pytest.approx(Decimal("566.666667"))

    empty = _item(items, name="Washer")
    with pg_pool.connection() as conn:
        assert stock.estimate_unit_cost(conn, empty, Decimal(2)) == (Decimal(0), True)


def test_a_wait_on_the_lots_takes_what_the_first_take_left(
    stock: StockStore, items: ItemStore, pg_pool: PgPool
) -> None:
    """`take` locks the item's lots `FOR UPDATE`: a second take waits and then takes
    from what the first left, not from what it read before the first committed."""
    item = _item(items)
    stock.receive_lot(item, ReceiveLot(qty=Decimal(10), unit_cost_minor=Decimal(500)), BY)

    second = _while_held(
        pg_pool,
        lambda conn: stock.take(conn, item, Decimal(8), BY),
        lambda: stock.use(item, UseInput(qty=Decimal(5)), BY),
    )

    assert [_row(m.qty, m.needs_review) for m in second] == [(-2, False), (-3, True)]
    assert [lot.on_hand for lot in stock.lots(item)] == [-3]
    _on_hand_is_the_sum_of_moves(pg_pool)


def test_two_takes_on_an_item_with_no_lot_open_one_lot(
    stock: StockStore, items: ItemStore, pg_pool: PgPool
) -> None:
    item = _item(items)

    _while_held(
        pg_pool,
        lambda conn: stock.take(conn, item, Decimal(2), BY),
        lambda: stock.use(item, UseInput(qty=Decimal(3)), BY),
    )

    assert [_row(lot.origin, lot.on_hand) for lot in stock.lots(item)] == [("adjustment", -5)]
    _on_hand_is_the_sum_of_moves(pg_pool)


def test_two_give_backs_return_each_use_once(
    stock: StockStore, items: ItemStore, pg_pool: PgPool
) -> None:
    item = _item(items)
    _two_lots(stock, item)
    _take(pg_pool, stock, item, 15, order_item_id="oi1")

    def give_back() -> list[Move]:
        with pg_pool.connection() as conn, conn.transaction():
            return stock.give_back(conn, order_item_id="oi1", item_id=item, by=BY)

    second = _while_held(
        pg_pool,
        lambda conn: stock.give_back(conn, order_item_id="oi1", item_id=item, by=BY),
        give_back,
    )

    assert second == []
    assert [lot.on_hand for lot in stock.lots(item)] == [10, 10]


def _while_held[T](pool: PgPool, first: Callable[[Conn], object], second: Callable[[], T]) -> T:
    """`first(conn)` in an open transaction, then `second()` in a thread, which must wait
    on it (`pg_blocking_pids`) and finish once it commits. Gives `second`'s result."""
    with pool.connection() as holder, ThreadPoolExecutor(1) as ex:
        waited = False
        with holder.transaction():
            first(holder)
            done = ex.submit(second)
            for _ in range(200):
                with pool.connection() as conn:
                    row = conn.execute(
                        "SELECT count(*) AS n FROM pg_stat_activity"
                        " WHERE %s = ANY(pg_blocking_pids(pid))",
                        (holder.info.backend_pid,),
                    ).fetchone()
                if row is not None and row["n"] > 0:
                    waited = True
                    break
                if done.done():
                    break
                threading.Event().wait(0.05)
        result = done.result(timeout=30)
    assert waited, "the second call never waited on the first's locks"
    return result


def test_a_kind_made_by_a_person_is_counted_by_its_tracking(
    stock: StockStore, items: ItemStore, kinds: KindStore
) -> None:
    kinds.create(KindInput(id="fastener", name="Fastener", parent_id="part", tracking="lot"), BY)
    item = _item(items, kind="fastener", name="Rivet")

    lot = stock.receive_lot(item, ReceiveLot(qty=Decimal(2), unit_cost_minor=Decimal(1)), BY)

    assert stock.lot(lot.id) == lot
    assert stock.lot(uuid4()) is None
    wire = lot.model_dump(mode="json")
    assert (wire["on_hand"], wire["unit_cost_minor"]) == ("2", "1")
