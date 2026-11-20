"""The filament tables and their seeds (#6261): the checks and unique indexes that
later stores rely on."""

from __future__ import annotations

import psycopg
import pytest
from psycopg import errors

from scadbuddy.render.pg_store import MIGRATIONS, migrate
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
        # An archived brand keeps its key (#6706, R-6706-5): the name stays taken.
        conn.execute("UPDATE filament_brands SET archived_at = now() WHERE name = 'Acme'")
        with pytest.raises(errors.UniqueViolation), conn.transaction():
            conn.execute(brand.replace("Acme", "a-c-m-e"))


@pg
def test_capture_migration_makes_bb_weighings_scale(pg_conninfo: str) -> None:
    # #6263: a Bambuddy weighing is a scale reading; older print estimates get data.job.
    # Only the label changes: grams_left cannot tell `bb` from `scale`.
    idx = next(i for i, m in enumerate(MIGRATIONS) if m.id.endswith("_filament_capture"))
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[:idx])
        spool = conn.execute(_SPOOL).fetchone()
        assert spool is not None
        sid = spool[0]
        ins = (
            "INSERT INTO filament_spool_events"
            " (spool_id, kind, source, ref_kind, ref_id, data) VALUES (%s, %s, %s, %s, %s, %s)"
        )
        rows = [
            ("weighed", "bb", "bb_scale", "7/t1", "{}"),
            ("weighed", "person", None, None, "{}"),
            ("print_started", "print_estimate", "print_run", "r1/J/0", "{}"),
            ("print_started", "print_estimate", "print_run", "r2/a/b/1", "{}"),
            ("print_started", "print_estimate", "print_run", "r3/K/0", '{"job": "keep"}'),
            ("print_started", "print_estimate", "print_run", "r5/M/0", '{"job": 5}'),
            ("print_started", "print_estimate", "print_run", "a//b", "{}"),
            ("print_started", "print_estimate", "print_run", "odd", "{}"),
            ("print_started", "print_estimate", "other", "r4/L/0", "{}"),
        ]
        for kind, source, ref_kind, ref_id, data in rows:
            conn.execute(ins, (sid, kind, source, ref_kind, ref_id, data))
        assert migrate(conn)[0] == MIGRATIONS[idx].id
        got = conn.execute(
            "SELECT kind, source, ref_id, data ->> 'job' FROM filament_spool_events ORDER BY seq"
        ).fetchall()
        index = conn.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = 'filament_spool_events_job'"
        ).fetchone()
    assert got == [
        ("weighed", "scale", "7/t1", None),
        ("weighed", "person", None, None),
        ("print_started", "print_estimate", "r1/J/0", "J"),
        ("print_started", "print_estimate", "r2/a/b/1", "a/b"),
        ("print_started", "print_estimate", "r3/K/0", "keep"),
        ("print_started", "print_estimate", "r5/M/0", "M"),
        ("print_started", "print_estimate", "a//b", ""),
        ("print_started", "print_estimate", "odd", None),
        ("print_started", "print_estimate", "r4/L/0", None),
    ]
    assert index is not None


@pg
def test_the_sensor_tables_column_and_unit_index_exist(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        tables = {
            r["table_name"]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name IN (%s, %s)",
                ("filament_location_sensors", "filament_sensor_readings"),
            ).fetchall()
        }
        column = conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
            ("filament_materials", "max_hours_outside_dry"),
        ).fetchone()
        index = conn.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = %s", ("filament_locations_ams_unit",)
        ).fetchone()
    assert tables == {"filament_location_sensors", "filament_sensor_readings"}
    assert column is not None and index is not None


@pg
def test_sensors_migration_hangs_trays_under_an_existing_unit(pg_conninfo: str) -> None:
    idx = next(i for i, m in enumerate(MIGRATIONS) if m.id.endswith("_filament_sensors"))
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[:idx])
        printer = conn.execute(
            "INSERT INTO printers (id, serial, name) VALUES (gen_random_uuid(), 'SNMIG', 'P')"
            " RETURNING id"
        ).fetchone()
        kind = conn.execute(
            "SELECT id FROM filament_location_kinds WHERE ams ORDER BY sort_order LIMIT 1"
        ).fetchone()
        assert printer is not None and kind is not None
        ins = (
            "INSERT INTO filament_locations (name, kind_id, printer_id, ams_id, tray_id)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id"
        )
        unit = conn.execute(ins, ("AMS 0", kind[0], printer[0], 0, None)).fetchone()
        tray = conn.execute(ins, ("T1", kind[0], printer[0], 0, 1)).fetchone()
        orphan = conn.execute(ins, ("T9", kind[0], printer[0], 1, 1)).fetchone()
        assert unit is not None and tray is not None and orphan is not None
        migrate(conn)
        rows = conn.execute("SELECT id, parent_id FROM filament_locations").fetchall()
        parents: dict[object, object] = {r[0]: r[1] for r in rows}
    assert parents[tray[0]] == unit[0]
    assert parents[orphan[0]] is None


def test_the_migration_queues_open_bindings(pg_conninfo: str) -> None:
    # #6264: a tag with an open binding is pending after the migration; a closed one is not.
    idx = next(i for i, m in enumerate(MIGRATIONS) if m.id.endswith("_filament_rfid"))
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        assert migrate(conn, MIGRATIONS[:idx]) == [m.id for m in MIGRATIONS[:idx]]
        spools = [conn.execute(_SPOOL).fetchone() for _ in range(2)]
        tags = [
            conn.execute(
                "INSERT INTO filament_tags (kind, tag_uid) VALUES ('other', %s) RETURNING id", (u,)
            ).fetchone()
            for u in ("AA", "BB")
        ]
        for spool, tag, closed in zip(spools, tags, (False, True), strict=True):
            assert spool is not None and tag is not None
            conn.execute(
                "INSERT INTO filament_tag_bindings (tag_id, spool_id, unbound_at)"
                " VALUES (%s, %s, CASE WHEN %s THEN now() END)",
                (tag[0], spool[0], closed),
            )
        assert migrate(conn)[0] == MIGRATIONS[idx].id
        pending = conn.execute("SELECT tag_id FROM filament_bb_tag_pending").fetchall()
        state = conn.execute(
            "SELECT override_mode, saved_auto_add FROM filament_rfid_state"
        ).fetchall()
    first = tags[0]
    assert first is not None
    assert pending == [(first[0],)]
    assert state == [(False, None)]
