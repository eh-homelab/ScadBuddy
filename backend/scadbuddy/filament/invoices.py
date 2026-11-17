"""Invoice drafts (#6266, epic #6260, spec §5.8): a header and its lines.

A draft is edited freely; every write to one ends in `_recompute`, which spreads the
receipt's tax and shipping over the lines (`invoice_money`) in the same transaction, so a
reader never sees a line total that disagrees with the header. A confirmed invoice is
frozen but for its note (and, in Task 5, its file). No trigger keeps ``updated_at``: each
write here sets it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from psycopg import Connection, errors
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.filament import purchasing_models as pm
from scadbuddy.filament.invoice_money import (
    RECONCILE_TOLERANCE,
    allocate,
    difference,
    tax_per_line,
)
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.types import SpoolTypeStore
from scadbuddy.orders.currency import minor_exponent

Conn = Connection[DictRow]
Row = Mapping[str, Any]
_Ids = list[UUID]
_Invoices = list[pm.Invoice]

_UNIT = Decimal("0.0001")
_COMPUTED = {"minor_exponent", "difference", "reconciles", "line_count", "file"}
_INVOICE_COLUMNS = (
    ", ".join(f"i.{n}" for n in pm.Invoice.model_fields if n not in _COMPUTED)
    + ", i.file_name, i.file_media_type, i.file_size"
)
_LINE_COLUMNS = ", ".join(
    f"l.{n}" for n in pm.InvoiceLine.model_fields if n not in {"unit_cost", "spool_ids"}
)
_HOLDER = "filament_invoice"


class InvoiceRefError(ValueError):
    """An invoice or a line names a supplier, type or product that does not exist."""

    def __init__(self) -> None:
        super().__init__("no such supplier, spool type or supplier product")


class DuplicateInvoiceError(ValueError):
    """The supplier already has an invoice with the number."""

    def __init__(self, existing_id: UUID) -> None:
        super().__init__("the supplier has an invoice with that number")
        self.existing_id = existing_id


class InvoiceConfirmedError(ValueError):
    """A confirmed invoice is frozen."""

    def __init__(self) -> None:
        super().__init__("a confirmed invoice cannot be changed; edit its spools instead")


def _sets(patch: pm.InvoicePatch) -> tuple[list[str], list[Any]]:
    columns = [n for n in pm.InvoicePatch.model_fields if n in patch.model_fields_set]
    return [f"{n} = %s" for n in columns], [getattr(patch, n) for n in columns]


class InvoiceStore:
    """The invoice tables over a pool whose connections autocommit and return dict rows.
    ``shop_currency`` is read on each use; ``spools`` and ``types`` are what confirm
    (Task 6) writes through. Synchronous: callers run it in a thread."""

    def __init__(
        self,
        pool: ConnectionPool[Conn],
        shop_currency: Callable[[], str],
        spools: FilamentSpoolStore,
        types: SpoolTypeStore,
    ) -> None:
        self._pool = pool
        self._shop_currency = shop_currency
        self._spools = spools
        self._types = types

    # -- reads --------------------------------------------------------------------------

    def get(self, invoice_id: UUID) -> pm.InvoiceDetail | None:
        with self._pool.connection() as conn:
            return self._detail(conn, invoice_id)

    def file_key(self, invoice_id: UUID) -> tuple[str, pm.FileRef] | None:
        """The blob key and pointer of the invoice's file; None for no invoice or no file."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT file_key, file_name, file_media_type, file_size FROM filament_invoices"
                " WHERE id = %s AND file_key IS NOT NULL",
                (invoice_id,),
            ).fetchone()
        if row is None:
            return None
        ref = pm.FileRef(
            name=row["file_name"], media_type=row["file_media_type"], size=row["file_size"]
        )
        return row["file_key"], ref

    def list(self, *, supplier_id: UUID | None = None, status: str | None = None) -> _Invoices:
        where: list[str] = []
        args: list[Any] = []
        if supplier_id is not None:
            where.append("i.supplier_id = %s")
            args.append(supplier_id)
        if status is not None:
            where.append("i.status = %s")
            args.append(status)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_INVOICE_COLUMNS} FROM filament_invoices i{clause}"
                " ORDER BY i.invoice_date DESC, i.created_at DESC, i.id",
                args,
            ).fetchall()
            sums = self._sums(conn, [r["id"] for r in rows])
        return [self._invoice(row, sums.get(row["id"], (Decimal(0), 0))) for row in rows]

    @staticmethod
    def _sums(conn: Conn, ids: _Ids) -> dict[UUID, tuple[Decimal, int]]:
        if not ids:
            return {}
        rows = conn.execute(
            "SELECT invoice_id, sum(line_total) AS total, count(*)::int AS n"
            " FROM filament_invoice_lines WHERE invoice_id = ANY(%s) GROUP BY invoice_id",
            (ids,),
        ).fetchall()
        return {r["invoice_id"]: (r["total"], r["n"]) for r in rows}

    @staticmethod
    def _invoice(row: Row, sums: tuple[Decimal, int]) -> pm.Invoice:
        line_total, count = sums
        diff = difference(row["total"], [line_total], row["shipping"])
        fields = {k: row[k] for k in pm.Invoice.model_fields if k in row}
        fields.update(
            minor_exponent=minor_exponent(row["currency"]),
            difference=diff,
            reconciles=abs(diff) <= RECONCILE_TOLERANCE,
            line_count=count,
            file=(
                None
                if row["file_name"] is None
                else pm.FileRef(
                    name=row["file_name"],
                    media_type=row["file_media_type"],
                    size=row["file_size"],
                )
            ),
        )
        return pm.Invoice.model_validate(fields)

    def _detail(self, conn: Conn, invoice_id: UUID) -> pm.InvoiceDetail | None:
        row = conn.execute(
            f"SELECT {_INVOICE_COLUMNS} FROM filament_invoices i WHERE i.id = %s", (invoice_id,)
        ).fetchone()
        if row is None:
            return None
        line_rows = conn.execute(
            f"SELECT {_LINE_COLUMNS} FROM filament_invoice_lines l"
            " WHERE l.invoice_id = %s ORDER BY l.position",
            (invoice_id,),
        ).fetchall()
        spool_rows = conn.execute(
            "SELECT id, invoice_line_id FROM filament_spools"
            " WHERE invoice_line_id = ANY(%s) ORDER BY created_at, id",
            ([r["id"] for r in line_rows],),
        ).fetchall()
        spools: dict[UUID, _Ids] = {}
        for spool in spool_rows:
            spools.setdefault(spool["invoice_line_id"], []).append(spool["id"])
        lines = [
            pm.InvoiceLine.model_validate(
                {
                    **line,
                    "unit_cost": (line["allocated_cost"] / line["quantity"]).quantize(
                        _UNIT, rounding=ROUND_HALF_UP
                    ),
                    "spool_ids": spools.get(line["id"], []),
                }
            )
            for line in line_rows
        ]
        total = sum((line.line_total for line in lines), Decimal(0))
        head = self._invoice(row, (total, len(lines)))
        return pm.InvoiceDetail.model_validate({**head.model_dump(), "lines": lines})

    # -- writes -------------------------------------------------------------------------

    def create(self, body: pm.InvoiceCreate, *, actor: str) -> tuple[pm.InvoiceDetail, bool]:
        """``(invoice, made)``; ``made`` is false when ``import_run_id`` matched one.
        ``actor`` is for the events of Task 6's confirm: a draft records none."""
        run = body.import_run_id
        if run is not None:
            found = self._by_run(run)
            if found is not None:
                return found, False
        data = body.model_dump(exclude={"lines"})
        names = list(data)
        try:
            with self._pool.connection() as conn, conn.transaction():
                row = conn.execute(
                    f"INSERT INTO filament_invoices ({', '.join(names)})"
                    f" VALUES ({', '.join('%s' for _ in names)}) RETURNING id",
                    [data[n] for n in names],
                ).fetchone()
                assert row is not None
                invoice_id: UUID = row["id"]
                self._replace(conn, invoice_id, body.lines)
                made = self._detail(conn, invoice_id)
        except errors.UniqueViolation:
            # Postgres names the first index that fails, which may be the number's: a
            # retry of a numbered invoice meets both. The run id wins.
            if run is not None:
                raced = self._by_run(run)
                if raced is not None:
                    return raced, False
            taken = self._taken(body.supplier_id, body.number, None)
            if taken is None:
                raise
            raise DuplicateInvoiceError(taken) from None
        except errors.ForeignKeyViolation:
            raise InvoiceRefError() from None
        assert made is not None
        return made, True

    def _by_run(self, run: str) -> pm.InvoiceDetail | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT id FROM filament_invoices WHERE import_run_id = %s", (run,)
            ).fetchone()
            return None if row is None else self._detail(conn, row["id"])

    def _taken(self, supplier_id: UUID, number: str | None, own_id: UUID | None) -> UUID | None:
        """The id of the other invoice of ``supplier_id`` that has ``number``."""
        if number is None:
            return None
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT id FROM filament_invoices WHERE supplier_id = %s"
                " AND upper(number) = upper(%s) AND id IS DISTINCT FROM %s",
                (supplier_id, number, own_id),
            ).fetchone()
        return None if row is None else row["id"]

    def update(self, invoice_id: UUID, patch: pm.InvoicePatch) -> pm.InvoiceDetail | None:
        sets, values = _sets(patch)
        supplier_id: UUID | None = None
        try:
            with self._pool.connection() as conn, conn.transaction():
                head = self._lock(conn, invoice_id)
                if head is None:
                    return None
                supplier_id = head["supplier_id"]
                if head["status"] == "confirmed" and patch.model_fields_set - {"note"}:
                    raise InvoiceConfirmedError()
                conn.execute(
                    f"UPDATE filament_invoices SET {', '.join([*sets, 'updated_at = now()'])}"
                    " WHERE id = %s",
                    [*values, invoice_id],
                )
                if head["status"] == "draft":
                    self._recompute(conn, invoice_id)
                return self._detail(conn, invoice_id)
        except errors.UniqueViolation:
            taken = (
                None if supplier_id is None else self._taken(supplier_id, patch.number, invoice_id)
            )
            if taken is None:
                # Not the number index (the only unique index an update can break).
                raise
            raise DuplicateInvoiceError(taken) from None

    def replace_lines(
        self, invoice_id: UUID, lines: Sequence[pm.InvoiceLineIn]
    ) -> pm.InvoiceDetail | None:
        try:
            with self._pool.connection() as conn, conn.transaction():
                head = self._lock(conn, invoice_id)
                if head is None:
                    return None
                if head["status"] == "confirmed":
                    raise InvoiceConfirmedError()
                self._replace(conn, invoice_id, lines)
                return self._detail(conn, invoice_id)
        except errors.ForeignKeyViolation:
            raise InvoiceRefError() from None

    def delete(self, invoice_id: UUID) -> bool:
        with self._pool.connection() as conn, conn.transaction():
            head = self._lock(conn, invoice_id)
            if head is None:
                return False
            if head["status"] == "confirmed":
                raise InvoiceConfirmedError()
            conn.execute(
                "DELETE FROM blob_refs WHERE holder_kind = %s AND holder_id = %s",
                (_HOLDER, str(invoice_id)),
            )
            conn.execute("DELETE FROM filament_invoices WHERE id = %s", (invoice_id,))
        return True

    def set_file(
        self, invoice_id: UUID, *, key: str, name: str, media_type: str, size: int
    ) -> pm.FileRef | None:
        """Keep ``key`` as the invoice's file, replacing a previous one: the old hold goes
        and the new one comes in the same transaction. A confirmed invoice accepts a file
        (Decision 10). None for no such invoice."""
        with self._pool.connection() as conn, conn.transaction():
            if self._lock(conn, invoice_id) is None:
                return None
            conn.execute(
                "DELETE FROM blob_refs WHERE holder_kind = %s AND holder_id = %s",
                (_HOLDER, str(invoice_id)),
            )
            conn.execute(
                "INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES (%s, %s, %s)",
                (key, _HOLDER, str(invoice_id)),
            )
            conn.execute(
                "UPDATE filament_invoices SET file_key = %s, file_name = %s,"
                " file_media_type = %s, file_size = %s, updated_at = now() WHERE id = %s",
                (key, name, media_type, size, invoice_id),
            )
        return pm.FileRef(name=name, media_type=media_type, size=size)

    @staticmethod
    def _lock(conn: Conn, invoice_id: UUID) -> Row | None:
        return conn.execute(
            "SELECT id, supplier_id, status FROM filament_invoices WHERE id = %s FOR UPDATE",
            (invoice_id,),
        ).fetchone()

    @staticmethod
    def _replace(conn: Conn, invoice_id: UUID, lines: Sequence[pm.InvoiceLineIn]) -> None:
        conn.execute("DELETE FROM filament_invoice_lines WHERE invoice_id = %s", (invoice_id,))
        for position, line in enumerate(lines, start=1):
            subtotal = (
                line.line_subtotal
                if line.line_subtotal is not None
                else (line.quantity * line.unit_price).quantize(_UNIT, rounding=ROUND_HALF_UP)
            )
            given = line.line_tax is not None
            proposed = (
                None
                if line.proposed_type is None
                else Jsonb(line.proposed_type.model_dump(mode="json"))
            )
            # The tax and basis are placeholders until `_recompute` settles them: a given
            # tax is marked ``line`` so the recompute keeps it.
            conn.execute(
                "INSERT INTO filament_invoice_lines (invoice_id, position, description, sku,"
                " quantity, unit_price, line_subtotal, line_tax, line_total, allocated_cost,"
                " tax_basis, colour_code, type_id, supplier_product_id, proposed_type,"
                " update_spool_ids) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, 0, %s, %s, %s,"
                " %s, %s, %s::uuid[])",
                (
                    invoice_id,
                    position,
                    line.description,
                    line.sku,
                    line.quantity,
                    line.unit_price,
                    subtotal,
                    line.line_tax if given else Decimal(0),
                    "line" if given else "none",
                    line.colour_code,
                    line.type_id,
                    line.supplier_product_id,
                    proposed,
                    line.update_spool_ids,
                ),
            )
        InvoiceStore._recompute(conn, invoice_id)

    @staticmethod
    def _recompute(conn: Conn, invoice_id: UUID) -> None:
        """Tax, line totals and allocated cost of every line, from the header and the
        lines' subtotals; raises `MixedLineTaxError` or `InvoiceMathError`."""
        head = conn.execute(
            "SELECT shipping, tax FROM filament_invoices WHERE id = %s", (invoice_id,)
        ).fetchone()
        assert head is not None
        lines = conn.execute(
            "SELECT id, line_subtotal, line_tax, tax_basis FROM filament_invoice_lines"
            " WHERE invoice_id = %s ORDER BY position",
            (invoice_id,),
        ).fetchall()
        if not lines:
            return
        subtotals = [row["line_subtotal"] for row in lines]
        given = [row["line_tax"] if row["tax_basis"] == "line" else None for row in lines]
        taxes, basis = tax_per_line(subtotals, given, head["tax"])
        totals = [s + t for s, t in zip(subtotals, taxes, strict=True)]
        costs = allocate(totals, head["shipping"])
        for row, tax, total, cost in zip(lines, taxes, totals, costs, strict=True):
            conn.execute(
                "UPDATE filament_invoice_lines SET line_tax = %s, line_total = %s,"
                " allocated_cost = %s, tax_basis = %s, updated_at = now() WHERE id = %s",
                (tax, total, cost, basis, row["id"]),
            )
