"""The charges changed on an order past `quoted` (#5351, spec 2026-10-18 §3.5 "As built"):
only `accepted`, `in_production` and `ready` take the change, never on hold; the audit row
``charges.replaced_after_acceptance`` holds the charges before and after; the plain edit
still refuses charges past `quoted` and names the route. A zero-price custom item added
after acceptance keeps the agreed total."""

from __future__ import annotations

import pytest

from scadbuddy.core.events import OrderChanged
from scadbuddy.orders.lifecycle import LifecycleError, OrderEdit
from scadbuddy.orders.models import ChargeInput
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_add_after_acceptance import START, accepted, ready
from tests.orders.test_lifecycle import BY, Shop

pytestmark = pytest.mark.requires_postgres

POST = ChargeInput(kind="shipping", label="Post", amount_minor=500)
CAP = ChargeInput(kind="discount", label="Agreed cap", amount_minor=-300)


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


def change(shop: Shop, order_id: str, *charges: ChargeInput) -> None:
    shop.lifecycle.replace_charges_after_acceptance(order_id, BY, list(charges))


@pytest.mark.parametrize("start", sorted(START))
def test_the_charges_change_and_the_audit_row_holds_before_and_after(
    shop: Shop, start: str
) -> None:
    order, _ = START[start](shop)
    shop.events.clear()

    changed = shop.lifecycle.replace_charges_after_acceptance(order.id, BY, [POST, CAP])

    assert changed.status == start
    assert [(c.kind, c.amount_minor, c.position) for c in shop.rows.charges(order.id)] == [
        ("shipping", 500, 0),
        ("discount", -300, 1),
    ]
    rows = [e for e in shop.rows.events(order.id) if e.kind == "charges.replaced_after_acceptance"]
    assert len(rows) == 1
    assert rows[0].detail == {
        "order_status": start,
        "before": [{"kind": "shipping", "label": "Post", "amount_minor": 500}],
        "after": [
            {"kind": "shipping", "label": "Post", "amount_minor": 500},
            {"kind": "discount", "label": "Agreed cap", "amount_minor": -300},
        ],
    }
    # Published after commit, as every order write is.
    assert [type(e) for e in shop.events] == [OrderChanged]
    # The status and the accepted prices do not move.
    assert shop.get(order).status == start


def test_the_plain_edit_refuses_charges_past_quoted_and_names_the_route(shop: Shop) -> None:
    order, _ = accepted(shop)
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.edit_order(order.id, BY, OrderEdit(charges=[CAP]))
    assert refused.value.slug == "order_not_editable"
    assert f"PUT /orders/{order.id}/charges/after-acceptance" in str(refused.value)
    # Notes still change there.
    shop.lifecycle.edit_order(order.id, BY, OrderEdit(notes="rush"))


@pytest.mark.parametrize("status", ["draft", "quoted"])
def test_before_acceptance_is_refused(shop: Shop, status: str) -> None:
    order = shop.order()
    if status == "quoted":
        shop.quoted(order, shop.item(order))
    assert shop.get(order).status == status
    with pytest.raises(LifecycleError) as refused:
        change(shop, order.id, CAP)
    assert refused.value.slug == "order_not_accepted"
    assert f"PATCH /orders/{order.id}" in str(refused.value)


def test_shipped_closed_and_cancelled_are_refused(shop: Shop) -> None:
    order, item = ready(shop)
    shop.shipped(order, item)
    assert shop.get(order).status == "shipped"
    with pytest.raises(LifecycleError) as refused:
        change(shop, order.id, CAP)
    assert refused.value.slug == "order_not_editable"
    shop.lifecycle.close_order(order.id, BY)
    with pytest.raises(LifecycleError) as refused:
        change(shop, order.id, CAP)
    assert refused.value.slug == "order_closed"
    other, _ = accepted(shop)
    shop.lifecycle.cancel_order(other.id, BY)
    with pytest.raises(LifecycleError) as refused:
        change(shop, other.id, CAP)
    assert refused.value.slug == "order_cancelled"
    for each in (order, other):
        assert [c.amount_minor for c in shop.rows.charges(each.id)] == [500]
        assert "charges.replaced_after_acceptance" not in shop.kinds(each)


@pytest.mark.parametrize("start", sorted(START))
def test_on_hold_is_refused(shop: Shop, start: str) -> None:
    order, _ = START[start](shop)
    shop.lifecycle.hold(order.id, BY)
    with pytest.raises(LifecycleError) as refused:
        change(shop, order.id, CAP)
    assert refused.value.slug == "order_on_hold"
    assert [c.amount_minor for c in shop.rows.charges(order.id)] == [500]
    assert "charges.replaced_after_acceptance" not in shop.kinds(order)
    shop.lifecycle.release(order.id, BY)
    change(shop, order.id, CAP)
    assert [c.amount_minor for c in shop.rows.charges(order.id)] == [-300]


def test_a_zero_price_item_is_added_and_accepted_after_acceptance(shop: Shop) -> None:
    """#5540's after-acceptance custom item takes a zero price, and its quote's acceptance
    takes it too: no new path is needed for a no-charge item."""
    order, _ = accepted(shop)
    item = shop.lifecycle.add_custom_item(
        order.id,
        BY,
        title="Extra tag, no charge",
        quantity=2,
        unit_price_minor=0,
        accept=False,
        after_acceptance=True,
    )
    assert item.status == "quoted"
    (quote,) = shop.rows.quotes(item.id)
    assert (quote.total_minor, quote.accepted) == (0, False)
    approved = shop.lifecycle.accept_quote(order.id, item.id, quote.id, BY)
    assert approved.status == "approved"
    assert [q.accepted for q in shop.rows.quotes(item.id)] == [True]
    assert shop.get(order).status == "accepted"
