"""The purchasing stores over one pool, for the tests of invoices (#6266)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.invoices import InvoiceStore
from scadbuddy.filament.models import Material, SpoolTypeCreate
from scadbuddy.filament.purchasing_models import (
    InvoiceCreate,
    InvoiceDetail,
    InvoiceLineIn,
    SupplierCreate,
)
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.suppliers import SuppliersStore
from scadbuddy.filament.types import SpoolTypeStore
from tests.conftest import PgPool

D = Decimal


class Shop:
    """A shop's stores over one pool, with helpers to draft and count invoices (#6266)."""

    def __init__(self, pool: PgPool, currency: str = "USD") -> None:
        self.pool = pool
        self.currency = currency
        self.spools = FilamentSpoolStore(pool)
        self.types = SpoolTypeStore(pool)
        self.suppliers = SuppliersStore(pool, lambda: self.currency)
        self.invoices = InvoiceStore(pool, lambda: self.currency, self.spools, self.types)
        self.supplier_id = self.suppliers.create(SupplierCreate(name="Acme")).id

    def material(self) -> UUID:
        rows = CatalogStore(self.pool).list("materials")
        return next(r.id for r in rows if isinstance(r, Material) and r.name == "PLA")

    def type_body(self, code: str = "PLA-WHT", **over: Any) -> SpoolTypeCreate:
        values: dict[str, Any] = {
            "code": code,
            "material_id": self.material(),
            "rgba": "#FFFFFF",
            "label_weight_g": 1000,
        }
        return SpoolTypeCreate.model_validate(values | over)

    def type_id(self, code: str = "PLA-WHT") -> UUID:
        return self.types.create(self.type_body(code)).id

    def draft(
        self,
        lines: list[InvoiceLineIn],
        *,
        total: str = "42.80",
        subtotal: str = "40",
        tax: str = "2.80",
        shipping: str = "0",
        currency: str | None = None,
        number: str | None = None,
    ) -> InvoiceDetail:
        made, new = self.invoices.create(
            InvoiceCreate(
                supplier_id=self.supplier_id,
                number=number,
                invoice_date=date(2026, 11, 1),
                currency=currency or self.currency,
                subtotal=D(subtotal),
                shipping=D(shipping),
                tax=D(tax),
                total=D(total),
                lines=lines,
            ),
            actor="test",
        )
        assert new
        return made

    def one(self, type_id: UUID | None, **over: Any) -> InvoiceDetail:
        """One line of two spools, 40 plus 2.80 tax."""
        line = InvoiceLineIn.model_validate(
            {
                "description": "A00-B01",
                "quantity": 2,
                "unit_price": D(20),
                "line_subtotal": D(40),
                "type_id": type_id,
            }
            | over
        )
        return self.draft([line])

    def confirmed(self, type_id: UUID) -> InvoiceDetail:
        done = self.invoices.confirm(self.one(type_id).id, actor="ann")
        assert done is not None
        return done[0]

    def count(self, table: str) -> int:
        with self.pool.connection() as conn:
            row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
        assert row is not None
        return int(row["n"])

    def spool_rows(self, invoice_id: UUID) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT s.id, s.cost_per_kg, s.grams_left, s.invoice_line_id, s.type_id"
                " FROM filament_spools s JOIN filament_invoice_lines l"
                " ON l.id = s.invoice_line_id WHERE l.invoice_id = %s ORDER BY s.number",
                (invoice_id,),
            ).fetchall()

    def status(self, invoice_id: UUID) -> str:
        found = self.invoices.get(invoice_id)
        assert found is not None
        return found.status

    def nothing_was_made(self, invoice_id: UUID, spools: int = 0) -> None:
        assert self.status(invoice_id) == "draft"
        assert self.count("filament_spools") == spools
        assert self.count("filament_price_points") == 0
