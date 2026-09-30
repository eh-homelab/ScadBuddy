"""#313 — the print dialog's choices per Bambuddy library file, in Postgres."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from scadbuddy.bambuddy.models import NozzleChoice, SlotChoice
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import ModelPrintChoices, SettingsStore
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
        assert conn.execute("SELECT count(*) FROM library_print_choices").fetchone() == (0,)


def test_a_row_that_no_longer_validates_is_nothing_remembered(
    store: SettingsStore, pg_conninfo: str
) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(
            "INSERT INTO library_print_choices (file_id, choices) VALUES (89, %s::jsonb)",
            ('{"nozzles": [{"size": "0.4"}, {"size": "0.4"}, {"size": "0.4"}]}',),
        )

    assert store.library_choices(89) == ModelPrintChoices()


def test_forget_all_forgets_the_library_files_choices_too(store: SettingsStore) -> None:
    """#322's "Forget all" drops every remembered choice, a library file's included."""
    store.set_library_choices(89, CHOSEN)

    store.forget_remembered()

    assert store.library_choices(89) == ModelPrintChoices()
