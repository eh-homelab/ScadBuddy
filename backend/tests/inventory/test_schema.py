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


def _lot(pool: PgPool, item_id: str) -> str:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO stock_lots (item_id, received_qty, on_hand, unit_cost_minor, origin,"
            " created_by) VALUES (%s, 10, 10, 25, 'manual', %s) RETURNING id",
            (item_id, ACTOR),
        ).fetchone()
    assert row is not None
    return str(row["id"])


def _unit(pool: PgPool, item_id: str) -> str:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO stock_units (item_id, cost_minor, origin, created_by)"
            " VALUES (%s, 1000, 'manual', %s) RETURNING id",
            (item_id, ACTOR),
        ).fetchone()
    assert row is not None
    return str(row["id"])


def _move(pool: PgPool, item_id: str, **cols: Any) -> None:
    row = {
        "item_id": item_id,
        "kind": "use",
        "qty": -2,
        "unit_cost_minor": 25,
        "amount_minor": -50,
        "actor": ACTOR,
        **cols,
    }
    names = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    with pool.connection() as conn:
        conn.execute(
            f"INSERT INTO stock_moves ({names}) VALUES ({marks})",
            tuple(row.values()),
        )


def _supplier(pool: PgPool, name: str = "Bolt Depot") -> str:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO filament_suppliers (name) VALUES (%s) RETURNING id", (name,)
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
