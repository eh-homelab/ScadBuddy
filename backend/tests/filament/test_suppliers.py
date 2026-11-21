"""Suppliers, supplier products and price points (#6266)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest

from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.ledger import record
from scadbuddy.filament.models import Material, SpoolTypeCreate, SpoolTypePatch
from scadbuddy.filament.purchasing_models import (
    InvoiceLineIn,
    PricePointCreate,
    ProductCreate,
    ProductPatch,
    SupplierCreate,
    SupplierPatch,
)
from scadbuddy.filament.suppliers import (
    PriceFromInvoiceError,
    ProductExistsError,
    ProductRefError,
    SupplierExistsError,
    SuppliersStore,
)
from scadbuddy.filament.types import SpoolTypeStore
from tests.conftest import PgPool
from tests.support.purchasing import Shop

pytestmark = pytest.mark.requires_postgres


def _types(pool: PgPool) -> SpoolTypeStore:
    return SpoolTypeStore(pool)


def _type(pool: PgPool, code: str = "PLA-WHT", label_weight_g: int = 1000) -> UUID:
    material = next(
        r.id
        for r in CatalogStore(pool).list("materials")
        if isinstance(r, Material) and r.name == "PLA"
    )
    made = _types(pool).create(
        SpoolTypeCreate.model_validate(
            {
                "code": code,
                "material_id": material,
                "rgba": "#FFFFFF",
                "label_weight_g": label_weight_g,
            }
        )
    )
    return made.id


def _store(pool: PgPool) -> SuppliersStore:
    return SuppliersStore(pool, lambda: "USD")


def _product(store: SuppliersStore, pool: PgPool, pack_size: int = 1, sku: str = "SKU-1") -> UUID:
    supplier = store.create(SupplierCreate(name=f"Acme {sku}"))
    made = store.add_product(
        supplier.id, ProductCreate(type_id=_type(pool, f"T-{sku}"), sku=sku, pack_size=pack_size)
    )
    assert made is not None
    return made.id


def test_a_second_supplier_with_the_name_is_refused(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    first = store.create(SupplierCreate(name="Acme"))
    with pytest.raises(SupplierExistsError):
        store.create(SupplierCreate(name="acme"))
    archived = store.update(first.id, SupplierPatch(archived=True))
    assert archived is not None
    assert archived.archived_at is not None
    assert store.create(SupplierCreate(name="ACME")).name == "ACME"
    assert [s.id for s in store.list()] != [first.id]
    assert len(store.list(include_archived=True)) == 2


def test_a_product_names_a_type_and_a_unique_sku(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    supplier = store.create(SupplierCreate(name="Acme"))
    type_id = _type(pg_pool)
    product = store.add_product(supplier.id, ProductCreate(type_id=type_id, sku="a-1"))
    assert product is not None
    assert product.newest_price is None
    with pytest.raises(ProductExistsError):
        store.add_product(supplier.id, ProductCreate(type_id=type_id, sku="A-1"))
    with pytest.raises(ProductRefError):
        store.add_product(supplier.id, ProductCreate(type_id=UUID(int=9), sku="b"))
    assert store.add_product(UUID(int=9), ProductCreate(type_id=type_id, sku="c")) is None
    assert store.get(supplier.id).product_count == 1  # type: ignore[union-attr]
    changed = store.update_product(product.id, ProductPatch(pack_size=2))
    assert changed is not None
    assert changed.pack_size == 2
    assert store.update_product(UUID(int=9), ProductPatch(pack_size=2)) is None


def test_price_per_kg_is_derived_from_the_types_label_weight(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    product_id = _product(store, pg_pool, pack_size=2)
    point = store.add_price(
        product_id, PricePointCreate(price=Decimal("40.00"), on_date=date(2026, 11, 1))
    )
    assert point is not None
    assert point.price_per_kg == Decimal("20.0000")
    assert (point.currency, point.source) == ("USD", "manual")
    product = store.product(product_id)
    assert product is not None
    _types(pg_pool).update(product.type_id, SpoolTypePatch(label_weight_g=500), actor="ann")
    assert store.prices(product_id)[0].price_per_kg == Decimal("40.0000")
    newest = store.product(product_id)
    assert newest is not None
    assert newest.newest_price is not None
    assert newest.newest_price.price_per_kg == Decimal("40.0000")


def test_the_sheet_shows_newest_previous_and_change(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    product_id = _product(store, pg_pool)
    store.add_price(product_id, PricePointCreate(price=Decimal("20.00"), on_date=date(2026, 10, 1)))
    store.add_price(product_id, PricePointCreate(price=Decimal("22.00"), on_date=date(2026, 11, 1)))
    product = store.product(product_id)
    assert product is not None
    sheet = store.price_sheet(product.supplier_id)
    assert sheet is not None
    (row,) = sheet
    assert row.newest is not None
    assert row.previous is not None
    assert (row.newest.price, row.previous.price) == (Decimal("22.0000"), Decimal("20.0000"))
    assert row.change_pct == Decimal("10.0")
    assert (row.type_code, row.type_label) == ("T-SKU-1", "T-SKU-1")
    assert row.product.newest_price == row.newest
    assert [p.on_date for p in store.prices(product_id)] == [date(2026, 11, 1), date(2026, 10, 1)]


def test_the_sheet_has_none_for_one_point(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    product_id = _product(store, pg_pool)
    product = store.product(product_id)
    assert product is not None
    (empty,) = store.price_sheet(product.supplier_id) or []
    assert (empty.newest, empty.previous, empty.change_pct) == (None, None, None)
    store.add_price(product_id, PricePointCreate(price=Decimal("20")))
    (one,) = store.price_sheet(product.supplier_id) or []
    assert one.newest is not None
    assert one.previous is None
    assert one.change_pct is None
    assert store.price_sheet(UUID(int=9)) is None


def test_the_change_is_one_decimal_rounded_half_up(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    product_id = _product(store, pg_pool)
    product = store.product(product_id)
    assert product is not None
    for price, day in (("30", date(2026, 10, 1)), ("31", date(2026, 11, 1))):
        store.add_price(product_id, PricePointCreate(price=Decimal(price), on_date=day))
    (third,) = store.price_sheet(product.supplier_id) or []
    assert third.change_pct == Decimal("3.3")
    other = _product(store, pg_pool, sku="SKU-2")
    for price, day in (("40", date(2026, 10, 1)), ("40.02", date(2026, 11, 1))):
        store.add_price(other, PricePointCreate(price=Decimal(price), on_date=day))
    changed = store.product(other)
    assert changed is not None
    (half,) = store.price_sheet(changed.supplier_id) or []
    assert half.change_pct == Decimal("0.1")


def test_an_invoice_price_is_not_deletable(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    product_id = _product(store, pg_pool)
    product = store.product(product_id)
    assert product is not None
    with pg_pool.connection() as conn:
        invoice = conn.execute(
            "INSERT INTO filament_invoices (supplier_id, invoice_date, currency, subtotal, total)"
            " VALUES (%s, '2026-11-01', 'USD', 10, 10) RETURNING id",
            (product.supplier_id,),
        ).fetchone()
        assert invoice is not None
        line = conn.execute(
            "INSERT INTO filament_invoice_lines (invoice_id, position, description, quantity,"
            " unit_price, line_subtotal, line_tax, line_total, allocated_cost)"
            " VALUES (%s, 0, 'x', 1, 10, 10, 0, 10, 10) RETURNING id",
            (invoice["id"],),
        ).fetchone()
        assert line is not None
        point = conn.execute(
            "INSERT INTO filament_price_points (product_id, price, currency, on_date, source,"
            " invoice_line_id) VALUES (%s, 10, 'USD', '2026-11-01', 'invoice', %s) RETURNING id",
            (product_id, line["id"]),
        ).fetchone()
        assert point is not None
    with pytest.raises(PriceFromInvoiceError):
        store.delete_price(point["id"])
    manual = store.add_price(product_id, PricePointCreate(price=Decimal("5")))
    assert manual is not None
    assert store.delete_price(manual.id) is True
    assert store.delete_price(manual.id) is False


def test_a_types_price_history_spans_its_suppliers(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    type_id = _type(pg_pool)
    a = store.create(SupplierCreate(name="A"))
    b = store.create(SupplierCreate(name="B"))
    pa = store.add_product(a.id, ProductCreate(type_id=type_id, sku="a"))
    pb = store.add_product(b.id, ProductCreate(type_id=type_id, sku="b"))
    assert pa is not None
    assert pb is not None
    store.add_price(pa.id, PricePointCreate(price=Decimal("20"), on_date=date(2026, 10, 1)))
    store.add_price(pb.id, PricePointCreate(price=Decimal("18"), on_date=date(2026, 11, 1)))
    history = store.type_prices(type_id)
    assert [(p.supplier_name, p.sku, p.price_per_kg) for p in history] == [
        ("B", "b", Decimal("18.0000")),
        ("A", "a", Decimal("20.0000")),
    ]
    assert store.type_prices(UUID(int=9)) == []


def test_the_type_history_keeps_archived_rows_and_marks_them(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    type_id = _type(pg_pool)
    live = store.create(SupplierCreate(name="Live"))
    gone = store.create(SupplierCreate(name="Gone"))
    kept = store.add_product(live.id, ProductCreate(type_id=type_id, sku="k"))
    old = store.add_product(live.id, ProductCreate(type_id=type_id, sku="o"))
    other = store.add_product(gone.id, ProductCreate(type_id=type_id, sku="g"))
    assert kept is not None
    assert old is not None
    assert other is not None
    for product in (kept, old, other):
        store.add_price(product.id, PricePointCreate(price=Decimal("20")))
    store.update_product(old.id, ProductPatch(archived=True))
    store.update(gone.id, SupplierPatch(archived=True))
    flags = {p.sku: (p.product_archived, p.supplier_archived) for p in store.type_prices(type_id)}
    assert flags == {"k": (False, False), "o": (True, False), "g": (False, True)}


def test_products_of_a_missing_supplier_is_none(pg_pool: PgPool) -> None:
    assert _store(pg_pool).products(UUID(int=9)) is None


def test_a_restore_onto_a_taken_name_or_sku_is_refused(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    type_id = _type(pg_pool)
    first = store.create(SupplierCreate(name="Acme"))
    store.update(first.id, SupplierPatch(archived=True))
    store.create(SupplierCreate(name="Acme"))
    with pytest.raises(SupplierExistsError):
        store.update(first.id, SupplierPatch(archived=False))
    supplier = store.create(SupplierCreate(name="Other"))
    one = store.add_product(supplier.id, ProductCreate(type_id=type_id, sku="X"))
    assert one is not None
    store.update_product(one.id, ProductPatch(archived=True))
    assert store.add_product(supplier.id, ProductCreate(type_id=type_id, sku="x")) is not None
    with pytest.raises(ProductExistsError):
        store.update_product(one.id, ProductPatch(archived=False))


def _confirmed(shop: Shop, type_id: UUID, number: str) -> list[UUID]:
    line = InvoiceLineIn(
        description="A00",
        quantity=2,
        unit_price=Decimal(20),
        line_subtotal=Decimal(40),
        type_id=type_id,
    )
    done = shop.invoices.confirm(shop.draft([line], number=number).id, actor="ann")
    assert done is not None
    return done[0].lines[0].spool_ids


def test_stats_count_spend_and_stock(pg_pool: PgPool) -> None:
    shop = Shop(pg_pool)
    type_id = shop.type_id()
    first = _confirmed(shop, type_id, "R-1")
    second = _confirmed(shop, type_id, "R-2")
    shop.draft(
        [
            InvoiceLineIn(
                description="D",
                quantity=1,
                unit_price=Decimal(40),
                line_subtotal=Decimal(40),
                type_id=type_id,
            )
        ],
        number="R-DRAFT",
    )
    with pg_pool.connection() as conn:
        record(
            conn, first[0], "print_ended", source="print_actual", delta_g=-50,
            ref_kind="print_run", ref_id="a",
        )  # fmt: skip
        record(
            conn, first[0], "print_started", source="print_estimate", delta_g=-30,
            ref_kind="print_run", ref_id="b",
        )  # fmt: skip
        record(
            conn, first[0], "print_ended", source="print_actual", delta_g=None,
            ref_kind="print_run", ref_id="b",
        )  # fmt: skip
        record(
            conn, first[1], "print_started", source="print_estimate", delta_g=-7,
            ref_kind="print_run", ref_id="c",
        )  # fmt: skip
        record(conn, second[0], "weighed", source="scale", absolute_g=800)
        record(conn, second[0], "edited", source="person", absolute_g=790)
        record(conn, second[1], "ams_reading", source="ams_remain", absolute_g=600)
    shop.spools.archive(second[1], True, actor="ann")
    [row] = shop.suppliers.stats()
    assert row.supplier_name == "Acme"
    assert not row.archived
    assert row.spool_count == 4
    assert row.consumed_g == 50.0  # c's estimate is still open: not a plate yet
    assert row.spend == Decimal("85.6000")
    assert row.remaining_g == 950 + 993 + 790  # the archived spool is not counted


def test_a_supplier_with_only_drafts_is_left_out(pg_pool: PgPool) -> None:
    shop = Shop(pg_pool)
    shop.one(shop.type_id())
    assert shop.suppliers.stats() == []


def test_stats_list_an_archived_supplier_marked(pg_pool: PgPool) -> None:
    shop = Shop(pg_pool)
    _confirmed(shop, shop.type_id(), "R-1")
    shop.suppliers.update(shop.supplier_id, SupplierPatch(archived=True))
    [row] = shop.suppliers.stats()
    assert row.archived
    assert row.spool_count == 2


def test_stats_consumed_counts_a_paired_plate_once(pg_pool: PgPool) -> None:
    shop = Shop(pg_pool)
    spool = _confirmed(shop, shop.type_id(), "R-1")[0]
    with pg_pool.connection() as conn:
        record(
            conn, spool, "print_ended", source="print_actual", delta_g=-30,
            ref_kind="print_run", ref_id="run/a/1", data={"job": "a", "basis": "sliced"},
        )  # fmt: skip
        record(
            conn, spool, "print_ended", source="print_actual", delta_g=-28,
            ref_kind="bb_usage", ref_id="77",
            data={"job": "a", "basis": "bb_usage", "bb_status": "completed"},
        )  # fmt: skip
        record(
            conn, spool, "print_started", source="print_estimate", delta_g=-9,
            ref_kind="print_run", ref_id="run/b/1", data={"job": "b"},
        )  # fmt: skip
    [row] = shop.suppliers.stats()
    assert row.consumed_g == 28.0
