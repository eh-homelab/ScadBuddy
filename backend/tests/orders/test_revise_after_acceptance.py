"""An accepted item revised after acceptance (#5350, spec 2026-10-18 §3.5 "As built"): the
new design waits for its own quote and that quote's acceptance, and the old accepted price
stands until then. Refused before acceptance, once shipped, ended or on hold, on a settled
item, on an item with no accepted quote, and while a print runs. The lock stays per item:
the quantity stays pinned, the old quote is never accepted again."""

from __future__ import annotations

import pytest

from scadbuddy.orders.lifecycle import (
    FulfilmentInput,
    LifecycleError,
    ManualQuoteInput,
    requote_open,
)
from scadbuddy.orders.models import Order, OrderItem, Quote
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_add_after_acceptance import START
from tests.orders.test_lifecycle import BY, Shop, resolved

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


def revise(shop: Shop, order: Order, item: OrderItem, slug: str = "other") -> None:
    shop.lifecycle.add_revision(order.id, item.id, BY, resolved(slug), after_acceptance=True)


def accepted_quote(shop: Shop, item: OrderItem) -> Quote | None:
    return next((q for q in shop.rows.quotes(item.id) if q.accepted), None)


def requote(shop: Shop, order: Order, item: OrderItem, price: int) -> Quote:
    return shop.lifecycle.record_manual_quote(
        order.id, item.id, ManualQuoteInput(unit_price_minor=price, label="new design"), BY
    )


def test_the_old_price_stands_until_the_new_quote_is_accepted(shop: Shop) -> None:
    order, item = shop.accepted()
    old = accepted_quote(shop, item)
    assert old is not None

    revise(shop, order, item)

    item = shop.get_item(item)
    assert (item.status, item.revised_after_acceptance) == ("draft", True)
    assert item.reference is not None and item.reference.slug == "other"
    # The agreed price is still the accepted one.
    assert accepted_quote(shop, item) == old
    assert shop.get(order).status == "accepted"
    rows = [e for e in shop.rows.events(order.id) if e.kind == "revision.added_after_acceptance"]
    assert len(rows) == 1
    assert rows[0].detail["order_status"] == "accepted"
    assert rows[0].detail["accepted_quote_id"] == old.id
    assert rows[0].detail["revision_id"] == item.current_revision_id

    new = requote(shop, order, item, 1200)
    item = shop.get_item(item)
    assert item.status == "quoted"
    # A quote recorded is not yet the price: the old one stands.
    assert accepted_quote(shop, item) == old
    # The old quote prices the earlier design: it is never accepted again.
    with pytest.raises(LifecycleError) as stale:
        shop.lifecycle.accept_quote(order.id, item.id, old.id, BY)
    assert stale.value.slug == "quote_stale"

    item = shop.lifecycle.accept_quote(order.id, item.id, new.id, BY)
    assert (item.status, item.revised_after_acceptance) == ("approved", False)
    assert accepted_quote(shop, item) == new.model_copy(update={"accepted": True})
    assert [q.accepted for q in shop.rows.quotes(item.id)] == [False, True]


@pytest.mark.parametrize("start", ["accepted", "in_production"])
def test_accepted_and_in_production_take_it(shop: Shop, start: str) -> None:
    order, item = START[start](shop)
    revise(shop, order, item)
    item = shop.get_item(item)
    # A failed attempt's item is `draft` too: it is not printed at the old quote.
    assert (item.status, item.revised_after_acceptance) == ("draft", True)
    assert shop.get(order).status == start


def test_a_ready_order_refuses_it_for_its_fulfilled_item(shop: Shop) -> None:
    order, item = START["ready"](shop)
    with pytest.raises(LifecycleError) as refused:
        revise(shop, order, item)
    assert refused.value.slug == "item_not_open"


@pytest.mark.parametrize("status", ["draft", "quoted"])
def test_before_acceptance_is_refused(shop: Shop, status: str) -> None:
    order = shop.order()
    item = shop.item(order)
    if status == "quoted":
        item = shop.quoted(order, item)
    assert shop.get(order).status == status
    with pytest.raises(LifecycleError) as refused:
        revise(shop, order, item)
    assert refused.value.slug == "order_not_accepted"
    assert f"/items/{item.id}/revisions" in str(refused.value)


def test_a_shipped_order_refuses_it(shop: Shop) -> None:
    order, item = shop.accepted()
    other = shop.lifecycle.add_item(
        order.id, BY, resolved(), output_id=None, quantity=1, after_acceptance=True
    )
    shop.quoted(order, other)
    shop.fulfilled(order, shop.get_item(item))
    shop.shipped(order, shop.get_item(item))
    # The other item is not done, but the order shipped what it fulfilled.
    shop.lifecycle.cancel_item(order.id, other.id, BY)
    assert shop.get(order).status == "shipped"
    with pytest.raises(LifecycleError) as refused:
        revise(shop, order, shop.get_item(item))
    # Every item of a shipped order is settled, so the item's own check answers first.
    assert refused.value.slug == "item_not_open"


@pytest.mark.parametrize("end", ["cancel", "close"])
def test_an_ended_order_refuses_it(shop: Shop, end: str) -> None:
    order, item = shop.accepted()
    if end == "cancel":
        shop.lifecycle.cancel_order(order.id, BY)
    else:
        shop.fulfilled(order, item)
        shop.shipped(order, shop.get_item(item))
        shop.lifecycle.close_order(order.id, BY)
    with pytest.raises(LifecycleError) as refused:
        revise(shop, order, item)
    assert refused.value.slug == f"order_{'cancelled' if end == 'cancel' else 'closed'}"


def test_on_hold_is_refused(shop: Shop) -> None:
    order, item = shop.accepted()
    shop.lifecycle.hold(order.id, BY)
    with pytest.raises(LifecycleError) as refused:
        revise(shop, order, item)
    assert refused.value.slug == "order_on_hold"
    assert shop.get_item(item).revised_after_acceptance is False
    shop.lifecycle.release(order.id, BY)
    revise(shop, order, item)
    # On hold again, the new quote is recorded but not accepted.
    shop.lifecycle.hold(order.id, BY)
    new = requote(shop, order, item, 1200)
    with pytest.raises(LifecycleError) as held:
        shop.lifecycle.accept_quote(order.id, item.id, new.id, BY)
    assert held.value.slug == "order_on_hold"


def test_a_cancelled_item_is_refused(shop: Shop) -> None:
    order, item = shop.accepted()
    other = shop.lifecycle.add_item(
        order.id, BY, resolved(), output_id=None, quantity=1, after_acceptance=True
    )
    shop.quoted(order, other)
    shop.lifecycle.cancel_item(order.id, item.id, BY)
    with pytest.raises(LifecycleError) as refused:
        revise(shop, order, item)
    assert refused.value.slug == "item_not_open"


def test_an_item_with_no_accepted_quote_uses_the_plain_route(shop: Shop) -> None:
    order, _ = shop.accepted()
    added = shop.lifecycle.add_item(
        order.id, BY, resolved(), output_id=None, quantity=1, after_acceptance=True
    )
    with pytest.raises(LifecycleError) as refused:
        revise(shop, order, added)
    assert refused.value.slug == "item_not_priced"
    # The plain route takes it (#5349).
    shop.lifecycle.add_revision(order.id, added.id, BY, resolved("other"))


def test_a_custom_item_is_refused(shop: Shop) -> None:
    order, _ = shop.accepted()
    custom = shop.lifecycle.add_custom_item(
        order.id,
        BY,
        title="Tag",
        quantity=1,
        unit_price_minor=300,
        accept=False,
        after_acceptance=True,
    )
    with pytest.raises(LifecycleError) as refused:
        revise(shop, order, custom)
    assert refused.value.slug == "item_custom"


def test_the_plain_route_refuses_an_accepted_item_and_names_this_one(shop: Shop) -> None:
    order, item = shop.accepted()
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.add_revision(order.id, item.id, BY, resolved("other"))
    assert refused.value.slug == "item_locked"
    assert f"/items/{item.id}/revisions/after-acceptance" in str(refused.value)


def test_the_lock_stays_per_item(shop: Shop) -> None:
    order, item = shop.accepted()
    assert not requote_open(order, item, shop.rows.quotes(item.id))
    revise(shop, order, item)
    item = shop.get_item(item)
    assert requote_open(shop.get(order), item, shop.rows.quotes(item.id))
    # The quantity stays pinned: it would reprice the old quote.
    with pytest.raises(LifecycleError) as pinned:
        shop.lifecycle.update_item(order.id, item.id, BY, quantity=3)
    assert pinned.value.slug == "item_locked"
    # A further design takes the same path, and the old price still stands.
    old = accepted_quote(shop, item)
    shop.lifecycle.add_revision(order.id, item.id, BY, resolved("third"))
    assert accepted_quote(shop, shop.get_item(item)) == old
    # Nothing is printed or fulfilled at the old quote.
    with pytest.raises(LifecycleError) as unapproved:
        shop.lifecycle.record_fulfilment(
            order.id, item.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
        )
    assert unapproved.value.slug == "item_not_approved"


def test_a_cancelled_and_reopened_item_still_waits_for_its_quote(shop: Shop) -> None:
    order, item = shop.accepted()
    other = shop.lifecycle.add_item(
        order.id, BY, resolved(), output_id=None, quantity=1, after_acceptance=True
    )
    shop.quoted(order, other)
    revise(shop, order, item)
    shop.lifecycle.cancel_item(order.id, item.id, BY)
    item = shop.lifecycle.reopen_item(order.id, item.id, BY)
    assert (item.status, item.revised_after_acceptance) == ("draft", True)
    requote(shop, order, item, 1200)
    shop.lifecycle.cancel_item(order.id, item.id, BY)
    item = shop.lifecycle.reopen_item(order.id, item.id, BY)
    assert item.status == "quoted"


def test_a_print_in_flight_is_refused(shop: Shop) -> None:
    order, item = shop.accepted()
    shop.lifecycle.claim_print(
        order.id, item.id, BY, request_id="req-1", sliced_sha256="a" * 64, sliced_blob_key="q-a"
    )
    assert shop.get_item(item).status == "queued"
    with pytest.raises(LifecycleError) as refused:
        revise(shop, order, item)
    assert refused.value.slug == "print_in_flight"
