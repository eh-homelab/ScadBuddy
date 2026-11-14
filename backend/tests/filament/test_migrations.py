"""The filament tables and their seeds (#6261): the checks and unique indexes that
later stores rely on."""

from __future__ import annotations

import pytest
from psycopg import errors

from tests.conftest import PgPool

pg = pytest.mark.requires_postgres

_TYPE = (
    "INSERT INTO filament_spool_types (code, material_id, rgba, label_weight_g) "
    "SELECT 'T1', id, %s, 1000 FROM filament_materials WHERE name = 'PLA'"
)
_SPOOL = (
    "INSERT INTO filament_spools (material_id) "
    "SELECT id FROM filament_materials WHERE name = 'PLA' RETURNING id"
)


@pg
def test_the_filament_tables_and_seeds_exist(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        row = conn.execute("SELECT count(*) FROM filament_materials").fetchone()
        assert row is not None and row["count"] >= 8
        row = conn.execute("SELECT count(*) FROM filament_location_kinds").fetchone()
        assert row is not None and row["count"] >= 6

        conn.execute(_TYPE, ("#FF0000FF",))
        with pytest.raises(errors.CheckViolation), conn.transaction():
            conn.execute(_TYPE.replace("'T1'", "'T2'"), ("#ff0000ff",))

        with pytest.raises(errors.CheckViolation), conn.transaction():
            conn.execute("INSERT INTO filament_spools (note) VALUES ('x')")

        spool = conn.execute(_SPOOL).fetchone()
        other = conn.execute(_SPOOL).fetchone()
        tag = conn.execute(
            "INSERT INTO filament_tags (kind, tag_uid) VALUES ('other', 'AA') RETURNING id"
        ).fetchone()
        assert spool is not None and other is not None and tag is not None
        bind = "INSERT INTO filament_tag_bindings (tag_id, spool_id) VALUES (%s, %s)"
        conn.execute(bind, (tag["id"], spool["id"]))
        with pytest.raises(errors.UniqueViolation), conn.transaction():
            conn.execute(bind, (tag["id"], other["id"]))

        event = (
            "INSERT INTO filament_spool_events (spool_id, kind, ref_kind, ref_id) "
            "VALUES (%s, 'print_ended', 'run', 'r1')"
        )
        conn.execute(event, (spool["id"],))
        with pytest.raises(errors.UniqueViolation), conn.transaction():
            conn.execute(event, (spool["id"],))

        with pytest.raises(errors.CheckViolation), conn.transaction():
            conn.execute(
                "INSERT INTO filament_spool_events (spool_id, kind, ref_id) "
                "VALUES (%s, 'print_ended', 'r2')",
                (spool["id"],),
            )

        tag2 = conn.execute(
            "INSERT INTO filament_tags (kind, tag_uid) VALUES ('other', 'BB') RETURNING id"
        ).fetchone()
        assert tag2 is not None
        with pytest.raises(errors.UniqueViolation), conn.transaction():
            conn.execute(bind, (tag2["id"], spool["id"]))

        brand = "INSERT INTO filament_brands (name) VALUES ('Acme')"
        conn.execute(brand)
        with pytest.raises(errors.UniqueViolation), conn.transaction():
            conn.execute(brand.replace("Acme", "ACME"))
        conn.execute("UPDATE filament_brands SET archived_at = now() WHERE name = 'Acme'")
        conn.execute(brand.replace("Acme", "ACME"))
