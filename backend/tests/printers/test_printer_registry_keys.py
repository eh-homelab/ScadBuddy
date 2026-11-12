"""#4971 (tier 3b, part 2) and its contract step #5167: what is remembered per printer is
keyed by the registry printer (`printers.id`) only. That covers the per-printer settings
maps, the default printer, a subject's remembered printer, a Bambuddy project's last
printer and the rack tables. A reader or a write that has only a Bambuddy id reaches the
registry printer that links it. ``migrations/20261112T1451Z_printer_settings_registry_only.sql``
rewrites the Bambuddy keys that the builds before it wrote."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from scadbuddy.api.printing import names_no_printer
from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.bambuddy.print_run import _named_printer
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, ProjectTarget
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import (
    ModelPrintChoices,
    SettingsPatch,
    SettingsStore,
    StoredSettings,
    UnknownPrinterError,
)
from scadbuddy.printers.models import PrinterCreate
from scadbuddy.printers.store import PrinterStore
from scadbuddy.rack.usage import RackUsageStore
from scadbuddy.render.pg_store import MIGRATIONS, MIGRATIONS_DIR, migrate
from tests.conftest import UNUSED_TEMPORAL_ADDRESS, PgPool

pytestmark = pytest.mark.requires_postgres

LAN = "00000000-0000-4000-8000-0000000000aa"
LINKED = "00000000-0000-4000-8000-000000000003"
CONTRACT = MIGRATIONS_DIR / "20261112T1451Z_printer_settings_registry_only.sql"


def _before(migration: Path) -> int:
    return next(i for i, m in enumerate(MIGRATIONS) if m.id == migration.stem)


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


def test_a_per_printer_map_is_read_by_the_registry_id() -> None:
    settings = StoredSettings(
        printer_rack_algorithms={LINKED: "newest_first", LAN: "oldest_first"},
        printer_print_options={
            LINKED: PrintOptions(timelapse=True),
            LAN: PrintOptions(bed_levelling="auto"),
        },
        bambuddy_links={LINKED: 3},
    )
    assert settings.rack_algorithm(None, LAN) == "oldest_first"
    # A Bambuddy id is read through the registry.
    assert settings.rack_algorithm(3) == "newest_first"
    assert settings.printer_options(3) == PrintOptions(timelapse=True)
    # The registry id wins over a Bambuddy id sent beside it.
    assert settings.rack_algorithm(3, LAN) == "oldest_first"
    # A Bambuddy id no registry printer links has nothing remembered.
    assert settings.rack_algorithm(9) == "least_used"
    assert settings.printer_options(9) is None
    assert settings.rack_algorithm(None) == "least_used"
    assert settings.printer_options(None, LAN) == PrintOptions(bed_levelling="auto")


def test_a_remembered_printer_keeps_its_registry_id_beside_a_bambuddy_one() -> None:
    assert ModelPrintChoices(printer=LAN).printer == LAN
    assert ModelPrintChoices(printer_id=3, printer=LINKED).printer == LINKED


def test_the_load_reads_the_bambuddy_ids_through_the_registry(
    store: SettingsStore, printers: PrinterStore, pg_pool: PgPool
) -> None:
    """The default printer and a subject's printer are stored by registry id only; the
    load gives the Bambuddy printer each links, and ignores a stale `printer_id` row."""
    linked = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C")
    lan = printers.create(PrinterCreate(serial="0938AB000000020", name="LAN"))
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO settings (name, value) VALUES ('printer_id', '9'),"
            " ('default_printer', %s)",
            (Jsonb(linked.id),),
        )
    store.set_model_choices("gear", ModelPrintChoices(printer=linked.id))
    store.set_model_choices("keychain", ModelPrintChoices(printer=lan.id))

    loaded = store.load()

    assert (loaded.default_printer, loaded.printer_id) == (linked.id, 4)
    assert loaded.bambuddy_links == {linked.id: 4}
    assert loaded.model_print_choices["gear"].printer_id == 4
    assert loaded.model_print_choices["keychain"].printer_id is None
    with pg_pool.connection() as conn:
        stored = conn.execute(
            "SELECT choices FROM model_print_choices WHERE model_id = 'gear'"
        ).fetchone()
    assert stored is not None
    assert "printer_id" not in stored["choices"]


def test_a_bambuddy_id_saved_is_kept_as_the_registry_printer(
    store: SettingsStore, printers: PrinterStore
) -> None:
    """An older client's `printer_id`: the default printer and a subject's printer are
    kept as the registry printer that links it; a library file's choices too."""
    linked = printers.ensure_bambuddy(5, serial=None, name="H2C", model="H2C")

    saved = store.save(SettingsPatch(printer_id=5))
    assert (saved.default_printer, saved.printer_id) == (linked.id, 5)
    with pytest.raises(UnknownPrinterError):
        store.save(SettingsPatch(printer_id=6))
    assert store.save(SettingsPatch(printer_id=None)).default_printer is None

    store.set_model_choices("gear", ModelPrintChoices(printer_id=5, tier="fine"))
    assert store.load().model_print_choices["gear"].printer == linked.id
    # One no registry printer links is remembered as no printer.
    store.set_model_choices("gear", ModelPrintChoices(printer_id=6, tier="fine"))
    assert store.load().model_print_choices["gear"].printer is None
    remembered = store.set_library_choices(7, ModelPrintChoices(printer_id=5))
    assert (remembered.printer, remembered.printer_id) == (linked.id, 5)


async def test_the_default_printer_is_named_by_its_registry_id(printers: PrinterStore) -> None:
    row = printers.create(PrinterCreate(serial="0938AB000000021", name="LAN"))
    settings = StoredSettings(default_printer=row.id)

    named, bambuddy_id = await _named_printer(None, None, settings, printers)

    assert (named.id if named else None, bambuddy_id) == (row.id, None)
    # A default that links a Bambuddy printer prints there.
    _, configured = await _named_printer(
        None, None, settings.model_copy(update={"printer_id": 4}), printers
    )
    assert configured == 4
    # The dialog's reads name it too, rather than opening on the first native row.
    assert not names_no_printer(settings, None, None)


async def test_a_projects_printer_is_kept_by_its_registry_id(
    pg_pool: PgPool, printers: PrinterStore
) -> None:
    lan = printers.create(PrinterCreate(serial="0938AB000000022", name="LAN"))
    linked = printers.ensure_bambuddy(5, serial=None, name="H2C", model="H2C")
    uploads = BambuddyUploadStore(pg_pool)

    await uploads.remember_project_target(
        1, ProjectTarget(printer_id=0, printer=lan.id, nozzle_diameter="0.4")
    )
    assert await uploads.project_target(1) == ProjectTarget(
        printer_id=0, printer=lan.id, nozzle_diameter="0.4"
    )

    # A build before #5167 sends only the Bambuddy id: kept as the registry printer.
    await uploads.remember_project_target(1, ProjectTarget(printer_id=5, nozzle_diameter="0.6"))
    assert (await uploads.project_targets())[1] == ProjectTarget(
        printer_id=5, printer=linked.id, nozzle_diameter="0.6"
    )
    # A Bambuddy printer the registry does not link changes nothing.
    await uploads.remember_project_target(1, ProjectTarget(printer_id=8))
    assert (await uploads.project_target(1)) == ProjectTarget(
        printer_id=5, printer=linked.id, nozzle_diameter="0.6"
    )
    await uploads.remember_project_target(2, ProjectTarget(printer_id=8))
    assert await uploads.project_target(2) is None

    # A deleted printer takes its targets with it.
    printers.delete(linked.id)
    assert await uploads.project_target(1) is None


async def test_the_rack_tables_name_the_registry_printer(
    pg_conninfo: str, printers: PrinterStore
) -> None:
    """A hotend is seen on a registry printer; the row is rewritten only when it moves
    (#1082), and a pick with no registry printer still counts by serial."""
    row = printers.create(PrinterCreate(serial="0938AB000000023", name="LAN"))
    other = printers.create(PrinterCreate(serial="0938AB000000024", name="Other"))
    store = RackUsageStore(pg_conninfo)

    def seen() -> tuple[str | None, str]:
        with psycopg.connect(pg_conninfo) as conn:
            got = conn.execute("SELECT printer::text, xmin::text FROM rack_nozzle_seen").fetchone()
        assert got is not None
        return got

    try:
        await store.seen(row.id, ["HOTEND"])
        before = seen()
        await store.seen(row.id, ["HOTEND"])
        assert seen() == before
        await store.seen(other.id, ["HOTEND"])
        assert seen()[0] == other.id
        assert await store.record_picks(10, None, []) == 0
    finally:
        store.close()


def _seed_old_shape(conn: psycopg.Connection[Any], linked: str, lan: str) -> None:
    """Rows as the expand build (#5170) left them: Bambuddy keys for a linked printer,
    registry keys for a LAN-only one, and keys the registry does not hold."""
    settings = {
        "printer_rack_algorithms": {
            "3": "newest_first",
            linked: "oldest_first",
            lan: "x",
            "9": "y",
        },
        "printer_rack_algorithm_versions": {"3": 4, linked: 2, lan: 1, "9": 1},
        "printer_print_options": {"3": {"a": 1}, "99999999999": {"b": 2}, LAN: {"c": 3}},
        "printer_id": 3,
    }
    for name, value in settings.items():
        conn.execute("INSERT INTO settings (name, value) VALUES (%s, %s)", (name, Jsonb(value)))
    choices = {
        "gear": {"printer_id": 3, "tier": "fine"},
        "keychain": {"printer": lan, "tier": "draft"},
        "gone": {"printer_id": 9},
        "deleted": {"printer": "00000000-0000-4000-8000-0000000000ff"},
    }
    for scope, value in choices.items():
        conn.execute(
            "INSERT INTO model_print_choices (model_id, choices) VALUES (%s, %s)",
            (scope, Jsonb(value)),
        )
    conn.execute(
        "INSERT INTO project_print_targets (project_id, printer_id, printer)"
        " VALUES (1, 3, NULL), (2, 0, %s), (3, 9, NULL), (4, 0, NULL)",
        (lan,),
    )
    conn.execute(
        "INSERT INTO rack_nozzle_seen (serial, printer_id, printer)"
        " VALUES ('A', 3, NULL), ('B', NULL, %s), ('C', 9, NULL)",
        (lan,),
    )
    conn.execute(
        "INSERT INTO rack_nozzle_picks (queue_item_id, group_id, printer_id, serial)"
        " VALUES (10, 0, 3, 'A'), (11, 0, 9, 'C')"
    )


def test_the_contract_migration_rewrites_every_bambuddy_key(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[: _before(CONTRACT)])
        linked_row = conn.execute(
            "INSERT INTO printers (id, serial, name, bambuddy_printer_id)"
            " VALUES (%s, '0938AB000000031', 'H2C', 3) RETURNING id::text",
            (LINKED,),
        ).fetchone()
        lan_row = conn.execute(
            "INSERT INTO printers (id, serial, name) VALUES (%s, '0938AB000000032', 'LAN')"
            " RETURNING id::text",
            (LAN,),
        ).fetchone()
        assert linked_row is not None and lan_row is not None
        _seed_old_shape(conn, LINKED, LAN)

        migrate(conn)

        settings: dict[str, Any] = dict(conn.execute("SELECT name, value FROM settings").fetchall())
        choices: dict[str, Any] = dict(
            conn.execute("SELECT model_id, choices FROM model_print_choices").fetchall()
        )
        targets = conn.execute(
            "SELECT project_id, printer::text FROM project_print_targets ORDER BY 1"
        ).fetchall()
        target_columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = 'project_print_targets'"
            ).fetchall()
        }
        seen = conn.execute(
            "SELECT serial, printer::text FROM rack_nozzle_seen ORDER BY 1"
        ).fetchall()
        picks = conn.execute(
            "SELECT queue_item_id, printer::text FROM rack_nozzle_picks ORDER BY 1"
        ).fetchall()
        rack_columns = conn.execute(
            "SELECT count(*) FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND column_name = 'printer_id'"
            " AND table_name IN ('rack_nozzle_seen', 'rack_nozzle_picks')"
        ).fetchone()

    # The Bambuddy key wins over the registry key of the same printer; a key no registry
    # printer matches is dropped.
    assert settings["printer_rack_algorithms"] == {LINKED: "newest_first", LAN: "x"}
    assert settings["printer_rack_algorithm_versions"] == {LINKED: 4, LAN: 1}
    assert settings["printer_print_options"] == {LINKED: {"a": 1}, LAN: {"c": 3}}
    assert "printer_id" not in settings
    assert settings["default_printer"] == LINKED
    assert choices == {
        "gear": {"printer": LINKED, "tier": "fine"},
        "keychain": {"printer": LAN, "tier": "draft"},
        "gone": {},
        "deleted": {},
    }
    assert targets == [(1, LINKED), (2, LAN)]
    assert "printer_id" not in target_columns
    assert seen == [("A", LINKED), ("B", LAN), ("C", None)]
    assert picks == [(10, LINKED), (11, None)]
    assert rack_columns == (0,)


def test_the_contract_migration_drops_a_default_no_registry_printer_links(
    pg_conninfo: str,
) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[: _before(CONTRACT)])
        conn.execute("INSERT INTO settings (name, value) VALUES ('printer_id', '7')")
        migrate(conn)
        names = [row[0] for row in conn.execute("SELECT name FROM settings").fetchall()]
    assert "printer_id" not in names and "default_printer" not in names


def test_the_project_target_migration_keeps_every_row(pg_conninfo: str) -> None:
    migration = MIGRATIONS_DIR / "20261101T1910Z_project_print_targets_registry.sql"
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[: _before(migration)])
        conn.execute(
            "INSERT INTO project_print_targets (project_id, printer_id) VALUES (1, 3), (2, 0)"
        )
        migrate(conn, MIGRATIONS[: _before(CONTRACT)])
        rows = conn.execute(
            "SELECT project_id, printer_id, printer FROM project_print_targets ORDER BY 1"
        ).fetchall()
    assert rows == [(1, 3, None), (2, 0, None)]


def test_the_rack_migration_backfills_the_registry_printer(pg_conninfo: str) -> None:
    migration = MIGRATIONS_DIR / "20261101T1911Z_rack_nozzle_registry_printer.sql"
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[: _before(migration)])
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
        migrate(conn, MIGRATIONS[: _before(CONTRACT)])
        seen = conn.execute(
            "SELECT serial, printer::text FROM rack_nozzle_seen ORDER BY 1"
        ).fetchall()
        picks = conn.execute("SELECT printer::text FROM rack_nozzle_picks").fetchall()
    assert seen == [("A", linked[0]), ("B", None)]
    assert picks == [(linked[0],)]


def _settings(pg_pool: PgPool) -> dict[str, object]:
    with pg_pool.connection() as conn:
        rows = conn.execute(
            "SELECT name, value FROM settings WHERE name = ANY(%s)",
            (
                [
                    "default_printer",
                    "printer_rack_algorithms",
                    "printer_rack_algorithm_versions",
                    "printer_print_options",
                    "print_auto_import_skip_lan_printers",
                ],
            ),
        ).fetchall()
    return {row["name"]: row["value"] for row in rows}


def _put(pg_pool: PgPool, values: dict[str, object]) -> None:
    with pg_pool.connection() as conn:
        for name, value in values.items():
            conn.execute(
                "INSERT INTO settings (name, value) VALUES (%s, %s)"
                " ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value",
                (name, Jsonb(value)),
            )


def test_a_merge_moves_the_default_printer_and_its_registry_keyed_entries(
    printers: PrinterStore, pg_pool: PgPool
) -> None:
    """The default printer, a map entry keyed by the gone row and a subject's printer
    follow it to the kept row, as a column would; an entry the kept row has of its own
    wins."""
    synthetic = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C")
    mine = printers.create(PrinterCreate(serial="0938AB000000026", name="Mine"))
    _put(
        pg_pool,
        {
            "default_printer": synthetic.id,
            "printer_rack_algorithms": {synthetic.id: "minimize_swaps", "7": "x"},
            "printer_rack_algorithm_versions": {synthetic.id: 2},
            "printer_print_options": {synthetic.id: {"a": 1}, mine.id: {"b": 2}},
        },
    )
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO model_print_choices (model_id, choices) VALUES ('gear', %s)",
            (Jsonb({"printer": synthetic.id, "tier": "fine"}),),
        )

    printers.ensure_bambuddy(4, serial="0938AB000000026", name="H2C", model="H2C")

    assert printers.get(synthetic.id) is None
    assert _settings(pg_pool) == {
        "default_printer": mine.id,
        "printer_rack_algorithms": {mine.id: "minimize_swaps", "7": "x"},
        "printer_rack_algorithm_versions": {mine.id: 2},
        "printer_print_options": {mine.id: {"b": 2}},
    }
    with pg_pool.connection() as conn:
        row = conn.execute(
            "SELECT choices FROM model_print_choices WHERE model_id = 'gear'"
        ).fetchone()
    assert row is not None and row["choices"] == {"printer": mine.id, "tier": "fine"}


def test_deleting_a_printer_forgets_it_as_the_default_and_in_the_maps(
    printers: PrinterStore, pg_pool: PgPool
) -> None:
    """A deleted default would refuse every run naming no printer (`_named_printer`)."""
    row = printers.create(PrinterCreate(serial="0938AB000000027", name="LAN"))
    other = printers.create(PrinterCreate(serial="0938AB000000028", name="Other"))
    _put(
        pg_pool,
        {
            "default_printer": row.id,
            "printer_rack_algorithms": {row.id: "minimize_swaps", other.id: "x"},
            "printer_print_options": {row.id: {"a": 1}},
        },
    )

    printers.delete(row.id)

    assert _settings(pg_pool) == {
        "printer_rack_algorithms": {other.id: "x"},
        "printer_print_options": {},
    }


def test_the_watchers_skip_list_follows_a_merge_and_forgets_a_delete(
    printers: PrinterStore, pg_pool: PgPool
) -> None:
    """#4699 phase 4: `print_auto_import_skip_lan_printers` holds registry ids, so a
    merged row's id becomes the kept row's (listed once), and a deleted row's goes."""
    synthetic = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C")
    mine = printers.create(PrinterCreate(serial="0938AB000000029", name="Mine"))
    other = printers.create(PrinterCreate(serial="0938AB000000030", name="Other"))
    _put(pg_pool, {"print_auto_import_skip_lan_printers": [synthetic.id, other.id]})

    printers.ensure_bambuddy(4, serial="0938AB000000029", name="H2C", model="H2C")
    assert _settings(pg_pool) == {"print_auto_import_skip_lan_printers": [other.id, mine.id]}

    _put(pg_pool, {"print_auto_import_skip_lan_printers": [mine.id, other.id]})
    printers.delete(other.id)
    assert _settings(pg_pool) == {"print_auto_import_skip_lan_printers": [mine.id]}
