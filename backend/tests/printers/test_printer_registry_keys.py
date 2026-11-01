"""#4971 (tier 3b, part 2): what is remembered per printer reaches a printer with no
Bambuddy id (LAN-only) by its registry id, while a printer that has one keeps its
Bambuddy key, which the previous build still reads and writes during a rolling deploy:
the per-printer settings maps, the default printer, a subject's remembered printer and a
Bambuddy project's last printer."""

from __future__ import annotations

import psycopg
import pytest

from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.bambuddy.print_run import _named_printer
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, ProjectTarget
from scadbuddy.library.settings_store import ModelPrintChoices, StoredSettings
from scadbuddy.printers.models import PrinterCreate
from scadbuddy.printers.store import PrinterStore
from scadbuddy.rack.usage import RackUsageStore
from scadbuddy.render.pg_store import MIGRATIONS, MIGRATIONS_DIR, migrate
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

LAN = "00000000-0000-4000-8000-0000000000aa"


def test_a_per_printer_map_reads_the_bambuddy_key_then_the_registry_key() -> None:
    settings = StoredSettings(
        printer_rack_algorithms={"3": "newest_first", LAN: "oldest_first"},
        printer_print_options={
            "3": PrintOptions(timelapse=True),
            LAN: PrintOptions(bed_levelling="auto"),
        },
    )
    assert settings.rack_algorithm(3) == "newest_first"
    assert settings.rack_algorithm(None, LAN) == "oldest_first"
    # A printer linked after its LAN-only entry was stored still finds it.
    assert settings.rack_algorithm(9, LAN) == "oldest_first"
    # The Bambuddy key wins: both builds write it.
    assert settings.rack_algorithm(3, LAN) == "newest_first"
    assert settings.rack_algorithm(None) == "least_used"
    assert settings.printer_options(3) == PrintOptions(timelapse=True)
    assert settings.printer_options(None, LAN) == PrintOptions(bed_levelling="auto")
    assert settings.printer_options(None, None) is None


def test_a_remembered_printer_keeps_the_registry_id_only_without_a_bambuddy_one() -> None:
    assert ModelPrintChoices(printer=LAN).printer == LAN
    assert ModelPrintChoices(printer_id=3, printer=LAN).printer is None


async def test_the_default_printer_with_no_bambuddy_id_is_named_by_its_registry_id(
    printers: PrinterStore,
) -> None:
    row = printers.create(PrinterCreate(serial="0938AB000000021", name="LAN"))
    settings = StoredSettings(default_printer=row.id)

    named, bambuddy_id = await _named_printer(None, None, settings, printers)

    assert (named.id if named else None, bambuddy_id) == (row.id, None)
    # A configured Bambuddy printer wins, as the previous build reads it.
    _, configured = await _named_printer(
        None, None, settings.model_copy(update={"printer_id": 4}), printers
    )
    assert configured == 4


async def test_a_projects_lan_only_printer_is_kept_beside_a_zero(
    pg_pool: PgPool, printers: PrinterStore
) -> None:
    row = printers.create(PrinterCreate(serial="0938AB000000022", name="LAN"))
    uploads = BambuddyUploadStore(pg_pool)

    await uploads.remember_project_target(
        1, ProjectTarget(printer_id=0, printer=row.id, nozzle_diameter="0.4")
    )
    target = await uploads.project_target(1)
    assert target is not None
    assert (target.printer_id, target.printer, target.nozzle_diameter) == (0, row.id, "0.4")

    # The previous build's upsert names a Bambuddy printer and leaves the column alone:
    # the registry id beside it is not read.
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO project_print_targets (project_id, printer_id, nozzle_diameter)"
            " VALUES (1, 5, '0.4') ON CONFLICT (project_id) DO UPDATE"
            " SET printer_id = EXCLUDED.printer_id, updated_at = now()"
        )
    assert (await uploads.project_targets())[1] == ProjectTarget(
        printer_id=5, nozzle_diameter="0.4"
    )

    # A deleted printer leaves the project's target, with no printer named.
    await uploads.remember_project_target(1, ProjectTarget(printer_id=0, printer=row.id))
    printers.delete(row.id)
    assert await uploads.project_target(1) == ProjectTarget(printer_id=0)


def test_the_project_target_migration_keeps_every_row(pg_conninfo: str) -> None:
    migration = MIGRATIONS_DIR / "20261101T1910Z_project_print_targets_registry.sql"
    before = next(i for i, m in enumerate(MIGRATIONS) if m.id == migration.stem)
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[:before])
        conn.execute(
            "INSERT INTO project_print_targets (project_id, printer_id) VALUES (1, 3), (2, 0)"
        )
        migrate(conn)
        rows = conn.execute(
            "SELECT project_id, printer_id, printer FROM project_print_targets ORDER BY 1"
        ).fetchall()
    assert rows == [(1, 3, None), (2, 0, None)]


async def test_a_lan_only_printers_hotends_are_seen_under_its_registry_id(
    pg_conninfo: str, printers: PrinterStore
) -> None:
    row = printers.create(PrinterCreate(serial="0938AB000000023", name="LAN"))
    store = RackUsageStore(pg_conninfo)
    try:
        await store.seen(None, ["HOTEND1"], row.id)
        # Neither id: nothing to record it under.
        await store.seen(None, ["HOTEND2"])
    finally:
        store.close()
    with psycopg.connect(pg_conninfo) as conn:
        rows = conn.execute(
            "SELECT serial, printer_id, printer::text FROM rack_nozzle_seen ORDER BY 1"
        ).fetchall()
    assert rows == [("HOTEND1", None, row.id)]


def test_the_rack_migration_backfills_the_registry_printer(pg_conninfo: str) -> None:
    migration = MIGRATIONS_DIR / "20261101T1911Z_rack_nozzle_registry_printer.sql"
    before = next(i for i, m in enumerate(MIGRATIONS) if m.id == migration.stem)
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[:before])
        linked = conn.execute(
            "INSERT INTO printers (id, serial, name, bambuddy_printer_id)"
            " VALUES (gen_random_uuid(), '0938AB000000024', 'H2C', 1) RETURNING id::text"
        ).fetchone()
        assert linked is not None
        conn.execute("INSERT INTO rack_nozzle_seen (serial, printer_id) VALUES ('A', 1), ('B', 2)")
        conn.execute(
            "INSERT INTO rack_nozzle_picks (queue_item_id, group_id, printer_id, serial)"
            " VALUES (10, 0, 1, 'A')"
        )
        migrate(conn)
        seen = conn.execute(
            "SELECT serial, printer::text FROM rack_nozzle_seen ORDER BY 1"
        ).fetchall()
        picks = conn.execute("SELECT printer::text FROM rack_nozzle_picks").fetchall()
    assert seen == [("A", linked[0]), ("B", None)]
    assert picks == [(linked[0],)]
