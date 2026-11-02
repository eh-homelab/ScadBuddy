"""#4971 (tier 3b, part 1): the plate remembered per printer, keyed by the registry
printer, with the Bambuddy id kept beside it while the previous build still reads it
(``migrations/20261101T1804Z_printer_bed_types_registry.sql``)."""

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

#: The previous build's write and read (`set_printer_bed_type`, `snapshot` before #4971),
#: which run beside this build's during a rolling deploy.
OLD_WRITE = (
    "INSERT INTO printer_bed_types (printer_id, bed_type) VALUES (%s, %s)"
    " ON CONFLICT (printer_id) DO UPDATE SET bed_type = EXCLUDED.bed_type, updated_at = now()"
)
OLD_READ = "SELECT printer_id, bed_type FROM printer_bed_types"


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


def _old_read(conninfo: str) -> dict[str, str]:
    with psycopg.connect(conninfo) as conn:
        return {str(pid): bed for pid, bed in conn.execute(OLD_READ).fetchall()}


def test_a_lan_only_printer_remembers_its_plate(
    store: SettingsStore, printers: PrinterStore
) -> None:
    lan = printers.create(PrinterCreate(serial="0938AB000000001", name="Garage H2C"))

    store.set_printer_bed_type(None, "Cool Plate", printer=lan.id)

    loaded = store.load()
    assert loaded.printer_plates == [
        PrinterPlate(printer=lan.id, name="Garage H2C", bed_type="Cool Plate")
    ]
    assert loaded.bed_type_of(printer=lan.id) == "Cool Plate"
    assert loaded.printer_bed_types == {}

    store.set_printer_bed_type(None, None, printer=lan.id)
    assert store.load().printer_plates == []


def test_a_linked_printer_is_kept_under_both_ids(
    store: SettingsStore, printers: PrinterStore, pg_conninfo: str
) -> None:
    row = printers.create(PrinterCreate(serial="0938AB000000002", name="H2C"))
    printers.link_bambuddy(row.id, 3)

    store.set_printer_bed_type(3, "Textured PEI Plate", printer=row.id)

    loaded = store.load()
    assert loaded.bed_type_of(printer=row.id) == "Textured PEI Plate"
    assert loaded.printer_bed_types == {"3": "Textured PEI Plate"}
    # The previous build still finds it by the Bambuddy id, and its own write lands on
    # the same row.
    assert _old_read(pg_conninfo) == {"3": "Textured PEI Plate"}
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(OLD_WRITE, (3, "Cool Plate"))
    assert store.load().printer_plates == [
        PrinterPlate(printer=row.id, bambuddy_printer_id=3, name="H2C", bed_type="Cool Plate")
    ]


def test_a_row_the_previous_build_wrote_is_found_by_the_registry_id(
    store: SettingsStore, printers: PrinterStore, pg_conninfo: str
) -> None:
    row = printers.create(PrinterCreate(serial="0938AB000000003", name="H2C"))
    printers.link_bambuddy(row.id, 5)
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(OLD_WRITE, (5, "Cool Plate"))
        conn.execute(OLD_WRITE, (6, "Engineering Plate"))  # a printer the registry lacks

    loaded = store.load()
    assert loaded.bed_type_of(printer=row.id) == "Cool Plate"
    assert loaded.printer_bed_types == {"5": "Cool Plate", "6": "Engineering Plate"}

    # This build's write by the registry id takes the old row over: one row, both keys.
    store.set_printer_bed_type(5, "Textured PEI Plate", printer=row.id)
    with psycopg.connect(pg_conninfo) as conn:
        rows = conn.execute(
            "SELECT printer::text, printer_id, bed_type FROM printer_bed_types ORDER BY printer_id"
        ).fetchall()
    assert rows == [(row.id, 5, "Textured PEI Plate"), (None, 6, "Engineering Plate")]


def test_a_plate_follows_the_registry_link_not_a_stale_bambuddy_id(
    store: SettingsStore, printers: PrinterStore
) -> None:
    row = printers.create(PrinterCreate(serial="0938AB000000004", name="H2C"))
    printers.link_bambuddy(row.id, 7)
    store.set_printer_bed_type(7, "Cool Plate", printer=row.id)

    printers.unlink_bambuddy(row.id)

    loaded = store.load()
    assert loaded.bed_type_of(printer=row.id) == "Cool Plate"
    assert loaded.printer_bed_types == {}


def test_forgetting_by_the_bambuddy_id_forgets_the_registry_row(
    store: SettingsStore, printers: PrinterStore
) -> None:
    row = printers.create(PrinterCreate(serial="0938AB000000005", name="H2C"))
    printers.link_bambuddy(row.id, 8)
    store.set_printer_bed_type(8, "Cool Plate", printer=row.id)

    store.set_printer_bed_type(8, None)

    assert store.load().printer_plates == []


def test_a_merge_moves_the_plate_and_the_kept_printers_wins(
    store: SettingsStore, printers: PrinterStore
) -> None:
    synthetic = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C")
    store.set_printer_bed_type(4, "Cool Plate", printer=synthetic.id)
    mine = printers.create(PrinterCreate(serial="0938AB000000006", name="Mine"))

    kept = printers.ensure_bambuddy(4, serial="0938AB000000006", name="H2C", model="H2C")

    assert kept.id == mine.id
    assert store.load().bed_type_of(printer=mine.id) == "Cool Plate"


def _rows(conn: psycopg.Connection[Any]) -> list[Any]:
    return conn.execute(
        "SELECT printer::text, printer_id, bed_type FROM printer_bed_types ORDER BY printer_id"
    ).fetchall()


def test_the_migration_backfills_the_registry_id(pg_conninfo: str) -> None:
    """Each row whose Bambuddy printer the registry links gets that registry printer; one
    the registry lacks keeps only its Bambuddy id."""
    before = next(i for i, m in enumerate(MIGRATIONS) if m.id == MIGRATION.stem)
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[:before])
        linked = conn.execute(
            "INSERT INTO printers (id, serial, name, bambuddy_printer_id)"
            " VALUES (gen_random_uuid(), '0938AB000000007', 'H2C', 1) RETURNING id::text"
        ).fetchone()
        assert linked is not None
        conn.execute(OLD_WRITE, (1, "Cool Plate"))
        conn.execute(OLD_WRITE, (2, "Engineering Plate"))
        migrate(conn)

        assert _rows(conn) == [(linked[0], 1, "Cool Plate"), (None, 2, "Engineering Plate")]
        # Neither key may be missing.
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO printer_bed_types (bed_type) VALUES ('Cool Plate')")
