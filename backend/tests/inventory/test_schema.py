"""The inventory core tables (#7067, spec 6.4-6.6): the built-in kinds the migration
seeds (Ruling 1), and the CHECKs and unique indexes that keep moves and offers whole."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from scadbuddy.entities.properties import check_schema
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

#: id: (parent, tracking, uom, life_basis, saleable), as the plan's Task 1 table.
BUILTIN_KINDS: dict[str, tuple[str | None, str, str, str, bool]] = {
    "filament": (None, "filament", "g", "none", False),
    "printer": (None, "unit", "each", "hours", False),
    "part": (None, "lot", "each", "none", False),
    "nozzle": ("part", "unit", "each", "hours", False),
    "hotend": ("part", "unit", "each", "hours", False),
    "belt": ("part", "unit", "each", "hours", False),
    "build_plate": ("part", "unit", "each", "months", False),
    "ams_part": ("part", "lot", "each", "none", False),
    "hardware": ("part", "lot", "each", "none", False),
    "consumable": (None, "lot", "each", "none", False),
    "desiccant": ("consumable", "lot", "g", "none", False),
    "packaging": ("consumable", "lot", "each", "none", False),
    "lubricant": ("consumable", "lot", "ml", "none", False),
    "equipment": (None, "unit", "each", "months", False),
    "dry_bin": ("equipment", "unit", "each", "months", False),
    "tool": ("equipment", "unit", "each", "months", False),
    "printed_part": (None, "lot", "each", "none", True),
    "assembly": (None, "lot", "each", "none", True),
    "service": (None, "none", "each", "none", False),
    "spool_part": (None, "unit", "each", "none", False),
}

ACTOR = Jsonb({"kind": "user", "id": "test"})


def _item(pool: PgPool, name: str, kind: str = "hardware") -> str:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO items (kind_id, name) VALUES (%s, %s) RETURNING id", (kind, name)
        ).fetchone()
    assert row is not None
    return str(row["id"])


def _insert(pool: PgPool, table: str, row: dict[str, Any]) -> str:
    names = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    with pool.connection() as conn:
        got = conn.execute(
            f"INSERT INTO {table} ({names}) VALUES ({marks}) RETURNING id",
            tuple(row.values()),
        ).fetchone()
    assert got is not None
    return str(got["id"])


def _lot(pool: PgPool, item_id: str, **cols: Any) -> str:
    row = {
        "item_id": item_id,
        "received_qty": 10,
        "on_hand": 10,
        "unit_cost_minor": 25,
        "origin": "manual",
        "created_by": ACTOR,
        **cols,
    }
    return _insert(pool, "stock_lots", row)


def _unit(pool: PgPool, item_id: str, **cols: Any) -> str:
    row = {"item_id": item_id, "cost_minor": 1000, "origin": "manual", "created_by": ACTOR, **cols}
    return _insert(pool, "stock_units", row)


def _move(pool: PgPool, item_id: str, **cols: Any) -> str:
    row = {
        "item_id": item_id,
        "kind": "use",
        "qty": -2,
        "unit_cost_minor": 25,
        "amount_minor": -50,
        "actor": ACTOR,
        **cols,
    }
    return _insert(pool, "stock_moves", row)


def _supplier(pool: PgPool, name: str = "Bolt Depot") -> str:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO filament_suppliers (name) VALUES (%s) RETURNING id", (name,)
        ).fetchone()
    assert row is not None
    return str(row["id"])


def _price_point(pool: PgPool, offer_id: str, **cols: Any) -> str:
    row = {
        "offer_id": offer_id,
        "price": "12.5000",
        "currency": "USD",
        "on_date": "2026-11-23",
        "source": "manual",
        **cols,
    }
    return _insert(pool, "offer_price_points", row)


def _invoice_line(pool: PgPool, supplier_id: str) -> str:
    with pool.connection() as conn:
        invoice = conn.execute(
            "INSERT INTO filament_invoices (supplier_id, invoice_date, currency, subtotal, total)"
            " VALUES (%s, '2026-11-23', 'USD', 10, 10) RETURNING id",
            (supplier_id,),
        ).fetchone()
        assert invoice is not None
        line = conn.execute(
            "INSERT INTO filament_invoice_lines (invoice_id, position, description, quantity,"
            " unit_price, line_subtotal, line_tax, line_total, allocated_cost)"
            " VALUES (%s, 1, 'M3 nuts', 1, 10, 10, 0, 10, 10) RETURNING id",
            (invoice["id"],),
        ).fetchone()
    assert line is not None
    return str(line["id"])


def _printer(pool: PgPool) -> str:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO printers (id, serial, name) VALUES (gen_random_uuid(), 'TEST7067', 'H2C')"
            " RETURNING id"
        ).fetchone()
    assert row is not None
    return str(row["id"])


def _offer(pool: PgPool, supplier_id: str, item_id: str, sku: str) -> str:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO supplier_offers (supplier_id, item_id, sku) VALUES (%s, %s, %s)"
            " RETURNING id",
            (supplier_id, item_id, sku),
        ).fetchone()
    assert row is not None
    return str(row["id"])


def test_the_builtin_kinds_are_seeded(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, parent_id, tracking, uom, life_basis, saleable, builtin, name"
            " FROM item_kinds"
        ).fetchall()
    seeded = {
        r["id"]: (r["parent_id"], r["tracking"], r["uom"], r["life_basis"], r["saleable"])
        for r in rows
    }
    assert seeded == BUILTIN_KINDS
    assert all(r["builtin"] and r["name"] for r in rows)


def test_spool_part_schema_passes_check_schema(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        rows = conn.execute("SELECT id, attribute_schema FROM item_kinds").fetchall()
    for row in rows:
        check_schema(row["attribute_schema"])
    spool_part = next(r["attribute_schema"] for r in rows if r["id"] == "spool_part")
    assert set(spool_part["properties"]) == {"empty_weight_g", "role"}
    assert spool_part["properties"]["role"]["enum"] == ["shell", "addon"]


def test_a_move_needs_one_lot_or_one_unit(pg_pool: PgPool) -> None:
    item = _item(pg_pool, "M3 nut")
    lot = _lot(pg_pool, item)
    nozzle = _item(pg_pool, "0.4 nozzle", kind="nozzle")
    unit = _unit(pg_pool, nozzle)

    _move(pg_pool, item, lot_id=lot)
    _move(pg_pool, nozzle, unit_id=unit)
    with pytest.raises(psycopg.errors.CheckViolation):
        _move(pg_pool, item)
    with pytest.raises(psycopg.errors.CheckViolation):
        _move(pg_pool, item, lot_id=lot, unit_id=unit)


def test_a_move_amount_is_qty_times_cost(pg_pool: PgPool) -> None:
    item = _item(pg_pool, "M3 bolt")
    lot = _lot(pg_pool, item)

    _move(pg_pool, item, lot_id=lot, qty=-1.5, unit_cost_minor=0.333333, amount_minor=-0.4999995)
    with pytest.raises(psycopg.errors.CheckViolation):
        _move(pg_pool, item, lot_id=lot, qty=-2, unit_cost_minor=25, amount_minor=-49)
    with pytest.raises(psycopg.errors.CheckViolation):
        _move(pg_pool, item, lot_id=lot, qty=0, unit_cost_minor=25, amount_minor=0)


def test_an_offer_sku_is_unique_per_item_not_per_supplier(pg_pool: PgPool) -> None:
    supplier = _supplier(pg_pool)
    red = _item(pg_pool, "Bundle red")
    blue = _item(pg_pool, "Bundle blue")

    first = _offer(pg_pool, supplier, red, "BUNDLE-1")
    _offer(pg_pool, supplier, blue, "bundle-1")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _offer(pg_pool, supplier, red, "Bundle-1")

    with pg_pool.connection() as conn:
        conn.execute("UPDATE supplier_offers SET archived_at = now() WHERE id = %s", (first,))
    _offer(pg_pool, supplier, red, "BUNDLE-1")


def test_the_lookup_indexes_exist(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        rows = conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename IN"
            " ('stock_units', 'stock_moves')"
        ).fetchall()
    defs = {r["indexname"]: r["indexdef"] for r in rows}
    assert "(item_id)" in defs["stock_units_item"]
    assert "(unit_id) WHERE (unit_id IS NOT NULL)" in defs["stock_moves_unit"]
    assert "(entity_id) WHERE (entity_id IS NOT NULL)" in defs["stock_moves_entity"]


def test_a_price_point_source_matches_its_line(pg_pool: PgPool) -> None:
    supplier = _supplier(pg_pool)
    offer = _offer(pg_pool, supplier, _item(pg_pool, "M3 nut"), "NUT-M3")
    line = _invoice_line(pg_pool, supplier)

    _price_point(pg_pool, offer, source="invoice", invoice_line_id=line)
    _price_point(pg_pool, offer, source="manual")
    with pytest.raises(psycopg.errors.CheckViolation):
        _price_point(pg_pool, offer, source="invoice")
    with pytest.raises(psycopg.errors.CheckViolation):
        _price_point(pg_pool, offer, source="manual", invoice_line_id=line)


def test_a_price_point_takes_any_iso_currency(pg_pool: PgPool) -> None:
    offer = _offer(pg_pool, _supplier(pg_pool), _item(pg_pool, "M3 nut"), "NUT-M3")

    _price_point(pg_pool, offer, currency="EUR")
    _price_point(pg_pool, offer, currency="JPY")
    for bad in ("usd", "US"):
        with pytest.raises(psycopg.errors.CheckViolation):
            _price_point(pg_pool, offer, currency=bad)


def test_a_move_is_reversed_at_most_once(pg_pool: PgPool) -> None:
    item = _item(pg_pool, "M3 nut")
    lot = _lot(pg_pool, item)
    use = _move(pg_pool, item, lot_id=lot)
    back = {"kind": "return", "qty": 2, "amount_minor": 50, "reverses_move_id": use}

    _move(pg_pool, item, lot_id=lot, **back)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _move(pg_pool, item, lot_id=lot, **back)


def test_only_a_return_reverses_a_move(pg_pool: PgPool) -> None:
    item = _item(pg_pool, "M3 nut")
    lot = _lot(pg_pool, item)
    use = _move(pg_pool, item, lot_id=lot)

    with pytest.raises(psycopg.errors.CheckViolation):
        _move(pg_pool, item, lot_id=lot, kind="adjust", reverses_move_id=use)


def test_a_move_cost_is_not_negative(pg_pool: PgPool) -> None:
    item = _item(pg_pool, "M3 nut")
    lot = _lot(pg_pool, item)

    with pytest.raises(psycopg.errors.CheckViolation):
        _move(pg_pool, item, lot_id=lot, qty=2, unit_cost_minor=-25, amount_minor=-50)


def test_a_lot_cost_currency_is_an_iso_code(pg_pool: PgPool) -> None:
    item = _item(pg_pool, "M3 nut")

    _lot(pg_pool, item, cost_currency="EUR")
    _lot(pg_pool, item)
    for bad in ("eur", "EU", ""):
        with pytest.raises(psycopg.errors.CheckViolation):
            _lot(pg_pool, item, cost_currency=bad)


def test_a_foreign_unit_cost_needs_its_currency(pg_pool: PgPool) -> None:
    """R-P2-11: the amount a person gave in another currency is kept with that currency."""
    item = _item(pg_pool, "M3 nut")

    _lot(pg_pool, item, cost_currency="EUR", foreign_unit_cost_minor=250)
    _lot(pg_pool, item, cost_currency="EUR")
    with pytest.raises(psycopg.errors.CheckViolation):
        _lot(pg_pool, item, foreign_unit_cost_minor=250)
    with pytest.raises(psycopg.errors.CheckViolation):
        _lot(pg_pool, item, cost_currency="EUR", foreign_unit_cost_minor=-1)


def test_a_unit_serial_is_unique_per_item(pg_pool: PgPool) -> None:
    nozzle = _item(pg_pool, "0.4 nozzle", kind="nozzle")
    hotend = _item(pg_pool, "Hotend", kind="hotend")

    _unit(pg_pool, nozzle)
    _unit(pg_pool, nozzle)
    _unit(pg_pool, nozzle, serial="SN1")
    _unit(pg_pool, hotend, serial="SN1")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _unit(pg_pool, nozzle, serial="SN1")


def test_a_blank_serial_or_source_ref_is_refused(pg_pool: PgPool) -> None:
    nozzle = _item(pg_pool, "0.4 nozzle", kind="nozzle")

    with pytest.raises(psycopg.errors.CheckViolation):
        _unit(pg_pool, nozzle, serial="")
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(
            pg_pool,
            "items",
            {"kind_id": "hardware", "name": "M3 nut", "source": "bambu_store", "source_ref": ""},
        )


def test_an_item_name_is_unique_per_kind_among_unarchived(pg_pool: PgPool) -> None:
    first = _item(pg_pool, "M3 Nut")
    _item(pg_pool, "M3 nut", kind="ams_part")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _item(pg_pool, "m3 NUT")

    with pg_pool.connection() as conn:
        conn.execute("UPDATE items SET archived_at = now() WHERE id = %s", (first,))
    _item(pg_pool, "m3 NUT")


def test_deleting_a_printer_clears_unit_printer_id(pg_pool: PgPool) -> None:
    printer = _printer(pg_pool)
    unit = _unit(pg_pool, _item(pg_pool, "H2C", kind="printer"), printer_id=printer)

    with pg_pool.connection() as conn:
        conn.execute("DELETE FROM printers WHERE id = %s", (printer,))
        row = conn.execute("SELECT printer_id FROM stock_units WHERE id = %s", (unit,)).fetchone()
    assert row is not None
    assert row["printer_id"] is None


def test_kind_checks(pg_pool: PgPool) -> None:
    def kind(**cols: Any) -> None:
        _insert(
            pg_pool, "item_kinds", {"id": "bearing", "name": "Bearing", "tracking": "lot", **cols}
        )

    for bad in ({"id": "Bearing"}, {"id": "9mm"}, {"tracking": "count"}, {"parent_id": "bearing"}):
        with pytest.raises(psycopg.errors.CheckViolation):
            kind(**bad)
    kind()

    nozzle = _item(pg_pool, "0.4 nozzle", kind="nozzle")
    unit = _unit(pg_pool, nozzle)
    with pytest.raises(psycopg.errors.CheckViolation), pg_pool.connection() as conn:
        conn.execute("UPDATE stock_units SET installed_in_unit_id = id WHERE id = %s", (unit,))
