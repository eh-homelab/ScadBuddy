"""The customer actor and the storefront's acceptance rules (#5283, spec 2026-11-02 §6.1,
§6.2, §6.4): on a storefront order only the customer accepts a revision or a quote, the
shop only "for the customer" with a reason, and the customer's last accept accepts the
order in the same transaction."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from scadbuddy.core.events import OrderChanged, OrderItemChanged
from scadbuddy.orders.lifecycle import (
    LifecycleError,
    ManualQuoteInput,
    NewCustomItem,
    NewModelItem,
)
from scadbuddy.orders.models import Actor, Order, OrderItem, Revision
from scadbuddy.orders.numbers import next_number
from scadbuddy.orders.revisions import RevisionAnswerInput
from scadbuddy.orders.store import OrderStore
from scadbuddy.orders.storefront.customers import customer_actor
from scadbuddy.orders.storefront.visitors import VisitorStore, visitor_actor
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop, resolved, set_request_status
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


# -- fix round 1 ---------------------------------------------------------------------------


def test_a_refused_order_accept_keeps_the_customers_accept(
    shop: Shop, pool: PgPool, customer: Actor
) -> None:
    """A pending payment (a capture can stay pending for days) refuses `accept_order`:
    the customer's accept still commits, and an event says why the order waits."""
    order = storefront_order(shop, pool, 1)
    [item] = shop.rows.items(order.id)
    req = shop.lifecycle.open_payment_request(order.id, BY, amount_minor=200, provider=None)
    set_request_status(shop, req.id, "pending")
    shop.lifecycle.answer_revision(order.id, item.id, revision(shop, item).id, ACCEPTED, customer)
    assert revision(shop, item).answer == "accepted"
    assert shop.get_item(item).status == "approved"
    assert shop.get(order).status == "quoted"
    last = shop.rows.events(order.id)[-1]
    assert last.kind == "order.accept_skipped" and last.detail["slug"] == "payment_in_flight"
    assert last.by.kind == "customer"


def test_a_cancelled_item_is_left_out_of_the_last_accept(
    shop: Shop, pool: PgPool, customer: Actor
) -> None:
    order = storefront_order(shop, pool, 2)
    a, b = shop.rows.items(order.id)
    shop.lifecycle.cancel_item(order.id, b.id, BY)
    shop.lifecycle.answer_revision(order.id, a.id, revision(shop, a).id, ACCEPTED, customer)
    assert shop.get(order).status == "accepted"


def test_a_shop_cancel_never_accepts_the_order(shop: Shop, pool: PgPool, customer: Actor) -> None:
    """Only the customer's own accept accepts a storefront order (§6.2)."""
    order = storefront_order(shop, pool, 2)
    a, b = shop.rows.items(order.id)
    shop.lifecycle.answer_revision(order.id, a.id, revision(shop, a).id, ACCEPTED, customer)
    shop.lifecycle.cancel_item(order.id, b.id, BY)
    assert shop.get(order).status == "quoted"


def test_accept_for_the_customer_refuses_a_wrong_revision(shop: Shop, pool: PgPool) -> None:
    order = storefront_order(shop, pool, 2)
    a, b = shop.rows.items(order.id)
    other = revision(shop, b)
    wrong = refused(
        lambda: shop.lifecycle.accept_for_customer(order.id, a.id, "revision", other.id, "x", BY)
    )
    assert wrong.slug == "revision_not_found" and wrong.not_found
    first = revision(shop, a)
    shop.lifecycle.add_revision(order.id, a.id, BY, note="again")
    older = refused(
        lambda: shop.lifecycle.accept_for_customer(order.id, a.id, "revision", first.id, "x", BY)
    )
    assert older.slug == "revision_not_newest"
    unsent = revision(shop, a)
    never = refused(
        lambda: shop.lifecycle.accept_for_customer(order.id, a.id, "revision", unsent.id, "x", BY)
    )
    assert never.slug == "revision_not_sent"


def test_a_refused_accept_writes_nothing(shop: Shop, pool: PgPool) -> None:
    order = storefront_order(shop, pool, 1)
    [item] = shop.rows.items(order.id)
    before, told = shop.rows.events(order.id), len(shop.events)
    refused(
        lambda: shop.lifecycle.answer_revision(
            order.id, item.id, revision(shop, item).id, ACCEPTED, BY
        )
    )
    [quote] = shop.rows.quotes(item.id)
    refused(lambda: shop.lifecycle.accept_quote(order.id, item.id, quote.id, BY))
    assert shop.rows.events(order.id) == before and len(shop.events) == told
    assert shop.get(order).status == order.status
    assert shop.get_item(item).status == item.status
    assert revision(shop, item).answer is None


def test_the_accepted_item_is_told(shop: Shop, pool: PgPool, customer: Actor) -> None:
    order = storefront_order(shop, pool, 1)
    [item] = shop.rows.items(order.id)
    shop.events.clear()
    shop.lifecycle.answer_revision(order.id, item.id, revision(shop, item).id, ACCEPTED, customer)
    told = [e for e in shop.events if isinstance(e, OrderItemChanged)]
    assert [(e.item_id, e.status) for e in told] == [(item.id, "approved")]


def test_a_customer_accepts_only_on_their_own_order(shop: Shop, pool: PgPool) -> None:
    order = storefront_order(shop, pool, 1)
    [item] = shop.rows.items(order.id)
    stranger = customer_actor("someone-else", "visitor-row-2")
    missing = refused(
        lambda: shop.lifecycle.answer_revision(
            order.id, item.id, revision(shop, item).id, ACCEPTED, stranger
        )
    )
    assert missing.slug == "order_not_found" and missing.not_found
    [quote] = shop.rows.quotes(item.id)
    missing = refused(lambda: shop.lifecycle.accept_quote(order.id, item.id, quote.id, stranger))
    assert missing.slug == "order_not_found" and missing.not_found


def test_only_the_shops_staff_accept_for_the_customer(
    shop: Shop, pool: PgPool, customer: Actor
) -> None:
    order = storefront_order(shop, pool, 1)
    [item] = shop.rows.items(order.id)
    rev = revision(shop, item)
    for by in (AGENT, customer):
        refusal = refused(
            lambda by=by: shop.lifecycle.accept_for_customer(
                order.id, item.id, "revision", rev.id, "by phone", by
            )
        )
        assert refusal.slug == "customer_accepts" and not refusal.not_found
    assert revision(shop, item).answer is None


def _custom(**fields: Any) -> NewCustomItem:
    return NewCustomItem(title="Gift wrap", quantity=1, unit_price_minor=1500, **fields)


def test_a_storefront_custom_price_waits_for_the_customer(shop: Shop, pool: PgPool) -> None:
    """§4.5: the shop's custom price on a storefront order is recorded, never accepted."""
    order = storefront_order(shop, pool, 1)
    [added] = shop.lifecycle.add_items(order.id, BY, [_custom()])
    assert added.status == "quoted"
    assert not any(q.accepted for q in shop.rows.quotes(added.id))
    explicit = refused(lambda: shop.lifecycle.add_items(order.id, BY, [_custom(accept=True)]))
    assert explicit.slug == "customer_accepts"
    copy = shop.lifecycle.duplicate_item(order.id, added.id, BY)
    assert copy.status == "quoted"
    changed = shop.lifecycle.update_item(order.id, added.id, BY, unit_price_minor=2000)
    assert changed.status == "quoted"
    again = refused(
        lambda: shop.lifecycle.update_item(
            order.id, added.id, BY, unit_price_minor=2500, accept=True
        )
    )
    assert again.slug == "customer_accepts"


def test_a_back_office_custom_price_is_accepted_as_before(shop: Shop) -> None:
    order = shop.order()
    [added] = shop.lifecycle.add_items(order.id, BY, [_custom()])
    assert added.status == "approved"
    assert shop.lifecycle.duplicate_item(order.id, added.id, BY).status == "approved"
    changed = shop.lifecycle.update_item(order.id, added.id, BY, unit_price_minor=2000)
    assert changed.status == "approved"
