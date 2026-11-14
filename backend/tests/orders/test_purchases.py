"""Purchases, the handling fee and the `purchase_pending` gate (#6133, spec 2026-11-13
§10): the shop records what it bought for a customer as one cost entry and one
`purchase` charge, written together under the order's lock; the `handling` charge follows
the recorded purchases; and a purchase still `pending` stops the accepts and the balance
request."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import psycopg
import pytest

from scadbuddy.core.events import OrderChanged
from scadbuddy.orders.lifecycle import LifecycleError, OrderEdit
from scadbuddy.orders.models import (
    Actor,
    ChargeInput,
    CostEntryInput,
    Order,
    OrderItem,
    PurchaseRecordInput,
    PurchaseUpdateInput,
)
from scadbuddy.orders.money import OrderRules, handling_minor
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.storefront.test_customer_actor import storefront_order
from tests.orders.test_lifecycle import BY, RulesShop, requests_for

pytestmark = pytest.mark.requires_postgres

#: No tax, a flat 2.00 and 10 % per recorded purchase.
FEES = OrderRules(
    "USD", Decimal(0), False, (), (), purchase_fee_flat_minor=200, purchase_fee_percent=Decimal(10)
)
NO_FEES = OrderRules("USD", Decimal(0), False, (), ())
LINK = "https://parts.example.com/magnets?ref=abc"


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> RulesShop:
    return RulesShop(rows, BlobRefs(pool), NO_FEES)


def quoted(shop: RulesShop, currency: str = "USD") -> tuple[Order, OrderItem]:
    """A back-office order, `quoted`, with one custom item at 10.00."""
    order = shop.lifecycle.create_order(shop.customer.id, BY, currency=currency)
    item = shop.lifecycle.add_custom_item(
        order.id, BY, title="Box", quantity=1, unit_price_minor=1000, accept=True
    )
    return shop.get(order), item


def purchase(
    shop: RulesShop, order: Order, item: OrderItem, *, note: str = "", url: str = LINK
) -> str:
    """A customer's purchase link on ``item``, `pending`, as a request writes it."""
    with shop.rows.transaction() as conn:
        position = conn.execute(
            "SELECT count(*) AS n FROM order_item_sources WHERE item_id = %s", (item.id,)
        ).fetchone()
        assert position is not None
        row = conn.execute(
            "INSERT INTO order_item_sources (order_id, item_id, position, kind, purpose, url,"
            " link_type, note, status) VALUES (%s, %s, %s, 'link', 'purchase', %s, 'manual',"
            " %s, 'pending') RETURNING id",
            (order.id, item.id, position["n"], url, note),
        ).fetchone()
    assert row is not None
    return str(row["id"])


def record(shop: RulesShop, order: Order, source_id: str, amount: int) -> None:
    shop.lifecycle.record_purchase(
        order.id, source_id, PurchaseRecordInput(amount_minor=amount), BY
    )


def charges(shop: RulesShop, order: Order) -> list[tuple[str, str, int]]:
    return [(c.kind, c.label, c.amount_minor) for c in shop.rows.charges(order.id)]


def refused(call: object) -> LifecycleError:
    with pytest.raises(LifecycleError) as caught:
        call()  # type: ignore[operator]
    return caught.value


# -- record, update, decline -------------------------------------------------------------


def test_a_recorded_purchase_writes_a_cost_entry_a_charge_and_the_source(
    shop: RulesShop,
) -> None:
    order, item = quoted(shop)
    source_id = purchase(shop, order, item, note="Neodymium magnets 6x3")
    shop.events.clear()

    view, written = shop.lifecycle.record_purchase(
        order.id,
        source_id,
        PurchaseRecordInput(amount_minor=899, cost_kind="material", note="amazon order 1"),
        BY,
    )

    assert written
    [entry] = shop.rows.cost_entries(order.id)
    assert (entry.kind, entry.amount_minor, entry.source, entry.order_item_id) == (
        "material",
        899,
        f"purchase:{source_id}",
        item.id,
    )
    assert entry.note == "amazon order 1"
    [charge] = [c for c in shop.rows.charges(order.id) if c.kind == "purchase"]
    assert (charge.label, charge.amount_minor, charge.purchase_link_id) == (
        "Bought for your project: Neodymium magnets 6x3",
        899,
        source_id,
    )
    assert (view.status, view.cost_entry_id, view.charge_id) == ("recorded", entry.id, charge.id)
    assert (view.amount_minor, view.cost_kind) == (899, "material")
    last = shop.rows.events(order.id)[-1]
    assert last.kind == "purchase.recorded"
    assert last.detail == {
        "source_id": source_id,
        "item_id": item.id,
        "amount_minor": 899,
        "cost_kind": "material",
        "entry_id": entry.id,
        "charge_id": charge.id,
    }
    assert any(isinstance(e, OrderChanged) for e in shop.events)


def test_a_repeated_record_writes_once(shop: RulesShop) -> None:
    order, item = quoted(shop)
    source_id = purchase(shop, order, item)
    record(shop, order, source_id, 500)
    events = len(shop.rows.events(order.id))

    view, written = shop.lifecycle.record_purchase(
        order.id, source_id, PurchaseRecordInput(amount_minor=700), BY
    )

    assert not written
    assert view.amount_minor == 500
    assert len(shop.rows.cost_entries(order.id)) == 1
    assert [c.amount_minor for c in shop.rows.charges(order.id) if c.kind == "purchase"] == [500]
    assert len(shop.rows.events(order.id)) == events


def test_the_label_names_the_host_without_a_note_and_is_at_most_200_characters(
    shop: RulesShop,
) -> None:
    order, item = quoted(shop)
    plain = purchase(shop, order, item, url="https://WWW.Parts.Example.com:8443/x")
    long = purchase(shop, order, item, note="m" * 500)
    record(shop, order, plain, 100)
    record(shop, order, long, 100)
    labels = [c.label for c in shop.rows.charges(order.id) if c.kind == "purchase"]
    assert labels[0] == "Bought for your project: www.parts.example.com"
    assert len(labels[1]) == 200
    assert labels[1].startswith("Bought for your project: mmm")


def test_an_update_changes_the_entry_and_the_charge_together(shop: RulesShop) -> None:
    order, item = quoted(shop)
    source_id = purchase(shop, order, item)
    record(shop, order, source_id, 500)

    view = shop.lifecycle.update_purchase(
        order.id,
        source_id,
        PurchaseUpdateInput(amount_minor=650, cost_kind="other", note="the paid file"),
        BY,
    )

    [entry] = shop.rows.cost_entries(order.id)
    assert (entry.amount_minor, entry.kind, entry.note) == (650, "other", "the paid file")
    assert [c.amount_minor for c in shop.rows.charges(order.id) if c.kind == "purchase"] == [650]
    assert (view.amount_minor, view.cost_kind, view.status) == (650, "other", "recorded")
    last = shop.rows.events(order.id)[-1]
    assert (last.kind, last.detail["before_minor"], last.detail["amount_minor"]) == (
        "purchase.updated",
        500,
        650,
    )


def test_an_update_of_a_purchase_not_recorded_is_refused(shop: RulesShop) -> None:
    order, item = quoted(shop)
    source_id = purchase(shop, order, item)
    error = refused(
        lambda: shop.lifecycle.update_purchase(
            order.id, source_id, PurchaseUpdateInput(amount_minor=1), BY
        )
    )
    assert error.slug == "purchase_not_recorded"


def test_a_decline_removes_the_entry_and_the_charge(shop: RulesShop) -> None:
    order, item = quoted(shop)
    source_id = purchase(shop, order, item)
    record(shop, order, source_id, 500)

    view = shop.lifecycle.decline_purchase(order.id, source_id, "We had magnets in stock.", BY)

    assert (view.status, view.shop_note, view.cost_entry_id, view.charge_id) == (
        "not_needed",
        "We had magnets in stock.",
        None,
        None,
    )
    assert view.amount_minor is None
    assert shop.rows.cost_entries(order.id) == []
    assert [c.kind for c in shop.rows.charges(order.id)] == []
    assert shop.rows.events(order.id)[-1].kind == "purchase.declined"


def test_a_pending_purchase_can_be_declined_and_a_second_decline_writes_nothing(
    shop: RulesShop,
) -> None:
    order, item = quoted(shop)
    source_id = purchase(shop, order, item)
    view = shop.lifecycle.decline_purchase(order.id, source_id, "", BY)
    assert view.status == "not_needed"
    events = len(shop.rows.events(order.id))
    again = shop.lifecycle.decline_purchase(order.id, source_id, "other words", BY)
    assert (again.status, again.shop_note) == ("not_needed", "")
    assert len(shop.rows.events(order.id)) == events
    error = refused(lambda: record(shop, order, source_id, 100))
    assert error.slug == "purchase_not_pending"


def test_after_acceptance_a_purchase_is_locked(shop: RulesShop) -> None:
    order, item = quoted(shop)
    source_id = purchase(shop, order, item)
    record(shop, order, source_id, 500)
    shop.lifecycle.accept_order(order.id, BY)

    update = refused(
        lambda: shop.lifecycle.update_purchase(
            order.id, source_id, PurchaseUpdateInput(amount_minor=600), BY
        )
    )
    decline = refused(lambda: shop.lifecycle.decline_purchase(order.id, source_id, "", BY))
    assert (update.slug, decline.slug) == ("purchase_locked", "purchase_locked")


def test_a_source_not_a_purchase_of_the_order_is_not_found(shop: RulesShop) -> None:
    order, _ = quoted(shop)
    other, other_item = quoted(shop)
    source_id = purchase(shop, other, other_item)
    error = refused(lambda: record(shop, order, source_id, 100))
    assert (error.slug, error.not_found) == ("source_not_found", True)
    with shop.rows.transaction() as conn:
        conn.execute(
            "UPDATE order_item_sources SET purpose = 'print', status = 'manual' WHERE id = %s",
            (source_id,),
        )
    error = refused(lambda: record(shop, other, source_id, 100))
    assert error.slug == "source_not_found"


def test_a_purchase_on_a_cart_is_refused(shop: RulesShop, pool: PgPool) -> None:
    from scadbuddy.orders.lifecycle import NewRequestItem
    from scadbuddy.orders.storefront.visitors import VisitorStore, visitor_actor

    visitor = VisitorStore(pool).ensure("c" * 43)
    by = visitor_actor(visitor)
    cart = shop.lifecycle.ensure_draft(visitor.id, by, currency="USD")
    [item] = shop.lifecycle.add_items(
        cart.id, by, [NewRequestItem(title="Your models", quantity=1)], cart_of=visitor.id
    )
    source_id = purchase(shop, cart, item)
    assert refused(lambda: record(shop, cart, source_id, 100)).slug == "order_not_submitted"


# -- cost entries and charges keep the link ----------------------------------------------


def test_a_purchase_cost_entry_is_not_removed_or_written_by_hand(shop: RulesShop) -> None:
    order, item = quoted(shop)
    source_id = purchase(shop, order, item)
    record(shop, order, source_id, 500)
    [entry] = shop.rows.cost_entries(order.id)

    removed = refused(lambda: shop.lifecycle.remove_cost_entry(order.id, entry.id, BY))
    assert removed.slug == "purchase_cost_linked"
    assert shop.rows.cost_entries(order.id) == [entry]
    written = refused(
        lambda: shop.lifecycle.add_cost_entry(
            order.id,
            CostEntryInput(kind="other", amount_minor=5, source=f"purchase:{source_id}x"),
            BY,
        )
    )
    assert written.slug == "purchase_cost_linked"


def test_a_charges_replace_keeps_the_purchase_and_handling_rows(shop: RulesShop) -> None:
    shop.now = FEES
    order, item = quoted(shop)
    source_id = purchase(shop, order, item, note="magnets")
    record(shop, order, source_id, 1000)
    before = [c for c in shop.rows.charges(order.id) if c.kind in ("purchase", "handling")]

    shop.lifecycle.edit_order(
        order.id,
        BY,
        OrderEdit(
            charges=[
                ChargeInput(kind="fee", label="Rush", amount_minor=300),
                ChargeInput(kind="purchase", label="forged", amount_minor=1),
                ChargeInput(kind="handling", label="forged", amount_minor=1),
            ]
        ),
    )

    assert charges(shop, order) == [
        ("fee", "Rush", 300),
        ("purchase", "Bought for your project: magnets", 1000),
        ("handling", "Handling for bought items", 300),
    ]
    assert [c for c in shop.rows.charges(order.id) if c.kind in ("purchase", "handling")] == before
    shop.lifecycle.accept_order(order.id, BY)
    shop.lifecycle.replace_charges_after_acceptance(order.id, BY, [])
    assert charges(shop, order) == [
        ("purchase", "Bought for your project: magnets", 1000),
        ("handling", "Handling for bought items", 300),
    ]


def test_the_database_keeps_one_purchase_charge_per_link_and_one_handling_row(
    shop: RulesShop,
) -> None:
    order, item = quoted(shop)
    source_id = purchase(shop, order, item)
    insert = (
        "INSERT INTO order_charges (order_id, kind, label, amount_minor, position,"
        " purchase_link_id) VALUES (%s, %s, 'x', 1, 0, %s)"
    )
    for kind, link in (("purchase", None), ("fee", source_id)):
        with pytest.raises(psycopg.errors.CheckViolation), shop.rows.transaction() as conn:
            conn.execute(insert, (order.id, kind, link))
    with shop.rows.transaction() as conn:
        conn.execute(insert, (order.id, "purchase", source_id))
    with pytest.raises(psycopg.errors.UniqueViolation), shop.rows.transaction() as conn:
        conn.execute(insert, (order.id, "purchase", source_id))
    with shop.rows.transaction() as conn:
        conn.execute(insert, (order.id, "handling", None))
    with pytest.raises(psycopg.errors.UniqueViolation), shop.rows.transaction() as conn:
        conn.execute(insert, (order.id, "handling", None))


# -- the handling fee --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amounts", "flat", "percent", "fee"),
    [
        ([], 200, Decimal(10), 0),
        ([1000], 0, Decimal(0), 0),
        ([1000, 555], 200, Decimal(10), 556),
        ([1], 0, Decimal("50"), 1),
        ([999], 0, Decimal("12.34"), 123),
        ([1000], 150, Decimal(0), 150),
    ],
)
def test_the_fee_is_flat_per_purchase_plus_a_percent_rounded_once(
    amounts: list[int], flat: int, percent: Decimal, fee: int
) -> None:
    assert handling_minor(amounts, flat_minor=flat, percent=percent) == fee


def test_the_handling_fee_follows_the_recorded_purchases(shop: RulesShop) -> None:
    shop.now = FEES
    order, item = quoted(shop)
    first = purchase(shop, order, item)
    second = purchase(shop, order, item)

    record(shop, order, first, 1000)
    # 2.00 + 10 % of 10.00.
    assert [c for c in charges(shop, order) if c[0] == "handling"] == [
        ("handling", "Handling for bought items", 300)
    ]
    record(shop, order, second, 555)
    # 2 x 2.00 + 10 % of 15.55 = 5.555, rounded half up once.
    assert [c[2] for c in charges(shop, order) if c[0] == "handling"] == [556]
    shop.lifecycle.update_purchase(order.id, second, PurchaseUpdateInput(amount_minor=1000), BY)
    assert [c[2] for c in charges(shop, order) if c[0] == "handling"] == [600]
    shop.lifecycle.decline_purchase(order.id, first, "", BY)
    assert [c[2] for c in charges(shop, order) if c[0] == "handling"] == [300]
    shop.lifecycle.decline_purchase(order.id, second, "", BY)
    assert [c for c in charges(shop, order) if c[0] == "handling"] == []
    assert [e.amount_minor for e in shop.rows.cost_entries(order.id)] == []


def test_an_order_not_in_the_shops_currency_takes_the_percent_only(shop: RulesShop) -> None:
    shop.now = FEES
    order, item = quoted(shop, currency="EUR")
    record(shop, order, purchase(shop, order, item), 1000)
    assert [c[2] for c in charges(shop, order) if c[0] == "handling"] == [100]


def test_no_fee_writes_no_handling_row(shop: RulesShop) -> None:
    order, item = quoted(shop)
    record(shop, order, purchase(shop, order, item), 1000)
    assert [c[0] for c in charges(shop, order)] == ["purchase"]


def test_purchases_and_the_fee_are_not_taxed(shop: RulesShop) -> None:
    shop.now = replace(FEES, tax_percent=Decimal(10))
    order, item = quoted(shop)
    shop.lifecycle.edit_order(order.id, BY, OrderEdit(charges=[]))  # the tax row, from the items
    record(shop, order, purchase(shop, order, item), 1000)
    assert [c[2] for c in charges(shop, order) if c[0] == "tax"] == [100]
    assert [c[0] for c in charges(shop, order)] == ["purchase", "handling", "tax"]


# -- the gate ----------------------------------------------------------------------------


def test_a_pending_purchase_stops_the_order_accept_until_it_is_recorded(shop: RulesShop) -> None:
    order, item = quoted(shop)
    source_id = purchase(shop, order, item)

    error = refused(lambda: shop.lifecycle.accept_order(order.id, BY))

    assert error.slug == "purchase_pending"
    assert (error.items, error.sources) == ([item.id], [source_id])
    assert shop.get(order).status == "quoted"
    record(shop, order, source_id, 100)
    assert shop.lifecycle.accept_order(order.id, BY).status == "accepted"


def test_a_pending_purchase_stops_a_quote_accept(shop: RulesShop) -> None:
    from scadbuddy.orders.lifecycle import ManualQuoteInput

    order = shop.order()
    item = shop.item(order)
    quote = shop.lifecycle.record_manual_quote(
        order.id, item.id, ManualQuoteInput(unit_price_minor=1000, label="typed"), BY
    )
    source_id = purchase(shop, order, item)
    error = refused(lambda: shop.lifecycle.accept_quote(order.id, item.id, quote.id, BY))
    assert (error.slug, error.sources) == ("purchase_pending", [source_id])
    shop.lifecycle.decline_purchase(order.id, source_id, "", BY)
    assert shop.lifecycle.accept_quote(order.id, item.id, quote.id, BY).status == "approved"


def test_a_pending_purchase_stops_a_balance_request_but_not_a_deposit(shop: RulesShop) -> None:
    order, item = quoted(shop)
    purchase(shop, order, item)
    error = refused(lambda: shop.lifecycle.open_payment_request(order.id, BY, provider=None))
    assert error.slug == "purchase_pending"
    deposit = shop.lifecycle.open_payment_request(
        order.id, BY, kind="deposit", amount_minor=300, provider=None
    )
    assert deposit.kind == "deposit"
    assert [r.kind for r in requests_for(shop, order)] == ["deposit"]


def test_the_last_accept_skips_the_order_accept_while_a_purchase_is_pending(
    shop: RulesShop, pool: PgPool
) -> None:
    order = storefront_order(shop, pool, 1)
    [item] = shop.rows.items(order.id)
    purchase(shop, order, item)
    customer = Actor(kind="customer", principal=f"customer:{shop.customer.id}", session="s")
    with shop.rows.transaction() as conn:
        conn.execute("UPDATE order_items SET status = 'approved' WHERE id = %s", (item.id,))
        locked = shop.rows.lock_order(conn, order.id)
        after = shop.lifecycle._accepted_by_the_last(conn, locked, customer)
    assert after.status == locked.status != "accepted"
    last = shop.rows.events(order.id)[-1]
    assert (last.kind, last.detail["slug"]) == ("order.accept_skipped", "purchase_pending")
