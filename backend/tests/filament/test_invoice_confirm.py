"""Confirming an invoice (#6266 Task 6): spools, costs, products and price points made in
one transaction, idempotent by state, and refused whole."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from psycopg import errors

from scadbuddy.filament.invoices import (
    InvoiceCurrencyError,
    InvoiceLineError,
    InvoiceNoLinesError,
    InvoiceNotReconciledError,
    InvoiceStore,
)
from scadbuddy.filament.models import SpoolCreate, SpoolPatch
from scadbuddy.filament.purchasing_models import (
    InvoiceDetail,
    InvoiceLineIn,
    InvoicePatch,
    ProductCreate,
    ProductPatch,
)
from tests.conftest import PgPool
from tests.support.purchasing import Shop

pytestmark = pytest.mark.requires_postgres

D = Decimal


@pytest.fixture
def shop(pg_pool: PgPool) -> Shop:
    return Shop(pg_pool)


def _confirm(shop: Shop, invoice: InvoiceDetail) -> tuple[InvoiceDetail, bool]:
    done = shop.invoices.confirm(invoice.id, actor="ann")
    assert done is not None
    return done[0], done[1]


def test_confirm_makes_spools_with_cost_and_link(shop: Shop) -> None:
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
    # #6869: the price names its invoice, so the UI links it to the line.
    assert (point.invoice_line_id, point.invoice_id) == (line.id, invoice.id)
    [newest] = shop.suppliers.prices(point.product_id)
    assert newest.invoice_id == invoice.id


def test_confirm_twice_creates_nothing_more(shop: Shop) -> None:
    invoice = shop.one(shop.type_id())
    _confirm(shop, invoice)
    spools, points = shop.count("filament_spools"), shop.count("filament_price_points")
    again, now = _confirm(shop, invoice)
    assert not now
    assert again.status == "confirmed"
    assert (shop.count("filament_spools"), shop.count("filament_price_points")) == (spools, points)


class _Held(InvoiceStore):
    """The first confirm stops inside its transaction, after it took the invoice row."""

    def __init__(self, shop: Shop) -> None:
        super().__init__(shop.pool, lambda: "USD", shop.spools, shop.types)
        self.inside = threading.Event()
        self.release = threading.Event()

    def _confirm_line(self, *args: Any, **kwargs: Any) -> None:
        self.inside.set()
        assert self.release.wait(30)
        super()._confirm_line(*args, **kwargs)


def test_two_confirms_at_once_make_one_set(shop: Shop) -> None:
    invoice = shop.one(shop.type_id(), quantity=3, unit_price=D("13.3333"))
    held = _Held(shop)

    def run() -> tuple[InvoiceDetail, bool, tuple[UUID, ...]] | None:
        return held.confirm(invoice.id, actor="ann")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(run)
        assert held.inside.wait(30)
        second = executor.submit(run)
        with pytest.raises(FuturesTimeout):
            second.result(timeout=1.5)  # blocked on the invoice row, not done
        assert not first.done()
        held.release.set()
        one, two = first.result(30), second.result(30)
    assert one is not None
    assert two is not None
    assert (one[1], two[1]) == (True, False)
    assert two[0].status == "confirmed"
    assert shop.count("filament_spools") == 3
    assert shop.count("filament_price_points") == 1


def test_a_line_fills_an_existing_costless_spool(shop: Shop) -> None:
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


def test_a_created_or_updated_spools_event_carries_the_invoice_line_ref(shop: Shop) -> None:
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


def test_confirm_writes_edited_on_existing_spools(shop: Shop) -> None:
    type_id = shop.type_id()
    old = shop.spools.create(SpoolCreate(material_id=shop.material()), actor="ann").id
    before = len(shop.spools.events(old))
    invoice = shop.one(type_id, update_spool_ids=[old])
    done, _ = _confirm(shop, invoice)
    line_id = done.lines[0].id
    [event] = [e for e in shop.spools.events(old)[before:] if e.kind == "edited"]
    changes = event.data["changes"]
    assert changes["invoice_line_id"] == [None, str(line_id)]
    assert changes["type_id"] == [None, str(type_id)]
    assert changes["cost_per_kg"][0] is None
    assert D(changes["cost_per_kg"][1]) == D("21.4000")
    assert event.ref_kind == "invoice_line"


def test_a_duplicate_proposed_type_is_a_409_line_error(shop: Shop) -> None:
    shop.type_id("PLA-WHT")
    invoice = shop.one(None, proposed_type=shop.type_body("PLA-WHT"))
    with pytest.raises(InvoiceLineError, match="line 1"):
        shop.invoices.confirm(invoice.id, actor="ann")
    assert shop.status(invoice.id) == "draft"


def test_a_missing_catalog_row_in_a_proposed_type_is_a_line_error(shop: Shop) -> None:
    body = shop.type_body("X").model_copy(update={"brand_id": UUID(int=7)})
    invoice = shop.one(None, proposed_type=body)
    with pytest.raises(InvoiceLineError, match="line 1"):
        shop.invoices.confirm(invoice.id, actor="ann")
    assert shop.status(invoice.id) == "draft"


def test_a_catalog_name_that_keeps_clashing_is_a_line_error(
    shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every lookup misses and the insert of the name clashes with the row that has it."""
    monkeypatch.setattr("scadbuddy.filament.catalog.find_by_key", lambda *_, **__: None)
    invoice = shop.one(
        None, proposed_type=shop.type_body("PLA-NEW", material_id=None, material_name="PLA")
    )
    with pytest.raises(InvoiceLineError, match="line 1"):
        shop.invoices.confirm(invoice.id, actor="ann")
    assert shop.status(invoice.id) == "draft"


def test_a_line_with_no_type_is_a_line_error(shop: Shop) -> None:
    invoice = shop.one(None)
    with pytest.raises(InvoiceLineError, match="line 1"):
        shop.invoices.confirm(invoice.id, actor="ann")
    shop.nothing_was_made(invoice.id)


def test_a_line_fills_a_costed_spool_and_replaces_its_cost(shop: Shop) -> None:
    """R-6700-1 and R-6700-2: a spool with its own cost and no invoice line is filled; its
    cost is the line's, and one `edited` event by the caller records the old and new cost."""
    type_id = shop.type_id()
    costed = shop.spools.create(SpoolCreate(type_id=type_id, cost_per_kg=D("9")), actor="ann").id
    invoice = shop.one(
        type_id, quantity=1, unit_price=D(40), line_subtotal=D(40), update_spool_ids=[costed]
    )
    done, now = _confirm(shop, invoice)
    assert now
    line = done.lines[0]
    assert line.spool_ids == [costed]
    filled = shop.spools.get(costed)
    assert filled is not None
    assert filled.cost_per_kg == D("42.8000")
    assert [r["id"] for r in shop.spool_rows(invoice.id)] == [costed]
    edited = [e for e in shop.spools.events(costed) if e.kind == "edited"]
    assert len(edited) == 1
    assert edited[0].actor == "ann"
    assert (edited[0].ref_kind, edited[0].ref_id) == ("invoice_line", str(line.id))
    assert edited[0].data["changes"] == {
        "cost_per_kg": ["9.0000", "42.8000"],
        "invoice_line_id": [None, str(line.id)],
    }


def test_the_spool_list_names_each_spools_invoice_line(shop: Shop) -> None:
    """R-6700-7: the invoice page offers a costed spool with no invoice line, never one on
    another invoice; the list says which is which."""
    type_id = shop.type_id()
    costed = shop.spools.create(SpoolCreate(type_id=type_id, cost_per_kg=D("9")), actor="ann").id
    assert {s.id: s.invoice_line_id for s in shop.spools.list()} == {costed: None}
    invoice = shop.one(
        type_id, quantity=1, unit_price=D(40), line_subtotal=D(40), update_spool_ids=[costed]
    )
    done, _ = _confirm(shop, invoice)
    assert {s.id: s.invoice_line_id for s in shop.spools.list()} == {costed: done.lines[0].id}


def test_a_costed_spool_of_a_line_that_is_refused_keeps_its_cost(shop: Shop) -> None:
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
                update_spool_ids=[costed],
            ),
            InvoiceLineIn(
                description="two",
                quantity=1,
                unit_price=D(20),
                line_subtotal=D(20),
                type_id=None,
            ),
        ]
    )
    with pytest.raises(InvoiceLineError, match="line 2"):
        shop.invoices.confirm(invoice.id, actor="ann")
    shop.nothing_was_made(invoice.id, spools=1)
    kept = shop.spools.get(costed)
    assert kept is not None
    assert kept.cost_per_kg == D("9.0000")


def test_a_line_fills_an_archived_spool_and_leaves_it_archived(shop: Shop) -> None:
    """#6707 R-6707-12: a spent spool was still bought. The line sets its cost and its
    invoice line only: it stays archived and keeps no type it did not have."""
    type_id = shop.type_id()
    spent = shop.spools.create(SpoolCreate(material_id=shop.material()), actor="ann").id
    shop.spools.update(spent, SpoolPatch(archived=True), actor="ann")
    invoice = shop.one(
        type_id, quantity=1, line_subtotal=D(40), unit_price=D(40), update_spool_ids=[spent]
    )
    done, _ = _confirm(shop, invoice)
    filled = shop.spools.get(spent)
    assert filled is not None
    assert filled.archived_at is not None
    assert filled.type_id is None
    assert filled.cost_per_kg == D("42.8000")
    assert done.lines[0].spool_ids == [spent]
    assert [r["id"] for r in shop.spool_rows(invoice.id)] == [spent]


def test_an_untyped_archived_spool_stays_untyped_where_an_open_one_takes_the_type(
    shop: Shop,
) -> None:
    """R-6707-15: two spools with a null `type_id` on one line. The open one takes the
    line's type; the archived one takes its cost and line only, and stays untyped."""
    type_id = shop.type_id()
    spent = shop.spools.create(SpoolCreate(material_id=shop.material()), actor="ann").id
    shop.spools.update(spent, SpoolPatch(archived=True), actor="ann")
    live = shop.spools.create(SpoolCreate(material_id=shop.material()), actor="ann").id
    invoice = shop.one(type_id, update_spool_ids=[spent, live])
    done, _ = _confirm(shop, invoice)
    got = {s: shop.spools.get(s) for s in (spent, live)}
    assert {s: (g.type_id, g.archived_at is not None) for s, g in got.items() if g} == {
        spent: (None, True),
        live: (type_id, False),
    }
    assert {g.cost_per_kg for g in got.values() if g} == {D("21.4000")}
    assert sorted(done.lines[0].spool_ids) == sorted([spent, live])


@pytest.mark.parametrize("kind", ["invoiced", "other_type"])
def test_a_spool_that_is_invoiced_or_of_another_type_is_refused(shop: Shop, kind: str) -> None:
    type_id = shop.type_id()
    other = shop.type_id("PLA-BLK")
    spool = shop.spools.create(
        SpoolCreate(type_id=other if kind == "other_type" else None, material_id=shop.material()),
        actor="ann",
    ).id
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
    reason = {
        "invoiced": "already belongs to an invoice line",
        "other_type": "has another spool type",
    }[kind]
    with pytest.raises(InvoiceLineError, match=f"line 1: spool {spool} {reason}"):
        shop.invoices.confirm(invoice.id, actor="ann")
    assert shop.status(invoice.id) == "draft"


def test_a_foreign_currency_is_refused_and_stays_a_draft(shop: Shop) -> None:
    invoice = shop.one(shop.type_id())
    shop.currency = "EUR"
    with pytest.raises(InvoiceCurrencyError):
        shop.invoices.confirm(invoice.id, actor="ann")
    shop.nothing_was_made(invoice.id)


def test_a_total_off_by_more_than_five_cents_is_refused(shop: Shop) -> None:
    invoice = shop.one(shop.type_id())
    off = shop.invoices.update(invoice.id, InvoicePatch(total=D("42.86")))
    assert off is not None
    with pytest.raises(InvoiceNotReconciledError) as raised:
        shop.invoices.confirm(invoice.id, actor="ann")
    assert raised.value.difference == D("0.0600")
    shop.nothing_was_made(invoice.id)


def test_a_total_off_by_four_cents_confirms(shop: Shop) -> None:
    invoice = shop.one(shop.type_id())
    shop.invoices.update(invoice.id, InvoicePatch(total=D("42.84")))
    done, now = _confirm(shop, invoice)
    assert now
    assert done.status == "confirmed"


def test_an_empty_invoice_is_refused(shop: Shop) -> None:
    invoice = shop.draft([], total="2.80", subtotal="0")
    with pytest.raises(InvoiceNoLinesError):
        shop.invoices.confirm(invoice.id, actor="ann")
    shop.nothing_was_made(invoice.id)


def test_a_missing_invoice_is_none(shop: Shop) -> None:
    assert shop.invoices.confirm(UUID(int=5), actor="ann") is None


def test_a_new_type_and_product_are_made_at_confirm(shop: Shop) -> None:
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


def test_confirm_names_the_types_it_made(shop: Shop) -> None:
    kept = shop.type_id("PLA-WHT")
    made = shop.one(None, proposed_type=shop.type_body("NEW-1"))
    done = shop.invoices.confirm(made.id, actor="ann")
    assert done is not None
    assert done[2] == (done[0].lines[0].type_id,)
    again = shop.invoices.confirm(made.id, actor="ann")
    assert again is not None
    assert again[2] == ()
    other = shop.invoices.confirm(shop.one(kept, description="B00").id, actor="ann")
    assert other is not None
    assert other[2] == ()


def test_a_sku_that_names_another_type_is_refused(shop: Shop) -> None:
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


def test_the_product_is_found_by_sku_case_insensitively(shop: Shop) -> None:
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


def test_a_product_with_a_pack_size_prices_a_pack(shop: Shop) -> None:
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


def test_a_line_may_not_fill_more_spools_than_its_quantity(shop: Shop) -> None:
    type_id = shop.type_id()
    spools = [
        shop.spools.create(SpoolCreate(material_id=shop.material()), actor="ann").id
        for _ in range(3)
    ]
    invoice = shop.one(type_id, quantity=2, update_spool_ids=spools[:2])
    # The table's check stops a longer list at write time; shorten the quantity under it.
    with shop.pool.connection() as conn:
        conn.execute(
            "ALTER TABLE filament_invoice_lines DROP CONSTRAINT filament_invoice_lines_update_ids"
        )
        conn.execute("UPDATE filament_invoice_lines SET update_spool_ids = %s", (spools,))
    with pytest.raises(InvoiceLineError, match=r"line 1.*quantity"):
        shop.invoices.confirm(invoice.id, actor="ann")
    shop.nothing_was_made(invoice.id, spools=3)


def test_a_line_may_not_name_a_spool_twice(shop: Shop) -> None:
    type_id = shop.type_id()
    spool = shop.spools.create(SpoolCreate(material_id=shop.material()), actor="ann").id
    invoice = shop.one(type_id, update_spool_ids=[spool, spool])
    with pytest.raises(InvoiceLineError, match=r"line 1.*twice"):
        shop.invoices.confirm(invoice.id, actor="ann")
    shop.nothing_was_made(invoice.id, spools=1)


def test_a_cost_too_large_for_a_spool_is_a_409_not_a_500(shop: Shop) -> None:
    type_id = shop.types.create(shop.type_body("TINY", label_weight_g=1)).id
    invoice = shop.draft(
        [
            InvoiceLineIn(
                description="x",
                quantity=1,
                unit_price=D(100_000),
                line_subtotal=D(100_000),
                type_id=type_id,
            )
        ],
        subtotal="100000",
        tax="0",
        total="100000",
    )
    with pytest.raises(InvoiceLineError, match="line 1"):
        shop.invoices.confirm(invoice.id, actor="ann")
    shop.nothing_was_made(invoice.id)


def test_a_database_data_error_in_a_spool_write_is_a_line_error(
    shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: Any, **__: Any) -> UUID:
        raise errors.NumericValueOutOfRange("out of range")

    monkeypatch.setattr(shop.spools, "create_on", boom)
    invoice = shop.one(shop.type_id())
    with pytest.raises(InvoiceLineError, match="line 1"):
        shop.invoices.confirm(invoice.id, actor="ann")
    shop.nothing_was_made(invoice.id)


def test_an_unexpected_error_is_not_swallowed(shop: Shop, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: Any, **__: Any) -> UUID:
        raise RuntimeError("bug")

    monkeypatch.setattr(shop.spools, "create_on", boom)
    invoice = shop.one(shop.type_id())
    with pytest.raises(RuntimeError):
        shop.invoices.confirm(invoice.id, actor="ann")


def test_an_archived_named_product_is_refused(shop: Shop) -> None:
    type_id = shop.type_id()
    product = shop.suppliers.add_product(
        shop.supplier_id, ProductCreate(type_id=type_id, sku="OLD")
    )
    assert product is not None
    shop.suppliers.update_product(product.id, ProductPatch(archived=True))
    invoice = shop.one(type_id, supplier_product_id=product.id)
    with pytest.raises(InvoiceLineError, match=r"line 1.*archived"):
        shop.invoices.confirm(invoice.id, actor="ann")
    shop.nothing_was_made(invoice.id)


def test_a_long_description_with_no_sku_is_refused(shop: Shop) -> None:
    invoice = shop.one(shop.type_id(), description="W" * 129)
    with pytest.raises(InvoiceLineError, match="set a SKU"):
        shop.invoices.confirm(invoice.id, actor="ann")
    shop.nothing_was_made(invoice.id)


def test_two_lines_proposing_one_new_type_share_it(shop: Shop) -> None:
    body = shop.type_body("SHARED")
    lines = [
        InvoiceLineIn(
            description=name,
            quantity=1,
            unit_price=D(20),
            line_subtotal=D(20),
            proposed_type=body,
        )
        for name in ("sku-a", "sku-b")
    ]
    invoice = shop.draft(lines, tax="0", total="40.00")
    done, _ = _confirm(shop, invoice)
    assert done.lines[0].type_id == done.lines[1].type_id
    assert len([t for t in shop.types.list() if t.code == "SHARED"]) == 1


def test_two_lines_on_one_product_make_two_price_points(shop: Shop) -> None:
    type_id = shop.type_id()
    lines = [
        InvoiceLineIn(
            description="x",
            sku="SAME",
            quantity=1,
            unit_price=D(price),
            line_subtotal=D(price),
            type_id=type_id,
        )
        for price in (20, 30)
    ]
    invoice = shop.draft(lines, tax="0", total="50.00")
    done, _ = _confirm(shop, invoice)
    assert shop.count("filament_supplier_products") == 1
    assert done.lines[0].supplier_product_id == done.lines[1].supplier_product_id
    assert shop.count("filament_price_points") == 2
    assert len(shop.spool_rows(invoice.id)) == 2


def test_a_claimed_spools_own_label_weight_sets_its_cost_per_kg(shop: Shop) -> None:
    type_id = shop.type_id()
    spool = shop.spools.create(
        SpoolCreate(material_id=shop.material(), label_weight_g=500), actor="ann"
    ).id
    invoice = shop.one(type_id, update_spool_ids=[spool])
    _confirm(shop, invoice)
    claimed = shop.spools.get(spool)
    assert claimed is not None
    assert claimed.cost_per_kg == D("42.8000")  # 21.40 a spool over 0.5 kg
    others = [r for r in shop.spool_rows(invoice.id) if r["id"] != spool]
    assert [r["cost_per_kg"] for r in others] == [D("21.4000")]
