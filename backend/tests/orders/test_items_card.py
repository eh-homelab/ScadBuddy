"""The items card's two writes (#4955, order operations spec 2026-11-07 §4.2): an item
duplicated with its current design and no quote, and several items fulfilled in one
transaction."""

from __future__ import annotations

import pytest

from scadbuddy.orders.lifecycle import FulfilLine, Lifecycle, LifecycleError
from scadbuddy.orders.models import LibraryFileRef
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


def kinds(shop: Shop, order_id: str) -> list[str]:
    return [event.kind for event in shop.rows.events(order_id)]


# -- duplicate ---------------------------------------------------------------------------------


def test_a_duplicate_copies_the_design_and_not_the_quote(shop: Shop) -> None:
    order = shop.order()
    item = shop.lifecycle.update_item(
        order.id, shop.item(order, quantity=3).id, BY, title="Tag", notes="blue", sku="T-1"
    )
    shop.quoted(order, item)
    copy = shop.lifecycle.duplicate_item(order.id, item.id, BY)
    assert copy.id != item.id
    assert (copy.reference, copy.colours, copy.output_id) == (
        item.reference,
        item.colours,
        item.output_id,
    )
    assert (copy.quantity, copy.title, copy.notes, copy.sku) == (3, "Tag", "blue", "T-1")
    assert copy.status == "draft"
    assert shop.rows.quotes(copy.id) == []
    assert len(shop.rows.revisions(copy.id)) == 1
    assert "item.duplicated" in kinds(shop, order.id)


def test_a_duplicate_on_an_accepted_order_goes_through_after_acceptance(shop: Shop) -> None:
    order, item = shop.accepted()
    with pytest.raises(LifecycleError) as locked:
        shop.lifecycle.duplicate_item(order.id, item.id, BY)
    assert locked.value.slug == "item_locked"
    copy = shop.lifecycle.duplicate_item(order.id, item.id, BY, after_acceptance=True)
    assert copy.status == "draft"
    assert "item.added_after_acceptance" in kinds(shop, order.id)


def test_a_custom_item_is_duplicated_with_its_own_price(shop: Shop) -> None:
    order = shop.order()
    custom = shop.lifecycle.add_custom_item(
        order.id, BY, title="Design work", quantity=2, unit_price_minor=1500
    )
    copy = shop.lifecycle.duplicate_item(order.id, custom.id, BY)
    assert (copy.kind, copy.title, copy.quantity) == ("custom", "Design work", 2)
    quotes = shop.rows.quotes(copy.id)
    assert [(q.unit_price_minor, q.accepted) for q in quotes] == [(1500, True)]


def test_a_library_file_item_is_duplicated_without_its_slots(shop: Shop) -> None:
    order = shop.order()
    item = shop.lifecycle.add_library_item(
        order.id,
        BY,
        LibraryFileRef(id=5_000_000_001, filename="bracket.gcode.3mf", library="native"),
        quantity=1,
        origin_archive_ids=(71,),
    )
    copy = shop.lifecycle.duplicate_item(order.id, item.id, BY)
    assert copy.library_file == item.library_file
    assert list(copy.origin_archive_ids) == [71]


def test_a_duplicate_names_an_item_of_the_order(shop: Shop) -> None:
    order, other = shop.order(), shop.order()
    item = shop.item(other)
    with pytest.raises(LifecycleError) as missing:
        shop.lifecycle.duplicate_item(order.id, item.id, BY)
    assert missing.value.slug == "item_not_found"


# -- bulk fulfil -------------------------------------------------------------------------------


def accepted_two(shop: Shop, lifecycle: Lifecycle) -> tuple[str, str, str]:
    order = shop.order()
    first, second = shop.item(order, quantity=2), shop.item(order, quantity=3)
    shop.quoted(order, first)
    shop.quoted(order, second)
    lifecycle.accept_order(order.id, BY)
    return order.id, first.id, second.id


def test_several_items_are_fulfilled_at_once(shop: Shop) -> None:
    order_id, first, second = accepted_two(shop, shop.lifecycle)
    attempts = shop.lifecycle.fulfil_items(
        order_id,
        [FulfilLine(item_id=first, quantity=2), FulfilLine(item_id=second, quantity=1)],
        BY,
    )
    assert [(a.quantity, a.outcome, a.accepted_at is not None) for a in attempts] == [
        (2, "succeeded", True),
        (1, "succeeded", True),
    ]
    # The first is made in full; the second has two still to make.
    assert [shop.rows.item(i).status for i in (first, second)] == ["fulfilled", "approved"]  # type: ignore[union-attr]
    assert kinds(shop, order_id).count("fulfilment.accepted") == 2


def test_one_refused_line_fulfils_nothing(shop: Shop) -> None:
    order_id, first, second = accepted_two(shop, shop.lifecycle)
    shop.lifecycle.fulfil_items(order_id, [FulfilLine(item_id=second, quantity=3)], BY)
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.fulfil_items(
            order_id,
            [FulfilLine(item_id=first, quantity=2), FulfilLine(item_id=second, quantity=1)],
            BY,
        )
    assert refused.value.slug == "item_not_open"
    assert refused.value.items == [second]
    assert shop.rows.fulfilments(first) == []
    assert shop.rows.item(first).status == "approved"  # type: ignore[union-attr]


def test_bulk_fulfil_refuses_a_held_or_open_order_and_a_repeated_item(shop: Shop) -> None:
    order_id, first, _ = accepted_two(shop, shop.lifecycle)
    with pytest.raises(LifecycleError) as twice:
        shop.lifecycle.fulfil_items(
            order_id,
            [FulfilLine(item_id=first, quantity=1), FulfilLine(item_id=first, quantity=1)],
            BY,
        )
    assert twice.value.slug == "items"
    shop.lifecycle.hold(order_id, BY)
    with pytest.raises(LifecycleError) as held:
        shop.lifecycle.fulfil_items(order_id, [FulfilLine(item_id=first, quantity=1)], BY)
    assert held.value.slug == "order_on_hold"
    draft = shop.order()
    item = shop.item(draft)
    with pytest.raises(LifecycleError) as open_order:
        shop.lifecycle.fulfil_items(draft.id, [FulfilLine(item_id=item.id, quantity=1)], BY)
    assert open_order.value.slug == "order_not_accepted"
