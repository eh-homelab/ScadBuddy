"""Confirming an invoice (#6266 Task 6): spools, costs, products and price points made in
one transaction, idempotent by state, and refused whole."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.invoices import (
    InvoiceCurrencyError,
    InvoiceLineError,
    InvoiceNoLinesError,
    InvoiceNotReconciledError,
    InvoiceStore,
)
from scadbuddy.filament.models import Material, SpoolCreate, SpoolPatch, SpoolTypeCreate
from scadbuddy.filament.purchasing_models import (
    InvoiceCreate,
    InvoiceDetail,
    InvoiceLineIn,
    InvoicePatch,
    ProductCreate,
    SupplierCreate,
)
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.suppliers import SuppliersStore
from scadbuddy.filament.types import SpoolTypeStore
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

D = Decimal


class _Shop:
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


@pytest.fixture
def shop(pg_pool: PgPool) -> _Shop:
    return _Shop(pg_pool)


def _confirm(shop: _Shop, invoice: InvoiceDetail) -> tuple[InvoiceDetail, bool]:
    done = shop.invoices.confirm(invoice.id, actor="ann")
    assert done is not None
    return done


def test_confirm_makes_spools_with_cost_and_link(shop: _Shop) -> None:
    type_id = shop.type_id()
    invoice = shop.one(type_id)
    done, now = _confirm(shop, invoice)
    assert now
    assert done.status == "confirmed"
    assert done.confirmed_at is not None
    spools = shop.spool_rows(invoice.id)
    assert len(spools) == 2
    line = done.lines[0]
    for spool in spools:
        assert spool["cost_per_kg"] == D("21.4000")
        assert spool["grams_left"] == 1000
        assert spool["invoice_line_id"] == line.id
        assert spool["type_id"] == type_id
        [created] = [e for e in shop.spools.events(spool["id"]) if e.kind == "created"]
        assert created.actor == f"invoice:{invoice.id}"
    assert sorted(line.spool_ids) == sorted(s["id"] for s in spools)
    [point] = shop.suppliers.type_prices(type_id)
    assert point.price == D("21.4000")
    assert (point.currency, point.source, point.on_date) == ("USD", "invoice", date(2026, 11, 1))


def test_confirm_twice_creates_nothing_more(shop: _Shop) -> None:
    invoice = shop.one(shop.type_id())
    _confirm(shop, invoice)
    spools, points = shop.count("filament_spools"), shop.count("filament_price_points")
    again, now = _confirm(shop, invoice)
    assert not now
    assert again.status == "confirmed"
    assert (shop.count("filament_spools"), shop.count("filament_price_points")) == (spools, points)


def test_two_confirms_at_once_make_one_set(shop: _Shop) -> None:
    invoice = shop.one(shop.type_id(), quantity=3, unit_price=D("13.3333"))

    def run(_: int) -> bool:
        done = shop.invoices.confirm(invoice.id, actor="ann")
        assert done is not None
        return done[1]

    with ThreadPoolExecutor(max_workers=2) as executor:
        made = list(executor.map(run, range(2)))
    assert sorted(made) == [False, True]
    assert shop.count("filament_spools") == 3
    assert shop.count("filament_price_points") == 1


def test_a_line_fills_an_existing_costless_spool(shop: _Shop) -> None:
    type_id = shop.type_id()
    typeless = shop.spools.create(SpoolCreate(material_id=shop.material()), actor="ann").id
    invoice = shop.one(
        type_id,
        quantity=3,
        unit_price=D("13.3333"),
        line_subtotal=D("40"),
        update_spool_ids=[typeless],
    )
    done, _ = _confirm(shop, invoice)
    rows = shop.spool_rows(invoice.id)
    assert len(rows) == 3
    assert typeless in {r["id"] for r in rows}
    filled = shop.spools.get(typeless)
    assert filled is not None
    assert filled.type_id == type_id
    assert filled.cost_per_kg == D("14.2667")
    assert len(done.lines[0].spool_ids) == 3


def test_a_created_or_updated_spools_event_carries_the_invoice_line_ref(shop: _Shop) -> None:
    type_id = shop.type_id()
    old = shop.spools.create(SpoolCreate(material_id=shop.material()), actor="ann").id
    invoice = shop.one(type_id, update_spool_ids=[old])
    done, _ = _confirm(shop, invoice)
    line_id = done.lines[0].id
    kinds = {"created", "edited"}
    for spool_id in done.lines[0].spool_ids:
        events = [
            e
            for e in shop.spools.events(spool_id)
            if e.actor == f"invoice:{invoice.id}" and e.kind in kinds
        ]
        assert len(events) == 1
        assert (events[0].ref_kind, events[0].ref_id) == ("invoice_line", str(line_id))
    assert old in done.lines[0].spool_ids


def test_a_duplicate_proposed_type_is_a_409_line_error(shop: _Shop) -> None:
    shop.type_id("PLA-WHT")
    invoice = shop.one(None, proposed_type=shop.type_body("PLA-WHT"))
    with pytest.raises(InvoiceLineError, match="line 1"):
        shop.invoices.confirm(invoice.id, actor="ann")
    assert shop.status(invoice.id) == "draft"


def test_a_missing_catalog_row_in_a_proposed_type_is_a_line_error(shop: _Shop) -> None:
    body = shop.type_body("X").model_copy(update={"brand_id": UUID(int=7)})
    invoice = shop.one(None, proposed_type=body)
    with pytest.raises(InvoiceLineError, match="line 1"):
        shop.invoices.confirm(invoice.id, actor="ann")
    assert shop.status(invoice.id) == "draft"


def test_a_line_with_no_type_is_a_line_error(shop: _Shop) -> None:
    invoice = shop.one(None)
    with pytest.raises(InvoiceLineError, match="line 1"):
        shop.invoices.confirm(invoice.id, actor="ann")
    shop.nothing_was_made(invoice.id)


def test_a_costed_spool_stops_the_whole_confirm(shop: _Shop) -> None:
    type_id = shop.type_id()
    costed = shop.spools.create(SpoolCreate(type_id=type_id, cost_per_kg=D("9")), actor="ann").id
    invoice = shop.draft(
        [
            InvoiceLineIn(
                description="one",
                quantity=1,
                unit_price=D(20),
                line_subtotal=D(20),
                type_id=type_id,
            ),
            InvoiceLineIn(
                description="two",
                quantity=1,
                unit_price=D(20),
                line_subtotal=D(20),
                type_id=type_id,
                update_spool_ids=[costed],
            ),
        ]
    )
    with pytest.raises(InvoiceLineError, match="line 2") as raised:
        shop.invoices.confirm(invoice.id, actor="ann")
    assert str(costed) in str(raised.value)
    shop.nothing_was_made(invoice.id, spools=1)


@pytest.mark.parametrize("kind", ["archived", "invoiced", "other_type"])
def test_a_spool_that_is_archived_invoiced_or_of_another_type_is_refused(
    shop: _Shop, kind: str
) -> None:
    type_id = shop.type_id()
    other = shop.type_id("PLA-BLK")
    spool = shop.spools.create(
        SpoolCreate(type_id=other if kind == "other_type" else None, material_id=shop.material()),
        actor="ann",
    ).id
    if kind == "archived":
        shop.spools.update(spool, SpoolPatch(archived=True), actor="ann")
    if kind == "invoiced":
        first = shop.one(
            type_id, quantity=1, line_subtotal=D(40), unit_price=D(40), update_spool_ids=[spool]
        )
        shop.invoices.confirm(first.id, actor="ann")
    invoice = shop.draft(
        [
            InvoiceLineIn(
                description="x",
                quantity=1,
                unit_price=D(40),
                line_subtotal=D(40),
                type_id=type_id,
                update_spool_ids=[spool],
            )
        ],
        number="N2",
    )
    with pytest.raises(InvoiceLineError, match="line 1"):
        shop.invoices.confirm(invoice.id, actor="ann")
    assert shop.status(invoice.id) == "draft"


def test_a_foreign_currency_is_refused_and_stays_a_draft(shop: _Shop) -> None:
    invoice = shop.one(shop.type_id())
    shop.currency = "EUR"
    with pytest.raises(InvoiceCurrencyError):
        shop.invoices.confirm(invoice.id, actor="ann")
    shop.nothing_was_made(invoice.id)


def test_a_total_off_by_more_than_five_cents_is_refused(shop: _Shop) -> None:
    invoice = shop.one(shop.type_id())
    off = shop.invoices.update(invoice.id, InvoicePatch(total=D("42.86")))
    assert off is not None
    with pytest.raises(InvoiceNotReconciledError) as raised:
        shop.invoices.confirm(invoice.id, actor="ann")
    assert raised.value.difference == D("0.0600")
    shop.nothing_was_made(invoice.id)


def test_a_total_off_by_four_cents_confirms(shop: _Shop) -> None:
    invoice = shop.one(shop.type_id())
    shop.invoices.update(invoice.id, InvoicePatch(total=D("42.84")))
    done, now = _confirm(shop, invoice)
    assert now
    assert done.status == "confirmed"


def test_an_empty_invoice_is_refused(shop: _Shop) -> None:
    invoice = shop.draft([], total="2.80", subtotal="0")
    with pytest.raises(InvoiceNoLinesError):
        shop.invoices.confirm(invoice.id, actor="ann")
    shop.nothing_was_made(invoice.id)


def test_a_missing_invoice_is_none(shop: _Shop) -> None:
    assert shop.invoices.confirm(UUID(int=5), actor="ann") is None


def test_a_new_type_and_product_are_made_at_confirm(shop: _Shop) -> None:
    invoice = shop.one(None, proposed_type=shop.type_body("NEW-1"))
    done, _ = _confirm(shop, invoice)
    line = done.lines[0]
    assert line.type_id is not None
    made = shop.types.get(line.type_id)
    assert made is not None
    assert made.code == "NEW-1"
    assert line.supplier_product_id is not None
    product = shop.suppliers.product(line.supplier_product_id)
    assert product is not None
    assert product.sku == line.description == "A00-B01"
    assert product.type_id == line.type_id


def test_a_sku_that_names_another_type_is_refused(shop: _Shop) -> None:
    first = shop.one(shop.type_id("PLA-WHT"), sku="A00-B01")
    shop.invoices.confirm(first.id, actor="ann")
    second = shop.draft(
        [
            InvoiceLineIn(
                description="x",
                sku="A00-B01",
                quantity=1,
                unit_price=D(40),
                line_subtotal=D(40),
                type_id=shop.type_id("PLA-BLK"),
            )
        ],
        number="N2",
    )
    with pytest.raises(InvoiceLineError, match="another spool type"):
        shop.invoices.confirm(second.id, actor="ann")
    assert shop.status(second.id) == "draft"


def test_the_product_is_found_by_sku_case_insensitively(shop: _Shop) -> None:
    type_id = shop.type_id()
    first = shop.one(type_id, sku="A00-B01")
    shop.invoices.confirm(first.id, actor="ann")
    second = shop.draft(
        [
            InvoiceLineIn(
                description="x",
                sku="a00-b01",
                quantity=1,
                unit_price=D(40),
                line_subtotal=D(40),
                type_id=type_id,
            )
        ],
        number="N2",
    )
    done, _ = _confirm(shop, second)
    assert shop.count("filament_supplier_products") == 1
    assert shop.count("filament_price_points") == 2
    assert done.lines[0].supplier_product_id is not None


def test_a_product_with_a_pack_size_prices_a_pack(shop: _Shop) -> None:
    type_id = shop.type_id()
    product = shop.suppliers.add_product(
        shop.supplier_id, ProductCreate(type_id=type_id, sku="PACK4", pack_size=4)
    )
    assert product is not None
    invoice = shop.one(type_id, sku="PACK4", quantity=4, unit_price=D(10))
    done, _ = _confirm(shop, invoice)
    [point] = shop.suppliers.prices(product.id)
    assert point.price == D("42.8000")
    assert done.lines[0].supplier_product_id == product.id
