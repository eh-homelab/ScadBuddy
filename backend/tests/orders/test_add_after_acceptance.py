"""An item added to an order past `quoted` (#5349, spec 2026-10-18 §3.5 "As built"): only
`accepted`, `in_production` and `ready` take one, never on hold; the item starts unpriced,
needs its own quote and that quote's acceptance, and leaves the accepted prices and the
charges alone; the order's status follows `derive`; the audit row is
``item.added_after_acceptance``."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from scadbuddy.core.events import OrderChanged, OrderItemChanged
from scadbuddy.orders.lifecycle import (
    FulfilmentInput,
    LifecycleError,
    OrderEdit,
    derive,
    item_open,
)
from scadbuddy.orders.models import ChargeInput, Order, OrderItem
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop, resolved

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


def accepted(shop: Shop) -> tuple[Order, OrderItem]:
    """An accepted order with one priced item and a shipping charge."""
    order = shop.order()
    shop.lifecycle.edit_order(
        order.id,
        BY,
        OrderEdit(charges=[ChargeInput(kind="shipping", label="Post", amount_minor=500)]),
    )
    item = shop.item(order)
    shop.quoted(order, item)
    return shop.lifecycle.accept_order(order.id, BY), shop.get_item(item)


def in_production(shop: Shop) -> tuple[Order, OrderItem]:
    order, item = accepted(shop)
    shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=1, outcome="failed"), BY
    )
    return shop.get(order), shop.get_item(item)


def ready(shop: Shop) -> tuple[Order, OrderItem]:
    order, item = accepted(shop)
    shop.fulfilled(order, item)
    return shop.get(order), shop.get_item(item)


START: dict[str, Callable[[Shop], tuple[Order, OrderItem]]] = {
    "accepted": accepted,
    "in_production": in_production,
    "ready": ready,
}
#: What `derive` gives each start once an unquoted item stands beside the first: a `ready`
#: order is not ready while an item is not done, and its fulfilled item has an attempt.
AFTER = {"accepted": "accepted", "in_production": "in_production", "ready": "in_production"}


def add(shop: Shop, order: Order) -> OrderItem:
    return shop.lifecycle.add_item(
        order.id, BY, resolved(), output_id=None, quantity=2, after_acceptance=True
    )


@pytest.mark.parametrize("start", sorted(START))
def test_an_item_is_added_unpriced_and_the_status_follows_derive(shop: Shop, start: str) -> None:
    order, first = START[start](shop)
    assert order.status == start
    before = shop.rows.quotes(first.id)
    shop.events.clear()

    item = add(shop, order)

    assert (item.status, item.quantity) == ("draft", 2)
    assert shop.rows.quotes(item.id) == []
    assert shop.get(order).status == AFTER[start]
    # The accepted price and the charges stay as they were.
    assert shop.rows.quotes(first.id) == before
    assert [c.amount_minor for c in shop.rows.charges(order.id)] == [500]
    # The audit row names the item and the status the order had; no plain `item.added`.
    added = [e for e in shop.rows.events(order.id) if e.kind == "item.added_after_acceptance"]
    assert len(added) == 1
    assert added[0].detail == {
        "item_id": item.id,
        "title": item.title,
        "quantity": 2,
        "order_status": start,
    }
    assert [e.detail["item_id"] for e in shop.rows.events(order.id) if e.kind == "item.added"] == [
        first.id
    ]
    # Published after commit, as every item write is.
    told = [
        (type(e).__name__, getattr(e, "item_id", None), e.status)
        for e in shop.events
        if isinstance(e, OrderChanged | OrderItemChanged)
    ]
    assert ("OrderItemChanged", item.id, "draft") in told
    assert ("OrderChanged", None, AFTER[start]) in told


@pytest.mark.parametrize("start", sorted(START))
def test_the_new_item_is_quoted_accepted_and_made(shop: Shop, start: str) -> None:
    order, first = START[start](shop)
    item = add(shop, order)
    # Not made or printed before its quote is accepted.
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.record_fulfilment(
            order.id, item.id, FulfilmentInput(quantity=2, outcome="succeeded"), BY
        )
    assert refused.value.slug == "item_not_approved"
    # Its quantity still changes, as on a draft order: it has no accepted price.
    item = shop.lifecycle.update_item(order.id, item.id, BY, quantity=3)
    assert (item.status, item.quantity) == ("draft", 3)
    approved = shop.quoted(order, item, price=700)
    assert approved.status == "approved"
    assert shop.get(order).status == AFTER[start]
    # Once its quote is accepted, it is pinned like the others.
    with pytest.raises(LifecycleError) as locked:
        shop.lifecycle.update_item(order.id, item.id, BY, quantity=4)
    assert locked.value.slug == "item_locked"
    if start != "ready":
        with pytest.raises(LifecycleError) as locked:
            shop.lifecycle.update_item(order.id, first.id, BY, quantity=4)
        assert locked.value.slug == "item_locked"
        shop.fulfilled(order, shop.get_item(first))
    shop.fulfilled(order, shop.get_item(item))
    assert shop.get(order).status == "ready"


def test_a_request_and_a_custom_item_are_added_too(shop: Shop) -> None:
    order, _ = accepted(shop)
    request = shop.lifecycle.add_request_item(
        order.id, BY, title="A dragon", quantity=1, after_acceptance=True
    )
    assert request.status == "draft"
    # Its first revision gives it a design: it has no accepted price to pin.
    revision = shop.lifecycle.add_revision(order.id, request.id, BY, resolved())
    assert revision.number == 1
    custom = shop.lifecycle.add_custom_item(
        order.id,
        BY,
        title="Gift wrap",
        quantity=1,
        unit_price_minor=300,
        accept=False,
        after_acceptance=True,
    )
    # Its price is recorded, never accepted as it is saved.
    assert custom.status == "quoted"
    assert [q.accepted for q in shop.rows.quotes(custom.id)] == [False]
    kinds = [
        str(e.detail.get("item_kind"))
        for e in shop.rows.events(order.id)
        if e.kind == "item.added_after_acceptance"
    ]
    assert sorted(kinds) == ["custom", "request"]
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.add_custom_item(
            order.id, BY, title="Rush", quantity=1, unit_price_minor=1, after_acceptance=True
        )
    assert (refused.value.slug, refused.value.unprocessable) == ("accept", True)


def test_the_plain_add_is_refused_past_quoted_and_names_the_route(shop: Shop) -> None:
    order, _ = accepted(shop)
    with pytest.raises(LifecycleError) as refused:
        shop.item(order)
    assert refused.value.slug == "item_locked"
    assert "/items/after-acceptance" in str(refused.value)


@pytest.mark.parametrize("status", ["draft", "quoted"])
def test_the_add_after_acceptance_is_refused_before_acceptance(shop: Shop, status: str) -> None:
    order = shop.order()
    if status == "quoted":
        shop.quoted(order, shop.item(order))
    assert shop.get(order).status == status
    with pytest.raises(LifecycleError) as refused:
        add(shop, order)
    assert refused.value.slug == "order_not_accepted"


def test_shipped_closed_and_cancelled_are_refused(shop: Shop) -> None:
    order, item = ready(shop)
    shop.shipped(order, item)
    assert shop.get(order).status == "shipped"
    with pytest.raises(LifecycleError) as refused:
        add(shop, order)
    assert refused.value.slug == "item_locked"
    shop.lifecycle.close_order(order.id, BY)
    with pytest.raises(LifecycleError) as refused:
        add(shop, order)
    assert refused.value.slug == "order_closed"
    other, _ = accepted(shop)
    shop.lifecycle.cancel_order(other.id, BY)
    with pytest.raises(LifecycleError) as refused:
        add(shop, other)
    assert refused.value.slug == "order_cancelled"


@pytest.mark.parametrize("start", sorted(START))
def test_on_hold_is_refused(shop: Shop, start: str) -> None:
    order, _ = START[start](shop)
    shop.lifecycle.hold(order.id, BY)
    with pytest.raises(LifecycleError) as refused:
        add(shop, order)
    assert refused.value.slug == "order_on_hold"
    assert len(shop.rows.items(order.id)) == 1
    assert "item.added_after_acceptance" not in shop.kinds(order)


def test_derive_with_an_unquoted_item_beside_accepted_ones(shop: Shop) -> None:
    order, first = ready(shop)
    item = add(shop, order)
    order = order.model_copy(update={"status": "ready"})
    items = [shop.get_item(first), item]
    # The fulfilled item has an attempt: in production while the new one is not done.
    assert derive(order, items, [], attempted={first.id}) == "in_production"
    # With no attempt anywhere, the order is plainly accepted.
    approved = shop.get_item(first).model_copy(update={"status": "approved"})
    assert derive(order, [approved, item], []) == "accepted"


def test_item_open_is_the_items_own_price(shop: Shop) -> None:
    order, first = accepted(shop)
    item = add(shop, order)
    assert item_open(order, shop.rows.quotes(item.id)) is True
    assert item_open(order, shop.rows.quotes(first.id)) is False
    shipped = order.model_copy(update={"status": "shipped"})
    assert item_open(shipped, shop.rows.quotes(item.id)) is False
