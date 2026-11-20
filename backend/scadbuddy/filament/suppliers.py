"""Suppliers, their products and price points (#6266, epic #6260, spec §5.8).

A supplier sells products (one SKU for one spool type); a product has price points, one
per price a person or an invoice gave. A price is per pack, with tax; ``price_per_kg``
is derived on read from the pack size and the type's label weight, so a change to the
label weight reads into every point at once. No trigger keeps ``updated_at``: each write
here sets it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from psycopg import Connection, errors
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.filament import purchasing_models as pm
from scadbuddy.filament.bb_sync_models import BbLinked, forget_sync
from scadbuddy.filament.invoice_money import price_per_kg

Conn = Connection[DictRow]
Row = Mapping[str, Any]
_Suppliers = list[pm.Supplier]
_Stats = list[pm.SupplierStats]
_Products = list[pm.SupplierProduct]
_Prices = list[pm.PricePoint]
_TypePrices = list[pm.TypePricePoint]
_Sheet = list[pm.PriceSheetRow]
_Ids = list[UUID]

_SUPPLIER_COLUMNS = ", ".join(f"s.{n}" for n in pm.Supplier.model_fields if n != "product_count")
_PRODUCT_COLUMNS = ", ".join(
    f"p.{n}" for n in pm.SupplierProduct.model_fields if n != "newest_price"
)
_PRODUCT_COUNT = (
    " (SELECT count(*) FROM filament_supplier_products p"
    " WHERE p.supplier_id = s.id AND p.archived_at IS NULL)::int AS product_count"
)
#: A price point's columns, with what ``price_per_kg`` needs of its product and type.
_PRICE_COLUMNS = (
    "pp.id, pp.product_id, pp.price, pp.currency, pp.on_date, pp.source,"
    " pp.invoice_line_id, pp.created_at, pp.updated_at, p.pack_size, t.label_weight_g"
)
_PRICE_FROM = (
    " FROM filament_price_points pp"
    " JOIN filament_supplier_products p ON p.id = pp.product_id"
    " JOIN filament_spool_types t ON t.id = p.type_id"
)
_PRICE_SELECT = f"SELECT {_PRICE_COLUMNS}{_PRICE_FROM}"
_PRICE_ORDER = " ORDER BY pp.on_date DESC, pp.created_at DESC, pp.id"


class SupplierExistsError(ValueError):
    """An unarchived supplier already has the name."""

    def __init__(self) -> None:
        super().__init__("a supplier with that name exists")


class ProductExistsError(ValueError):
    """The supplier has an unarchived product with the SKU."""

    def __init__(self) -> None:
        super().__init__("the supplier has a product with that SKU")


class ProductRefError(ValueError):
    """A product names a spool type that does not exist."""

    def __init__(self) -> None:
        super().__init__("no such spool type")


class PriceFromInvoiceError(ValueError):
    """A price point an invoice made cannot be deleted."""

    def __init__(self) -> None:
        super().__init__("a price from an invoice goes with its invoice, not alone")


def _price(row: Row) -> pm.PricePoint:
    fields = {k: row[k] for k in pm.PricePoint.model_fields if k in row}
    fields["price_per_kg"] = price_per_kg(row["price"], row["pack_size"], row["label_weight_g"])
    return pm.PricePoint.model_validate(fields)


def _change_pct(newest: pm.PricePoint | None, previous: pm.PricePoint | None) -> Decimal | None:
    if (
        newest is None
        or previous is None
        or newest.price_per_kg is None
        or not previous.price_per_kg
    ):
        return None
    pct = (newest.price_per_kg - previous.price_per_kg) / previous.price_per_kg * 100
    return pct.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _sets(patch: Any) -> tuple[list[str], list[Any]]:
    """The ``SET`` clauses of the fields ``patch`` sends; ``archived`` becomes
    ``archived_at``, and ``updated_at`` is always set."""
    sent = [n for n in type(patch).model_fields if n in patch.model_fields_set]
    columns = [n for n in sent if n != "archived"]
    sets = [f"{n} = %s" for n in columns]
    values = [getattr(patch, n) for n in columns]
    if "archived" in sent:
        sets.append(
            "archived_at = COALESCE(archived_at, now())" if patch.archived else "archived_at = NULL"
        )
    sets.append("updated_at = now()")
    return sets, values


class SuppliersStore:
    """The supplier tables over a pool whose connections autocommit and return dict rows.
    ``shop_currency`` is read on each use. Synchronous: callers run it in a thread."""

    def __init__(self, pool: ConnectionPool[Conn], shop_currency: Callable[[], str]) -> None:
        self._pool = pool
        self._shop_currency = shop_currency

    # -- suppliers ----------------------------------------------------------------------

    def list(self, *, include_archived: bool = False) -> _Suppliers:
        where = "" if include_archived else " WHERE s.archived_at IS NULL"
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_SUPPLIER_COLUMNS},{_PRODUCT_COUNT}"
                f" FROM filament_suppliers s{where} ORDER BY upper(s.name), s.id"
            ).fetchall()
        return [pm.Supplier.model_validate(row) for row in rows]

    def get(self, supplier_id: UUID) -> pm.Supplier | None:
        with self._pool.connection() as conn:
            return self._supplier(conn, supplier_id)

    def create(self, body: pm.SupplierCreate) -> pm.Supplier:
        data = body.model_dump()
        names = list(data)
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    f"INSERT INTO filament_suppliers ({', '.join(names)})"
                    f" VALUES ({', '.join('%s' for _ in names)}) RETURNING id",
                    [data[n] for n in names],
                ).fetchone()
                assert row is not None
                made = self._supplier(conn, row["id"])
        except errors.UniqueViolation:
            raise SupplierExistsError() from None
        assert made is not None
        return made

    def update(self, supplier_id: UUID, patch: pm.SupplierPatch) -> pm.Supplier | None:
        sets, values = _sets(patch)
        try:
            with self._pool.connection() as conn, conn.transaction():
                row = conn.execute(
                    f"UPDATE filament_suppliers SET {', '.join(sets)} WHERE id = %s RETURNING id",
                    [*values, supplier_id],
                ).fetchone()
                if row is None:
                    return None
                if patch.archived:
                    forget_sync(conn, "supplier", supplier_id)
                return self._supplier(conn, supplier_id)
        except errors.UniqueViolation:
            raise SupplierExistsError() from None

    @staticmethod
    def link_bb_on(
        conn: Conn, bb_id: int, name: str, values: Mapping[str, Any]
    ) -> tuple[UUID, BbLinked] | None:
        """The supplier Bambuddy's supplier ``bb_id`` is, on ``conn`` (#6265 Task 9, the
        pull): the one linked to it (``found``), else the unlinked, unarchived one of the
        same upper case name, which takes the link (``linked``), else a new one with
        ``name`` and ``values`` (``made``). ``None`` when another unarchived supplier holds
        the name. A link is bookkeeping: it does not move ``updated_at``."""
        found = conn.execute(
            "SELECT id FROM filament_suppliers WHERE bb_supplier_id = %s", (bb_id,)
        ).fetchone()
        if found is not None:
            return found["id"], "found"
        linked = conn.execute(
            "UPDATE filament_suppliers SET bb_supplier_id = %s WHERE id = (SELECT id"
            " FROM filament_suppliers WHERE upper(name) = upper(%s) AND archived_at IS NULL"
            " AND bb_supplier_id IS NULL ORDER BY id LIMIT 1) RETURNING id",
            (bb_id, name),
        ).fetchone()
        if linked is not None:
            return linked["id"], "linked"
        if conn.execute(
            "SELECT 1 FROM filament_suppliers WHERE upper(name) = upper(%s)"
            " AND archived_at IS NULL",
            (name,),
        ).fetchone():
            return None
        columns = ["name", "bb_supplier_id", *values]
        made = conn.execute(
            f"INSERT INTO filament_suppliers ({', '.join(columns)})"
            f" VALUES ({', '.join('%s' for _ in columns)}) RETURNING id",
            [name, bb_id, *values.values()],
        ).fetchone()
        assert made is not None
        return made["id"], "made"

    @staticmethod
    def _supplier(conn: Conn, supplier_id: UUID) -> pm.Supplier | None:
        row = conn.execute(
            f"SELECT {_SUPPLIER_COLUMNS},{_PRODUCT_COUNT}"
            " FROM filament_suppliers s WHERE s.id = %s",
            (supplier_id,),
        ).fetchone()
        return None if row is None else pm.Supplier.model_validate(row)

    # -- products -----------------------------------------------------------------------

    def products(self, supplier_id: UUID, *, include_archived: bool = False) -> _Products | None:
        """The supplier's products by SKU; ``None`` for no such supplier."""
        where = "" if include_archived else " AND p.archived_at IS NULL"
        with self._pool.connection() as conn:
            if self._supplier(conn, supplier_id) is None:
                return None
            rows = conn.execute(
                f"SELECT {_PRODUCT_COLUMNS} FROM filament_supplier_products p"
                f" WHERE p.supplier_id = %s{where} ORDER BY upper(p.sku), p.id",
                (supplier_id,),
            ).fetchall()
            newest = self._newest(conn, [r["id"] for r in rows])
        return [
            pm.SupplierProduct.model_validate({**row, "newest_price": newest.get(row["id"])})
            for row in rows
        ]

    def product(self, product_id: UUID) -> pm.SupplierProduct | None:
        with self._pool.connection() as conn:
            return self._product(conn, product_id)

    def add_product(self, supplier_id: UUID, body: pm.ProductCreate) -> pm.SupplierProduct | None:
        data = {"supplier_id": supplier_id, **body.model_dump()}
        names = list(data)
        try:
            with self._pool.connection() as conn:
                if self._supplier(conn, supplier_id) is None:
                    return None
                row = conn.execute(
                    f"INSERT INTO filament_supplier_products ({', '.join(names)})"
                    f" VALUES ({', '.join('%s' for _ in names)}) RETURNING id",
                    [data[n] for n in names],
                ).fetchone()
                assert row is not None
                return self._product(conn, row["id"])
        except errors.UniqueViolation:
            raise ProductExistsError() from None
        except errors.ForeignKeyViolation:
            raise ProductRefError() from None

    def update_product(self, product_id: UUID, patch: pm.ProductPatch) -> pm.SupplierProduct | None:
        sets, values = _sets(patch)
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    f"UPDATE filament_supplier_products SET {', '.join(sets)}"
                    " WHERE id = %s RETURNING id",
                    [*values, product_id],
                ).fetchone()
                if row is None:
                    return None
                return self._product(conn, product_id)
        except errors.UniqueViolation:
            raise ProductExistsError() from None
        except errors.ForeignKeyViolation:
            raise ProductRefError() from None

    def _product(self, conn: Conn, product_id: UUID) -> pm.SupplierProduct | None:
        row = conn.execute(
            f"SELECT {_PRODUCT_COLUMNS} FROM filament_supplier_products p WHERE p.id = %s",
            (product_id,),
        ).fetchone()
        if row is None:
            return None
        newest = self._newest(conn, [product_id])
        return pm.SupplierProduct.model_validate({**row, "newest_price": newest.get(product_id)})

    @staticmethod
    def _newest(conn: Conn, product_ids: _Ids) -> dict[UUID, pm.PricePoint]:
        if not product_ids:
            return {}
        rows = conn.execute(
            f"SELECT DISTINCT ON (pp.product_id) {_PRICE_COLUMNS}{_PRICE_FROM}"
            " WHERE pp.product_id = ANY(%s)"
            " ORDER BY pp.product_id, pp.on_date DESC, pp.created_at DESC, pp.id",
            (product_ids,),
        ).fetchall()
        return {row["product_id"]: _price(row) for row in rows}

    # -- stats --------------------------------------------------------------------------

    def stats(self) -> _Stats:
        """Per supplier with a confirmed invoice: the spools its invoices made, the grams
        they used and what the invoices cost. Archived suppliers are listed, marked
        ``archived``; ``spool_count`` counts every spool bought, archived too, and
        ``remaining_g`` the grams left of the unarchived ones only. Used grams follow the
        ledger's rule (`ledger.basis`): a ``print_actual`` delta, and a ``print_estimate``
        one only while no ``print_ended`` event has its ref. Sorted by grams used, most
        first, then by name."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT s.id AS supplier_id, s.name AS supplier_name,"
                " s.archived_at IS NOT NULL AS archived,"
                " (SELECT count(*) FROM filament_spools sp"
                "  JOIN filament_invoice_lines l ON l.id = sp.invoice_line_id"
                "  JOIN filament_invoices i ON i.id = l.invoice_id"
                "  WHERE i.supplier_id = s.id AND i.status = 'confirmed')::int AS spool_count,"
                " COALESCE((SELECT sum(sp.grams_left) FROM filament_spools sp"
                "  JOIN filament_invoice_lines l ON l.id = sp.invoice_line_id"
                "  JOIN filament_invoices i ON i.id = l.invoice_id"
                "  WHERE i.supplier_id = s.id AND i.status = 'confirmed'"
                "  AND sp.archived_at IS NULL), 0)::float8 AS remaining_g,"
                " COALESCE((SELECT sum(-e.delta_g) FROM filament_spool_events e"
                "  JOIN filament_spools sp ON sp.id = e.spool_id"
                "  JOIN filament_invoice_lines l ON l.id = sp.invoice_line_id"
                "  JOIN filament_invoices i ON i.id = l.invoice_id"
                "  WHERE i.supplier_id = s.id AND i.status = 'confirmed'"
                "  AND e.delta_g IS NOT NULL AND (e.source = 'print_actual'"
                "   OR (e.source = 'print_estimate' AND NOT EXISTS"
                "    (SELECT 1 FROM filament_spool_events d WHERE d.spool_id = e.spool_id"
                "     AND d.kind = 'print_ended' AND d.ref_id IS NOT NULL"
                "     AND d.ref_id = e.ref_id AND d.ref_kind IS NOT DISTINCT FROM e.ref_kind)))"
                "  ), 0)::float8 AS consumed_g,"
                " (SELECT sum(l.allocated_cost) FROM filament_invoice_lines l"
                "  JOIN filament_invoices i ON i.id = l.invoice_id"
                "  WHERE i.supplier_id = s.id AND i.status = 'confirmed') AS spend"
                " FROM filament_suppliers s WHERE EXISTS (SELECT 1 FROM filament_invoices i"
                "  WHERE i.supplier_id = s.id AND i.status = 'confirmed')"
                " ORDER BY consumed_g DESC, upper(s.name), s.id"
            ).fetchall()
        return [pm.SupplierStats.model_validate(row) for row in rows]

    # -- price points -------------------------------------------------------------------

    def prices(self, product_id: UUID) -> _Prices:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"{_PRICE_SELECT} WHERE pp.product_id = %s{_PRICE_ORDER}", (product_id,)
            ).fetchall()
        return [_price(row) for row in rows]

    def add_price(self, product_id: UUID, body: pm.PricePointCreate) -> pm.PricePoint | None:
        # The shop's currency is a settings read on a connection of its own: take it first.
        currency = body.currency or self._shop_currency()
        on_date = body.on_date or datetime.now(UTC).date()
        with self._pool.connection() as conn:
            if self._product(conn, product_id) is None:
                return None
            row = conn.execute(
                "INSERT INTO filament_price_points (product_id, price, currency, on_date, source)"
                " VALUES (%s, %s, %s, %s, 'manual') RETURNING id",
                (product_id, body.price, currency, on_date),
            ).fetchone()
            assert row is not None
            made = conn.execute(f"{_PRICE_SELECT} WHERE pp.id = %s", (row["id"],)).fetchone()
        assert made is not None
        return _price(made)

    def delete_price(self, price_id: UUID) -> bool:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT source FROM filament_price_points WHERE id = %s", (price_id,)
            ).fetchone()
            if row is None:
                return False
            if row["source"] == "invoice":
                raise PriceFromInvoiceError()
            deleted = conn.execute(
                "DELETE FROM filament_price_points WHERE id = %s AND source = 'manual'",
                (price_id,),
            )
        return deleted.rowcount > 0

    def type_prices(self, type_id: UUID) -> _TypePrices:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_PRICE_COLUMNS}, p.supplier_id, s.name AS supplier_name, p.sku,"
                " s.archived_at IS NOT NULL AS supplier_archived,"
                " p.archived_at IS NOT NULL AS product_archived"
                f"{_PRICE_FROM}"
                " JOIN filament_suppliers s ON s.id = p.supplier_id"
                f" WHERE p.type_id = %s{_PRICE_ORDER}",
                (type_id,),
            ).fetchall()
        return [
            pm.TypePricePoint.model_validate(
                {
                    **_price(row).model_dump(),
                    "supplier_id": row["supplier_id"],
                    "supplier_name": row["supplier_name"],
                    "sku": row["sku"],
                    "supplier_archived": row["supplier_archived"],
                    "product_archived": row["product_archived"],
                }
            )
            for row in rows
        ]

    def price_sheet(self, supplier_id: UUID) -> _Sheet | None:
        """One row per unarchived product, by type code; ``None`` for no such supplier."""
        with self._pool.connection() as conn:
            if self._supplier(conn, supplier_id) is None:
                return None
            products = conn.execute(
                f"SELECT {_PRODUCT_COLUMNS}, t.code AS type_code,"
                " COALESCE(t.colour_name, t.code) AS type_label"
                " FROM filament_supplier_products p"
                " JOIN filament_spool_types t ON t.id = p.type_id"
                " WHERE p.supplier_id = %s AND p.archived_at IS NULL"
                " ORDER BY upper(t.code), upper(p.sku), p.id",
                (supplier_id,),
            ).fetchall()
            points = conn.execute(
                f"{_PRICE_SELECT} WHERE p.supplier_id = %s AND p.archived_at IS NULL{_PRICE_ORDER}",
                (supplier_id,),
            ).fetchall()
        by_product: dict[UUID, _Prices] = {}
        for row in points:
            by_product.setdefault(row["product_id"], []).append(_price(row))
        sheet: _Sheet = []
        for row in products:
            own = by_product.get(row["id"], [])
            newest = own[0] if own else None
            previous = own[1] if len(own) > 1 else None
            sheet.append(
                pm.PriceSheetRow(
                    product=pm.SupplierProduct.model_validate({**row, "newest_price": newest}),
                    type_code=row["type_code"],
                    type_label=row["type_label"],
                    newest=newest,
                    previous=previous,
                    change_pct=_change_pct(newest, previous),
                )
            )
        return sheet
