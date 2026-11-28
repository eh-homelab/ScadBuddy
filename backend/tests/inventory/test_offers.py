"""Supplier offers and their price points (#7067, spec 6.5): one item at one supplier, a
SKU unique per supplier and item (D13), pack prices in any ISO currency (R-7067-11)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from scadbuddy.core.problems import ApiError
from scadbuddy.inventory.models import ItemInput, Offer, OfferInput, OfferPatch, PriceInput
from scadbuddy.inventory.offers import (
    CREATE_ROUTE,
    OfferExists,
    OfferNotFound,
    OfferStore,
    SupplierNotFound,
    offer_key,
)
from scadbuddy.inventory.stock import FilamentItemRefused, ItemNotFound
from scadbuddy.orders.models import Actor
from tests.conftest import PgPool
from tests.support.inventory_rows import ItemRows

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


def _item(items: ItemRows, name: str) -> UUID:
    return items.create(ItemInput(kind_id="hardware", name=name), BY).id


def test_a_bundle_sku_covers_several_items(
    offers: OfferStore, items: ItemRows, pg_pool: PgPool
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
    offers: OfferStore, items: ItemRows, pg_pool: PgPool
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
    offers: OfferStore, items: ItemRows, pg_pool: PgPool
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
    offers: OfferStore, items: ItemRows, pg_pool: PgPool
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
    offers: OfferStore, items: ItemRows, pg_pool: PgPool
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
    offers: OfferStore, items: ItemRows, pg_pool: PgPool
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


def _offer(offers: OfferStore, shop: UUID, item: UUID, sku: str = "NUT-3") -> Offer:
    return offers.create(OfferInput(supplier_id=shop, item_id=item, sku=sku), BY)


def test_restore_brings_an_offer_back(offers: OfferStore, items: ItemRows, pg_pool: PgPool) -> None:
    offer = _offer(offers, _supplier(pg_pool), _item(items, "M3 nut"))
    offers.archive(offer.id)

    assert offers.restore(offer.id).archived_at is None
    with pytest.raises(OfferNotFound):
        offers.restore(uuid4())


def test_restore_refuses_an_archived_supplier_or_item(
    offers: OfferStore, items: ItemRows, pg_pool: PgPool
) -> None:
    shop = _supplier(pg_pool)
    nut = _item(items, "M3 nut")
    offer = _offer(offers, shop, nut)
    offers.archive(offer.id)
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_suppliers SET archived_at = now() WHERE id = %s", (shop,))
    with pytest.raises(SupplierNotFound):
        offers.restore(offer.id)

    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_suppliers SET archived_at = NULL WHERE id = %s", (shop,))
    items.archive(nut)
    with pytest.raises(ItemNotFound):
        offers.restore(offer.id)


def test_restore_refuses_a_filament_item(offers: OfferStore, pg_pool: PgPool) -> None:
    shop = _supplier(pg_pool)
    with pg_pool.connection() as conn:
        item = conn.execute(
            "INSERT INTO items (kind_id, name) VALUES ('filament', 'PLA Red') RETURNING id"
        ).fetchone()
        assert item is not None
        conn.execute(
            "INSERT INTO supplier_offers (supplier_id, item_id, sku, archived_at)"
            " VALUES (%s, %s, 'PLA-R', now()) RETURNING id",
            (shop, item["id"]),
        )
        offer = conn.execute("SELECT id FROM supplier_offers").fetchone()
    assert offer is not None

    with pytest.raises(FilamentItemRefused):
        offers.restore(offer["id"])


def test_an_archived_item_takes_no_offer(
    offers: OfferStore, items: ItemRows, pg_pool: PgPool
) -> None:
    nut = _item(items, "M3 nut")
    items.archive(nut)

    with pytest.raises(ItemNotFound):
        _offer(offers, _supplier(pg_pool), nut)


def test_a_null_clears_the_note_and_keeps_the_pack_size(
    offers: OfferStore, items: ItemRows, pg_pool: PgPool
) -> None:
    offer = offers.create(
        OfferInput(
            supplier_id=_supplier(pg_pool),
            item_id=_item(items, "M3 nut"),
            sku=" NUT-3 ",
            pack_size=Decimal(50),
            pack_uom=" box ",
            note="ask for the zinc ones",
        ),
        BY,
    )
    assert (offer.sku, offer.pack_uom) == ("NUT-3", "box")

    changed = offers.update(offer.id, OfferPatch(note=None, pack_size=None), BY)
    assert (changed.note, changed.pack_size) == (None, Decimal(50))


def test_inputs_refuse_bad_links_skus_and_currencies() -> None:
    for bad in ("ftp://x", "http://", "https://a/" + "b" * 2000):
        with pytest.raises(ValidationError):
            OfferInput(supplier_id=uuid4(), item_id=uuid4(), sku="A", url=bad)
    with pytest.raises(ValidationError):
        OfferInput(supplier_id=uuid4(), item_id=uuid4(), sku="  ")
    with pytest.raises(ValidationError):
        OfferPatch(sku="  ")
    with pytest.raises(ValidationError):
        PriceInput(price=Decimal(1), currency="usd")


def test_the_latest_price_is_the_later_write_on_one_day(
    offers: OfferStore, items: ItemRows, pg_pool: PgPool
) -> None:
    offer = _offer(offers, _supplier(pg_pool), _item(items, "M3 nut"))
    day = date(2026, 11, 20)

    offers.add_price(offer.id, PriceInput(price=Decimal(2), on_date=day))
    offers.add_price(offer.id, PriceInput(price=Decimal(3), on_date=day))
    got = offers.get(offer.id)
    assert got is not None and got.latest_price is not None
    assert got.latest_price.price == Decimal(3)


def test_archived_offers_are_listed_only_when_asked(
    offers: OfferStore, items: ItemRows, pg_pool: PgPool
) -> None:
    shop = _supplier(pg_pool)
    offer = _offer(offers, shop, _item(items, "M3 nut"))
    offers.archive(offer.id)

    assert offers.for_supplier(shop) == []
    assert [o.id for o in offers.for_supplier(shop, include_archived=True)] == [offer.id]
    assert offers.by_sku(shop, "NUT-3") == []


# -- Idempotency-Key (#8437) -------------------------------------------------------------


def _ledger_routes(pg_pool: PgPool, key: str) -> list[str]:
    with pg_pool.connection() as conn:
        rows = conn.execute(
            "SELECT route FROM crud_requests WHERE caller = 'inventory' AND key = %s", (key,)
        ).fetchall()
    return [r["route"] for r in rows]


def test_a_keyed_create_runs_once_and_a_retry_gets_its_answer(
    offers: OfferStore, items: ItemRows, pg_pool: PgPool
) -> None:
    shop, nut = _supplier(pg_pool), _item(items, "M3 nut")
    key = offer_key(BY, "call-1:add")
    body = OfferInput(supplier_id=shop, item_id=nut, sku="NUT-3")

    first = offers.create(body, BY, key)
    # Without the key, the same SKU again is `OfferExists`; with it, the first answer.
    assert offers.create(body, BY, key) == first
    assert [o.id for o in offers.for_item(nut)] == [first.id]
    assert _ledger_routes(pg_pool, key) == [CREATE_ROUTE]
    with pytest.raises(OfferExists):
        offers.create(body, BY)


def test_a_keyed_price_is_added_once(offers: OfferStore, items: ItemRows, pg_pool: PgPool) -> None:
    offer = _offer(offers, _supplier(pg_pool), _item(items, "M3 nut"))
    key = offer_key(BY, "call-2:price")
    body = PriceInput(price=Decimal("4.5"), on_date=date(2026, 11, 1))

    first = offers.add_price(offer.id, body, key)
    assert offers.add_price(offer.id, body, key) == first
    assert [p.id for p in offers.prices(offer.id)] == [first.id]
    # With no key, each request is a write of its own.
    offers.add_price(offer.id, body)
    assert len(offers.prices(offer.id)) == 2


def test_a_keyed_edit_or_archive_answers_its_first_result(
    offers: OfferStore, items: ItemRows, pg_pool: PgPool
) -> None:
    offer = _offer(offers, _supplier(pg_pool), _item(items, "M3 nut"))
    edit, archive = offer_key(BY, "call-3:edit"), offer_key(BY, "call-3:archive")

    changed = offers.update(offer.id, OfferPatch(note="first"), BY, edit)
    offers.update(offer.id, OfferPatch(note="later"), BY)
    # The retry gets the first answer and does not write "first" over "later".
    assert offers.update(offer.id, OfferPatch(note="first"), BY, edit) == changed
    got = offers.get(offer.id)
    assert got is not None and got.note == "later"

    archived = offers.archive(offer.id, archive)
    assert archived.archived_at is not None
    assert offers.archive(offer.id, archive) == archived


def test_a_key_with_another_body_or_offer_is_refused(
    offers: OfferStore, items: ItemRows, pg_pool: PgPool
) -> None:
    shop = _supplier(pg_pool)
    offer = _offer(offers, shop, _item(items, "M3 nut"))
    other = _offer(offers, shop, _item(items, "M2 nut"), sku="NUT-2")
    key = offer_key(BY, "call-4:edit")
    offers.update(offer.id, OfferPatch(note="a"), BY, key)

    retries: list[Callable[[], Offer]] = [
        lambda: offers.update(offer.id, OfferPatch(note="b"), BY, key),
        lambda: offers.update(other.id, OfferPatch(note="a"), BY, key),
        lambda: offers.archive(offer.id, key),
    ]
    for retry in retries:
        with pytest.raises(ApiError) as refused:
            retry()
        assert refused.value.status == 422
    got = offers.get(other.id)
    assert got is not None and got.note is None


def test_a_refused_keyed_write_keeps_no_answer(
    offers: OfferStore, items: ItemRows, pg_pool: PgPool
) -> None:
    shop, nut = _supplier(pg_pool), _item(items, "M3 nut")
    _offer(offers, shop, nut)
    key = offer_key(BY, "call-5:add")
    body = OfferInput(supplier_id=shop, item_id=nut, sku="nut-3")

    with pytest.raises(OfferExists):
        offers.create(body, BY, key)
    assert _ledger_routes(pg_pool, key) == []
    lost = offer_key(BY, "call-5:archive")
    with pytest.raises(OfferNotFound):
        offers.archive(uuid4(), lost)
    assert _ledger_routes(pg_pool, lost) == []


def test_the_keys_of_two_senders_never_meet() -> None:
    agent = Actor(kind="agent", principal="p1")
    assert offer_key(BY, "k") != offer_key(agent, "k")
    assert offer_key(agent, "k") != offer_key(Actor(kind="agent", principal="p2"), "k")
