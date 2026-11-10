"""Shipping options (#5281, storefront spec §8): an option chosen on a `draft` or `quoted`
order is its one `shipping` charge, at the option's label and price, naming the option;
one that ships needs the order's shipping address, a pickup does not."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest

from scadbuddy.orders.lifecycle import LifecycleError, OrderEdit
from scadbuddy.orders.models import ChargeInput, Order
from scadbuddy.orders.money import OrderRules, ShippingOption
from scadbuddy.orders.problems import refused as problem_of
from scadbuddy.orders.store import OrderStore
from scadbuddy.orders.storefront.visitors import VisitorStore, visitor_actor
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, RulesShop

pytestmark = pytest.mark.requires_postgres

POST = ShippingOption(id="post", label="Post", amount_minor=500, ships=True)
PICKUP = ShippingOption(id="pickup", label="Pickup", amount_minor=0, ships=False)
RULES = OrderRules("USD", Decimal(0), False, (), (POST, PICKUP))


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> RulesShop:
    return RulesShop(rows, BlobRefs(pool), RULES)


def charges(shop: RulesShop, order: Order) -> list[tuple[str, str, int, str | None]]:
    return [
        (c.kind, c.label, c.amount_minor, c.shipping_option_id) for c in shop.rows.charges(order.id)
    ]


def without_address(shop: RulesShop) -> Order:
    order = shop.order()
    return shop.lifecycle.edit_order(order.id, BY, OrderEdit(shipping_address_id=None))


def test_an_option_writes_the_one_shipping_charge(shop: RulesShop) -> None:
    order = shop.order()
    rush = ChargeInput(kind="fee", label="Rush", amount_minor=100)
    old = ChargeInput(kind="shipping", label="Courier", amount_minor=900)
    shop.lifecycle.edit_order(order.id, BY, OrderEdit(charges=[rush, old, old]))

    shop.lifecycle.set_shipping_option(order.id, BY, "post")

    assert charges(shop, order) == [
        ("fee", "Rush", 100, None),
        ("shipping", "Post", 500, "post"),
    ]
    [event] = [e for e in shop.rows.events(order.id) if e.kind == "shipping.option_set"]
    assert event.detail == {"option_id": "post"}

    shop.lifecycle.set_shipping_option(order.id, BY, "pickup")
    assert charges(shop, order) == [
        ("fee", "Rush", 100, None),
        ("shipping", "Pickup", 0, "pickup"),
    ]

    # None removes the shipping charges.
    shop.lifecycle.set_shipping_option(order.id, BY, None)
    assert charges(shop, order) == [("fee", "Rush", 100, None)]
    events = [e.detail for e in shop.rows.events(order.id) if e.kind == "shipping.option_set"]
    assert events[-1] == {"option_id": None}


def test_a_pickup_needs_no_address(shop: RulesShop) -> None:
    order = without_address(shop)
    assert order.shipping_address_id is None
    shop.lifecycle.set_shipping_option(order.id, BY, "pickup")
    assert charges(shop, order) == [("shipping", "Pickup", 0, "pickup")]


def test_an_option_that_ships_needs_an_address(shop: RulesShop) -> None:
    order = without_address(shop)
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.set_shipping_option(order.id, BY, "post")
    assert refused.value.slug == "shipping_address_required"
    assert not refused.value.unprocessable
    assert charges(shop, order) == []


def test_an_order_in_another_currency_than_the_shops_takes_no_option(shop: RulesShop) -> None:
    """The options' amounts are in the shop's currency and none is converted: 500 yen
    must never become 5.00 dollars. None still removes the shipping charges."""
    order = shop.lifecycle.create_order(shop.customer.id, BY, currency="JPY", notes="", due_at=None)
    assert order.currency != shop.now.shop_currency
    for option_id in ("post", "pickup"):
        with pytest.raises(LifecycleError) as refused:
            shop.lifecycle.set_shipping_option(order.id, BY, option_id)
        assert refused.value.slug == "shipping_currency_mismatch"
        assert not refused.value.unprocessable and not refused.value.not_found
    assert charges(shop, order) == []
    shop.lifecycle.set_shipping_option(order.id, BY, None)
    # The route answers it as a 409 problem.
    error = problem_of(refused.value)
    assert (error.status, error.type.rsplit("/", 1)[-1]) == (409, "shipping_currency_mismatch")


def test_an_unknown_option_is_422(shop: RulesShop) -> None:
    order = shop.order()
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.set_shipping_option(order.id, BY, "drone")
    # A 422 on the body field (``orders/problems.py`` names the field by the slug).
    assert refused.value.slug == "option_id"
    assert refused.value.unprocessable
    assert "shipping_option_not_found" in str(refused.value)
    assert charges(shop, order) == []


def test_options_close_with_the_charges_after_quoted(shop: RulesShop) -> None:
    order = shop.order()
    shop.quoted(order, shop.item(order))
    shop.lifecycle.set_shipping_option(order.id, BY, "post")  # quoted: still open
    shop.lifecycle.accept_order(order.id, BY)
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.set_shipping_option(order.id, BY, "pickup")
    assert refused.value.slug == "order_not_editable"
    assert f"PUT /orders/{order.id}/charges/after-acceptance" in str(refused.value)
    assert charges(shop, order) == [("shipping", "Post", 500, "post")]


def test_a_refusal_on_a_cart_names_the_cart(shop: RulesShop, pool: PgPool) -> None:
    visitor = VisitorStore(pool).ensure("v" * 43)
    by = visitor_actor(visitor)
    cart = shop.lifecycle.ensure_draft(visitor.id, by, currency="USD")
    with pytest.raises(LifecycleError) as e:
        shop.lifecycle.set_shipping_option(cart.id, by, "post")
    assert e.value.slug == "shipping_address_required"
    assert str(e.value).startswith(f"order cart {cart.id} has no shipping address")


def test_the_visitor_sets_the_option_and_charges_on_their_cart(
    shop: RulesShop, pool: PgPool
) -> None:
    visitor = VisitorStore(pool).ensure("v" * 43)
    by = visitor_actor(visitor)
    cart = shop.lifecycle.ensure_draft(visitor.id, by, currency="USD")
    shop.lifecycle.set_shipping_option(cart.id, by, "pickup")
    assert charges(shop, cart) == [("shipping", "Pickup", 0, "pickup")]
    rush = ChargeInput(kind="fee", label="Rush", amount_minor=100)
    shop.lifecycle.edit_order(cart.id, by, OrderEdit(charges=[rush]))
    assert charges(shop, cart) == [("fee", "Rush", 100, None)]


def test_the_back_office_never_sets_a_carts_option_or_charges(
    shop: RulesShop, pool: PgPool
) -> None:
    visitor = VisitorStore(pool).ensure("v" * 43)
    cart = shop.lifecycle.ensure_draft(visitor.id, visitor_actor(visitor), currency="USD")
    rush = ChargeInput(kind="fee", label="Rush", amount_minor=100)
    calls: list[Callable[[], object]] = [
        lambda: shop.lifecycle.set_shipping_option(cart.id, BY, "pickup"),
        lambda: shop.lifecycle.edit_order(cart.id, BY, OrderEdit(charges=[rush])),
    ]
    for call in calls:
        with pytest.raises(LifecycleError) as e:
            call()
        assert e.value.slug == "order_not_submitted"
    assert charges(shop, cart) == []
    # Notes are not a charge: the back office may still write them.
    assert shop.lifecycle.edit_order(cart.id, BY, OrderEdit(notes="hi")).notes == "hi"
