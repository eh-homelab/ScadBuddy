"""The customer actor and the storefront's acceptance rules (#5283, spec 2026-11-02 §6.1,
§6.2, §6.4): on a storefront order only the customer accepts a revision or a quote, the
shop only "for the customer" with a reason, and the customer's last accept accepts the
order in the same transaction."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from scadbuddy.core.events import OrderChanged
from scadbuddy.orders.lifecycle import LifecycleError, ManualQuoteInput, NewModelItem
from scadbuddy.orders.models import Actor, Order, OrderItem, Revision
from scadbuddy.orders.numbers import next_number
from scadbuddy.orders.revisions import RevisionAnswerInput
from scadbuddy.orders.store import OrderStore
from scadbuddy.orders.storefront.customers import customer_actor
from scadbuddy.orders.storefront.visitors import VisitorStore, visitor_actor
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop, resolved
from tests.orders.test_revisions import attach_render

pytestmark = pytest.mark.requires_postgres

AGENT = Actor(kind="agent", principal="agent:claude", session="s-1")
ACCEPTED = RevisionAnswerInput(answer="accepted")


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


@pytest.fixture
def customer(shop: Shop) -> Actor:
    return customer_actor(shop.customer.id, "visitor-row-1")


def submit(shop: Shop, order: Order) -> Order:
    """The store writes a submit makes (Task 2): a number, the customer and
    ``submitted_at``."""
    with shop.rows.transaction() as conn:
        conn.execute(
            "UPDATE orders SET number = %s, customer_id = %s, submitted_at = %s WHERE id = %s",
            (
                next_number(conn, datetime.now(UTC).year),
                shop.customer.id,
                datetime.now(UTC),
                order.id,
            ),
        )
    return shop.get(order)


def storefront_order(shop: Shop, pool: PgPool, count: int, approval: str = "together") -> Order:
    """A submitted storefront order of ``count`` model items, each with a manual quote on
    revision 1, a render on it and the revision sent."""
    visitor = VisitorStore(pool).ensure("v" * 43)
    by = visitor_actor(visitor)
    order = shop.lifecycle.ensure_draft(visitor.id, by, currency="USD")
    shop.lifecycle.add_items(
        order.id, by, [NewModelItem(resolved=resolved(), quantity=1) for _ in range(count)]
    )
    order = submit(shop, order)
    for item in shop.rows.items(order.id):
        sent(shop, order, item, approval)
    return shop.get(order)


def sent(shop: Shop, order: Order, item: OrderItem, approval: str) -> None:
    shop.lifecycle.update_item(order.id, item.id, BY, approval=approval)  # type: ignore[arg-type]
    shop.lifecycle.record_manual_quote(
        order.id, item.id, ManualQuoteInput(unit_price_minor=1000, label="typed"), BY
    )
    [rev] = shop.rows.revisions(item.id)
    attach_render(shop, order, shop.get_item(item), rev)
    shop.lifecycle.send_revision(order.id, item.id, rev.id, BY)


def revision(shop: Shop, item: OrderItem) -> Revision:
    return shop.rows.revisions(item.id)[-1]


def refused(call: Any) -> LifecycleError:
    with pytest.raises(LifecycleError) as caught:
        call()
    return caught.value


def test_the_customer_actor_names_the_customer_and_the_session() -> None:
    actor = customer_actor("cust-1", "visitor-row-1")
    assert (actor.kind, actor.principal, actor.session) == (
        "customer",
        "customer:cust-1",
        "visitor-row-1",
    )


def test_the_last_customer_acceptance_accepts_a_storefront_order(
    shop: Shop, pool: PgPool, customer: Actor
) -> None:
    order = storefront_order(shop, pool, 2)
    a, b = shop.rows.items(order.id)
    shop.lifecycle.answer_revision(order.id, a.id, revision(shop, a).id, ACCEPTED, customer)
    assert shop.get(order).status == "draft"
    shop.events.clear()
    shop.lifecycle.answer_revision(order.id, b.id, revision(shop, b).id, ACCEPTED, customer)
    assert shop.get(order).status == "accepted"
    last = shop.rows.events(order.id)[-1]
    assert (last.kind, last.detail["to"]) == ("order.status", "accepted")
    assert last.by.kind == "customer" and last.by.principal == f"customer:{shop.customer.id}"
    told = [e for e in shop.events if isinstance(e, OrderChanged)]
    assert told and told[-1].status == "accepted"


def test_the_customers_price_accept_accepts_a_design_first_order(
    shop: Shop, pool: PgPool, customer: Actor
) -> None:
    order = storefront_order(shop, pool, 1, approval="design_first")
    [item] = shop.rows.items(order.id)
    shop.lifecycle.answer_revision(order.id, item.id, revision(shop, item).id, ACCEPTED, customer)
    assert shop.get(order).status == "draft"
    [quote] = shop.rows.quotes(item.id)
    shop.lifecycle.accept_quote(order.id, item.id, quote.id, customer)
    assert shop.get(order).status == "accepted"
    assert shop.rows.events(order.id)[-1].by.kind == "customer"


def test_the_shop_does_not_accept_for_a_storefront_customer(shop: Shop, pool: PgPool) -> None:
    order = storefront_order(shop, pool, 1, approval="design_first")
    [item] = shop.rows.items(order.id)
    [quote] = shop.rows.quotes(item.id)
    rev = revision(shop, item)
    for by in (BY, AGENT):
        answer = refused(
            lambda by=by: shop.lifecycle.answer_revision(order.id, item.id, rev.id, ACCEPTED, by)
        )
        accept = refused(lambda by=by: shop.lifecycle.accept_quote(order.id, item.id, quote.id, by))
        assert answer.slug == accept.slug == "customer_accepts"
        assert not (answer.not_found or answer.unprocessable)
    assert revision(shop, item).answer is None
    # Asking for changes is not an accept: the shop may record the customer's.
    changes = RevisionAnswerInput(answer="changes_requested", note="by phone: bigger")
    assert shop.lifecycle.answer_revision(order.id, item.id, rev.id, changes, BY).answer == (
        "changes_requested"
    )


def test_the_shop_accepts_for_the_customer_only_with_a_reason(shop: Shop, pool: PgPool) -> None:
    order = storefront_order(shop, pool, 2)
    a, b = shop.rows.items(order.id)
    rev = revision(shop, a)
    blank = refused(
        lambda: shop.lifecycle.accept_for_customer(order.id, a.id, "revision", rev.id, "  ", BY)
    )
    assert blank.unprocessable and blank.slug == "reason"
    item = shop.lifecycle.accept_for_customer(
        order.id, a.id, "revision", rev.id, " By phone, 9 Nov ", BY
    )
    assert item.status == "approved" and revision(shop, a).answer == "accepted"
    [event] = [e for e in shop.rows.events(order.id) if e.kind == "item.accepted_for_customer"]
    assert event.detail == {
        "item_id": a.id,
        "what": "revision",
        "target_id": rev.id,
        "reason": "By phone, 9 Nov",
    }
    assert event.by == BY
    assert shop.get(order).status == "draft"
    [quote] = shop.rows.quotes(b.id)
    refused_quote = refused(
        lambda: shop.lifecycle.accept_for_customer(order.id, b.id, "quote", "nope", "x", BY)
    )
    assert refused_quote.slug == "quote_not_found" and refused_quote.not_found
    shop.lifecycle.accept_for_customer(order.id, b.id, "quote", quote.id, "In the shop", BY)
    # The accept that leaves every item approved accepts the order, as the customer's would.
    assert shop.get(order).status == "accepted"


def test_accept_for_the_customer_is_for_storefront_orders_only(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    quote = shop.lifecycle.record_manual_quote(
        order.id, item.id, ManualQuoteInput(unit_price_minor=1000, label="typed"), BY
    )
    wrong = refused(
        lambda: shop.lifecycle.accept_for_customer(order.id, item.id, "quote", quote.id, "x", BY)
    )
    assert wrong.slug == "order_not_storefront"


def test_a_back_office_order_is_not_accepted_by_the_last_answer(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    sent(shop, order, item, "together")
    shop.lifecycle.answer_revision(order.id, item.id, revision(shop, item).id, ACCEPTED, BY)
    assert shop.get_item(item).status == "approved"
    assert shop.get(order).status == "quoted"
    other = shop.order()
    quoted = shop.item(other)
    assert shop.quoted(other, quoted).status == "approved"
    assert shop.get(other).status == "quoted"
