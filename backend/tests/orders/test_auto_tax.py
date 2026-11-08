"""The auto tax charge (#5281, storefront spec §9): one `auto` row of kind `tax`, at
``orders_tax_percent`` of the items (plus the shipping with ``orders_tax_shipping``),
written again by every transition that changes the items, their quotes or the charges
while the order is `draft` through `ready`. A typed tax charge replaces it; none at 0
percent or at 0; and a shipped order's row stays as it was."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal

import pytest
from pydantic import ValidationError

from scadbuddy.orders.lifecycle import ManualQuoteInput, OrderEdit
from scadbuddy.orders.models import ChargeInput, Order
from scadbuddy.orders.money import OrderRules, ShippingOption, tax_minor
from scadbuddy.orders.store import OrderStore
from scadbuddy.orders.totals import totals_of
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, RulesShop, Shop

pytestmark = pytest.mark.requires_postgres

TEN = OrderRules("USD", Decimal("10"), False, (), ())
POST = ShippingOption(id="post", label="Post", amount_minor=500, ships=True)


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> RulesShop:
    return RulesShop(rows, BlobRefs(pool), TEN)


@pytest.fixture
def shop_taxing_shipping(rows: OrderStore, pool: PgPool) -> RulesShop:
    return RulesShop(
        rows, BlobRefs(pool), replace(TEN, tax_shipping=True, shipping_options=(POST,))
    )


def tax(shop: Shop, order: Order) -> list[tuple[int, bool]]:
    return [(c.amount_minor, c.auto) for c in shop.rows.charges(order.id) if c.kind == "tax"]


def subtotal(shop: Shop, order: Order) -> int:
    with shop.rows.transaction() as conn:
        return totals_of(shop.rows, conn, order.id).subtotal_minor


def test_tax_follows_a_quote_its_acceptance_and_a_quantity_change(shop: RulesShop) -> None:
    order = shop.order()
    item = shop.item(order)
    assert tax(shop, order) == []
    quote = shop.lifecycle.record_manual_quote(
        order.id, item.id, ManualQuoteInput(unit_price_minor=1005, label="typed"), BY
    )
    assert tax(shop, order) == [(101, True)]
    shop.lifecycle.accept_quote(order.id, item.id, quote.id, BY)
    assert tax(shop, order) == [(101, True)]
    [row] = [c for c in shop.rows.charges(order.id) if c.auto]
    assert row.label == "Tax 10%"
    shop.lifecycle.update_item(order.id, item.id, BY, quantity=2)  # draft; newest quote x 2
    assert tax(shop, order) == [(201, True)]


def _quoted(shop: RulesShop) -> Order:
    order = shop.order()
    shop.quoted(order, shop.item(order), price=1005)
    return order


def _add_custom_item(shop: RulesShop) -> tuple[Order, Callable[[], object]]:
    order = _quoted(shop)
    return order, lambda: shop.lifecycle.add_custom_item(
        order.id, BY, title="Box", quantity=1, unit_price_minor=500
    )


def _cancel_item(shop: RulesShop) -> tuple[Order, Callable[[], object]]:
    order = _quoted(shop)
    second = shop.quoted(order, shop.item(order), price=2000)
    return order, lambda: shop.lifecycle.cancel_item(order.id, second.id, BY)


def _reopen_item(shop: RulesShop) -> tuple[Order, Callable[[], object]]:
    order = _quoted(shop)
    second = shop.quoted(order, shop.item(order), price=2000)
    shop.lifecycle.cancel_item(order.id, second.id, BY)
    return order, lambda: shop.lifecycle.reopen_item(order.id, second.id, BY)


def _delete_item(shop: RulesShop) -> tuple[Order, Callable[[], object]]:
    order = _quoted(shop)
    second = shop.item(order)
    shop.lifecycle.record_manual_quote(
        order.id, second.id, ManualQuoteInput(unit_price_minor=2000, label="typed"), BY
    )
    shop.lifecycle.update_item(order.id, second.id, BY, quantity=2)  # draft again, 4000
    return order, lambda: shop.lifecycle.delete_item(order.id, second.id, BY)


def _edit_order_charges(shop: RulesShop) -> tuple[Order, Callable[[], object]]:
    order = _quoted(shop)
    vat = ChargeInput(kind="tax", label="VAT", amount_minor=77)
    shop.lifecycle.edit_order(order.id, BY, OrderEdit(charges=[vat]))
    return order, lambda: shop.lifecycle.edit_order(order.id, BY, OrderEdit(charges=[]))


def _replace_charges_after_acceptance(shop: RulesShop) -> tuple[Order, Callable[[], object]]:
    order = _quoted(shop)
    vat = ChargeInput(kind="tax", label="VAT", amount_minor=77)
    shop.lifecycle.edit_order(order.id, BY, OrderEdit(charges=[vat]))
    shop.lifecycle.accept_order(order.id, BY)
    return order, lambda: shop.lifecycle.replace_charges_after_acceptance(order.id, BY, [])


def _add_item_after_acceptance(shop: RulesShop) -> tuple[Order, Callable[[], object]]:
    order = _quoted(shop)
    shop.lifecycle.accept_order(order.id, BY)
    return order, lambda: shop.lifecycle.add_custom_item(
        order.id,
        BY,
        title="Box",
        quantity=1,
        unit_price_minor=500,
        accept=False,
        after_acceptance=True,
    )


def _add_revision(shop: RulesShop) -> tuple[Order, Callable[[], object]]:
    order = shop.order()
    item = shop.quoted(order, shop.item(order), price=1005)
    # A newer price, not accepted: the accepted one counts until a revision drops it.
    shop.lifecycle.record_manual_quote(
        order.id, item.id, ManualQuoteInput(unit_price_minor=3000, label="typed"), BY
    )
    return order, lambda: shop.lifecycle.add_revision(order.id, item.id, BY)


def _reopen_order(shop: RulesShop) -> tuple[Order, Callable[[], object]]:
    order = _quoted(shop)
    shop.lifecycle.cancel_order(order.id, BY)

    def reopen() -> object:
        shop.now = replace(TEN, tax_percent=Decimal("20"))
        return shop.lifecycle.reopen_order(order.id, BY)

    return order, reopen


def _set_shipping_option(shop: RulesShop) -> tuple[Order, Callable[[], object]]:
    order = _quoted(shop)
    shop.now = replace(TEN, tax_shipping=True, shipping_options=(POST,))
    return order, lambda: shop.lifecycle.set_shipping_option(order.id, BY, "post")


TRANSITIONS: dict[str, Callable[[RulesShop], tuple[Order, Callable[[], object]]]] = {
    "add_custom_item": _add_custom_item,
    "cancel_item": _cancel_item,
    "reopen_item": _reopen_item,
    "delete_item": _delete_item,
    "edit_order_charges": _edit_order_charges,
    "replace_charges_after_acceptance": _replace_charges_after_acceptance,
    "add_item_after_acceptance=True": _add_item_after_acceptance,
    "add_revision": _add_revision,
    "reopen_order": _reopen_order,
    "set_shipping_option": _set_shipping_option,
}


@pytest.mark.parametrize("transition", list(TRANSITIONS))
def test_every_change_of_items_quotes_or_charges_retaxes(shop: RulesShop, transition: str) -> None:
    order, run = TRANSITIONS[transition](shop)
    before = tax(shop, order)
    run()
    shipping = sum(c.amount_minor for c in shop.rows.charges(order.id) if c.kind == "shipping")
    base = subtotal(shop, order) + (shipping if shop.now.tax_shipping else 0)
    assert tax(shop, order) == [(tax_minor(base, shop.now.tax_percent), True)]
    assert tax(shop, order) != before


def test_shipping_is_in_the_base_only_with_orders_tax_shipping(
    shop_taxing_shipping: RulesShop,
) -> None:
    shop = shop_taxing_shipping
    order = _quoted(shop)
    assert tax(shop, order) == [(101, True)]
    shop.lifecycle.set_shipping_option(order.id, BY, "post")
    assert tax(shop, order) == [(151, True)]  # (1005 + 500) x 10%, half up
    # Fees and discounts are never in the base.
    charges = [
        ChargeInput(kind="shipping", label="Post", amount_minor=500),
        ChargeInput(kind="fee", label="Rush", amount_minor=1000),
        ChargeInput(kind="discount", label="Friend", amount_minor=-400),
    ]
    shop.lifecycle.edit_order(order.id, BY, OrderEdit(charges=charges))
    assert tax(shop, order) == [(151, True)]
    shop.now = replace(shop.now, tax_shipping=False)
    shop.lifecycle.edit_order(order.id, BY, OrderEdit(charges=charges))
    assert tax(shop, order) == [(101, True)]


def test_no_tax_at_zero_percent_and_none_once_shipped(shop: RulesShop) -> None:
    order = _quoted(shop)
    assert tax(shop, order) == [(101, True)]
    # At 0 percent the next transition drops the row.
    shop.now = replace(TEN, tax_percent=Decimal(0))
    shop.lifecycle.update_item(order.id, shop.rows.items(order.id)[0].id, BY, quantity=2)
    assert tax(shop, order) == []
    # Nor is there one at 0: a free item.
    shop.now = TEN
    free = _quoted_at(shop, 0)
    assert tax(shop, free) == []

    # Once shipped the row is as it stood: a close reopened to `shipped` writes none.
    order = _quoted(shop)
    [item] = shop.rows.items(order.id)
    order = shop.lifecycle.accept_order(order.id, BY)
    shop.fulfilled(order, item)
    shop.shipped(order, shop.get_item(item))
    shop.lifecycle.close_order(order.id, BY)
    shop.now = replace(TEN, tax_percent=Decimal("20"))
    reopened = shop.lifecycle.reopen_order(order.id, BY)
    assert reopened.status == "shipped"
    assert tax(shop, order) == [(101, True)]


def _quoted_at(shop: RulesShop, price: int) -> Order:
    order = shop.order()
    shop.quoted(order, shop.item(order), price=price)
    return order


# Review Focus 4


def test_a_typed_tax_charge_replaces_the_auto_one(shop: RulesShop) -> None:
    order = _quoted(shop)
    vat = ChargeInput(kind="tax", label="VAT", amount_minor=77)
    shop.lifecycle.edit_order(order.id, BY, OrderEdit(charges=[vat]))
    assert tax(shop, order) == [(77, False)]
    shop.quoted(order, shop.item(order), price=5000)
    assert tax(shop, order) == [(77, False)]


# Review Focus 5


def test_replacing_the_charges_keeps_and_recomputes_the_auto_row(shop: RulesShop) -> None:
    order = _quoted(shop)
    rush = ChargeInput(kind="fee", label="Rush", amount_minor=100)
    shop.lifecycle.edit_order(order.id, BY, OrderEdit(charges=[rush]))
    assert tax(shop, order) == [(101, True)]
    assert [(c.kind, c.auto) for c in shop.rows.charges(order.id)] == [
        ("fee", False),
        ("tax", True),
    ]


def test_the_audit_row_after_acceptance_holds_the_recomputed_tax(shop: RulesShop) -> None:
    order = _quoted(shop)
    shop.lifecycle.accept_order(order.id, BY)
    shop.now = replace(TEN, tax_percent=Decimal("20"))
    shop.lifecycle.replace_charges_after_acceptance(order.id, BY, [])
    [row] = [e for e in shop.rows.events(order.id) if e.kind == "charges.replaced_after_acceptance"]
    assert row.detail["after"] == [{"kind": "tax", "label": "Tax 20%", "amount_minor": 201}]


def test_a_charge_body_cannot_set_auto() -> None:
    with pytest.raises(ValidationError):
        OrderEdit.model_validate(
            {"charges": [{"kind": "tax", "label": "x", "amount_minor": 1, "auto": True}]}
        )


def test_reopening_an_item_on_a_shipped_order_retaxes_after_the_rederive(
    shop: RulesShop,
) -> None:
    order = _quoted(shop)
    [first] = shop.rows.items(order.id)
    second = shop.quoted(order, shop.item(order), price=2000)
    order = shop.lifecycle.accept_order(order.id, BY)
    shop.lifecycle.cancel_item(order.id, second.id, BY)
    assert tax(shop, order) == [(101, True)]
    shop.fulfilled(order, first)
    shop.shipped(order, shop.get_item(first))
    assert shop.get(order).status == "shipped"

    shop.lifecycle.reopen_item(order.id, second.id, BY)

    assert shop.get(order).status != "shipped"
    assert subtotal(shop, order) == 3005
    assert tax(shop, order) == [(tax_minor(3005, Decimal("10")), True)]


@pytest.mark.parametrize(
    ("percent", "label"),
    [(Decimal("7.50"), "Tax 7.5%"), (Decimal("10"), "Tax 10%"), (Decimal("1E+1"), "Tax 10%")],
)
def test_the_tax_label_is_a_plain_percent(shop: RulesShop, percent: Decimal, label: str) -> None:
    shop.now = replace(TEN, tax_percent=percent)
    order = _quoted(shop)
    assert [c.label for c in shop.rows.charges(order.id) if c.auto] == [label]
