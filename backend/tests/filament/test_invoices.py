"""Invoice drafts: tax, shipping, duplicates, idempotent create and the frozen confirmed
state (#6266)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from scadbuddy.filament.invoice_money import MixedLineTaxError
from scadbuddy.filament.invoices import (
    DuplicateInvoiceError,
    InvoiceConfirmedError,
    InvoiceRefError,
    InvoiceStore,
)
from scadbuddy.filament.purchasing_models import (
    InvoiceCreate,
    InvoiceDetail,
    InvoiceLineIn,
    InvoicePatch,
    ProductCreate,
    SupplierCreate,
    SupplierPatch,
)
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.suppliers import SuppliersStore
from scadbuddy.filament.types import SpoolTypeStore
from tests.conftest import PgPool
from tests.support.purchasing import Shop

pytestmark = pytest.mark.requires_postgres

D = Decimal


def _store(pool: PgPool) -> InvoiceStore:
    return InvoiceStore(pool, lambda: "USD", FilamentSpoolStore(pool), SpoolTypeStore(pool))


def _supplier(pool: PgPool, name: str = "Acme") -> UUID:
    return SuppliersStore(pool, lambda: "USD").create(SupplierCreate(name=name)).id


def _line(subtotal: str, tax: str | None = None, quantity: int = 1) -> InvoiceLineIn:
    return InvoiceLineIn(
        description="PLA white",
        quantity=quantity,
        unit_price=D(subtotal) / quantity,
        line_subtotal=D(subtotal),
        line_tax=None if tax is None else D(tax),
    )


def _body(
    supplier_id: UUID,
    *,
    lines: list[InvoiceLineIn] | None = None,
    number: str | None = None,
    total: str = "117.00",
    tax: str = "7.00",
    shipping: str = "10.00",
    currency: str = "USD",
    run: str | None = None,
    invoice_date: date = date(2026, 11, 1),
) -> InvoiceCreate:
    return InvoiceCreate(
        supplier_id=supplier_id,
        number=number,
        invoice_date=invoice_date,
        currency=currency,
        subtotal=D("100"),
        shipping=D(shipping),
        tax=D(tax),
        total=D(total),
        import_run_id=run,
        lines=lines if lines is not None else [_line("40"), _line("60")],
    )


def _make(store: InvoiceStore, body: InvoiceCreate) -> InvoiceDetail:
    made, new = store.create(body, actor="test")
    assert new
    return made


def test_a_draft_allocates_shipping_and_receipt_tax(pg_pool: PgPool) -> None:
    invoice = _make(_store(pg_pool), _body(_supplier(pg_pool)))
    first, second = invoice.lines
    assert first.tax_basis == "receipt-wide 7.0%"
    assert (first.line_tax, second.line_tax) == (D("2.8000"), D("4.2000"))
    assert (first.line_total, second.line_total) == (D("42.8000"), D("64.2000"))
    assert (first.allocated_cost, second.allocated_cost) == (D("46.8000"), D("70.2000"))
    assert first.allocated_cost + second.allocated_cost == D("117.0000") == D(107) + D(10)
    assert first.unit_cost == D("46.8000")
    assert invoice.line_count == 2


def test_line_tax_given_on_every_line_is_kept(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    invoice = _make(store, _body(_supplier(pg_pool), lines=[_line("40", "3"), _line("60", "5")]))
    assert {line.tax_basis for line in invoice.lines} == {"line"}
    assert [line.line_tax for line in invoice.lines] == [D("3.0000"), D("5.0000")]
    changed = store.update(invoice.id, InvoicePatch(shipping=D("20")))
    assert changed is not None
    assert [line.line_tax for line in changed.lines] == [D("3.0000"), D("5.0000")]
    assert sum(line.allocated_cost for line in changed.lines) == D("128.0000")


def test_mixed_line_tax_rolls_back(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    supplier = _supplier(pg_pool)
    mixed = [_line("40", "3"), _line("60")]
    with pytest.raises(MixedLineTaxError):
        store.create(_body(supplier, lines=mixed, number="M"), actor="t")
    assert store.list() == []
    invoice = _make(store, _body(supplier))
    with pytest.raises(MixedLineTaxError):
        store.replace_lines(invoice.id, mixed)
    kept = store.get(invoice.id)
    assert kept is not None
    assert kept.lines[0].tax_basis == "receipt-wide 7.0%"


def test_a_missing_line_subtotal_is_quantity_times_price(pg_pool: PgPool) -> None:
    line = InvoiceLineIn(description="x", quantity=3, unit_price=D("12.5"))
    invoice = _make(_store(pg_pool), _body(_supplier(pg_pool), lines=[line], tax="0"))
    assert invoice.lines[0].line_subtotal == D("37.5000")
    assert invoice.lines[0].tax_basis == "none"


def test_the_draft_reports_its_difference(pg_pool: PgPool) -> None:
    supplier = _supplier(pg_pool)
    near = _make(_store(pg_pool), _body(supplier, total="117.04", number="A"))
    assert near.difference == D("0.0400")
    assert near.reconciles is True
    far = _make(_store(pg_pool), _body(supplier, total="118.00", number="B"))
    assert far.difference == D("1.0000")
    assert far.reconciles is False


def test_the_same_number_twice_is_refused(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    supplier = _supplier(pg_pool)
    first = _make(store, _body(supplier, number="INV-1"))
    with pytest.raises(DuplicateInvoiceError) as err:
        store.create(_body(supplier, number="inv-1", run="other"), actor="t")
    assert err.value.existing_id == first.id
    assert len(store.list()) == 1
    other = _make(store, _body(_supplier(pg_pool, "Other"), number="INV-1"))
    assert other.id != first.id


def test_a_number_edit_onto_a_taken_number_is_refused(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    supplier = _supplier(pg_pool)
    first = _make(store, _body(supplier, number="INV-1"))
    numberless = _make(store, _body(supplier))
    with pytest.raises(DuplicateInvoiceError) as err:
        store.update(numberless.id, InvoicePatch(number="INV-1"))
    assert err.value.existing_id == first.id
    assert store.update(first.id, InvoicePatch(number="INV-1")) is not None


def test_a_second_import_run_id_returns_the_first(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    supplier = _supplier(pg_pool)
    first, made = store.create(_body(supplier, run="run-1"), actor="t")
    assert made
    again, made = store.create(_body(supplier, run="run-1", lines=[_line("1")]), actor="t")
    assert not made
    assert again.id == first.id
    assert again.line_count == 2


def test_a_missing_supplier_is_a_ref_error(pg_pool: PgPool) -> None:
    with pytest.raises(InvoiceRefError):
        _store(pg_pool).create(_body(uuid4()), actor="t")


def test_editing_lines_recomputes(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    invoice = _make(store, _body(_supplier(pg_pool)))
    edited = store.replace_lines(invoice.id, [_line("40"), _line("120", quantity=2)])
    assert edited is not None
    assert [line.quantity for line in edited.lines] == [1, 2]
    assert [line.allocated_cost for line in edited.lines] != [D("46.8000"), D("70.2000")]
    # 160 subtotal, tax 7 spread, shipping 10: lines plus shipping.
    assert sum(line.allocated_cost for line in edited.lines) == D("177.0000")
    assert edited.lines[1].unit_cost == (edited.lines[1].allocated_cost / 2).quantize(D("0.0001"))
    assert store.replace_lines(uuid4(), []) is None


def test_a_header_edit_recomputes(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    invoice = _make(store, _body(_supplier(pg_pool)))
    changed = store.update(invoice.id, InvoicePatch(tax=D("10"), shipping=D("0")))
    assert changed is not None
    assert changed.lines[0].tax_basis == "receipt-wide 10.0%"
    assert sum(line.allocated_cost for line in changed.lines) == D("110.0000")


def _confirm(pool: PgPool, invoice_id: UUID) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE filament_invoices SET status = 'confirmed', confirmed_at = now() WHERE id = %s",
            (invoice_id,),
        )


def test_a_draft_deletes_and_a_confirmed_one_does_not(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    supplier = _supplier(pg_pool)
    gone = _make(store, _body(supplier, number="G"))
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES ('k', %s, %s)",
            ("filament_invoice", str(gone.id)),
        )
    assert store.delete(gone.id) is True
    assert store.delete(gone.id) is False
    assert store.get(gone.id) is None
    with pg_pool.connection() as conn:
        held = conn.execute(
            "SELECT count(*) AS n FROM blob_refs WHERE holder_id = %s", (str(gone.id),)
        ).fetchone()
    assert held is not None
    assert held["n"] == 0

    kept = _make(store, _body(supplier, number="K"))
    _confirm(pg_pool, kept.id)
    with pytest.raises(InvoiceConfirmedError):
        store.delete(kept.id)
    with pytest.raises(InvoiceConfirmedError):
        store.replace_lines(kept.id, [_line("1")])
    with pytest.raises(InvoiceConfirmedError):
        store.update(kept.id, InvoicePatch(shipping=D("1")))
    noted = store.update(kept.id, InvoicePatch(note="paid"))
    assert noted is not None
    assert noted.note == "paid"
    assert noted.shipping == D("10.0000")


def test_the_exponent_comes_from_the_currency(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    jpy = _make(store, _body(_supplier(pg_pool), currency="JPY"))
    assert jpy.minor_exponent == 0
    usd = _make(store, _body(_supplier(pg_pool, "B"), currency="USD"))
    assert usd.minor_exponent == 2


def test_the_list_is_newest_first_and_filters(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    one, two = _supplier(pg_pool, "One"), _supplier(pg_pool, "Two")
    old = _make(store, _body(one, number="O"))
    new = _make(store, _body(two, number="N", invoice_date=date(2026, 12, 1)))
    assert [i.id for i in store.list()] == [new.id, old.id]
    assert [i.id for i in store.list(supplier_id=one)] == [old.id]
    _confirm(pg_pool, old.id)
    assert [i.id for i in store.list(status="confirmed")] == [old.id]


def test_a_header_only_draft_has_no_lines(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    invoice = _make(store, _body(_supplier(pg_pool), lines=[]))
    assert invoice.lines == []
    assert invoice.line_count == 0
    assert invoice.difference == D("107.0000")
    assert store.get(invoice.id) == invoice


def test_set_file_on_a_missing_invoice_is_none_and_holds_nothing(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    ghost = uuid4()
    assert (
        store.set_file(
            ghost, key="attachment-x", name="a.pdf", media_type="application/pdf", size=1
        )
        is None
    )
    with pg_pool.connection() as conn:
        held = conn.execute(
            "SELECT count(*) AS n FROM blob_refs WHERE holder_kind = 'filament_invoice'"
            " AND holder_id = %s",
            (str(ghost),),
        ).fetchone()
    assert held is not None
    assert held["n"] == 0


def test_file_key_is_none_until_a_file_is_set(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    invoice = _make(store, _body(_supplier(pg_pool)))
    assert store.file_key(invoice.id) is None
    assert store.file_key(uuid4()) is None
    made = store.set_file(
        invoice.id, key="attachment-x", name="a.pdf", media_type="application/pdf", size=7
    )
    assert made is not None
    assert store.file_key(invoice.id) == ("attachment-x", made)


def test_a_draft_moves_to_another_supplier(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    one, two = _supplier(pg_pool, "One"), _supplier(pg_pool, "Two")
    draft = _make(store, _body(one, number="A-1"))
    moved = store.update(draft.id, InvoicePatch(supplier_id=two))
    assert moved is not None
    assert moved.supplier_id == two
    assert moved.number == "A-1"
    assert [i.id for i in store.list(supplier_id=two)] == [draft.id]
    assert store.list(supplier_id=one) == []


def test_a_move_names_a_missing_or_archived_supplier_as_a_ref_error(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    one, two = _supplier(pg_pool, "One"), _supplier(pg_pool, "Two")
    draft = _make(store, _body(one))
    with pytest.raises(InvoiceRefError):
        store.update(draft.id, InvoicePatch(supplier_id=uuid4()))
    SuppliersStore(pg_pool, lambda: "USD").update(two, SupplierPatch(archived=True))
    with pytest.raises(InvoiceRefError):
        store.update(draft.id, InvoicePatch(supplier_id=two))
    kept = store.get(draft.id)
    assert kept is not None
    assert kept.supplier_id == one


def test_a_confirmed_invoice_keeps_its_supplier(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    one, two = _supplier(pg_pool, "One"), _supplier(pg_pool, "Two")
    kept = _make(store, _body(one, number="K"))
    _confirm(pg_pool, kept.id)
    with pytest.raises(InvoiceConfirmedError):
        store.update(kept.id, InvoicePatch(supplier_id=two))


def test_a_move_onto_a_supplier_that_has_the_number_is_refused(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    one, two = _supplier(pg_pool, "One"), _supplier(pg_pool, "Two")
    first = _make(store, _body(two, number="SAME"))
    draft = _make(store, _body(one, number="same"))
    with pytest.raises(DuplicateInvoiceError) as err:
        store.update(draft.id, InvoicePatch(supplier_id=two))
    assert err.value.existing_id == first.id
    unmoved = store.get(draft.id)
    assert unmoved is not None
    assert unmoved.supplier_id == one


def test_a_move_drops_the_lines_product_of_the_old_supplier(pg_pool: PgPool) -> None:
    shop = Shop(pg_pool)
    type_id = shop.type_id()
    product = shop.suppliers.add_product(shop.supplier_id, ProductCreate(type_id=type_id, sku="S1"))
    assert product is not None
    draft = shop.draft(
        [
            InvoiceLineIn(
                description="PLA", quantity=1, unit_price=D("40"), supplier_product_id=product.id
            )
        ]
    )
    assert draft.lines[0].supplier_product_id == product.id
    other = shop.suppliers.create(SupplierCreate(name="Other")).id
    moved = shop.invoices.update(draft.id, InvoicePatch(supplier_id=other))
    assert moved is not None
    assert moved.lines[0].supplier_product_id is None


def test_the_list_counts_each_invoices_spools(pg_pool: PgPool) -> None:
    shop = Shop(pg_pool)
    type_id = shop.type_id()
    line = InvoiceLineIn(description="PLA", quantity=3, unit_price=D("10"), type_id=type_id)
    confirmed = shop.draft([line], subtotal="30", tax="0", total="30", number="C")
    waiting = shop.draft([line], subtotal="30", tax="0", total="30", number="D")
    assert shop.invoices.confirm(confirmed.id, actor="t") is not None
    counts = {i.id: i.spool_count for i in shop.invoices.list()}
    assert counts == {confirmed.id: 3, waiting.id: 0}
    detail = shop.invoices.get(confirmed.id)
    assert detail is not None
    assert detail.spool_count == 3


def test_a_check_the_validator_missed_is_a_ref_error_not_a_500(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    one = _supplier(pg_pool, "One")
    draft = _make(store, _body(one))
    bad = InvoiceLineIn.model_construct(
        description="x",
        sku=None,
        quantity=1,
        unit_price=Decimal(1),
        line_subtotal=None,
        line_tax=None,
        colour_code=None,
        type_id=None,
        supplier_product_id=None,
        proposed_type=None,
        update_spool_ids=[uuid4(), uuid4()],
    )
    with pytest.raises(InvoiceRefError):
        store.replace_lines(draft.id, [bad])
