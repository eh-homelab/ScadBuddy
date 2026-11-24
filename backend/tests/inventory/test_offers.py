"""Supplier offers and their price points (#7067, spec 6.5): one item at one supplier, a
SKU unique per supplier and item (D13), pack prices in any ISO currency (R-7067-11)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from scadbuddy.inventory.items import FilamentItemRefused, ItemNotFound, ItemStore
from scadbuddy.inventory.models import ItemInput, OfferInput, OfferPatch, PriceInput
from scadbuddy.inventory.offers import (
    OfferExists,
    OfferNotFound,
    OfferStore,
    SupplierNotFound,
)
from scadbuddy.orders.models import Actor
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

BY = Actor(kind="browser")


@pytest.fixture
def offers(pg_pool: PgPool) -> OfferStore:
    return OfferStore(pg_pool, lambda: "USD")


def _supplier(pg_pool: PgPool, name: str = "Bolt Depot", archived: bool = False) -> UUID:
    with pg_pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO filament_suppliers (name, archived_at) VALUES (%s, %s) RETURNING id",
            (name, "2026-11-01" if archived else None),
        ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def _item(items: ItemStore, name: str) -> UUID:
    return items.create(ItemInput(kind_id="hardware", name=name), BY).id


def test_a_bundle_sku_covers_several_items(
    offers: OfferStore, items: ItemStore, pg_pool: PgPool
) -> None:
    shop = _supplier(pg_pool)
    red, blue = _item(items, "Bundle red"), _item(items, "Bundle blue")

    first = offers.create(OfferInput(supplier_id=shop, item_id=red, sku="BUNDLE-1"), BY)
    offers.create(OfferInput(supplier_id=shop, item_id=blue, sku="bundle-1"), BY)
    assert (first.supplier_name, first.item_name, first.pack_size) == (
        "Bolt Depot",
        "Bundle red",
        Decimal(1),
    )
    with pytest.raises(OfferExists):
        offers.create(OfferInput(supplier_id=shop, item_id=red, sku="Bundle-1"), BY)

    assert {o.item_id for o in offers.by_sku(shop, "BUNDLE-1")} == {red, blue}
    assert offers.by_sku(shop, "nope") == []


def test_an_archived_offer_frees_its_sku(
    offers: OfferStore, items: ItemStore, pg_pool: PgPool
) -> None:
    shop = _supplier(pg_pool)
    nut = _item(items, "M3 nut")
    old = offers.create(OfferInput(supplier_id=shop, item_id=nut, sku="NUT-3"), BY)

    offers.archive(old.id)
    offers.create(OfferInput(supplier_id=shop, item_id=nut, sku="NUT-3"), BY)
    assert [o.sku for o in offers.for_item(nut)] == ["NUT-3"]
    assert len(offers.for_item(nut, include_archived=True)) == 2
    with pytest.raises(OfferExists):
        offers.restore(old.id)


def test_an_archived_or_unknown_supplier_is_refused(
    offers: OfferStore, items: ItemStore, pg_pool: PgPool
) -> None:
    nut = _item(items, "M3 nut")

    with pytest.raises(SupplierNotFound):
        offers.create(
            OfferInput(supplier_id=_supplier(pg_pool, archived=True), item_id=nut, sku="A"), BY
        )
    with pytest.raises(SupplierNotFound):
        offers.create(OfferInput(supplier_id=uuid4(), item_id=nut, sku="A"), BY)
    with pytest.raises(ItemNotFound):
        offers.create(OfferInput(supplier_id=_supplier(pg_pool), item_id=uuid4(), sku="A"), BY)


def test_a_filament_item_takes_no_offer(
    offers: OfferStore, items: ItemStore, pg_pool: PgPool
) -> None:
    # No filament item can be made through the store (Ruling 9); one from phase 3 is a row.
    with pg_pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO items (kind_id, name) VALUES ('filament', 'PLA Red') RETURNING id"
        ).fetchone()
    assert row is not None

    with pytest.raises(FilamentItemRefused):
        offers.create(
            OfferInput(supplier_id=_supplier(pg_pool), item_id=row["id"], sku="PLA-R"), BY
        )


def test_a_price_is_manual_in_any_iso_currency(
    offers: OfferStore, items: ItemStore, pg_pool: PgPool
) -> None:
    offer = offers.create(
        OfferInput(supplier_id=_supplier(pg_pool), item_id=_item(items, "M3 nut"), sku="N"), BY
    )

    usd = offers.add_price(offer.id, PriceInput(price=Decimal("4.5"), on_date=date(2026, 11, 1)))
    assert (usd.source, usd.currency, usd.invoice_line_id) == ("manual", "USD", None)
    eur = offers.add_price(
        offer.id, PriceInput(price=Decimal("3.99"), currency="EUR", on_date=date(2026, 11, 20))
    )
    assert eur.currency == "EUR"

    (got,) = offers.for_item(offer.item_id)
    assert got.latest_price is not None
    assert (got.latest_price.price, got.latest_price.currency) == (Decimal("3.99"), "EUR")
    assert [p.currency for p in offers.prices(offer.id)] == ["EUR", "USD"]
    with pytest.raises(OfferNotFound):
        offers.add_price(uuid4(), PriceInput(price=Decimal(1)))


def test_update_keeps_the_sku_rule_and_blanks_the_url(
    offers: OfferStore, items: ItemStore, pg_pool: PgPool
) -> None:
    shop = _supplier(pg_pool)
    nut = _item(items, "M3 nut")
    offers.create(OfferInput(supplier_id=shop, item_id=nut, sku="NUT-3"), BY)
    other = offers.create(
        OfferInput(supplier_id=shop, item_id=nut, sku="NUT-3B", url="https://example.com/n"), BY
    )

    with pytest.raises(OfferExists):
        offers.update(other.id, OfferPatch(sku="nut-3"), BY)
    changed = offers.update(other.id, OfferPatch(url="  ", pack_size=Decimal(100)), BY)
    assert (changed.url, changed.pack_size) == (None, Decimal(100))
    assert (
        offers.create(
            OfferInput(supplier_id=shop, item_id=_item(items, "M2 nut"), sku="X", url=""), BY
        ).url
        is None
    )
    assert [o.sku for o in offers.for_supplier(shop)] == ["NUT-3", "NUT-3B", "X"]
    with pytest.raises(OfferNotFound):
        offers.update(uuid4(), OfferPatch(sku="Y"), BY)
