"""#4971 (tier 3b): the plate remembered per printer, keyed by the registry printer.
Part 1 (``migrations/20261101T1804Z_printer_bed_types_registry.sql``) kept the Bambuddy
id beside it for the previous build. #5153
(``migrations/20261111T1842Z_printer_bed_types_backfill_registry.sql``) moves every row
onto its registry printer, and this build never reads or writes the Bambuddy id. The
column stays for the previous build's rollout until #6059 drops it."""

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
BACKFILL = MIGRATIONS_DIR / "20261111T1842Z_printer_bed_types_backfill_registry.sql"

#: The write of the build before #4971 part 1, which ran beside part 1's during its
#: rolling deploy. Part 1 writes it too, for a Bambuddy printer the registry does not link.
OLD_WRITE = (
    "INSERT INTO printer_bed_types (printer_id, bed_type) VALUES (%s, %s)"
    " ON CONFLICT (printer_id) DO UPDATE SET bed_type = EXCLUDED.bed_type, updated_at = now()"
)

#: Part 1's other statements (`set_printer_bed_type` and `_PLATES` before #5153), which
#: run beside this build's during its rolling deploy.
PART1_FORGET = "DELETE FROM printer_bed_types WHERE printer = %s OR printer_id = %s"
PART1_DROP_UNREGISTERED = "DELETE FROM printer_bed_types WHERE printer_id = %s AND printer IS NULL"
PART1_RELEASE = (
    "UPDATE printer_bed_types SET printer_id = NULL WHERE printer_id = %s AND printer <> %s"
)
PART1_WRITE = (
    "INSERT INTO printer_bed_types (printer, printer_id, bed_type)"
    " VALUES (%s, %s, %s) ON CONFLICT (printer) DO UPDATE"
    " SET printer_id = EXCLUDED.printer_id, bed_type = EXCLUDED.bed_type,"
    " updated_at = now()"
)
PART1_READ = (
    "SELECT coalesce(b.printer, linked.id)::text AS printer,"
    " CASE WHEN own.id IS NOT NULL THEN own.bambuddy_printer_id ELSE b.printer_id END"
    " AS bambuddy_printer_id,"
    " coalesce(own.name, linked.name) AS name, b.bed_type"
    " FROM printer_bed_types AS b"
    " LEFT JOIN printers AS own ON own.id = b.printer"
    " LEFT JOIN printers AS linked"
    " ON b.printer IS NULL AND linked.bambuddy_printer_id = b.printer_id"
    " ORDER BY b.updated_at, b.bed_type"
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


def test_the_previous_builds_statements_still_run_beside_this_ones(
    store: SettingsStore, printers: PrinterStore, pg_conninfo: str
) -> None:
    """#5153 keeps ``printer_id`` for part 1's pods, which still run during the rollout:
    each of their statements works, and this build reads and upserts their rows by the
    registry printer."""
    row = printers.create(PrinterCreate(serial="0938AB000000009", name="H2C"))
    printers.link_bambuddy(row.id, 3)
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(PART1_DROP_UNREGISTERED, (3,))
        conn.execute(PART1_RELEASE, (3, row.id))
        conn.execute(PART1_WRITE, (row.id, 3, "Cool Plate"))
        conn.execute(OLD_WRITE, (6, "Engineering Plate"))  # a printer the registry lacks

    loaded = store.load()
    assert loaded.printer_plates == [
        PrinterPlate(printer=row.id, bambuddy_printer_id=3, name="H2C", bed_type="Cool Plate")
    ]
    assert loaded.printer_bed_types == {"3": "Cool Plate"}

    # This build's upsert lands on part 1's row, by the registry printer.
    store.set_printer_bed_type(row.id, "Textured PEI Plate")
    with psycopg.connect(pg_conninfo) as conn:
        rows = conn.execute(
            "SELECT printer::text, printer_id, bed_type FROM printer_bed_types ORDER BY printer_id"
        ).fetchall()
        assert rows == [(row.id, 3, "Textured PEI Plate"), (None, 6, "Engineering Plate")]
        read = conn.execute(PART1_READ).fetchall()
        assert sorted((r[0] or "", r[1], r[3]) for r in read) == sorted(
            [("", 6, "Engineering Plate"), (row.id, 3, "Textured PEI Plate")]
        )
        conn.execute(PART1_FORGET, (None, 3))
    assert store.load().printer_plates == []


def test_a_plate_names_a_printer(pg_conninfo: str, store: SettingsStore) -> None:
    with psycopg.connect(pg_conninfo) as conn, pytest.raises(psycopg.errors.CheckViolation):
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


def test_the_backfill_puts_every_plate_on_its_registry_printer(pg_conninfo: str) -> None:
    """#5153: a row the old build wrote after part 1's backfill gets its registry printer;
    where its printer also has a registry-keyed row, the newer of the two is kept; a row
    of a Bambuddy printer the registry never held goes. ``printer_id`` stays for part 1's
    pods (#6059 drops it)."""
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[: _index(BACKFILL.stem)])
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
            "SELECT printer::text, printer_id, bed_type FROM printer_bed_types ORDER BY bed_type"
        ).fetchall()
        assert rows == [
            (both, 1, "Cool Plate"),
            (late, 2, "Engineering Plate"),
            (old_newer, 3, "Old build, newer"),
            (own_newer, None, "Own, newer"),
            (lan, None, "Textured PEI Plate"),
        ]


def test_the_backfill_touches_only_the_matching_printers_rows(pg_conninfo: str) -> None:
    """#5153: the de-duplicate and delete statements match a stale row only to the
    printer that links its Bambuddy id: a part-1 row (both keys) of another Bambuddy id is
    left as it is."""
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[: _index(BACKFILL.stem)])
        part1 = _printer(conn, "0938AB000000021", 1)
        other = _printer(conn, "0938AB000000022", 2)
        insert = (
            "INSERT INTO printer_bed_types (printer, printer_id, bed_type, updated_at)"
            " VALUES (%s, %s, %s, now() + %s * interval '1 minute')"
        )
        conn.execute(insert, (part1, 1, "Part 1", 0))
        conn.execute(insert, (other, None, "Other's own", 0))
        conn.execute(insert, (None, 2, "Other's stale", 1))

        migrate(conn)

        rows = conn.execute(
            "SELECT printer::text, printer_id, bed_type FROM printer_bed_types ORDER BY printer_id"
        ).fetchall()
        assert rows == [(part1, 1, "Part 1"), (other, 2, "Other's stale")]
