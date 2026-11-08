"""The balance (#5281, storefront spec §9): accepting an order with a deposit or a payment
request opens a `balance` request for what is not paid yet; while that request stands and
the balance is above zero, a shipment is refused (`balance_unpaid`) until it is paid or
the shop ships without full payment (`waive_balance`). A back-office order with no
request ships as it always did."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from scadbuddy.orders.lifecycle import LifecycleError, ManualQuoteInput, ShipmentInput
from scadbuddy.orders.models import Order, OrderItem, ShipmentItem
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop, requests_for, resolved

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


def with_deposit(shop: Shop) -> tuple[Order, OrderItem]:
    """An order quoted at 1000, its deposit of 300 paid in cash, then accepted."""
    order = shop.order()
    item = shop.item(order)
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


def test_an_order_with_a_payment_request_opens_the_balance_request(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    shop.quoted(order, item)
    first = shop.lifecycle.open_payment_request(order.id, BY, amount_minor=200, provider=None)
    shop.lifecycle.cancel_payment_request(order.id, first.id, BY)
    order = shop.lifecycle.accept_order(order.id, BY)
    assert [(r.kind, r.amount_minor, r.status) for r in requests_for(shop, order)] == [
        ("balance", 200, "cancelled"),
        ("balance", 1000, "open"),
    ]


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
