"""#4971 (tier 3b): the plate remembered per printer, keyed by the registry printer.
Part 1 (``migrations/20261101T1804Z_printer_bed_types_registry.sql``) kept the Bambuddy
id beside it for the previous build; #5153
(``migrations/20261111T1842Z_printer_bed_types_drop_bambuddy_id.sql``) drops it."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import PrinterPlate, SettingsStore
from scadbuddy.printers.models import PrinterCreate
from scadbuddy.printers.store import PrinterStore
from scadbuddy.render.pg_store import MIGRATIONS, MIGRATIONS_DIR, migrate
from tests.conftest import UNUSED_TEMPORAL_ADDRESS, PgPool

pytestmark = pytest.mark.requires_postgres

MIGRATION = MIGRATIONS_DIR / "20261101T1804Z_printer_bed_types_registry.sql"
CONTRACT = MIGRATIONS_DIR / "20261111T1842Z_printer_bed_types_drop_bambuddy_id.sql"

#: The build before #4971's write, which ran beside part 1's during its rolling deploy.
OLD_WRITE = (
    "INSERT INTO printer_bed_types (printer_id, bed_type) VALUES (%s, %s)"
    " ON CONFLICT (printer_id) DO UPDATE SET bed_type = EXCLUDED.bed_type, updated_at = now()"
)


@pytest.fixture
def store(tmp_path: Path, pg_conninfo: str, pg_pool: PgPool) -> Iterator[SettingsStore]:
    opened = SettingsStore(
        Settings(
            data_dir=tmp_path, database_url=pg_conninfo, temporal_address=UNUSED_TEMPORAL_ADDRESS
        )
    )
    opened.open()
    try:
        yield opened
    finally:
        opened.close()


def test_a_lan_only_printer_remembers_its_plate(
    store: SettingsStore, printers: PrinterStore
) -> None:
    lan = printers.create(PrinterCreate(serial="0938AB000000001", name="Garage H2C"))

    store.set_printer_bed_type(lan.id, "Cool Plate")

    loaded = store.load()
    assert loaded.printer_plates == [
        PrinterPlate(printer=lan.id, name="Garage H2C", bed_type="Cool Plate")
    ]
    assert loaded.bed_type_of(printer=lan.id) == "Cool Plate"
    assert loaded.printer_bed_types == {}

    store.set_printer_bed_type(lan.id, None)
    assert store.load().printer_plates == []


def test_a_linked_printer_is_listed_under_its_bambuddy_id(
    store: SettingsStore, printers: PrinterStore
) -> None:
    row = printers.create(PrinterCreate(serial="0938AB000000002", name="H2C"))
    printers.link_bambuddy(row.id, 3)

    store.set_printer_bed_type(row.id, "Textured PEI Plate")

    loaded = store.load()
    assert loaded.bed_type_of(printer=row.id) == "Textured PEI Plate"
    assert loaded.bed_type_of(bambuddy_printer_id=3) == "Textured PEI Plate"
    assert loaded.printer_bed_types == {"3": "Textured PEI Plate"}
    assert loaded.printer_plates == [
        PrinterPlate(
            printer=row.id, bambuddy_printer_id=3, name="H2C", bed_type="Textured PEI Plate"
        )
    ]


def test_a_plate_follows_the_registry_link_not_a_stale_bambuddy_id(
    store: SettingsStore, printers: PrinterStore
) -> None:
    row = printers.create(PrinterCreate(serial="0938AB000000004", name="H2C"))
    printers.link_bambuddy(row.id, 7)
    store.set_printer_bed_type(row.id, "Cool Plate")

    printers.unlink_bambuddy(row.id)

    loaded = store.load()
    assert loaded.bed_type_of(printer=row.id) == "Cool Plate"
    assert loaded.printer_bed_types == {}


def test_a_merge_moves_the_plate_and_the_kept_printers_wins(
    store: SettingsStore, printers: PrinterStore
) -> None:
    synthetic = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C")
    store.set_printer_bed_type(synthetic.id, "Cool Plate")
    mine = printers.create(PrinterCreate(serial="0938AB000000006", name="Mine"))

    kept = printers.ensure_bambuddy(4, serial="0938AB000000006", name="H2C", model="H2C")

    assert kept.id == mine.id
    assert store.load().bed_type_of(printer=mine.id) == "Cool Plate"


def test_a_plate_goes_with_its_printer(store: SettingsStore, printers: PrinterStore) -> None:
    row = printers.create(PrinterCreate(serial="0938AB000000008", name="H2C"))
    store.set_printer_bed_type(row.id, "Cool Plate")

    printers.delete(row.id)

    assert store.load().printer_plates == []


def test_a_plate_needs_a_registry_printer(pg_conninfo: str, store: SettingsStore) -> None:
    with psycopg.connect(pg_conninfo) as conn, pytest.raises(psycopg.errors.NotNullViolation):
        conn.execute("INSERT INTO printer_bed_types (bed_type) VALUES ('Cool Plate')")


def _index(name: str) -> int:
    return next(i for i, m in enumerate(MIGRATIONS) if m.id == name)


def _printer(conn: psycopg.Connection[Any], serial: str, bambuddy_id: int | None) -> str:
    row = conn.execute(
        "INSERT INTO printers (id, serial, name, bambuddy_printer_id)"
        " VALUES (gen_random_uuid(), %s, 'H2C', %s) RETURNING id::text",
        (serial, bambuddy_id),
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_the_migration_backfills_the_registry_id(pg_conninfo: str) -> None:
    """Part 1: each row whose Bambuddy printer the registry links gets that registry
    printer; one the registry lacks keeps only its Bambuddy id."""
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[: _index(MIGRATION.stem)])
        linked = _printer(conn, "0938AB000000007", 1)
        conn.execute(OLD_WRITE, (1, "Cool Plate"))
        conn.execute(OLD_WRITE, (2, "Engineering Plate"))
        migrate(conn, MIGRATIONS[: _index(MIGRATION.stem) + 1])

        rows = conn.execute(
            "SELECT printer::text, printer_id, bed_type FROM printer_bed_types ORDER BY printer_id"
        ).fetchall()
        assert rows == [(linked, 1, "Cool Plate"), (None, 2, "Engineering Plate")]
        # Neither key may be missing.
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO printer_bed_types (bed_type) VALUES ('Cool Plate')")


def test_the_contract_migration_keys_every_plate_by_the_registry_printer(
    pg_conninfo: str,
) -> None:
    """#5153: a row the old build wrote after part 1's backfill gets its registry printer;
    where its printer also has a registry-keyed row, the newer of the two is kept; a row
    of a Bambuddy printer the registry never held goes; then ``printer_id`` goes."""
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[: _index(CONTRACT.stem)])
        both = _printer(conn, "0938AB000000011", 1)
        late = _printer(conn, "0938AB000000012", 2)
        old_newer = _printer(conn, "0938AB000000013", 3)
        own_newer = _printer(conn, "0938AB000000014", 4)
        lan = _printer(conn, "0938AB000000015", None)
        insert = (
            "INSERT INTO printer_bed_types (printer, printer_id, bed_type, updated_at)"
            " VALUES (%s, %s, %s, now() + %s * interval '1 minute')"
        )
        conn.execute(insert, (both, 1, "Cool Plate", 0))
        conn.execute(insert, (None, 2, "Engineering Plate", 0))
        # Each printer's own row from before it was linked, beside the old build's.
        conn.execute(insert, (old_newer, None, "Own, older", 0))
        conn.execute(insert, (None, 3, "Old build, newer", 1))
        conn.execute(insert, (own_newer, None, "Own, newer", 1))
        conn.execute(insert, (None, 4, "Old build, older", 0))
        conn.execute(insert, (lan, None, "Textured PEI Plate", 0))
        conn.execute(insert, (None, 9, "Supertack Plate", 0))  # the registry never held 9

        migrate(conn)

        rows = conn.execute(
            "SELECT printer::text, bed_type FROM printer_bed_types ORDER BY bed_type"
        ).fetchall()
        assert sorted(rows) == sorted(
            [
                (both, "Cool Plate"),
                (late, "Engineering Plate"),
                (old_newer, "Old build, newer"),
                (own_newer, "Own, newer"),
                (lan, "Textured PEI Plate"),
            ]
        )
        columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = 'printer_bed_types'"
            )
        }
        assert columns == {"printer", "bed_type", "updated_at"}
        key = conn.execute(
            "SELECT a.attname FROM pg_index i"
            " JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)"
            " WHERE i.indrelid = 'printer_bed_types'::regclass AND i.indisprimary"
        ).fetchall()
        assert key == [("printer",)]
