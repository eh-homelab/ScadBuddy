"""#6574: the migration that moves the stored shop slice profile from a spool per
material to a filament preset per material."""

from __future__ import annotations

import json
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from scadbuddy.orders.storefront.slice_profile import StorefrontSliceProfile
from scadbuddy.render.pg_store import MIGRATIONS, MIGRATIONS_DIR, migrate

pytestmark = pytest.mark.requires_postgres

MIGRATION = MIGRATIONS_DIR / "20261117T1557Z_storefront_slice_profile_presets.sql"
P1 = "00000000-0000-4000-8000-000000000001"


def _profile(conn: psycopg.Connection[Any]) -> Any:
    found = conn.execute(
        "SELECT value FROM settings WHERE name = 'storefront_slice_profile'"
    ).fetchone()
    return None if found is None else found["value"]


def test_the_profile_keeps_its_printer_and_drops_each_spool(pg_conninfo: str) -> None:
    """Prod's profile named loaded tray -1 for PLA. No table holds the preset a tray
    slices with, so each material's entry is dropped and the printer is kept: the shop
    sets each preset again. The new shape loads; running the file again changes
    nothing."""
    before = next(i for i, m in enumerate(MIGRATIONS) if m.id == MIGRATION.stem)
    with psycopg.connect(pg_conninfo, row_factory=dict_row) as conn:
        migrate(conn, MIGRATIONS[:before])
        conn.execute(
            "INSERT INTO settings (name, value) VALUES ('storefront_slice_profile', %s::jsonb)",
            (json.dumps({"printer": P1, "spools": {"pla": -1, "petg": 15}}),),
        )
        sql = MIGRATION.read_text()
        conn.execute(sql)
        moved = _profile(conn)
        assert moved == {"printer": P1, "materials": {}}
        assert StorefrontSliceProfile.model_validate(moved).printer == P1
        conn.execute(sql)
        assert _profile(conn) == moved


def test_a_profile_of_the_new_shape_or_none_is_left_alone(pg_conninfo: str) -> None:
    before = next(i for i, m in enumerate(MIGRATIONS) if m.id == MIGRATION.stem)
    new = {"printer": P1, "materials": {"pla": {"source": "standard", "id": "Bambu PLA"}}}
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
