"""The purchasing tables (#6266): the checks and unique indexes the stores rely on."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from psycopg import Connection, errors
from psycopg.rows import DictRow
from pydantic import ValidationError

from scadbuddy.filament.purchasing_models import (
    InvoiceCreate,
    PricePointCreate,
    ShoppingItemCreate,
    SupplierCreate,
)
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

Conn = Connection[DictRow]

_SUPPLIER = "INSERT INTO filament_suppliers (name) VALUES (%s) RETURNING id"
_INVOICE = (
    "INSERT INTO filament_invoices (supplier_id, number, invoice_date, currency, subtotal, total) "
    "VALUES (%s, %s, '2026-11-01', 'USD', 10, 10) RETURNING id"
)


def _id(conn: Conn, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return next(iter(row.values()))


def _supplier(conn: Conn, name: str = "Acme") -> Any:
    row = conn.execute(_SUPPLIER, (name,)).fetchone()
    assert row is not None
    return row["id"]


def _type(conn: Conn) -> Any:
    row = conn.execute(
        "INSERT INTO filament_spool_types (code, material_id, rgba, label_weight_g) "
        "SELECT 'PT1', id, '#112233FF', 1000 FROM filament_materials WHERE name = 'PLA' "
        "RETURNING id"
    ).fetchone()
    assert row is not None
    return row["id"]


def _product(conn: Conn, supplier: Any, type_id: Any, sku: str = "S1") -> Any:
    row = conn.execute(
        "INSERT INTO filament_supplier_products (supplier_id, type_id, sku) "
        "VALUES (%s, %s, %s) RETURNING id",
        (supplier, type_id, sku),
    ).fetchone()
    assert row is not None
    return row["id"]


def _line(conn: Conn, invoice: Any, quantity: int = 1, ids: list[Any] | None = None) -> Any:
    row = conn.execute(
        "INSERT INTO filament_invoice_lines (invoice_id, position, description, quantity, "
        "unit_price, line_subtotal, line_tax, line_total, allocated_cost, update_spool_ids) "
        "VALUES (%s, 1, 'x', %s, 1, 1, 0, 1, 1, %s) RETURNING id",
        (invoice, quantity, ids or []),
    ).fetchone()
    assert row is not None
    return row["id"]


def test_a_supplier_name_is_unique_while_unarchived(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        conn.execute("TRUNCATE filament_suppliers CASCADE")
        conn.execute(_SUPPLIER, ("acme",))
        with pytest.raises(errors.UniqueViolation), conn.transaction():
            conn.execute(_SUPPLIER, ("ACME",))
        conn.execute("UPDATE filament_suppliers SET archived_at = now()")
        conn.execute(_SUPPLIER, ("ACME",))


def test_a_product_sku_is_unique_per_supplier_case_insensitively(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        conn.execute("TRUNCATE filament_suppliers CASCADE")
        a, b, t = _supplier(conn, "A"), _supplier(conn, "B"), _type(conn)
        _product(conn, a, t, "abc")
        with pytest.raises(errors.UniqueViolation), conn.transaction():
            _product(conn, a, t, "ABC")
        _product(conn, b, t, "ABC")


def test_a_price_point_of_source_invoice_names_a_line(pg_pool: PgPool) -> None:
    sql = (
        "INSERT INTO filament_price_points (product_id, price, currency, on_date, source, "
        "invoice_line_id) VALUES (%s, 5, 'USD', '2026-11-01', %s, %s)"
    )
    with pg_pool.connection() as conn:
        conn.execute("TRUNCATE filament_suppliers CASCADE")
        s = _supplier(conn)
        product = _product(conn, s, _type(conn))
        line = _line(conn, _id(conn, _INVOICE, (s, "N1")))
        with pytest.raises(errors.CheckViolation), conn.transaction():
            conn.execute(sql, (product, "invoice", None))
        with pytest.raises(errors.CheckViolation), conn.transaction():
            conn.execute(sql, (product, "manual", line))
        conn.execute(sql, (product, "invoice", line))
        conn.execute(sql, (product, "manual", None))


def test_an_invoice_number_is_unique_per_supplier(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        conn.execute("TRUNCATE filament_suppliers CASCADE")
        a, b = _supplier(conn, "A"), _supplier(conn, "B")
        conn.execute(_INVOICE, (a, "inv-1"))
        with pytest.raises(errors.UniqueViolation), conn.transaction():
            conn.execute(_INVOICE, (a, "INV-1"))
        conn.execute(_INVOICE, (b, "INV-1"))
        conn.execute(_INVOICE, (a, None))
        conn.execute(_INVOICE, (a, None))


def test_a_line_cannot_fill_more_spools_than_it_has(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        conn.execute("TRUNCATE filament_suppliers CASCADE")
        s = _supplier(conn)
        invoice = _id(conn, _INVOICE, (s, "N1"))
        ids = [_id(conn, "SELECT gen_random_uuid()") for _ in range(2)]
        with pytest.raises(errors.CheckViolation), conn.transaction():
            _line(conn, invoice, 1, ids)
        _line(conn, invoice, 2, ids)


def test_a_file_is_all_or_nothing(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        conn.execute("TRUNCATE filament_suppliers CASCADE")
        s = _supplier(conn)
        with pytest.raises(errors.CheckViolation), conn.transaction():
            conn.execute(
                "INSERT INTO filament_invoices (supplier_id, invoice_date, currency, "
                "subtotal, total, file_key) VALUES (%s, '2026-11-01', 'USD', 1, 1, 'k')",
                (s,),
            )


def test_the_models_refuse_a_script_url() -> None:
    with pytest.raises(ValidationError):
        SupplierCreate(name="A", website="javascript:alert(1)")
    assert SupplierCreate(name="A", website="https://acme.example/x").website == (
        "https://acme.example/x"
    )


@pytest.mark.parametrize("url", ["http://a b", "http://a\nb", "http://a\tb", "ftp://a.example"])
def test_the_models_refuse_a_url_with_whitespace_or_another_scheme(url: str) -> None:
    with pytest.raises(ValidationError):
        SupplierCreate(name="A", website=url)


def test_a_shopping_item_needs_a_material_or_a_type() -> None:
    with pytest.raises(ValidationError):
        ShoppingItemCreate()
    assert ShoppingItemCreate(material="PLA").material == "PLA"
    assert ShoppingItemCreate(type_id=uuid4()).material is None


def test_money_refuses_a_fifth_decimal_and_too_many_digits() -> None:
    with pytest.raises(ValidationError):
        PricePointCreate(price=Decimal("1.00001"))
    with pytest.raises(ValidationError):
        PricePointCreate(price=Decimal("100000000.0000"))
    assert PricePointCreate(price=Decimal("1.2345")).price == Decimal("1.2345")
    with pytest.raises(ValidationError):
        InvoiceCreate(
            supplier_id=uuid4(),
            invoice_date=date(2026, 11, 1),
            currency="USD",
            subtotal=Decimal("1"),
            total=Decimal("1.00001"),
        )
