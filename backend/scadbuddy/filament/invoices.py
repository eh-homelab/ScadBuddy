"""Invoice drafts (#6266, epic #6260, spec §5.8): a header and its lines.

A draft is edited freely; every write to one ends in `_recompute`, which spreads the
receipt's tax and shipping over the lines (`invoice_money`) in the same transaction, so a
reader never sees a line total that disagrees with the header. A confirmed invoice is
frozen but for its note and its file. No trigger keeps ``updated_at``: each
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
from pydantic import ValidationError

from scadbuddy.filament import models as m
from scadbuddy.filament import purchasing_models as pm
from scadbuddy.filament.catalog import (
    CatalogExistsError,
    CatalogKeyEmptyError,
    TypeTempError,
    find_by_key,
)
from scadbuddy.filament.invoice_money import (
    RECONCILE_TOLERANCE,
    allocate,
    difference,
    price_per_pack,
    spool_cost_per_kg,
    tax_per_line,
    unit_cost,
)
from scadbuddy.filament.spools import FilamentSpoolStore, SpoolInUseError, SpoolTypelessError
from scadbuddy.filament.types import SpoolTypeStore
from scadbuddy.orders.currency import minor_exponent

Conn = Connection[DictRow]
Row = Mapping[str, Any]
_Ids = list[UUID]
_Invoices = list[pm.Invoice]
_Candidates = list[pm.LineCandidates]
_Claimed = list[tuple[UUID, int | None, Decimal | None, bool]]
_Made = dict[str, tuple[UUID, int]]
_SKU_MAX = 128
_LINE_BOUNDS = "a line is out of range: its spool list, its total or the invoice total"
#: What a line's spool or type writes can be refused with; each is a 409 naming the line.
_WRITE_REFUSALS = (
    errors.IntegrityError,
    errors.DataError,
    SpoolTypelessError,
    SpoolInUseError,
    ValidationError,
)

_UNIT = Decimal("0.0001")
_COMPUTED = {"minor_exponent", "difference", "reconciles", "line_count", "spool_count", "file"}
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

    def __init__(self, reason: str = "no such supplier, spool type or supplier product") -> None:
        super().__init__(reason)


class DuplicateInvoiceError(ValueError):
    """The supplier already has an invoice with the number."""

    def __init__(self, existing_id: UUID) -> None:
        super().__init__("the supplier has an invoice with that number")
        self.existing_id = existing_id


class ImportRunChangedError(ValueError):
    """The write named the import run it read, and another run holds the invoice now."""

    def __init__(self, message: str = "the invoice's import run changed since it was read") -> None:
        super().__init__(message)


class InvoiceConfirmedError(ValueError):
    """A confirmed invoice is frozen."""

    def __init__(self) -> None:
        super().__init__("a confirmed invoice cannot be changed; edit its spools instead")


class InvoiceNoLinesError(ValueError):
    """An invoice with no lines has nothing to confirm."""

    def __init__(self) -> None:
        super().__init__("an invoice with no lines cannot be confirmed")


class InvoiceCurrencyError(ValueError):
    """Only an invoice in the shop's currency is confirmed (Decision 6)."""

    def __init__(self, currency: str, shop: str) -> None:
        super().__init__(
            f"the invoice is in {currency} but the shop's currency is {shop}: "
            "change the shop's currency or retype the invoice"
        )


class InvoiceSupplierArchivedError(ValueError):
    """An archived supplier gets no new products or price points from a confirm."""

    def __init__(self) -> None:
        super().__init__("the supplier is archived: restore it or move the invoice to another")


class InvoiceNotReconciledError(ValueError):
    """The total is further from the lines and shipping than the tolerance (Decision 7)."""

    def __init__(self, diff: Decimal) -> None:
        super().__init__(
            f"the total is {diff} away from the lines plus shipping; "
            f"confirm needs it within {RECONCILE_TOLERANCE}"
        )
        self.difference = diff


class InvoiceLineError(ValueError):
    """A line cannot be confirmed: nothing is written."""

    def __init__(self, position: int, reason: str) -> None:
        super().__init__(f"line {position}: {reason}")
        self.position = position
        self.reason = reason


_PRODUCT_COLUMNS = "id, type_id, pack_size"
_REF_KIND = "invoice_line"


def _sets(patch: pm.InvoicePatch) -> tuple[list[str], list[Any]]:
    columns = [
        n
        for n in pm.InvoicePatch.model_fields
        if n in patch.model_fields_set and n != "if_import_run_id"
    ]
    return [f"{n} = %s" for n in columns], [getattr(patch, n) for n in columns]


class InvoiceStore:
    """The invoice tables over a pool whose connections autocommit and return dict rows.
    ``shop_currency`` is read on each use; ``spools`` and ``types`` are what confirm
    writes through. Synchronous: callers run it in a thread."""

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
        return [self._invoice(row, sums.get(row["id"], (Decimal(0), 0, 0))) for row in rows]

    @staticmethod
    def _sums(conn: Conn, ids: _Ids) -> dict[UUID, tuple[Decimal, int, int]]:
        """Per invoice: its lines' total, its line count and the spools its lines made or
        filled (archived ones too, as the supplier stats count them)."""
        if not ids:
            return {}
        rows = conn.execute(
            "SELECT l.invoice_id, sum(l.line_total) AS total, count(*)::int AS n,"
            " (SELECT count(*)::int FROM filament_spools s WHERE s.invoice_line_id IN"
            "  (SELECT id FROM filament_invoice_lines WHERE invoice_id = l.invoice_id))"
            " AS spools"
            " FROM filament_invoice_lines l WHERE l.invoice_id = ANY(%s) GROUP BY l.invoice_id",
            (ids,),
        ).fetchall()
        return {r["invoice_id"]: (r["total"], r["n"], r["spools"]) for r in rows}

    @staticmethod
    def _invoice(row: Row, sums: tuple[Decimal, int, int]) -> pm.Invoice:
        line_total, count, spools = sums
        diff = difference(row["total"], [line_total], row["shipping"])
        fields = {k: row[k] for k in pm.Invoice.model_fields if k in row}
        fields.update(
            minor_exponent=minor_exponent(row["currency"]),
            difference=diff,
            reconciles=abs(diff) <= RECONCILE_TOLERANCE,
            line_count=count,
            spool_count=spools,
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

    def candidates(self, invoice_id: UUID) -> _Candidates | None:
        """Per line, the spools that match its type, or its proposed type, by effective
        material and exact RGBA, split by whether the spool's own purchase is known: an own
        cost or an invoice line. A type's cost is a default, not a purchase. A costed spool
        names its invoice line, if it has one: one without may be filled, and the line's cost
        replaces its own (#6700). An archived spool is listed too, marked: a used-up spool
        was still bought (#6707). A line with neither type has none. None for no such invoice
        (#6267)."""
        invoice = self.get(invoice_id)
        if invoice is None:
            return None
        spools = self._spools.list(include_archived=True)
        found: _Candidates = []
        with self._pool.connection() as conn:
            keys = [self._line_key(conn, line) for line in invoice.lines]
        for line, key in zip(invoice.lines, keys, strict=True):
            matching = [
                (
                    s.cost_per_kg is not None or s.invoice_line_id is not None,
                    pm.SpoolBrief(
                        id=s.id,
                        number=s.number,
                        type_id=s.type_id,
                        colour_name=s.effective.colour_name,
                        rgba=s.effective.rgba,
                        cost_per_kg=s.effective.cost_per_kg,
                        grams_left=s.grams_left,
                        invoice_line_id=s.invoice_line_id,
                        archived=s.archived_at is not None,
                    ),
                )
                for s in spools
                if key is not None and (s.effective.material_id, s.effective.rgba) == key
            ]
            found.append(
                pm.LineCandidates(
                    position=line.position,
                    uncosted=[b for known, b in matching if not known],
                    costed=[b for known, b in matching if known],
                )
            )
        return found

    def _line_key(self, conn: Conn, line: pm.InvoiceLine) -> tuple[UUID, str] | None:
        if line.type_id is not None:
            kind = self._types.get(line.type_id)
            return None if kind is None else (kind.material_id, kind.rgba)
        proposed = line.proposed_type
        if proposed is None:
            return None
        material = proposed.material_id
        if material is None and proposed.material_name is not None:
            # A proposed type that names its material (#8228): that material, if it exists.
            material = find_by_key(conn, "materials", proposed.material_name)
        return None if material is None else (material, proposed.rgba)

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
                    "unit_cost": unit_cost(line["allocated_cost"], line["quantity"]),
                    "spool_ids": spools.get(line["id"], []),
                }
            )
            for line in line_rows
        ]
        total = sum((line.line_total for line in lines), Decimal(0))
        head = self._invoice(row, (total, len(lines), sum(len(ids) for ids in spools.values())))
        return pm.InvoiceDetail.model_validate({**head.model_dump(), "lines": lines})

    # -- writes -------------------------------------------------------------------------

    def create(self, body: pm.InvoiceCreate, *, actor: str) -> tuple[pm.InvoiceDetail, bool]:
        """``(invoice, made)``; ``made`` is false when ``import_run_id`` matched one.
        ``actor`` is unused: a draft records no event (confirm's carry ``invoice:<id>``).
        A supplier that is missing or archived is an `InvoiceRefError`."""
        run = body.import_run_id
        if run is not None:
            found = self._by_run(run)
            if found is not None:
                return found, False
        data = body.model_dump(exclude={"lines"})
        names = list(data)
        try:
            with self._pool.connection() as conn, conn.transaction():
                self._live_supplier(conn, body.supplier_id)
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
        except (errors.CheckViolation, errors.DataError) as exc:
            raise InvoiceRefError(_LINE_BOUNDS) from exc
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
                if (
                    "if_import_run_id" in patch.model_fields_set
                    and head["import_run_id"] != patch.if_import_run_id
                ):
                    raise ImportRunChangedError()
                if not sets:
                    return self._detail(conn, invoice_id)
                if patch.supplier_id is not None and patch.supplier_id != supplier_id:
                    supplier_id = patch.supplier_id
                    self._move(conn, invoice_id, supplier_id)
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
                None
                if supplier_id is None
                else self._taken(supplier_id, self._number(patch, invoice_id), invoice_id)
            )
            if taken is not None:
                raise DuplicateInvoiceError(taken) from None
            if patch.import_run_id is not None:
                # The import-run index: another invoice already holds that run.
                raise ImportRunChangedError(
                    "another invoice already holds that import run"
                ) from None
            raise
        except errors.ForeignKeyViolation:
            raise InvoiceRefError() from None
        except (errors.CheckViolation, errors.DataError) as exc:
            # The recompute writes each line's total and allocated cost from the new tax
            # and shipping; a sum past the columns' range is the same 422 as a line's.
            raise InvoiceRefError(_LINE_BOUNDS) from exc

    def _number(self, patch: pm.InvoicePatch, invoice_id: UUID) -> str | None:
        """The number the invoice has after ``patch``: the patch's, else the one it kept."""
        if "number" in patch.model_fields_set:
            return patch.number
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT number FROM filament_invoices WHERE id = %s", (invoice_id,)
            ).fetchone()
        return None if row is None else row["number"]

    @staticmethod
    def _live_supplier(conn: Conn, supplier_id: UUID) -> None:
        """The supplier must be there and not archived (`InvoiceRefError`)."""
        row = conn.execute(
            "SELECT archived_at FROM filament_suppliers WHERE id = %s", (supplier_id,)
        ).fetchone()
        if row is None:
            raise InvoiceRefError()
        if row["archived_at"] is not None:
            raise InvoiceRefError("the supplier is archived: restore it or pick another")

    @staticmethod
    def _move(conn: Conn, invoice_id: UUID, supplier_id: UUID) -> None:
        """A draft's lines name the old supplier's products: they name none after the move
        (confirm matches by SKU). The supplier must be there and not archived."""
        InvoiceStore._live_supplier(conn, supplier_id)
        conn.execute(
            "UPDATE filament_invoice_lines SET supplier_product_id = NULL WHERE invoice_id = %s",
            (invoice_id,),
        )

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
        except (errors.CheckViolation, errors.DataError) as exc:
            raise InvoiceRefError(_LINE_BOUNDS) from exc

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

    # -- confirm ------------------------------------------------------------------------

    def confirm(
        self, invoice_id: UUID, *, actor: str
    ) -> tuple[pm.InvoiceDetail, bool, tuple[UUID, ...]] | None:
        """Turn a draft into types, supplier products, spools with their cost, and price
        points, in one transaction (Decisions 3, 4, 9, 11-13). ``(invoice, confirmed_now,
        new_type_ids)``: the ids of the types this confirm made from proposed types
        (#6625: the caller starts their preset fetches); ``confirmed_now`` is false, and
        the ids empty, when it was already confirmed (nothing more is made); ``None`` for no such
        invoice. A refusal is a typed `ValueError` and nothing is written. The events carry
        `invoice:<id>` as the actor, except the `edited` event of a filled spool whose own
        cost the line replaces: that one carries `actor` (#6700, R-6700-2)."""
        shop = self._shop_currency()
        with self._pool.connection() as conn, conn.transaction():
            head = conn.execute(
                "SELECT id, supplier_id, status, currency, invoice_date, subtotal, shipping,"
                " total FROM filament_invoices WHERE id = %s FOR UPDATE",
                (invoice_id,),
            ).fetchone()
            if head is None:
                return None
            if head["status"] == "confirmed":
                return self._detail_of(conn, invoice_id), False, ()
            lines = conn.execute(
                "SELECT id, position, description, sku, quantity, line_total, allocated_cost,"
                " type_id, supplier_product_id, proposed_type, update_spool_ids"
                " FROM filament_invoice_lines WHERE invoice_id = %s ORDER BY position",
                (invoice_id,),
            ).fetchall()
            if not lines:
                raise InvoiceNoLinesError()
            archived = conn.execute(
                "SELECT archived_at FROM filament_suppliers WHERE id = %s", (head["supplier_id"],)
            ).fetchone()
            if archived is not None and archived["archived_at"] is not None:
                raise InvoiceSupplierArchivedError()
            if head["currency"] != shop:
                raise InvoiceCurrencyError(head["currency"], shop)
            diff = difference(head["total"], [row["line_total"] for row in lines], head["shipping"])
            if abs(diff) > RECONCILE_TOLERANCE:
                raise InvoiceNotReconciledError(diff)
            # One sorted lock over every line's spools, so two confirms that claim the
            # same spools in a different line order cannot deadlock.
            claimed = sorted({i for line in lines for i in line["update_spool_ids"]})
            if claimed:
                conn.execute(
                    "SELECT id FROM filament_spools WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                    (claimed,),
                )
            who = f"invoice:{invoice_id}"
            made: _Made = {}
            for line in lines:
                self._confirm_line(conn, head, line, who, made, actor=actor)
            conn.execute(
                "UPDATE filament_invoices SET status = 'confirmed', confirmed_at = now(),"
                " updated_at = now() WHERE id = %s",
                (invoice_id,),
            )
            made_ids = tuple(made_id for made_id, _ in made.values())
            return self._detail_of(conn, invoice_id), True, made_ids

    def _detail_of(self, conn: Conn, invoice_id: UUID) -> pm.InvoiceDetail:
        found = self._detail(conn, invoice_id)
        assert found is not None
        return found

    def _confirm_line(
        self, conn: Conn, head: Row, line: Row, who: str, made: _Made, *, actor: str
    ) -> None:
        position: int = line["position"]
        type_id, label_g = self._line_type(conn, line, made)
        product_id, pack_size = self._line_product(conn, head, line, type_id)
        known = self._claimed(conn, line, type_id)
        touched: _Ids = []
        try:
            for spool_id, own_label_g, own_cost, archived in known:
                grams = own_label_g or label_g
                cost = spool_cost_per_kg(line["allocated_cost"], line["quantity"], grams)
                # An archived spool takes its cost and line only, and stays archived (#6707).
                patch = (
                    m.SpoolPatch(cost_per_kg=cost)
                    if archived
                    else m.SpoolPatch(type_id=type_id, cost_per_kg=cost)
                )
                # A spool with its own cost is replaced at the person's word (#6700), so its
                # `edited` event, which records the old and new cost, is theirs.
                self._spools.update_on(
                    conn,
                    spool_id,
                    patch,
                    actor=who if own_cost is None else actor,
                    ref_kind=_REF_KIND,
                    ref_id=line["id"],
                    invoice_line_id=line["id"],
                )
                touched.append(spool_id)
            cost = spool_cost_per_kg(line["allocated_cost"], line["quantity"], label_g)
            for _ in range(line["quantity"] - len(known)):
                touched.append(
                    self._spools.create_on(
                        conn,
                        m.SpoolCreate(type_id=type_id, cost_per_kg=cost, grams_left=label_g),
                        actor=who,
                        ref_kind=_REF_KIND,
                        ref_id=line["id"],
                    )
                )
        except _WRITE_REFUSALS as exc:
            raise InvoiceLineError(position, f"its spools cannot be written ({exc})") from exc
        conn.execute(
            "UPDATE filament_spools SET invoice_line_id = %s, updated_at = now()"
            " WHERE id = ANY(%s)",
            (line["id"], touched),
        )
        try:
            with conn.transaction():
                conn.execute(
                    "INSERT INTO filament_price_points (product_id, price, currency, on_date,"
                    " source, invoice_line_id) VALUES (%s, %s, %s, %s, 'invoice', %s)",
                    (
                        product_id,
                        price_per_pack(line["line_total"], line["quantity"], pack_size),
                        head["currency"],
                        head["invoice_date"],
                        line["id"],
                    ),
                )
        except _WRITE_REFUSALS as exc:
            raise InvoiceLineError(position, f"its price point cannot be written ({exc})") from exc
        conn.execute(
            "UPDATE filament_invoice_lines SET type_id = %s, supplier_product_id = %s,"
            " updated_at = now() WHERE id = %s",
            (type_id, product_id, line["id"]),
        )

    def _line_type(self, conn: Conn, line: Row, made: _Made) -> tuple[UUID, int]:
        """The line's type id and its label weight in grams; a ``proposed_type`` is made
        here when the line names no type. A code an earlier line of this confirm made is that
        line's type (``made``)."""
        position: int = line["position"]
        if line["type_id"] is not None:
            row = conn.execute(
                "SELECT id, label_weight_g FROM filament_spool_types WHERE id = %s",
                (line["type_id"],),
            ).fetchone()
            if row is None:
                raise InvoiceLineError(position, "its spool type no longer exists")
            return row["id"], row["label_weight_g"]
        if line["proposed_type"] is None:
            raise InvoiceLineError(position, "it has no spool type and proposes none")
        try:
            body = m.SpoolTypeCreate.model_validate(line["proposed_type"])
        except ValidationError as exc:
            raise InvoiceLineError(position, f"its proposed spool type is invalid ({exc})") from exc
        key = body.code.upper()
        if key in made:
            return made[key]
        try:
            # A savepoint: the failed insert must not poison the outer transaction's
            # error, though the caller rolls back whatever follows.
            with conn.transaction():
                created = self._types.create_on(conn, body)
        except errors.UniqueViolation as exc:
            raise InvoiceLineError(
                position, f"a spool type like '{body.code}' already exists; choose it instead"
            ) from exc
        except errors.ForeignKeyViolation as exc:
            raise InvoiceLineError(
                position, "its proposed spool type names a catalog row that does not exist"
            ) from exc
        except (
            errors.IntegrityError,
            errors.DataError,
            TypeTempError,
            CatalogKeyEmptyError,
            CatalogExistsError,
        ) as exc:
            raise InvoiceLineError(position, f"its proposed spool type is invalid ({exc})") from exc
        made[key] = (created.id, created.label_weight_g)
        return made[key]

    @staticmethod
    def _line_product(conn: Conn, head: Row, line: Row, type_id: UUID) -> tuple[UUID, int]:
        """The supplier product the line prices: the one it names, else the supplier's by
        ``upper(sku)`` (the line's ``sku``, else its ``description``), else a new one."""
        position: int = line["position"]
        if line["supplier_product_id"] is not None:
            found = conn.execute(
                f"SELECT {_PRODUCT_COLUMNS} FROM filament_supplier_products"
                " WHERE id = %s AND supplier_id = %s AND archived_at IS NULL",
                (line["supplier_product_id"], head["supplier_id"]),
            ).fetchone()
            if found is None:
                raise InvoiceLineError(
                    position, "its supplier product is archived or not this supplier's"
                )
        else:
            sku = line["sku"] or line["description"]
            if len(sku) > _SKU_MAX:
                raise InvoiceLineError(
                    position,
                    f"it has no SKU and its description is over {_SKU_MAX} characters: set a SKU",
                )
            find = (
                f"SELECT {_PRODUCT_COLUMNS} FROM filament_supplier_products"
                " WHERE supplier_id = %s AND upper(sku) = upper(%s) AND archived_at IS NULL"
            )
            found = conn.execute(find, (head["supplier_id"], sku)).fetchone()
            if found is None:
                found = conn.execute(
                    "INSERT INTO filament_supplier_products (supplier_id, type_id, sku)"
                    " VALUES (%s, %s, %s) ON CONFLICT (supplier_id, upper(sku))"
                    f" WHERE archived_at IS NULL DO NOTHING RETURNING {_PRODUCT_COLUMNS}",
                    (head["supplier_id"], type_id, sku),
                ).fetchone()
            if found is None:
                # A concurrent confirm made the same SKU first: read its row.
                found = conn.execute(find, (head["supplier_id"], sku)).fetchone()
        if found is None:
            raise InvoiceLineError(position, "the product changed while confirming; try again")
        if found["type_id"] != type_id:
            raise InvoiceLineError(position, "its supplier product is for another spool type")
        return found["id"], found["pack_size"]

    @staticmethod
    def _claimed(conn: Conn, line: Row, type_id: UUID) -> _Claimed:
        """The spools the line fills in, each with its own label weight and its own cost, if
        it has them, and whether it is archived; every one is checked first (Decision 11). A
        spool with its own cost but no invoice line is filled, and the line's cost replaces
        its own (#6700, R-6700-1). An archived (used-up) spool may be filled too (#6707)."""
        position: int = line["position"]
        ids: _Ids = line["update_spool_ids"]
        if not ids:
            return []
        if len(ids) > line["quantity"]:
            raise InvoiceLineError(
                position, f"it fills {len(ids)} spools but its quantity is {line['quantity']}"
            )
        if len(set(ids)) != len(ids):
            raise InvoiceLineError(position, "it names a spool twice")
        rows = {
            row["id"]: row
            for row in conn.execute(
                "SELECT id, archived_at, invoice_line_id, cost_per_kg, type_id, label_weight_g"
                " FROM filament_spools WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                (ids,),
            ).fetchall()
        }
        known: _Claimed = []
        for spool_id in ids:
            row = rows.get(spool_id)
            if row is None:
                reason = "does not exist"
            elif row["invoice_line_id"] is not None:
                reason = "already belongs to an invoice line"
            elif row["type_id"] is not None and row["type_id"] != type_id:
                reason = "has another spool type"
            else:
                known.append(
                    (
                        spool_id,
                        row["label_weight_g"],
                        row["cost_per_kg"],
                        row["archived_at"] is not None,
                    )
                )
                continue
            raise InvoiceLineError(position, f"spool {spool_id} {reason}")
        return known

    @staticmethod
    def _lock(conn: Conn, invoice_id: UUID) -> Row | None:
        return conn.execute(
            "SELECT id, supplier_id, status, import_run_id FROM filament_invoices"
            " WHERE id = %s FOR UPDATE",
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
