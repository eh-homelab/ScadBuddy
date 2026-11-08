"""The balance (#5281, storefront spec §9): accepting an order with a deposit or a payment
request opens a `balance` request for what is not paid yet; while that request stands and
the balance is above zero, a shipment is refused (`balance_unpaid`) until it is paid or
the shop ships without full payment (`waive_balance`). A back-office order with no
request ships as it always did."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from scadbuddy.orders.lifecycle import (
    LifecycleError,
    ManualQuoteInput,
    ShipmentEdit,
    ShipmentInput,
)
from scadbuddy.orders.models import Order, OrderItem, ShipmentItem
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop, requests_for, resolved, set_request_status

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


def with_deposit(shop: Shop, quantity: int = 1) -> tuple[Order, OrderItem]:
    """An order quoted at 1000 a unit, its deposit of 300 paid in cash, then accepted."""
    order = shop.order()
    item = shop.item(order, quantity)
    shop.quoted(order, item)
    shop.lifecycle.set_deposit(order.id, BY, 300)
    shop.lifecycle.add_payment(order.id, BY, amount_minor=300, method="cash")
    order = shop.lifecycle.accept_order(order.id, BY)
    return order, shop.get_item(item)


def ready(shop: Shop) -> tuple[Order, OrderItem]:
    order, item = with_deposit(shop)
    shop.fulfilled(order, item)
    return shop.get(order), shop.get_item(item)


def _slug(call: Callable[[], object]) -> str:
    with pytest.raises(LifecycleError) as e:
        call()
    return e.value.slug


def test_accepting_an_order_with_a_deposit_opens_the_balance_request(shop: Shop) -> None:
    order, _ = with_deposit(shop)
    found = requests_for(shop, order)
    assert [(r.kind, r.amount_minor, r.status) for r in found][-1] == ("balance", 700, "open")
    opened = [e.detail for e in shop.rows.events(order.id) if e.kind == "payment_request.opened"]
    assert opened[-1] == {"request_id": found[-1].id, "kind": "balance", "amount_minor": 700}


def test_a_fully_paid_order_opens_no_balance_request(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    shop.quoted(order, item)
    shop.lifecycle.set_deposit(order.id, BY, 1000)
    shop.lifecycle.add_payment(order.id, BY, amount_minor=1000, method="cash")
    order = shop.lifecycle.accept_order(order.id, BY)
    assert [r.kind for r in requests_for(shop, order)] == ["deposit"]


def _open_sum(shop: Shop, order: Order) -> int:
    return sum(r.amount_minor for r in requests_for(shop, order) if r.status == "open")


def test_an_open_payment_request_opens_the_balance_request(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    shop.quoted(order, item)
    first = shop.lifecycle.open_payment_request(order.id, BY, amount_minor=200, provider=None)
    order = shop.lifecycle.accept_order(order.id, BY)
    found = requests_for(shop, order)
    assert [(r.kind, r.amount_minor, r.status) for r in found] == [
        ("balance", 200, "cancelled"),
        ("balance", 1000, "open"),
    ]
    assert _open_sum(shop, order) == 1000
    cancels = [
        e.detail for e in shop.rows.events(order.id) if e.kind == "payment_request.cancelled"
    ]
    assert cancels == [{"request_id": first.id}]


def test_a_balance_request_that_asks_the_rest_is_kept_at_accept(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    shop.quoted(order, item)
    first = shop.lifecycle.open_payment_request(order.id, BY, provider=None)
    order = shop.lifecycle.accept_order(order.id, BY)
    assert [(r.id, r.amount_minor, r.status) for r in requests_for(shop, order)] == [
        (first.id, 1000, "open")
    ]
    kinds = shop.kinds(order)
    assert kinds.count("payment_request.opened") == 1
    assert "payment_request.cancelled" not in kinds


def test_a_deposit_and_a_balance_request_leave_one_request_at_accept(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    shop.quoted(order, item)
    shop.lifecycle.set_deposit(order.id, BY, 300)
    shop.lifecycle.open_payment_request(order.id, BY, provider=None)
    order = shop.lifecycle.accept_order(order.id, BY)
    assert [(r.kind, r.status) for r in requests_for(shop, order)][-1] == ("balance", "open")
    assert _open_sum(shop, order) == 1000


def test_a_cancelled_request_alone_opens_no_balance_request(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    shop.quoted(order, item)
    first = shop.lifecycle.open_payment_request(order.id, BY, amount_minor=200, provider=None)
    shop.lifecycle.cancel_payment_request(order.id, first.id, BY)
    order = shop.lifecycle.accept_order(order.id, BY)
    assert [(r.kind, r.status) for r in requests_for(shop, order)] == [("balance", "cancelled")]


def test_accepting_cancels_the_open_deposit_request(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    shop.quoted(order, item)
    shop.lifecycle.set_deposit(order.id, BY, 300)
    shop.lifecycle.waive_deposit(order.id, BY, "known customer")
    order = shop.lifecycle.accept_order(order.id, BY)
    found = requests_for(shop, order)
    assert [(r.kind, r.amount_minor, r.status) for r in found] == [
        ("deposit", 300, "cancelled"),
        ("balance", 1000, "open"),
    ]
    events = shop.rows.events(order.id)
    cancels = [e.detail for e in events if e.kind == "payment_request.cancelled"]
    assert cancels == [{"request_id": found[0].id}]


def test_a_back_office_order_with_no_request_ships_as_today(shop: Shop) -> None:
    order, item = shop.accepted()
    shop.fulfilled(order, item)
    assert requests_for(shop, order) == []
    shop.shipped(order, item)


def test_an_unpaid_balance_refuses_a_shipment_and_a_pickup(shop: Shop) -> None:
    order, item = ready(shop)
    assert _slug(lambda: shop.shipped(order, item)) == "balance_unpaid"
    pickup = ShipmentInput(
        carrier="Pickup", items=[ShipmentItem(order_item_id=item.id, quantity=1)]
    )
    assert _slug(lambda: shop.lifecycle.add_shipment(order.id, pickup, BY)) == "balance_unpaid"
    assert "shipment.added" not in shop.kinds(order)


def test_paying_the_balance_lets_it_ship(shop: Shop) -> None:
    order, item = ready(shop)
    bal = requests_for(shop, order)[-1]
    shop.lifecycle.add_payment(order.id, BY, amount_minor=700, payment_request_id=bal.id)
    shop.shipped(order, item)


def test_a_cancelled_balance_request_does_not_hold_the_shipment(shop: Shop) -> None:
    order, item = ready(shop)
    bal = requests_for(shop, order)[-1]
    shop.lifecycle.cancel_payment_request(order.id, bal.id, BY)
    shop.shipped(order, item)


def test_ship_without_full_payment_needs_a_reason(shop: Shop) -> None:
    order, item = ready(shop)
    with pytest.raises(LifecycleError) as error:
        shop.lifecycle.waive_balance(order.id, BY, "  ")
    assert (error.value.slug, error.value.unprocessable) == ("reason", True)
    shop.events.clear()
    order = shop.lifecycle.waive_balance(order.id, BY, "  pays on pickup ")
    assert order.balance_waived_reason == "pays on pickup" and "balance.waived" in shop.kinds(order)
    [event] = [e for e in shop.rows.events(order.id) if e.kind == "balance.waived"]
    assert event.detail == {"reason": "pays on pickup"}
    assert shop.events
    shop.shipped(order, item)


def test_the_same_waiver_twice_writes_one_row(shop: Shop) -> None:
    order, _ = ready(shop)
    shop.lifecycle.waive_balance(order.id, BY, "pays on pickup")
    shop.events.clear()
    again = shop.lifecycle.waive_balance(order.id, BY, "pays on pickup")
    assert again.balance_waived_reason == "pays on pickup"
    assert shop.kinds(order).count("balance.waived") == 1
    assert shop.events == []
    changed = shop.lifecycle.waive_balance(order.id, BY, "known customer")
    assert changed.balance_waived_reason == "known customer"
    waivers = [e.detail for e in shop.rows.events(order.id) if e.kind == "balance.waived"]
    assert waivers == [{"reason": "pays on pickup"}, {"reason": "known customer"}]


def test_a_paid_balance_request_still_holds_a_grown_balance(shop: Shop) -> None:
    order, item = with_deposit(shop)
    bal = requests_for(shop, order)[-1]
    shop.lifecycle.add_payment(order.id, BY, amount_minor=700, payment_request_id=bal.id)
    added = shop.lifecycle.add_item(
        order.id, BY, resolved(), output_id=None, quantity=1, after_acceptance=True
    )
    quote = shop.lifecycle.record_manual_quote(
        order.id, added.id, ManualQuoteInput(unit_price_minor=400, label="typed"), BY
    )
    shop.lifecycle.accept_quote(order.id, added.id, quote.id, BY)
    shop.fulfilled(order, shop.get_item(item))
    shop.fulfilled(order, shop.get_item(added))
    order = shop.get(order)
    assert _slug(lambda: shop.shipped(order, item, shop.get_item(added))) == "balance_unpaid"


def test_the_balance_cannot_be_waived_before_acceptance(shop: Shop) -> None:
    order = shop.order()
    assert _slug(lambda: shop.lifecycle.waive_balance(order.id, BY, "x")) == "order_not_accepted"
    cancelled = shop.lifecycle.cancel_order(shop.order().id, BY)
    assert _slug(lambda: shop.lifecycle.waive_balance(cancelled.id, BY, "x")) == "order_cancelled"
    order, item = shop.accepted()
    shop.fulfilled(order, item)
    shop.shipped(order, item)
    closed = shop.lifecycle.close_order(order.id, BY)
    assert _slug(lambda: shop.lifecycle.waive_balance(closed.id, BY, "x")) == "order_closed"


def test_a_shipment_edit_that_raises_a_quantity_is_held_by_the_balance(shop: Shop) -> None:
    order, item = with_deposit(shop, quantity=3)
    bal = requests_for(shop, order)[-1]
    shop.lifecycle.add_payment(order.id, BY, amount_minor=2700, payment_request_id=bal.id)
    shop.fulfilled(order, item)
    shipment = shop.lifecycle.add_shipment(
        order.id,
        ShipmentInput(carrier="Post", items=[ShipmentItem(order_item_id=item.id, quantity=2)]),
        BY,
    )
    shop.lifecycle.add_payment(order.id, BY, amount_minor=-100, method="cash")

    def lines(quantity: int) -> ShipmentEdit:
        return ShipmentEdit(items=[ShipmentItem(order_item_id=item.id, quantity=quantity)])

    raised = lines(3)
    assert (
        _slug(lambda: shop.lifecycle.update_shipment(order.id, shipment.id, raised, BY))
        == "balance_unpaid"
    )
    edited = shop.lifecycle.update_shipment(
        order.id, shipment.id, ShipmentEdit(carrier="Courier", tracking="T1"), BY
    )
    assert (edited.carrier, edited.tracking) == ("Courier", "T1")
    same = shop.lifecycle.update_shipment(order.id, shipment.id, lines(2), BY)
    assert [line.quantity for line in same.items] == [2]
    lowered = shop.lifecycle.update_shipment(order.id, shipment.id, lines(1), BY)
    assert [line.quantity for line in lowered.items] == [1]


def test_nothing_to_waive_writes_nothing(shop: Shop) -> None:
    order, _ = shop.accepted()
    shop.events.clear()
    before = shop.kinds(order)
    same = shop.lifecycle.waive_balance(order.id, BY, "pays on pickup")
    assert same.balance_waived_reason is None
    assert shop.kinds(order) == before and shop.events == []
    paid, _ = with_deposit(shop)
    bal = requests_for(shop, paid)[-1]
    shop.lifecycle.add_payment(paid.id, BY, amount_minor=700, payment_request_id=bal.id)
    shop.events.clear()
    before = shop.kinds(paid)
    assert shop.lifecycle.waive_balance(paid.id, BY, "x").balance_waived_reason is None
    assert shop.kinds(paid) == before and shop.events == []


def test_moving_quantity_between_items_is_held_by_the_balance(shop: Shop) -> None:
    order = shop.order()
    first, second = shop.item(order), shop.item(order)
    shop.quoted(order, first)
    shop.quoted(order, second)
    shop.lifecycle.set_deposit(order.id, BY, 300)
    shop.lifecycle.add_payment(order.id, BY, amount_minor=300, method="cash")
    order = shop.lifecycle.accept_order(order.id, BY)
    bal = requests_for(shop, order)[-1]
    shop.lifecycle.add_payment(order.id, BY, amount_minor=1700, payment_request_id=bal.id)
    shop.fulfilled(order, shop.get_item(first))
    shop.fulfilled(order, shop.get_item(second))
    shipment = shop.shipped(order, shop.get_item(first))
    shop.lifecycle.add_payment(order.id, BY, amount_minor=-100, method="cash")
    moved = ShipmentEdit(items=[ShipmentItem(order_item_id=second.id, quantity=1)])
    assert (
        _slug(lambda: shop.lifecycle.update_shipment(order.id, shipment.id, moved, BY))
        == "balance_unpaid"
    )


def test_a_pending_request_holds_the_acceptance(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    shop.quoted(order, item)
    req = shop.lifecycle.open_payment_request(order.id, BY, amount_minor=200, provider=None)
    set_request_status(shop, req.id, "pending")
    before, told = shop.kinds(order), len(shop.events)
    with pytest.raises(LifecycleError) as e:
        shop.lifecycle.accept_order(order.id, BY)
    assert (e.value.slug, e.value.unprocessable, e.value.not_found) == (
        "payment_in_flight",
        False,
        False,
    )
    assert shop.get(order).status == "quoted"
    assert shop.kinds(order) == before and len(shop.events) == told


def test_a_failed_request_is_cancelled_at_accept(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    shop.quoted(order, item)
    req = shop.lifecycle.open_payment_request(order.id, BY, provider=None)
    set_request_status(shop, req.id, "failed")
    order = shop.lifecycle.accept_order(order.id, BY)
    assert [(r.kind, r.amount_minor, r.status) for r in requests_for(shop, order)] == [
        ("balance", 1000, "cancelled"),
        ("balance", 1000, "open"),
    ]
    cancels = [
        e.detail for e in shop.rows.events(order.id) if e.kind == "payment_request.cancelled"
    ]
    assert cancels == [{"request_id": req.id}]
