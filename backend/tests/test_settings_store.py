"""``SettingsStore`` in Postgres, under concurrent writers (PR #335 review 1).

The print dialog fires two remember PUTs back to back on every print and FastAPI runs
each sync handler on its own threadpool thread, so two setters really do overlap. A
store that read everything, changed one entry and wrote everything back would lose one
of them; each write here touches only its own row, and a map-valued setting merges its
one key inside the row's upsert.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path

import psycopg
import pytest

from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.core.events import Event, InProcessEventBus, SettingsChanged
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import (
    ModelPrintChoices,
    RenderStoreSettings,
    SettingsPatch,
    SettingsStore,
    StoredSettings,
    StoreNotReadyError,
    load_render_store_settings,
)
from scadbuddy.render.pg_store import MIGRATIONS
from tests.conftest import UNUSED_TEMPORAL_ADDRESS

#: Threads per race, and how many times a race is run.
WRITERS = 8
ROUNDS = 5


@pytest.fixture
def settings(tmp_path: Path, pg_conninfo: str) -> Settings:
    return Settings(
        data_dir=tmp_path, database_url=pg_conninfo, temporal_address=UNUSED_TEMPORAL_ADDRESS
    )


@pytest.fixture
def store(settings: Settings) -> Iterator[SettingsStore]:
    opened = SettingsStore(settings)
    opened.open()
    try:
        yield opened
    finally:
        opened.close()


def _fresh_load(settings: Settings) -> StoredSettings:
    """Read through a store of its own, as another request or replica would."""
    reader = SettingsStore(settings)
    reader.open()
    try:
        return reader.load()
    finally:
        reader.close()


def _at_once(writes: list[Callable[[], object]]) -> None:
    """Run every write on its own thread, all released together."""
    start = threading.Barrier(len(writes))
    errors: list[BaseException] = []

    def run(write: Callable[[], object]) -> None:
        start.wait(timeout=10)
        try:
            write()
        except BaseException as error:  # pragma: no cover - reported below
            errors.append(error)

    threads = [threading.Thread(target=run, args=(write,)) for write in writes]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []


def test_open_creates_the_tables_and_records_the_migrations(
    store: SettingsStore, pg_conninfo: str
) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = current_schema()"
            )
        }
        ids = [row[0] for row in conn.execute("SELECT id FROM scadbuddy_migrations")]
    assert {"settings", "model_print_choices", "printer_bed_types"} <= tables
    assert sorted(ids) == [migration.id for migration in MIGRATIONS]


def test_a_model_choice_and_a_printer_plate_written_at_once_both_persist(
    store: SettingsStore, settings: Settings
) -> None:
    """The Print dialog's pair, which it sends back to back on every print."""
    for round_ in range(ROUNDS):
        _at_once(
            [
                partial(store.set_model_choices, "gear", ModelPrintChoices(printer_id=round_)),
                partial(store.set_printer_bed_type, 1, f"Plate {round_}"),
            ]
        )
        loaded = _fresh_load(settings)
        assert loaded.model_print_choices["gear"].printer_id == round_
        assert loaded.printer_bed_types == {"1": f"Plate {round_}"}


def test_many_models_remembered_at_once_all_persist(
    store: SettingsStore, settings: Settings
) -> None:
    for round_ in range(ROUNDS):
        _at_once(
            [
                partial(
                    store.set_model_choices,
                    f"model-{model}",
                    ModelPrintChoices(printer_id=round_ * 100 + model),
                )
                for model in range(WRITERS)
            ]
        )
        remembered = _fresh_load(settings).model_print_choices
        assert {slug: choice.printer_id for slug, choice in remembered.items()} == {
            f"model-{m}": round_ * 100 + m for m in range(WRITERS)
        }


def test_many_printers_plates_remembered_at_once_all_persist(
    store: SettingsStore, settings: Settings
) -> None:
    _at_once(
        [
            partial(store.set_printer_bed_type, printer, f"Plate {printer}")
            for printer in range(WRITERS)
        ]
    )
    assert _fresh_load(settings).printer_bed_types == {str(p): f"Plate {p}" for p in range(WRITERS)}


def test_keys_of_one_map_setting_written_at_once_all_persist(
    store: SettingsStore, settings: Settings
) -> None:
    """Every printer's print options share one ``settings`` row; the merge is in the
    upsert, under the row's lock, so no writer's key is lost to another's."""
    _at_once(
        [
            partial(
                store.save_print_options,
                "printer",
                str(printer),
                PrintOptions(timelapse=printer % 2 == 0),
            )
            for printer in range(WRITERS)
        ]
    )
    stored = _fresh_load(settings).printer_print_options
    assert {key: options.timelapse for key, options in stored.items()} == {
        str(p): p % 2 == 0 for p in range(WRITERS)
    }


def test_a_key_removed_while_another_is_written_leaves_only_the_other(
    store: SettingsStore,
) -> None:
    store.save_print_options("model", "gone", PrintOptions(timelapse=True))
    _at_once(
        [
            lambda: store.save_print_options("model", "gone", PrintOptions()),
            lambda: store.save_print_options("model", "kept", PrintOptions(timelapse=False)),
        ]
    )
    assert list(store.load().model_print_options) == ["kept"]


def test_a_connection_save_leaves_the_remembered_choices_alone(store: SettingsStore) -> None:
    store.set_model_choices("gear", ModelPrintChoices(tier="fine"))
    store.set_printer_bed_type(1, "Cool Plate")
    store.save(SettingsPatch(pipeline_id=4, public_url="https://scad.example"))

    loaded = store.load()
    assert loaded.model_print_choices["gear"].tier == "fine"
    assert loaded.printer_bed_types == {"1": "Cool Plate"}
    assert loaded.pipeline_id == 4


def test_forgetting_removes_the_row(store: SettingsStore, pg_conninfo: str) -> None:
    store.set_model_choices("gear", ModelPrintChoices(tier="fine"))
    store.set_printer_bed_type(1, "Cool Plate")
    store.set_model_choices("gear", ModelPrintChoices())
    store.set_printer_bed_type(1, None)

    with psycopg.connect(pg_conninfo) as conn:
        choices = conn.execute("SELECT count(*) FROM model_print_choices").fetchone()
        plates = conn.execute("SELECT count(*) FROM printer_bed_types").fetchone()
    assert choices == (0,)
    assert plates == (0,)


def test_settings_changed_is_published_once_the_write_is_visible(settings: Settings) -> None:
    """A listener that re-reads on ``settings.changed`` sees the change announced."""
    bus = InProcessEventBus()
    seen: list[str | None] = []

    def reread(event: Event) -> None:
        if isinstance(event, SettingsChanged):
            seen.append(_fresh_load(settings).printer_bed_types.get("1"))

    bus.add_listener(reread)
    store = SettingsStore(settings, events=bus)
    store.open()
    try:
        store.set_printer_bed_type(1, "Cool Plate")
    finally:
        store.close()
    assert seen == ["Cool Plate"]


def test_the_render_key_is_seeded_stored_and_cleared_like_the_full_key(
    tmp_path: Path, pg_conninfo: str
) -> None:
    seeded = Settings(
        data_dir=tmp_path,
        database_url=pg_conninfo,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        bambuddy_render_api_key="from-env",
    )
    store = SettingsStore(seeded)
    store.open()
    try:
        assert store.load().bambuddy_render_api_key == "from-env"
        store.save(SettingsPatch(bambuddy_render_api_key="rotated"))
        assert store.load().bambuddy_render_api_key == "rotated"
        store.save(SettingsPatch(bambuddy_render_api_key=""))
        assert store.load().bambuddy_render_api_key is None  # cleared beats the env
    finally:
        store.close()


def test_render_workers_fall_back_to_the_full_key_and_say_so(store: SettingsStore) -> None:
    assert store.load().render_bambuddy_key() == (None, False)  # no key is not a fallback
    store.save(SettingsPatch(bambuddy_api_key="full"))
    assert store.load().render_bambuddy_key() == ("full", True)
    store.save(SettingsPatch(bambuddy_render_api_key="narrow"))
    assert store.load().render_bambuddy_key() == ("narrow", False)


def test_a_render_worker_gets_the_narrow_key_the_url_and_the_inbox(
    store: SettingsStore, settings: Settings
) -> None:
    store.save(
        SettingsPatch(
            bambuddy_url="https://b.test",
            bambuddy_api_key="full",
            bambuddy_render_api_key="narrow",
            library_folder_id=7,
            pipeline_id=3,
        )
    )
    assert load_render_store_settings(store.pool, settings) == RenderStoreSettings(
        store_backend="local",
        bambuddy_url="https://b.test",
        api_key="narrow",
        key_is_fallback=False,
        library_folder_id=7,
    )


def test_a_render_key_cleared_in_settings_beats_the_env_on_workers(
    tmp_path: Path, pg_conninfo: str
) -> None:
    seeded = Settings(
        data_dir=tmp_path,
        database_url=pg_conninfo,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        bambuddy_api_key="full",
        bambuddy_render_api_key="from-env",
    )
    store = SettingsStore(seeded)
    store.open()
    try:
        before = load_render_store_settings(store.pool, seeded)
        assert (before.api_key, before.key_is_fallback) == ("from-env", False)
        store.save(SettingsPatch(bambuddy_render_api_key=""))
        after = load_render_store_settings(store.pool, seeded)
        assert (after.api_key, after.key_is_fallback) == ("full", True)
    finally:
        store.close()


def test_the_bambuddy_store_needs_a_url_and_an_inbox_first(store: SettingsStore) -> None:
    with pytest.raises(StoreNotReadyError, match="library folder"):
        store.save(SettingsPatch(store_backend="bambuddy"))
    store.save(
        SettingsPatch(bambuddy_url="https://b.test", library_folder_id=7, store_backend="bambuddy")
    )
    assert store.load().store_backend == "bambuddy"
    store.save(SettingsPatch(store_backend=None))
    assert store.load().store_backend == "local"
