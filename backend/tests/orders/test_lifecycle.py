"""The order and item lifecycle (#4249, spec 2026-10-18 §3.5, plan Rulings 1-5, 11-12,
14): every transition under the order's row lock, the status past `accepted` derived
from the items and shipments, the audit line in the same transaction, and the bus told
after commit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from scadbuddy.core.events import Event, InProcessEventBus, OrderChanged, OrderItemChanged
from scadbuddy.orders.lifecycle import (
    QUOTE_HOLDER,
    FulfilmentInput,
    Lifecycle,
    LifecycleError,
    ManualQuoteInput,
    OrderEdit,
    ShipmentEdit,
    ShipmentInput,
    derive,
    reap_quote_holds,
)
from scadbuddy.orders.models import (
    Actor,
    AddressInput,
    ChargeInput,
    ColourSlot,
    FilamentInput,
    Fulfilment,
    Order,
    OrderItem,
    PricingPolicy,
    Quote,
    Reference,
    Shipment,
    ShipmentItem,
)
from scadbuddy.orders.pricing import Plate, PlateFilament, Priced, price
from scadbuddy.orders.reference import ResolvedReference
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

BY = Actor(kind="browser")
NOW = datetime(2026, 10, 20, tzinfo=UTC)


def resolved(slug: str = "demo") -> ResolvedReference:
    return ResolvedReference(
        Reference(
            slug=slug,
            commit="a" * 40,
            inputs={"params": {"width": 10}, "v": 0},
            template_name="Demo",
            render_key="c" * 64,
        ),
        [ColourSlot(slot=0, colour="#ff0000")],
    )


class Shop:
    """A store, a lifecycle and the events it published."""

    def __init__(self, rows: OrderStore, refs: BlobRefs | None = None) -> None:
        self.rows = rows
        self.refs = refs
        self.bus = InProcessEventBus()
        self.events: list[Event] = []
        self.bus.add_listener(self.events.append)
        self.lifecycle = Lifecycle(rows, self.bus, refs)
        self.customer = rows.insert_customer(
            display_name="Ada",
            company=None,
            notes="",
            tags=[],
            contacts=[],
            addresses=[
                AddressInput(label="home", recipient="Ada", lines=["1 Way"], default_shipping=True)
            ],
        )

    def order(self, **fields: Any) -> Order:
        return self.lifecycle.create_order(
            self.customer.id, BY, currency="USD", notes="", due_at=None, **fields
        )

    def item(self, order: Order, quantity: int = 1) -> OrderItem:
        return self.lifecycle.add_item(order.id, BY, resolved(), output_id=None, quantity=quantity)

    def quoted(self, order: Order, item: OrderItem, price: int = 1000) -> OrderItem:
        quote = self.lifecycle.record_manual_quote(
            order.id, item.id, ManualQuoteInput(unit_price_minor=price, label="typed"), BY
        )
        return self.lifecycle.accept_quote(order.id, item.id, quote.id, BY)

    def accepted(self, quantity: int = 1) -> tuple[Order, OrderItem]:
        order = self.order()
        item = self.item(order, quantity)
        self.quoted(order, item)
        return self.lifecycle.accept_order(order.id, BY), self.get_item(item)

    def fulfilled(self, order: Order, item: OrderItem) -> Fulfilment:
        attempt = self.lifecycle.record_fulfilment(
            order.id, item.id, FulfilmentInput(quantity=item.quantity, outcome="succeeded"), BY
        )
        return self.lifecycle.accept_fulfilment(order.id, item.id, attempt.id, BY)

    def shipped(self, order: Order, *items: OrderItem) -> Shipment:
        return self.lifecycle.add_shipment(
            order.id,
            ShipmentInput(
                carrier="Post",
                items=[ShipmentItem(order_item_id=i.id, quantity=i.quantity) for i in items],
            ),
            BY,
        )

    def get(self, order: Order) -> Order:
        found = self.rows.order(order.id)
        assert found is not None
        return found

    def get_item(self, item: OrderItem) -> OrderItem:
        found = self.rows.item(item.id)
        assert found is not None
        return found

    def kinds(self, order: Order) -> list[str]:
        return [e.kind for e in self.rows.events(order.id)]


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


# -- the happy path -----------------------------------------------------------------------


def test_the_happy_path_to_closed(shop: Shop) -> None:
    order = shop.order()
    assert order.status == "draft"
    item = shop.item(order, quantity=2)
    assert item.status == "draft"
    assert item.colours == [ColourSlot(slot=0, colour="#ff0000")]

    quote = shop.lifecycle.record_manual_quote(
        order.id, item.id, ManualQuoteInput(unit_price_minor=1500, label="typed"), BY
    )
    assert (quote.source, quote.quantity, quote.total_minor, quote.accepted) == (
        "manual",
        2,
        3000,
        False,
    )
    assert quote.lines[0].kind == "manual" and quote.unit_cost_minor == 1500
    assert shop.get_item(item).status == "quoted"
    assert shop.get(order).status == "draft"

    item = shop.lifecycle.accept_quote(order.id, item.id, quote.id, BY)
    assert item.status == "approved"
    assert shop.get(order).status == "quoted"

    order = shop.lifecycle.accept_order(order.id, BY)
    assert order.status == "accepted" and order.accepted_at is not None
    assert order.shipping_address is not None and order.shipping_address.recipient == "Ada"

    attempt = shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=2, outcome="succeeded"), BY
    )
    assert attempt.attempt == 1
    assert shop.get(order).status == "in_production"
    attempt = shop.lifecycle.accept_fulfilment(order.id, item.id, attempt.id, BY)
    assert attempt.accepted_at is not None
    assert shop.get_item(item).status == "fulfilled"
    assert shop.get(order).status == "ready"

    shipment = shop.shipped(order, shop.get_item(item))
    assert shipment.address is not None and shipment.address.recipient == "Ada"
    assert shop.get(order).status == "shipped"

    closed = shop.lifecycle.close_order(order.id, BY)
    assert closed.status == "closed" and closed.closed_at is not None
    # One audit row per status change (Ruling 12), the derived ones included.
    assert shop.kinds(order) == [
        "order.created",
        "item.added",
        "quote.recorded",
        "quote.accepted",
        "order.status",  # quoted
        "order.status",  # accepted
        "fulfilment.recorded",
        "order.status",  # in_production
        "fulfilment.accepted",
        "order.status",  # ready
        "shipment.added",
        "order.status",  # shipped
        "order.status",  # closed
    ]
    moves = [e.detail["to"] for e in shop.rows.events(order.id) if e.kind == "order.status"]
    assert moves == ["quoted", "accepted", "in_production", "ready", "shipped", "closed"]
    statuses = [e.status for e in shop.events if isinstance(e, OrderChanged)]
    assert statuses[-1] == "closed" and "accepted" in statuses
    assert any(
        isinstance(e, OrderItemChanged) and e.status == "fulfilled" and e.attempt == 1
        for e in shop.events
    )


# -- Review Focus 2: close names every open item --------------------------------------------


def test_close_names_every_open_item(shop: Shop) -> None:
    order = shop.order()
    done, open_one, open_two = (shop.item(order) for _ in range(3))
    for item in (done, open_one, open_two):
        shop.quoted(order, item)
    order = shop.lifecycle.accept_order(order.id, BY)
    shop.fulfilled(order, done)
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.close_order(order.id, BY)
    assert refused.value.slug == "order_not_closable"
    # Every open item, and the fulfilled one no shipment covers.
    assert set(refused.value.items) == {open_one.id, open_two.id, done.id}
    assert shop.get(order).status == "in_production"
    assert shop.kinds(order)[-1] != "order.status" or shop.get(order).closed_at is None


def test_shipped_needs_every_fulfilled_item_covered(shop: Shop) -> None:
    order = shop.order()
    first, second = shop.item(order, 2), shop.item(order, 1)
    shop.quoted(order, first)
    shop.quoted(order, second)
    order = shop.lifecycle.accept_order(order.id, BY)
    shop.fulfilled(order, first)
    shop.fulfilled(order, second)
    assert shop.get(order).status == "ready"
    partial = shop.lifecycle.add_shipment(
        order.id,
        ShipmentInput(items=[ShipmentItem(order_item_id=first.id, quantity=1)]),
        BY,
    )
    assert shop.get(order).status == "ready"
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.close_order(order.id, BY)
    assert refused.value.slug == "order_not_closable"
    assert set(refused.value.items) == {first.id, second.id}
    shop.lifecycle.update_shipment(
        order.id,
        partial.id,
        ShipmentEdit(
            items=[
                ShipmentItem(order_item_id=first.id, quantity=2),
                ShipmentItem(order_item_id=second.id, quantity=1),
            ]
        ),
        BY,
    )
    assert shop.get(order).status == "shipped"
    assert shop.lifecycle.close_order(order.id, BY).status == "closed"
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.add_shipment(order.id, ShipmentInput(items=[]), BY)
    assert refused.value.slug == "order_closed"


def test_a_shipment_edit_keeps_what_it_does_not_send(shop: Shop) -> None:
    """A PATCH that adds a tracking number is not a replacement: the carrier, the
    notes and every line stay, and the order stays `shipped`."""
    order, item = shop.accepted(quantity=2)
    shop.fulfilled(order, item)
    shipment = shop.shipped(order, shop.get_item(item))
    assert shop.get(order).status == "shipped"
    edited = shop.lifecycle.update_shipment(order.id, shipment.id, ShipmentEdit(tracking="T2"), BY)
    assert edited.tracking == "T2"
    assert edited.carrier == "Post"
    assert [(line.order_item_id, line.quantity) for line in edited.items] == [(item.id, 2)]
    assert shop.get(order).status == "shipped"
    # Sent lines replace the lines; a sent null clears a nullable field.
    edited = shop.lifecycle.update_shipment(
        order.id, shipment.id, ShipmentEdit(items=[], tracking=None), BY
    )
    assert edited.items == [] and edited.tracking is None and edited.carrier == "Post"
    assert shop.get(order).status == "ready"


def test_shipments_never_cover_more_than_the_item_quantity(shop: Shop) -> None:
    order, item = shop.accepted(quantity=2)
    shop.fulfilled(order, item)
    first = shop.lifecycle.add_shipment(
        order.id, ShipmentInput(items=[ShipmentItem(order_item_id=item.id, quantity=1)]), BY
    )
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.add_shipment(
            order.id, ShipmentInput(items=[ShipmentItem(order_item_id=item.id, quantity=2)]), BY
        )
    assert refused.value.slug == "shipment_over_quantity" and refused.value.items == [item.id]
    # An edit's lines replace that shipment's own, so they are not counted twice.
    both = ShipmentEdit(items=[ShipmentItem(order_item_id=item.id, quantity=2)])
    edited = shop.lifecycle.update_shipment(order.id, first.id, both, BY)
    assert edited.items[0].quantity == 2 and shop.get(order).status == "shipped"


def test_a_shipment_lists_each_item_once() -> None:
    line = ShipmentItem(order_item_id="i", quantity=1)
    with pytest.raises(ValueError, match="listed twice"):
        ShipmentInput(items=[line, line])
    with pytest.raises(ValueError, match="listed twice"):
        ShipmentEdit(items=[line, line])


def test_a_plain_edit_is_on_the_audit_line(shop: Shop) -> None:
    order = shop.order()
    shop.lifecycle.edit_order(order.id, BY, OrderEdit(notes="rush"))
    events = shop.rows.events(order.id)
    assert events[-1].kind == "order.edited" and events[-1].detail == {"fields": ["notes"]}


# -- Review Focus 3: a customer edit after accept ----------------------------------------


def test_accepted_order_keeps_its_address_snapshot(shop: Shop) -> None:
    order, _ = shop.accepted()
    home = shop.customer.addresses[0]
    with shop.rows.transaction() as conn:
        shop.rows.replace_addresses(
            conn,
            shop.customer.id,
            [AddressInput(id=home.id, label="home", recipient="A. Lovelace", lines=["2 Way"])],
        )
    after = shop.get(order)
    assert after.shipping_address is not None
    assert (after.shipping_address.recipient, after.shipping_address.lines) == ("Ada", ["1 Way"])
    # Dropping the row clears the pointer and leaves the copy.
    with shop.rows.transaction() as conn:
        shop.rows.replace_addresses(conn, shop.customer.id, [])
    after = shop.get(order)
    assert after.shipping_address_id is None
    assert after.shipping_address is not None and after.shipping_address.recipient == "Ada"


def test_a_draft_order_follows_the_customers_address(shop: Shop) -> None:
    order = shop.order()
    assert order.shipping_address_id == shop.customer.addresses[0].id
    assert order.shipping_address is None


# -- Review Focus 4: a reference or quantity change drops the quote ------------------------


def test_reference_change_drops_the_quote(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    shop.quoted(order, item)
    assert shop.get(order).status == "quoted"
    item = shop.lifecycle.update_item(order.id, item.id, BY, reference=resolved("other"))
    assert item.status == "draft" and item.reference.slug == "other"
    assert all(not q.accepted for q in shop.rows.quotes(item.id))
    assert shop.get(order).status == "draft"


def test_a_replaced_reference_drops_the_output_it_came_from(shop: Shop) -> None:
    """Review: a reference replaced without an output_id kept the old one, pointing at
    an output the item no longer matched."""
    order = shop.order()
    item = shop.lifecycle.add_item(order.id, BY, resolved(), output_id="out-1", quantity=1)
    assert item.output_id == "out-1"
    item = shop.lifecycle.update_item(order.id, item.id, BY, reference=resolved("other"))
    assert item.output_id is None
    item = shop.lifecycle.update_item(
        order.id, item.id, BY, reference=resolved("demo"), output_id="out-2"
    )
    assert item.output_id == "out-2"
    # A quantity or note change alone leaves it.
    assert shop.lifecycle.update_item(order.id, item.id, BY, quantity=2).output_id == "out-2"


def test_quantity_change_drops_the_quote(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    shop.quoted(order, item)
    item = shop.lifecycle.update_item(order.id, item.id, BY, quantity=3)
    assert (item.status, item.quantity) == ("draft", 3)
    assert shop.get(order).status == "draft"
    # Title and notes change nothing else.
    item = shop.lifecycle.update_item(order.id, item.id, BY, title="Two", notes="n")
    assert (item.title, item.notes, item.status) == ("Two", "n", "draft")


def test_reference_and_quantity_are_immutable_after_accept(shop: Shop) -> None:
    order, item = shop.accepted()
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.update_item(order.id, item.id, BY, reference=resolved("other"))
    assert refused.value.slug == "item_locked"
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.update_item(order.id, item.id, BY, quantity=5)
    assert refused.value.slug == "item_locked"
    assert shop.get_item(item).status == "approved"
    # A note still goes.
    assert shop.lifecycle.update_item(order.id, item.id, BY, notes="late").notes == "late"


# -- Review Focus 6: a cancelled order keeps its fulfilled items' attempts -----------------


def test_cancel_keeps_fulfilled_attempts(shop: Shop) -> None:
    order = shop.order()
    done, pending = shop.item(order), shop.item(order)
    shop.quoted(order, done)
    shop.quoted(order, pending)
    order = shop.lifecycle.accept_order(order.id, BY)
    attempt = shop.fulfilled(order, done)
    cancelled = shop.lifecycle.cancel_order(order.id, BY, reason="customer changed mind")
    assert cancelled.status == "cancelled" and cancelled.cancelled_at is not None
    assert shop.get_item(done).status == "fulfilled"
    assert shop.get_item(pending).status == "cancelled"
    assert shop.rows.fulfilments(done.id) == [attempt]
    assert shop.kinds(order)[-1] == "order.status"
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.cancel_order(order.id, BY)
    assert refused.value.slug == "order_not_cancellable"


def test_a_shipped_order_cannot_be_cancelled(shop: Shop) -> None:
    order, item = shop.accepted()
    shop.fulfilled(order, item)
    shop.shipped(order, shop.get_item(item))
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.cancel_order(order.id, BY)
    assert refused.value.slug == "order_not_cancellable"


# -- accept -------------------------------------------------------------------------------


def test_accept_needs_every_item_quoted(shop: Shop) -> None:
    order = shop.order()
    priced, unpriced = shop.item(order), shop.item(order)
    shop.quoted(order, priced)
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.accept_order(order.id, BY)
    assert refused.value.slug == "items_not_quoted"
    assert refused.value.items == [unpriced.id]
    assert shop.get(order).status == "draft"


def test_accept_ignores_cancelled_items(shop: Shop) -> None:
    order = shop.order()
    priced, dropped = shop.item(order), shop.item(order)
    shop.quoted(order, priced)
    assert shop.lifecycle.cancel_item(order.id, dropped.id, BY).status == "cancelled"
    assert shop.get(order).status == "quoted"
    assert shop.lifecycle.accept_order(order.id, BY).status == "accepted"


def test_accept_refuses_an_order_of_only_cancelled_items(shop: Shop) -> None:
    order = shop.order()
    only = shop.item(order)
    shop.lifecycle.cancel_item(order.id, only.id, BY)
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.accept_order(order.id, BY)
    assert refused.value.slug == "no_open_items"
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.accept_order(shop.order().id, BY)
    assert refused.value.slug == "no_open_items"


def test_accept_is_not_repeated(shop: Shop) -> None:
    order, _ = shop.accepted()
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.accept_order(order.id, BY)
    assert refused.value.slug == "order_not_acceptable"


def test_quoted_falls_back_to_draft_when_an_item_is_added(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    shop.quoted(order, item)
    assert shop.get(order).status == "quoted"
    added = shop.item(order)
    assert shop.get(order).status == "draft"
    shop.lifecycle.delete_item(order.id, added.id, BY)
    assert shop.get(order).status == "quoted"
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.delete_item(order.id, item.id, BY)
    assert refused.value.slug == "item_not_draft"


def test_a_quote_on_a_late_item_rederives_the_status_itself(shop: Shop) -> None:
    """#4459: recording a quote settles the order's status under its own lock, not by
    trusting that whatever made the draft item already moved the order to `draft`."""
    order = shop.order()
    shop.quoted(order, shop.item(order))
    late = shop.item(order)
    assert shop.get(order).status == "draft"
    with shop.rows.transaction() as conn:  # a path that left the status stale
        shop.rows.set_order_status(conn, order.id, "quoted")
    shop.lifecycle.record_manual_quote(order.id, late.id, ManualQuoteInput(unit_price_minor=5), BY)
    assert shop.get(order).status == "draft"
    assert shop.kinds(order)[-1] == "order.status"
    # A status the rows already say is not written again, nor put on the audit line.
    other = shop.item(order)
    shop.lifecycle.record_manual_quote(order.id, other.id, ManualQuoteInput(unit_price_minor=5), BY)
    assert shop.get(order).status == "draft"
    assert shop.kinds(order)[-1] == "quote.recorded"


def test_a_quote_is_the_items_own(shop: Shop) -> None:
    order = shop.order()
    one, two = shop.item(order), shop.item(order)
    quote = shop.lifecycle.record_manual_quote(
        order.id, one.id, ManualQuoteInput(unit_price_minor=5), BY
    )
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.accept_quote(order.id, two.id, quote.id, BY)
    assert refused.value.slug == "quote_not_found"
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.accept_quote(order.id, "nope", quote.id, BY)
    assert refused.value.slug == "item_not_found"
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.accept_quote("nope", one.id, quote.id, BY)
    assert refused.value.slug == "order_not_found"


# -- fulfilment -----------------------------------------------------------------------------


def test_a_failed_attempt_then_a_good_one_fulfils(shop: Shop) -> None:
    order, item = shop.accepted(quantity=2)
    failed = shop.lifecycle.record_fulfilment(
        order.id,
        item.id,
        FulfilmentInput(quantity=0, outcome="failed", failure_reason="spaghetti"),
        BY,
    )
    assert failed.attempt == 1
    assert shop.get_item(item).status == "failed"
    assert shop.get(order).status == "in_production"
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.accept_fulfilment(order.id, item.id, failed.id, BY)
    assert refused.value.slug == "fulfilment_failed"
    good = shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=2, outcome="succeeded"), BY
    )
    assert good.attempt == 2
    # The second attempt first returned the item to `approved` (Ruling 4).
    assert shop.get_item(item).status == "approved"
    statuses = [e.status for e in shop.events if isinstance(e, OrderItemChanged)]
    assert statuses[-2:] == ["failed", "approved"]
    shop.lifecycle.accept_fulfilment(order.id, item.id, good.id, BY)
    assert shop.get_item(item).status == "fulfilled"
    assert shop.get(order).status == "ready"
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.accept_fulfilment(order.id, item.id, good.id, BY)
    assert refused.value.slug == "fulfilment_accepted"


def test_partial_attempts_add_up(shop: Shop) -> None:
    order, item = shop.accepted(quantity=3)
    first = shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=2, outcome="partial"), BY
    )
    shop.lifecycle.accept_fulfilment(order.id, item.id, first.id, BY)
    assert shop.get_item(item).status == "approved"
    second = shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
    )
    shop.lifecycle.accept_fulfilment(order.id, item.id, second.id, BY)
    assert shop.get_item(item).status == "fulfilled"


def test_an_attempt_is_edited_until_accepted(shop: Shop) -> None:
    order, item = shop.accepted()
    attempt = shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
    )
    edited = shop.lifecycle.update_fulfilment(
        order.id, item.id, attempt.id, FulfilmentInput(quantity=0, outcome="failed"), BY
    )
    assert (edited.outcome, edited.quantity) == ("failed", 0)
    assert shop.get_item(item).status == "failed"
    edited = shop.lifecycle.update_fulfilment(
        order.id,
        item.id,
        attempt.id,
        FulfilmentInput(quantity=1, outcome="succeeded", notes="ok"),
        BY,
    )
    assert shop.get_item(item).status == "approved"
    shop.lifecycle.accept_fulfilment(order.id, item.id, attempt.id, BY)
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.update_fulfilment(
            order.id, item.id, attempt.id, FulfilmentInput(quantity=1, outcome="failed"), BY
        )
    assert refused.value.slug == "fulfilment_accepted"


def test_fulfilment_needs_an_accepted_order_and_an_approved_item(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    shop.quoted(order, item)
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.record_fulfilment(
            order.id, item.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
        )
    assert refused.value.slug == "order_not_accepted"
    order, item = shop.accepted()
    shop.lifecycle.cancel_item(order.id, item.id, BY)
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.record_fulfilment(
            order.id, item.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
        )
    assert refused.value.slug == "item_not_open"


# -- hold -----------------------------------------------------------------------------------


def test_on_hold_refuses_progress_but_not_release_or_cancel(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    shop.quoted(order, item)
    held = shop.lifecycle.hold(order.id, BY)
    assert held.on_hold is True
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.accept_order(order.id, BY)
    assert refused.value.slug == "order_on_hold"
    # Plain edits and payments still go: a person can fix what the hold was for.
    assert shop.lifecycle.edit_order(order.id, BY, OrderEdit(notes="waiting")).notes == "waiting"
    assert shop.lifecycle.add_payment(order.id, BY, amount_minor=100).amount_minor == 100
    released = shop.lifecycle.release(order.id, BY)
    assert released.on_hold is False
    order = shop.lifecycle.accept_order(order.id, BY)
    shop.lifecycle.hold(order.id, BY)
    for slug, call in (
        (
            "fulfil",
            lambda: shop.lifecycle.record_fulfilment(
                order.id, item.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
            ),
        ),
        ("close", lambda: shop.lifecycle.close_order(order.id, BY)),
        ("ship", lambda: shop.lifecycle.add_shipment(order.id, ShipmentInput(items=[]), BY)),
    ):
        with pytest.raises(LifecycleError, match="on hold") as refused:
            call()
        assert refused.value.slug == "order_on_hold", slug
    assert shop.lifecycle.cancel_item(order.id, item.id, BY).status == "cancelled"
    assert shop.lifecycle.cancel_order(order.id, BY).status == "cancelled"
    assert "order.hold" in shop.kinds(order) and "order.release" in shop.kinds(order)


# -- edits ----------------------------------------------------------------------------------


def test_edits_stop_at_accept_except_notes_and_due(shop: Shop) -> None:
    order = shop.order()
    edited = shop.lifecycle.edit_order(
        order.id,
        BY,
        OrderEdit(
            due_at=NOW,
            charges=[ChargeInput(kind="shipping", label="Post", amount_minor=500)],
        ),
    )
    assert edited.due_at == NOW
    assert [c.amount_minor for c in shop.rows.charges(order.id)] == [500]
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.edit_order(order.id, BY, OrderEdit(shipping_address_id="nope"))
    assert refused.value.slug == "address_not_found"
    order, _ = shop.accepted()
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.edit_order(order.id, BY, OrderEdit(charges=[]))
    assert refused.value.slug == "order_not_editable"
    assert shop.lifecycle.edit_order(order.id, BY, OrderEdit(notes="n", due_at=NOW)).notes == "n"


# -- derive is pure ---------------------------------------------------------------------------


def _order(status: str, **fields: Any) -> Order:
    return Order(
        id="o",
        number="ORD-2026-1",
        customer_id="c",
        status=status,  # type: ignore[arg-type]
        currency="USD",
        on_hold=False,
        shipping_address_id=None,
        billing_address_id=None,
        shipping_address=None,
        billing_address=None,
        notes="",
        due_at=None,
        accepted_at=None,
        closed_at=None,
        cancelled_at=None,
        created_at=NOW,
        updated_at=NOW,
        created_by=BY,
        **fields,
    )


def _item(item_id: str, status: str, quantity: int = 1) -> OrderItem:
    return OrderItem(
        id=item_id,
        order_id="o",
        position=0,
        status=status,  # type: ignore[arg-type]
        quantity=quantity,
        title="t",
        notes="",
        reference=resolved().reference,
        colours=[],
        output_id=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _shipment(*items: tuple[str, int]) -> Shipment:
    return Shipment(
        id="s",
        order_id="o",
        carrier=None,
        service=None,
        tracking=None,
        shipped_at=None,
        address=None,
        cost_minor=None,
        notes="",
        created_at=NOW,
        items=[ShipmentItem(order_item_id=i, quantity=q) for i, q in items],
    )


@pytest.mark.parametrize(
    ("status", "items", "shipments", "attempted", "expected"),
    [
        ("draft", [_item("a", "approved")], [], set(), "draft"),
        ("quoted", [_item("a", "approved")], [], set(), "quoted"),
        ("cancelled", [_item("a", "fulfilled")], [], set(), "cancelled"),
        ("closed", [_item("a", "fulfilled")], [], set(), "closed"),
        ("accepted", [_item("a", "approved")], [], set(), "accepted"),
        # Ruling 4: an attempt is the only way past `approved` in phase 1.
        ("accepted", [_item("a", "approved")], [], {"a"}, "in_production"),
        ("accepted", [_item("a", "failed")], [], {"a"}, "in_production"),
        ("accepted", [_item("a", "fulfilled"), _item("b", "approved")], [], {"a"}, "in_production"),
        ("accepted", [_item("a", "fulfilled"), _item("b", "cancelled")], [], {"a"}, "ready"),
        ("in_production", [_item("a", "fulfilled", 2)], [_shipment(("a", 1))], {"a"}, "ready"),
        (
            "ready",
            [_item("a", "fulfilled", 2)],
            [_shipment(("a", 1)), _shipment(("a", 1))],
            {"a"},
            "shipped",
        ),
        ("shipped", [_item("a", "fulfilled")], [_shipment(("a", 1))], {"a"}, "shipped"),
        # Every item cancelled is nothing made: it stays where it was, for a cancel.
        ("accepted", [_item("a", "cancelled")], [], set(), "accepted"),
        ("accepted", [], [], set(), "accepted"),
    ],
)
def test_derive_is_pure(
    status: str,
    items: list[OrderItem],
    shipments: list[Shipment],
    attempted: set[str],
    expected: str,
) -> None:
    assert derive(_order(status), items, shipments, attempted) == expected


# -- phase 2 (#4250): slice quotes, their refs, an item's filaments ------------------------


def _priced(shop: Shop) -> tuple[Priced, list[Plate]]:
    filament = shop.rows.insert_filament(
        FilamentInput(material="PLA", colour=f"#{len(shop.rows.filaments()):06d}", cost_per_kg="20")
    )
    plates = [
        Plate(
            index=1,
            print_seconds=600,
            filaments=[PlateFilament(slot=1, used_g=Decimal("10"))],
            blob_key="quote-a",
            sha256="a" * 64,
        )
    ]
    return price(plates, {1: filament}, PricingPolicy(), currency="USD", quantity=1), plates


def _slice_quote(shop: Shop, order: Order, item: OrderItem, key: str = "quote-a") -> Quote:
    """As the run records one (Ruling 3): the ref first, under the id made first."""
    priced, plates = _priced(shop)
    quote_id = f"q{len(shop.rows.quotes(item.id))}-{item.id}"
    assert shop.refs is not None
    shop.refs.add(key, QUOTE_HOLDER, quote_id)
    return shop.lifecycle.record_slice_quote(
        order.id,
        item.id,
        priced,
        plates,
        sliced_sha256="a" * 64,
        sliced_blob_key=key,
        by=BY,
        quote_id=quote_id,
    )


def _holders(shop: Shop) -> set[str]:
    assert shop.refs is not None
    with shop.refs.pool.connection() as conn:
        rows = conn.execute(
            "SELECT holder_id FROM blob_refs WHERE holder_kind = %s", (QUOTE_HOLDER,)
        ).fetchall()
    return {row["holder_id"] for row in rows}


def test_a_slice_quote_is_recorded_with_its_file(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order, quantity=2)
    quote = _slice_quote(shop, order, item)
    assert quote.source == "slice"
    assert (quote.sliced_sha256, quote.sliced_blob_key) == ("a" * 64, "quote-a")
    assert quote.plates[0]["index"] == 1
    assert quote.plates[0]["blob_key"] == "quote-a"
    assert quote.plates[0]["filaments"] == [{"slot": 1, "used_g": "10"}]
    assert quote.quantity == 2 and quote.total_minor == quote.unit_price_minor * 2
    assert shop.get_item(item).status == "quoted"
    assert "quote.recorded" in shop.kinds(order)
    assert any(isinstance(e, OrderItemChanged) for e in shop.events)


def test_quote_refs_follow_the_rules(shop: Shop) -> None:
    """Review Focus 4: an item holds at most its accepted quote's ref and its newest
    unaccepted one's; the rows stay."""
    order = shop.order()
    item = shop.item(order)
    first = _slice_quote(shop, order, item)
    second = _slice_quote(shop, order, item)
    # Recording drops the older unaccepted quote's ref.
    assert _holders(shop) == {second.id}
    shop.lifecycle.accept_quote(order.id, item.id, second.id, BY)
    third = _slice_quote(shop, order, item)
    # The accepted one and the newest unaccepted one.
    assert _holders(shop) == {second.id, third.id}
    # A manual quote is a recorded quote too: it supersedes the newest unaccepted one.
    shop.lifecycle.record_manual_quote(order.id, item.id, ManualQuoteInput(unit_price_minor=5), BY)
    assert _holders(shop) == {second.id}
    fourth = _slice_quote(shop, order, item)
    # Accepting drops every other quote's.
    shop.lifecycle.accept_quote(order.id, item.id, fourth.id, BY)
    assert _holders(shop) == {fourth.id}
    # Cancelling the item drops them all; the rows stay.
    shop.lifecycle.cancel_item(order.id, item.id, BY)
    assert _holders(shop) == set()
    assert len(shop.rows.quotes(item.id)) == 5
    assert first.id in {q.id for q in shop.rows.quotes(item.id)}

    # Cancelling the order drops its items' too.
    other = shop.order()
    one = shop.item(other)
    held = _slice_quote(shop, other, one)
    assert _holders(shop) == {held.id}
    shop.lifecycle.cancel_order(other.id, BY)
    assert _holders(shop) == set()


def test_closing_the_order_drops_its_quote_refs(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    quote = _slice_quote(shop, order, item)
    shop.lifecycle.accept_quote(order.id, item.id, quote.id, BY)
    order = shop.lifecycle.accept_order(order.id, BY)
    shop.fulfilled(order, shop.get_item(item))
    # A fulfilled item's accepted quote keeps its file until the order closes.
    assert _holders(shop) == {quote.id}
    shop.shipped(order, shop.get_item(item))
    shop.lifecycle.close_order(order.id, BY)
    assert _holders(shop) == set()


def _filaments(shop: Shop) -> tuple[str, str]:
    one = shop.rows.insert_filament(
        FilamentInput(material="PLA", colour="#111111", cost_per_kg="1")
    )
    two = shop.rows.insert_filament(
        FilamentInput(material="PLA", colour="#222222", cost_per_kg="1")
    )
    return one.id, two.id


def test_filaments_change_drops_the_quote(shop: Shop) -> None:
    """Review Focus 6: like a reference change (§5.2: a colours change is a new quote)."""
    one, two = _filaments(shop)
    order = shop.order()
    item = shop.item(order)
    shop.quoted(order, item)
    assert shop.get(order).status == "quoted"
    item = shop.lifecycle.update_item(order.id, item.id, BY, filaments={0: one})
    assert item.colours == [ColourSlot(slot=0, colour="#ff0000", filament_id=one)]
    assert item.status == "draft"
    assert all(not q.accepted for q in shop.rows.quotes(item.id))
    assert shop.get(order).status == "draft"
    events = [e for e in shop.rows.events(order.id) if e.kind == "item.updated"]
    assert events[-1].detail["repinned"] is True
    # A slot cleared is a change too.
    item = shop.lifecycle.update_item(order.id, item.id, BY, filaments={0: None})
    assert item.colours[0].filament_id is None
    # Once accepted, the filaments are pinned like the reference.
    shop.lifecycle.update_item(order.id, item.id, BY, filaments={0: two})
    order, item = shop.get(order), shop.get_item(item)
    shop.quoted(order, item)
    shop.lifecycle.accept_order(order.id, BY)
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.update_item(order.id, item.id, BY, filaments={0: one})
    assert refused.value.slug == "item_locked"


def test_the_same_filaments_drop_nothing(shop: Shop) -> None:
    one, _ = _filaments(shop)
    order = shop.order()
    item = shop.item(order)
    item = shop.lifecycle.update_item(order.id, item.id, BY, filaments={0: one})
    shop.quoted(order, item)
    item = shop.lifecycle.update_item(order.id, item.id, BY, filaments={0: one})
    assert item.status == "approved"
    assert any(q.accepted for q in shop.rows.quotes(item.id))
    assert shop.get(order).status == "quoted"
    # Unchanged on an accepted order too: nothing to refuse.
    order = shop.lifecycle.accept_order(order.id, BY)
    assert shop.lifecycle.update_item(order.id, item.id, BY, filaments={0: one}).status == (
        "approved"
    )


def test_filaments_name_known_slots_and_records(shop: Shop) -> None:
    one, two = _filaments(shop)
    order = shop.order()
    item = shop.item(order)
    for filaments, field in (({3: one}, "filaments"), ({0: "nobody"}, "filaments")):
        with pytest.raises(LifecycleError) as refused:
            shop.lifecycle.update_item(order.id, item.id, BY, filaments=filaments)
        assert refused.value.slug == field and refused.value.unprocessable
    shop.rows.archive_filament(two)
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.update_item(order.id, item.id, BY, filaments={0: two})
    assert "archived" in str(refused.value)
    assert shop.get_item(item).colours[0].filament_id is None


def _age(shop: Shop, holder_id: str, by: timedelta) -> None:
    assert shop.refs is not None
    with shop.refs.pool.connection() as conn:
        conn.execute(
            "UPDATE blob_refs SET created_at = created_at - %s"
            " WHERE holder_kind = %s AND holder_id = %s",
            (by, QUOTE_HOLDER, holder_id),
        )


def test_the_reaper_drops_what_the_rules_say(shop: Shop) -> None:
    """Ruling 3: a holder younger than the grace stays whatever it is; past it, one with
    no row goes, and so do a superseded unaccepted quote's, a cancelled item's and a
    closed order's, while an accepted quote's (a fulfilled item's on an order still
    open too) and an open item's newest stay, whatever their age. It also catches what
    an after-commit drop lost."""
    assert shop.refs is not None
    refs = shop.refs
    old = timedelta(hours=1)
    grace = timedelta(minutes=30)

    # A ref whose run never inserted its row: young, then old.
    refs.add("quote-x", QUOTE_HOLDER, "no-row-young")
    refs.add("quote-x", QUOTE_HOLDER, "no-row-old")

    # An open item: an older unaccepted quote whose drop was lost, and its newest.
    order = shop.order()
    item = shop.item(order)
    superseded = _slice_quote(shop, order, item)
    newest = _slice_quote(shop, order, item)
    refs.add("quote-a", QUOTE_HOLDER, superseded.id)  # the lost drop

    # An accepted quote on a fulfilled item, the order still open (shipped, not closed).
    done_order = shop.order()
    done_item = shop.item(done_order)
    kept = _slice_quote(shop, done_order, done_item)
    shop.lifecycle.accept_quote(done_order.id, done_item.id, kept.id, BY)
    done_order = shop.lifecycle.accept_order(done_order.id, BY)
    shop.fulfilled(done_order, shop.get_item(done_item))

    # A cancelled item's and a closed order's, each re-added as a lost drop would leave.
    cancelled_order = shop.order()
    cancelled_item = shop.item(cancelled_order)
    shop.item(cancelled_order)
    cancelled_quote = _slice_quote(shop, cancelled_order, cancelled_item)
    shop.lifecycle.cancel_item(cancelled_order.id, cancelled_item.id, BY)
    refs.add("quote-a", QUOTE_HOLDER, cancelled_quote.id)

    closed_order = shop.order()
    closed_item = shop.item(closed_order)
    closed_quote = _slice_quote(shop, closed_order, closed_item)
    shop.lifecycle.accept_quote(closed_order.id, closed_item.id, closed_quote.id, BY)
    closed_order = shop.lifecycle.accept_order(closed_order.id, BY)
    shop.fulfilled(closed_order, shop.get_item(closed_item))
    shop.shipped(closed_order, shop.get_item(closed_item))
    shop.lifecycle.close_order(closed_order.id, BY)
    refs.add("quote-a", QUOTE_HOLDER, closed_quote.id)

    # Young, nothing goes: the grace is every row's.
    assert reap_quote_holds(refs, shop.rows, grace=grace) == 0
    for holder in (
        "no-row-old",
        superseded.id,
        newest.id,
        kept.id,
        cancelled_quote.id,
        closed_quote.id,
    ):
        _age(shop, holder, old)
    assert reap_quote_holds(refs, shop.rows, grace=grace) == 4
    assert _holders(shop) == {"no-row-young", newest.id, kept.id}


# -- undo (§3.5 "Undo", #4251): a reopen holds its standing quotes' files again ---------


def _held_keys(shop: Shop) -> dict[str, set[str]]:
    assert shop.refs is not None
    with shop.refs.pool.connection() as conn:
        rows = conn.execute(
            "SELECT holder_id, key FROM blob_refs WHERE holder_kind = %s", (QUOTE_HOLDER,)
        ).fetchall()
    held: dict[str, set[str]] = {}
    for row in rows:
        held.setdefault(row["holder_id"], set()).add(row["key"])
    return held


def test_reopening_an_item_holds_its_standing_quotes_files_again(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    older = _slice_quote(shop, order, item, key="quote-older")
    accepted = _slice_quote(shop, order, item, key="quote-accepted")
    shop.lifecycle.accept_quote(order.id, item.id, accepted.id, BY)
    newest = _slice_quote(shop, order, item, key="quote-newest")
    standing = {accepted.id, newest.id}
    assert set(_held_keys(shop)) == standing
    shop.lifecycle.cancel_item(order.id, item.id, BY)
    assert _held_keys(shop) == {}

    shop.lifecycle.reopen_item(order.id, item.id, BY)
    # The accepted quote and the newest unaccepted one, each by every plate's key and its
    # own sliced file; never the older quote that no longer stands.
    assert _held_keys(shop) == {
        accepted.id: {"quote-a", "quote-accepted"},
        newest.id: {"quote-a", "quote-newest"},
    }
    assert older.id not in _held_keys(shop)


def test_reopening_a_cancelled_order_holds_its_quotes_files_again(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    quote = _slice_quote(shop, order, item, key="quote-one")
    shop.lifecycle.accept_quote(order.id, item.id, quote.id, BY)
    shop.lifecycle.cancel_order(order.id, BY)
    assert _held_keys(shop) == {}
    reopened = shop.lifecycle.reopen_order(order.id, BY)
    assert reopened.status == "quoted"
    assert shop.get_item(item).status == "approved"
    assert _held_keys(shop) == {quote.id: {"quote-a", "quote-one"}}


def test_reopening_a_closed_order_holds_its_quotes_files_again(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    quote = _slice_quote(shop, order, item, key="quote-closed")
    shop.lifecycle.accept_quote(order.id, item.id, quote.id, BY)
    order = shop.lifecycle.accept_order(order.id, BY)
    shop.fulfilled(order, shop.get_item(item))
    shop.shipped(order, shop.get_item(item))
    shop.lifecycle.close_order(order.id, BY)
    assert _held_keys(shop) == {}
    assert shop.lifecycle.reopen_order(order.id, BY).status == "shipped"
    assert _held_keys(shop) == {quote.id: {"quote-a", "quote-closed"}}
    # A reaper pass keeps them: every holder names a standing quote again.
    assert reap_quote_holds(shop.refs, shop.rows, grace=timedelta(0)) == 0  # type: ignore[arg-type]
    assert set(_held_keys(shop)) == {quote.id}


def test_a_refused_reopen_takes_no_hold(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    _slice_quote(shop, order, item, key="quote-open")
    shop.lifecycle.cancel_item(order.id, item.id, BY)
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.reopen_order(order.id, BY)
    assert refused.value.slug == "order_not_ended"
    assert _held_keys(shop) == {}
