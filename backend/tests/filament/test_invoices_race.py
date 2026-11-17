"""Concurrent creates of invoice drafts (#6266): the import run id and the number race."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest

from scadbuddy.filament.invoices import DuplicateInvoiceError, InvoiceStore
from scadbuddy.filament.purchasing_models import InvoiceCreate, InvoiceDetail, SupplierCreate
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.suppliers import SuppliersStore
from scadbuddy.filament.types import SpoolTypeStore
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres


class _Raced(InvoiceStore):
    """Both callers finish their first ``import_run_id`` read before either inserts, so the
    insert is where the two meet."""

    def __init__(self, pool: PgPool) -> None:
        super().__init__(pool, lambda: "USD", FilamentSpoolStore(pool), SpoolTypeStore(pool))
        self._gate = threading.Barrier(2, timeout=20)
        self._seen = threading.local()

    def _by_run(self, run: str) -> InvoiceDetail | None:
        found = super()._by_run(run)
        if not getattr(self._seen, "waited", False):
            self._seen.waited = True
            self._gate.wait()
        return found


def _body(supplier_id: UUID, number: str | None, run: str) -> InvoiceCreate:
    return InvoiceCreate(
        supplier_id=supplier_id,
        number=number,
        invoice_date=date(2026, 11, 1),
        currency="USD",
        subtotal=Decimal("10"),
        total=Decimal("10"),
        import_run_id=run,
    )


def _both(store: InvoiceStore, bodies: list[InvoiceCreate]) -> list[object]:
    def run(body: InvoiceCreate) -> object:
        try:
            return store.create(body, actor="t")
        except DuplicateInvoiceError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        return list(executor.map(run, bodies))


def _rows(pool: PgPool) -> int:
    with pool.connection() as conn:
        row = conn.execute("SELECT count(*) AS n FROM filament_invoices").fetchone()
    assert row is not None
    return int(row["n"])


def _supplier(pool: PgPool) -> UUID:
    return SuppliersStore(pool, lambda: "USD").create(SupplierCreate(name="Acme")).id


def test_two_creates_with_one_run_id_make_one_invoice(pg_pool: PgPool) -> None:
    supplier = _supplier(pg_pool)
    results = _both(_Raced(pg_pool), [_body(supplier, None, "run-1")] * 2)
    assert all(isinstance(r, tuple) for r in results)
    details = [r[0] for r in results if isinstance(r, tuple)]
    assert details[0].id == details[1].id
    assert sorted(r[1] for r in results if isinstance(r, tuple)) == [False, True]
    assert _rows(pg_pool) == 1


def test_two_creates_with_one_number_make_one_invoice(pg_pool: PgPool) -> None:
    supplier = _supplier(pg_pool)
    results = _both(
        _Raced(pg_pool), [_body(supplier, "INV-1", "run-a"), _body(supplier, "INV-1", "run-b")]
    )
    made = [r for r in results if isinstance(r, tuple)]
    refused = [r for r in results if isinstance(r, DuplicateInvoiceError)]
    assert len(made) == 1
    assert len(refused) == 1
    assert refused[0].existing_id == made[0][0].id
    assert _rows(pg_pool) == 1


def test_a_retry_with_the_same_run_id_and_number_returns_the_first(pg_pool: PgPool) -> None:
    supplier = _supplier(pg_pool)
    results = _both(_Raced(pg_pool), [_body(supplier, "INV-1", "run-1")] * 2)
    assert all(isinstance(r, tuple) for r in results), results
    details = [r[0] for r in results if isinstance(r, tuple)]
    assert details[0].id == details[1].id
    assert _rows(pg_pool) == 1
