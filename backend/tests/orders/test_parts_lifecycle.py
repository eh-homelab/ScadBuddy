"""An item's parts (#5543, job cost spec §11): filled from its model's rule, used when it
is fulfilled as a `material` cost entry, given back when it is unfulfilled."""

from __future__ import annotations

from decimal import Decimal

import pytest

from scadbuddy.orders.lifecycle import FulfilmentInput, LifecycleError
from scadbuddy.orders.models import ColourSlot
from scadbuddy.orders.parts import (
    ItemPart,
    PartInput,
    PartRuleInput,
    PartRuleLine,
    PartRuleWhen,
    order_parts,
)
from scadbuddy.orders.reference import ResolvedReference
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop, resolved

pytestmark = pytest.mark.requires_postgres


@pytest.fixture(name="shop")
def _shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


def _part(shop: Shop, name: str, on_hand: int = 50, currency: str = "USD") -> str:
    body = PartInput(name=name, pack_size=100, pack_price_minor=599, on_hand=on_hand)
    return shop.lifecycle.parts.create(body, currency=currency, by=BY).id


def _on_hand(shop: Shop, part_id: str) -> int:
    part = shop.lifecycle.parts.part(part_id)
    assert part is not None
    return part.on_hand


def _pink(slug: str = "demo") -> ResolvedReference:
    plain = resolved(slug)
    return ResolvedReference(plain.reference, [ColourSlot(slot=0, colour="#FF69B4")])


def test_a_new_item_takes_its_models_rule(shop: Shop) -> None:
    silver, pink = _part(shop, "Silver hook"), _part(shop, "Pink hook")
    line = PartRuleLine(part_id=silver, when=[PartRuleWhen(colour="#ff69b4", part_id=pink)])
    shop.lifecycle.parts.set_rule("demo", PartRuleInput(lines=[line]), BY)
    order = shop.order()

    plain = shop.item(order)
    pinked = shop.lifecycle.add_item(order.id, BY, _pink(), output_id=None, quantity=2)

    got = shop.lifecycle.parts.item_parts([plain.id, pinked.id])
    assert [(p.part_id, p.per_unit) for p in got[plain.id]] == [(silver, 1)]
    assert [(p.part_id, p.per_unit) for p in got[pinked.id]] == [(pink, 1)]
    assert shop.kinds(order).count("item.parts_set") == 2


def test_a_model_with_no_rule_gives_no_parts(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    assert shop.lifecycle.parts.item_parts([item.id])[item.id] == []
    assert "item.parts_set" not in shop.kinds(order)


def test_fulfil_uses_the_parts_as_a_material_cost(shop: Shop) -> None:
    hook = _part(shop, "Silver hook", on_hand=5)
    order, item = shop.accepted(quantity=3)
    shop.lifecycle.set_item_parts(order.id, item.id, [ItemPart(part_id=hook, per_unit=2)], BY)

    shop.fulfilled(order, item)

    # 6 used of 5: stock goes below zero and never blocks.
    assert _on_hand(shop, hook) == -1
    [entry] = shop.rows.cost_entries(order.id)
    assert (entry.kind, entry.amount_minor, entry.source, entry.order_item_id) == (
        "material",
        36,
        f"part:{item.id}:{hook}",
        item.id,
    )
    assert shop.kinds(order)[-1] in ("parts.used", "order.status")
    assert "parts.used" in shop.kinds(order)


def test_a_failed_print_uses_no_parts(shop: Shop) -> None:
    hook = _part(shop, "Silver hook")
    order, item = shop.accepted()
    shop.lifecycle.set_item_parts(order.id, item.id, [ItemPart(part_id=hook, per_unit=1)], BY)

    shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=1, outcome="failed"), BY
    )

    assert _on_hand(shop, hook) == 50
    assert shop.rows.cost_entries(order.id) == []


def test_unfulfil_gives_the_parts_back_and_refulfil_uses_them_once(shop: Shop) -> None:
    hook = _part(shop, "Silver hook", on_hand=10)
    order, item = shop.accepted(quantity=2)
    shop.lifecycle.set_item_parts(order.id, item.id, [ItemPart(part_id=hook, per_unit=1)], BY)
    shop.fulfilled(order, item)

    shop.lifecycle.unfulfil_item(order.id, item.id, BY)

    assert _on_hand(shop, hook) == 10
    assert shop.rows.cost_entries(order.id) == []
    part = shop.lifecycle.parts.part(hook)
    assert part is not None and part.unit_cost_minor == Decimal("5.99")
    assert "parts.returned" in shop.kinds(order)

    shop.fulfilled(order, shop.get_item(item))
    assert _on_hand(shop, hook) == 8
    assert len(shop.rows.cost_entries(order.id)) == 1


def test_a_fulfilled_items_parts_are_fixed(shop: Shop) -> None:
    hook = _part(shop, "Silver hook")
    order, item = shop.accepted()
    shop.fulfilled(order, item)

    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.set_item_parts(order.id, item.id, [ItemPart(part_id=hook, per_unit=1)], BY)
    assert refused.value.slug == "item_parts_used"


def test_an_unknown_part_is_refused(shop: Shop) -> None:
    order, item = shop.accepted()
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.set_item_parts(order.id, item.id, [ItemPart(part_id="nope", per_unit=1)], BY)
    assert refused.value.slug == "part_not_found"


def test_a_part_in_another_currency_moves_stock_but_costs_nothing(shop: Shop) -> None:
    hook = _part(shop, "Euro hook", on_hand=4, currency="EUR")
    order, item = shop.accepted()
    shop.lifecycle.set_item_parts(order.id, item.id, [ItemPart(part_id=hook, per_unit=1)], BY)

    shop.fulfilled(order, item)

    assert _on_hand(shop, hook) == 3
    assert shop.rows.cost_entries(order.id) == []


def test_an_order_shows_what_it_is_short(shop: Shop) -> None:
    hook = _part(shop, "Silver hook", on_hand=2)
    order, item = shop.accepted(quantity=5)
    shop.lifecycle.set_item_parts(order.id, item.id, [ItemPart(part_id=hook, per_unit=1)], BY)

    found = order_parts(shop.rows, shop.lifecycle.parts, order.id)

    assert found is not None
    assert [(n.name, n.needed, n.on_hand, n.short) for n in found.short] == [
        ("Silver hook", 5, 2, 3)
    ]
    shop.fulfilled(order, shop.get_item(item))
    after = order_parts(shop.rows, shop.lifecycle.parts, order.id)
    assert after is not None and after.short == []
