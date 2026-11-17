"""Suppliers, supplier products and price points (#6266)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest

from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.models import Material, SpoolTypeCreate, SpoolTypePatch
from scadbuddy.filament.purchasing_models import (
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
    _types(pg_pool).update(product.type_id, SpoolTypePatch(label_weight_g=500))
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
