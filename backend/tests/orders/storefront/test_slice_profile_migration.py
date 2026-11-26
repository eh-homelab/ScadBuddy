"""#6574: the migration that moves the stored shop slice profile from a spool per
material to a filament type per material."""

from __future__ import annotations

import json
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from scadbuddy.orders.storefront.slice_profile import StorefrontSliceProfile
from scadbuddy.render.pg_store import MIGRATIONS, MIGRATIONS_DIR, migrate

pytestmark = [pytest.mark.requires_postgres, pytest.mark.pg_empty]

MIGRATION = MIGRATIONS_DIR / "20261117T1557Z_storefront_slice_profile_presets.sql"
P1 = "00000000-0000-4000-8000-000000000001"


def _profile(conn: psycopg.Connection[Any]) -> Any:
    found = conn.execute(
        "SELECT value FROM settings WHERE name = 'storefront_slice_profile'"
    ).fetchone()
    return None if found is None else found["value"]


def _spool(conn: psycopg.Connection[Any], number: int, *, typed: bool) -> str:
    """SB spool #``number``, of a new PLA type when ``typed``; the type's id, or ''."""
    row = conn.execute(
        "INSERT INTO filament_materials (name) VALUES (%s) RETURNING id", (f"PLA {number}",)
    ).fetchone()
    assert row is not None
    material = row["id"]
    type_id = None
    if typed:
        made = conn.execute(
            "INSERT INTO filament_spool_types (code, material_id, rgba, label_weight_g)"
            " VALUES (%s, %s, '#FFFFFFFF', 1000) RETURNING id",
            (f"T{number}", material),
        ).fetchone()
        assert made is not None
        type_id = made["id"]
    conn.execute(
        "INSERT INTO filament_spools (number, type_id, material_id) OVERRIDING SYSTEM VALUE"
        " VALUES (%s, %s, %s)",
        (number, type_id, None if typed else material),
    )
    return "" if type_id is None else str(type_id)


def test_each_sb_spool_becomes_its_type_and_the_rest_are_dropped(pg_conninfo: str) -> None:
    """A profile names SB spool numbers since `20261116T0034Z`. Each becomes its spool's
    filament type, whose presets the slot is now sliced with. A loaded tray (-1), a
    number no spool has (15) and a spool with no type (9) are dropped; the printer is
    kept. The new shape loads; running the file again changes nothing."""
    before = next(i for i, m in enumerate(MIGRATIONS) if m.id == MIGRATION.stem)
    with psycopg.connect(pg_conninfo, row_factory=dict_row) as conn:
        migrate(conn, MIGRATIONS[:before])
        petg = _spool(conn, 7, typed=True)
        _spool(conn, 9, typed=False)
        conn.execute(
            "INSERT INTO settings (name, value) VALUES ('storefront_slice_profile', %s::jsonb)",
            (
                json.dumps(
                    {
                        "printer": P1,
                        "spools": {"pla": -1, "petg": 7, "tpu": 15, "asa": 9},
                    }
                ),
            ),
        )
        sql = MIGRATION.read_text()
        conn.execute(sql)
        moved = _profile(conn)
        assert moved == {"printer": P1, "materials": {"petg": petg}}
        assert StorefrontSliceProfile.model_validate(moved).printer == P1
        conn.execute(sql)
        assert _profile(conn) == moved


def test_a_profile_of_the_new_shape_or_none_is_left_alone(pg_conninfo: str) -> None:
    before = next(i for i, m in enumerate(MIGRATIONS) if m.id == MIGRATION.stem)
    new = {"printer": P1, "materials": {"pla": "00000000-0000-4000-8000-0000000000a1"}}
    with psycopg.connect(pg_conninfo, row_factory=dict_row) as conn:
        migrate(conn, MIGRATIONS[:before])
        conn.execute(MIGRATION.read_text())
        assert _profile(conn) is None
        conn.execute(
            "INSERT INTO settings (name, value) VALUES ('storefront_slice_profile', %s::jsonb)",
            (json.dumps(new),),
        )
        conn.execute(MIGRATION.read_text())
        assert _profile(conn) == new
