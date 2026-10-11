"""#313 — the print dialog's choices per Bambuddy library file, in Postgres: since #1754
in ``model_print_choices`` under the file's options scope (``library:<file id>``), the
one store a model's choices are in."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from scadbuddy.bambuddy.models import NozzleChoice, SlotChoice
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import ModelPrintChoices, SettingsStore
from scadbuddy.render.pg_store import MIGRATIONS, MIGRATIONS_DIR, migrate
from tests.conftest import UNUSED_TEMPORAL_ADDRESS

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def store(tmp_path: Path, pg_conninfo: str) -> Iterator[SettingsStore]:
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


CHOSEN = ModelPrintChoices(
    printer_id=1,
    filament_plan=[SlotChoice(slot_id=1, spool_id=9)],
    nozzles=[NozzleChoice(size="0.2")],
    tier="fine",
)
UNREADABLE = '{"nozzles": [{"size": "0.4"}, {"size": "0.4"}, {"size": "0.4"}]}'


def test_nothing_remembered_is_the_empty_choice(store: SettingsStore) -> None:
    assert store.library_choices(89) == ModelPrintChoices()


def test_a_files_choices_are_its_own(store: SettingsStore) -> None:
    store.set_library_choices(89, CHOSEN)

    assert store.library_choices(89) == CHOSEN.model_copy(
        update={"nozzles": [NozzleChoice(size="0.2"), NozzleChoice(size="0.2")]}
    )
    assert store.library_choices(67) == ModelPrintChoices()


def test_remembering_nothing_deletes_the_row(store: SettingsStore, pg_conninfo: str) -> None:
    store.set_library_choices(89, CHOSEN)
    store.set_library_choices(89, ModelPrintChoices())

    with psycopg.connect(pg_conninfo) as conn:
        assert conn.execute("SELECT count(*) FROM model_print_choices").fetchone() == (0,)


def test_a_row_that_no_longer_validates_is_nothing_remembered(
    store: SettingsStore, pg_conninfo: str
) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(
            "INSERT INTO model_print_choices (model_id, choices) VALUES ('library:89', %s::jsonb)",
            (UNREADABLE,),
        )

    assert store.library_choices(89) == ModelPrintChoices()


def test_forget_all_forgets_the_library_files_choices_too(store: SettingsStore) -> None:
    """#322's "Forget all" drops every remembered choice, a library file's included."""
    store.set_library_choices(89, CHOSEN)

    store.forget_remembered()

    assert store.library_choices(89) == ModelPrintChoices()


def test_a_files_choices_are_in_the_one_store_under_its_subject(store: SettingsStore) -> None:
    """C1 (#1754): a file's choices sit beside the models', keyed by its options scope,
    so Settings lists them with every other remembered choice."""
    store.set_library_choices(89, CHOSEN)

    assert set(store.load().model_print_choices) == {"library:89"}


def test_an_unreadable_row_does_not_stop_the_settings_loading(
    store: SettingsStore, pg_conninfo: str
) -> None:
    """A row this version cannot read (the migration copies a library file's verbatim)
    is left out of the snapshot, as ``library_choices`` always read one: the other rows
    and every setting still load."""
    store.set_model_choices("demo", CHOSEN)
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(
            "INSERT INTO model_print_choices (model_id, choices) VALUES ('library:89', %s::jsonb)",
            (UNREADABLE,),
        )

    assert set(store.load().model_print_choices) == {"demo"}


MIGRATION = MIGRATIONS_DIR / "20261009T0541Z_print_choices_by_subject.sql"


def _rows(conn: psycopg.Connection[Any]) -> list[Any]:
    return conn.execute(
        "SELECT model_id, choices, updated_at FROM model_print_choices ORDER BY model_id"
    ).fetchall()


def test_the_migration_moves_every_library_row_and_is_idempotent(pg_conninfo: str) -> None:
    """Every ``library_print_choices`` row lands under ``library:<file id>`` with its
    choices and time; applying the migration again changes nothing; a model's row is
    left alone; and on a key already present the newer row wins. The table itself is
    dropped by a later migration (#1963), so this stops just before the copy."""
    copy = next(i for i, m in enumerate(MIGRATIONS) if m.id == MIGRATION.stem)
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[:copy])
        conn.execute(
            "INSERT INTO model_print_choices (model_id, choices, updated_at) VALUES"
            """ ('demo', '{"printer_id": 1}', '2026-01-01'),"""
            """ ('library:3', '{"printer_id": 30}', '2026-03-01'),"""
            """ ('library:4', '{"printer_id": 40}', '2026-01-01')"""
        )
        conn.execute(
            "INSERT INTO library_print_choices (file_id, choices, updated_at) VALUES"
            """ (1, '{"printer_id": 2}', '2026-02-01'),"""
            """ (2, '{"tier": "fine"}', '2026-02-02'),"""
            """ (3, '{"printer_id": 3}', '2026-02-03'),"""
            """ (4, '{"printer_id": 4}', '2026-02-04')"""
        )
        sql = MIGRATION.read_text()
        conn.execute(sql)
        once = _rows(conn)
        conn.execute(sql)
        assert _rows(conn) == once
        # The old table keeps its rows until #1963's migration drops it.
        assert conn.execute("SELECT count(*) FROM library_print_choices").fetchone() == (4,)
        migrate(conn)
        assert _rows(conn) == once
        assert conn.execute(
            "SELECT to_regclass('library_print_choices') IS NULL",
        ).fetchone() == (True,)

    assert [(key, choices) for key, choices, _ in once] == [
        ("demo", {"printer_id": 1}),
        ("library:1", {"printer_id": 2}),
        ("library:2", {"tier": "fine"}),
        # Remembered after the old row was written: kept.
        ("library:3", {"printer_id": 30}),
        # Older than the old row: the old row's choice wins.
        ("library:4", {"printer_id": 4}),
    ]
