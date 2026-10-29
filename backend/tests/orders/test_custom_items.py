"""Custom line items (#4954): a title, a quantity and a typed price, with no template
behind them. The price is a `manual` quote accepted as the item is saved; a source
attached later puts the item on the normal flow."""

from __future__ import annotations

import psycopg
import pytest

from scadbuddy.core.events import OrderItemChanged
from scadbuddy.orders.lifecycle import FulfilmentInput, LifecycleError, ShipmentInput
from scadbuddy.orders.models import Order, OrderItem, ShipmentItem
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop, resolved

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


def custom(shop: Shop, order: Order, **fields: object) -> OrderItem:
    values: dict[str, object] = {
        "title": "Design work",
        "quantity": 2,
        "unit_price_minor": 2500,
        **fields,
    }
    return shop.lifecycle.add_custom_item(order.id, BY, **values)  # type: ignore[arg-type]


def test_a_custom_item_is_approved_at_its_accepted_typed_price(shop: Shop) -> None:
    order = shop.order()
    item = custom(shop, order, notes="2 h", sku="DES-1")
    assert (item.kind, item.reference, item.colours, item.output_id) == ("custom", None, [], None)
    assert (item.status, item.title, item.notes) == ("approved", "Design work", "2 h")
    assert item.sku == "DES-1"
    assert item.requires_printing is False
    [quote] = shop.rows.quotes(item.id)
    assert (quote.source, quote.accepted, quote.unit_price_minor) == ("manual", True, 2500)
    assert (quote.quantity, quote.total_minor) == (2, 5000)
    # Its only item is priced, so the order is quoted and accepts as any other.
    assert shop.get(order).status == "quoted"
    assert shop.kinds(order)[-3:] == ["item.added", "quote.accepted", "order.status"]
    assert shop.lifecycle.accept_order(order.id, BY).status == "accepted"
    assert shop.rows.item(item.id) == item
    assert [e for e in shop.events if isinstance(e, OrderItemChanged)][-1].status == "approved"


def test_a_custom_item_sits_beside_a_model_item(shop: Shop) -> None:
    order = shop.order()
    model = shop.item(order)
    item = custom(shop, order, requires_printing=True)
    assert item.requires_printing is True
    assert [i.id for i in shop.rows.items(order.id)] == [model.id, item.id]
    assert (model.kind, model.requires_printing) == ("model", True)
    # The model item still waits for its price.
    assert shop.get(order).status == "draft"
    # The asset sweep reads only the model item's inputs.
    assert model.reference is not None
    assert list(shop.rows.item_inputs()) == [model.reference.inputs]


def test_quantity_and_price_edits_are_priced_at_once(shop: Shop) -> None:
    order = shop.order()
    item = custom(shop, order)
    item = shop.lifecycle.update_item(order.id, item.id, BY, quantity=3)
    assert (item.status, item.quantity) == ("approved", 3)
    item = shop.lifecycle.update_item(order.id, item.id, BY, unit_price_minor=1000)
    quotes = shop.rows.quotes(item.id)
    accepted = [q for q in quotes if q.accepted]
    assert len(quotes) == 3 and len(accepted) == 1
    assert (accepted[0].unit_price_minor, accepted[0].total_minor) == (1000, 3000)
    assert shop.get(order).status == "quoted"
    # The same quantity and price again change nothing.
    again = shop.lifecycle.update_item(
        order.id, item.id, BY, quantity=3, unit_price_minor=1000, sku=None
    )
    assert again == item and len(shop.rows.quotes(item.id)) == 3
    # The title, the notes, the SKU and the flag change in place, with no new quote.
    item = shop.lifecycle.update_item(
        order.id, item.id, BY, title="Rush", sku="R", requires_printing=True
    )
    assert (item.title, item.sku, item.requires_printing) == ("Rush", "R", True)
    assert len(shop.rows.quotes(item.id)) == 3 and item.status == "approved"


def test_a_custom_items_price_is_pinned_once_accepted(shop: Shop) -> None:
    order = shop.order()
    item = custom(shop, order)
    shop.lifecycle.accept_order(order.id, BY)
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.update_item(order.id, item.id, BY, unit_price_minor=1)
    assert refused.value.slug == "item_locked"
    # A title still changes.
    assert shop.lifecycle.update_item(order.id, item.id, BY, title="T").title == "T"


def test_a_hold_refuses_a_custom_items_accepted_price(shop: Shop) -> None:
    order = shop.order()
    item = custom(shop, order)
    shop.lifecycle.hold(order.id, BY)
    for call in (
        lambda: custom(shop, order),
        lambda: shop.lifecycle.update_item(order.id, item.id, BY, unit_price_minor=1),
    ):
        with pytest.raises(LifecycleError) as refused:
            call()
        assert refused.value.slug == "order_on_hold"
    assert shop.lifecycle.update_item(order.id, item.id, BY, notes="n").notes == "n"


def test_a_model_item_refuses_the_custom_fields(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    for name, value in (("unit_price_minor", 5), ("requires_printing", False)):
        with pytest.raises(LifecycleError) as refused:
            shop.lifecycle.update_item(order.id, item.id, BY, **{name: value})  # type: ignore[arg-type]
        assert (refused.value.slug, refused.value.unprocessable) == (name, True)


def test_a_source_attached_puts_the_item_on_the_normal_flow(shop: Shop) -> None:
    order = shop.order()
    item = custom(shop, order, sku="S")
    item = shop.lifecycle.update_item(order.id, item.id, BY, reference=resolved())
    assert (item.kind, item.status, item.requires_printing, item.sku) == (
        "model",
        "draft",
        True,
        "S",
    )
    assert item.reference is not None and item.reference.slug == "demo"
    assert not any(q.accepted for q in shop.rows.quotes(item.id))
    assert shop.get(order).status == "draft"


def test_a_custom_item_is_marked_done_and_shipped(shop: Shop) -> None:
    order = shop.order()
    item = custom(shop, order, quantity=1)
    shop.lifecycle.accept_order(order.id, BY)
    attempt = shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
    )
    shop.lifecycle.accept_fulfilment(order.id, item.id, attempt.id, BY)
    assert shop.get(order).status == "ready"
    shop.lifecycle.add_shipment(
        order.id,
        ShipmentInput(items=[ShipmentItem(order_item_id=item.id, quantity=1)]),
        BY,
    )
    assert shop.lifecycle.close_order(order.id, BY).status == "closed"


def test_the_table_keeps_each_kind_whole(shop: Shop, pool: PgPool) -> None:
    order = shop.order()
    model = shop.item(order)
    item = custom(shop, order)
    with pool.connection() as conn:
        for sql, row in (
            ("UPDATE order_items SET slug = NULL WHERE id = %s", model.id),
            ("UPDATE order_items SET requires_printing = false WHERE id = %s", model.id),
            ("UPDATE order_items SET slug = 'demo' WHERE id = %s", item.id),
            ("UPDATE order_items SET output_id = 'out-1' WHERE id = %s", item.id),
        ):
            with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
                conn.execute(sql, (row,))
