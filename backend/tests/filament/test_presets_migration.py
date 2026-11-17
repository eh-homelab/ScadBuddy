"""The presets-per-nozzle migration (#6262, spec §5.3): a preset row for any printer or
any nozzle (``*``) becomes one row per printer model and nozzle diameter, marked
``migrated``; a row already kept for a key wins; then ``*`` is refused."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg import errors

from scadbuddy.render.pg_store import MIGRATIONS, migrate

pytestmark = pytest.mark.requires_postgres

NOZZLES = ["0.2", "0.4", "0.6", "0.8"]
_SPOOL = (
    "INSERT INTO filament_spools (material_id)"
    " SELECT id FROM filament_materials WHERE name = 'PLA' RETURNING id"
)
_TYPE = (
    "INSERT INTO filament_spool_types (code, material_id, rgba, label_weight_g)"
    " SELECT %s, id, '#FFFFFFFF', 1000 FROM filament_materials WHERE name = 'PLA' RETURNING id"
)
_PRESET = (
    "INSERT INTO filament_spool_presets (spool_id, printer_model, nozzle_diameter,"
    " slicer_filament) VALUES (%s, %s, %s, %s)"
)
_TYPE_PRESET = (
    "INSERT INTO filament_spool_type_presets (type_id, printer_model, nozzle_diameter,"
    " slicer_filament) VALUES (%s, %s, %s, %s)"
)

Conn = psycopg.Connection[tuple[Any, ...]]


def _before(conn: Conn) -> None:
    index = next(
        i for i, m in enumerate(MIGRATIONS) if m.id.endswith("_filament_presets_per_nozzle")
    )
    migrate(conn, MIGRATIONS[:index])


def _id(conn: Conn, sql: str, *args: object) -> Any:
    row = conn.execute(sql, args).fetchone()
    assert row is not None
    return row[0]


def _printer(conn: Conn, serial: str, model: str | None) -> None:
    conn.execute(
        "INSERT INTO printers (id, serial, name, model) VALUES (gen_random_uuid(), %s, %s, %s)",
        (serial, serial, model),
    )


def _rows(conn: Conn, table: str, key: str, owner: object) -> list[tuple[Any, ...]]:
    return conn.execute(
        f"SELECT printer_model, nozzle_diameter, slicer_filament, migrated FROM {table}"
        f" WHERE {key} = %s ORDER BY printer_model, nozzle_diameter",
        (owner,),
    ).fetchall()


def test_a_wildcard_preset_becomes_one_row_per_nozzle(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        _before(conn)
        _printer(conn, "S1", "H2C")
        spool = _id(conn, _SPOOL)
        conn.execute(_PRESET, (spool, "*", "*", "P1"))
        kept = _id(conn, _SPOOL)
        conn.execute(_PRESET, (kept, "*", "*", "P1"))
        conn.execute(_PRESET, (kept, "H2C", "0.4", "P2"))
        kind = _id(conn, _TYPE, "T1")
        conn.execute(_TYPE_PRESET, (kind, "*", "*", "T1"))
        migrate(conn)

        assert _rows(conn, "filament_spool_presets", "spool_id", spool) == [
            ("H2C", n, "P1", True) for n in NOZZLES
        ]
        # A row already kept for the key is the person's: it stays, not migrated.
        assert _rows(conn, "filament_spool_presets", "spool_id", kept) == [
            ("H2C", "0.2", "P1", True),
            ("H2C", "0.4", "P2", False),
            ("H2C", "0.6", "P1", True),
            ("H2C", "0.8", "P1", True),
        ]
        assert _rows(conn, "filament_spool_type_presets", "type_id", kind) == [
            ("H2C", n, "T1", True) for n in NOZZLES
        ]
        for table in ("filament_spool_presets", "filament_spool_type_presets"):
            assert conn.execute(
                f"SELECT count(*) FROM {table} WHERE '*' IN (printer_model, nozzle_diameter)"
            ).fetchone() == (0,)

        for model, nozzle in (("*", "0.4"), ("H2C", "*")):
            with pytest.raises(errors.CheckViolation), conn.transaction():
                conn.execute(_PRESET, (spool, model, nozzle, "X"))
            with pytest.raises(errors.CheckViolation), conn.transaction():
                conn.execute(_TYPE_PRESET, (kind, model, nozzle, "X"))


def test_with_no_printer_the_rows_are_the_h2cs(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        _before(conn)
        spool = _id(conn, _SPOOL)
        conn.execute(_PRESET, (spool, "*", "*", "P1"))
        migrate(conn)
        assert _rows(conn, "filament_spool_presets", "spool_id", spool) == [
            ("H2C", n, "P1", True) for n in NOZZLES
        ]


def test_every_registry_model_gets_rows_and_the_h2c_always(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        _before(conn)
        _printer(conn, "S1", " x1c ")
        _printer(conn, "S2", None)
        _printer(conn, "S3", "")
        spool = _id(conn, _SPOOL)
        conn.execute(_PRESET, (spool, "*", "*", "P1"))
        migrate(conn)
        assert _rows(conn, "filament_spool_presets", "spool_id", spool) == [
            *(("H2C", n, "P1", True) for n in NOZZLES),
            *(("X1C", n, "P1", True) for n in NOZZLES),
        ]


def test_a_row_for_one_wild_column_expands_that_column(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        _before(conn)
        _printer(conn, "S1", "H2C")
        spool = _id(conn, _SPOOL)
        conn.execute(_PRESET, (spool, "*", "*", "ANY"))
        conn.execute(_PRESET, (spool, "H2C", "*", "MODEL"))
        conn.execute(_PRESET, (spool, "*", "0.6", "NOZZLE"))
        migrate(conn)
        # The more specific row wins its keys: a model's over a nozzle's over any.
        assert _rows(conn, "filament_spool_presets", "spool_id", spool) == [
            ("H2C", "0.2", "MODEL", True),
            ("H2C", "0.4", "MODEL", True),
            ("H2C", "0.6", "MODEL", True),
            ("H2C", "0.8", "MODEL", True),
        ]


def test_printers_and_no_wildcard_row_change_nothing(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        _before(conn)
        _printer(conn, "S1", "H2C")
        spool = _id(conn, _SPOOL)
        conn.execute(_PRESET, (spool, "H2C", "0.4", "P2"))
        migrate(conn)
        assert _rows(conn, "filament_spool_presets", "spool_id", spool) == [
            ("H2C", "0.4", "P2", False)
        ]
