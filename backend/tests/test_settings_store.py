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
    SettingsPatch,
    SettingsStore,
    StoredSettings,
)
from scadbuddy.render.pg_store import MIGRATIONS

#: Threads per race, and how many times a race is run.
WRITERS = 8
ROUNDS = 5


@pytest.fixture
def settings(tmp_path: Path, pg_conninfo: str) -> Settings:
    return Settings(data_dir=tmp_path, database_url=pg_conninfo)


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


# -- #322: every runtime setting env-seeded, with its source --------------------------


def _store_over(settings: Settings) -> SettingsStore:
    opened = SettingsStore(settings)
    opened.open()
    return opened


def test_each_field_says_where_its_value_came_from(settings: Settings) -> None:
    deployed = settings.model_copy(
        update={"render_timeout": 30.0, "public_url": "https://env.example", "job_ttl": 60.0}
    )
    store = _store_over(deployed)
    try:
        store.save(SettingsPatch(job_ttl=90.0, public_url=None))
        snapshot = store.snapshot()
    finally:
        store.close()
    assert snapshot.sources["render_timeout"] == "env"
    assert snapshot.runtime.render_timeout == 30.0
    assert snapshot.sources["job_ttl"] == "stored"
    assert snapshot.runtime.job_ttl == 90.0
    assert snapshot.sources["public_url"] == "cleared"
    assert snapshot.stored.public_url is None
    assert snapshot.sources["render_concurrency"] == "default"
    # The bootstrap fields are not the store's to report.
    assert "database_url" not in snapshot.sources


def test_an_env_var_added_after_other_settings_were_saved_is_honoured(
    settings: Settings,
) -> None:
    """Review focus (#322): the ENV_SEEDED semantics, extended to every field."""
    store = _store_over(settings)
    try:
        store.save(SettingsPatch(render_timeout=45.0, pipeline_id=3))
    finally:
        store.close()
    later = settings.model_copy(update={"job_ttl": 600.0, "log_level": "DEBUG"})
    store = _store_over(later)
    try:
        snapshot = store.snapshot()
    finally:
        store.close()
    assert snapshot.runtime.job_ttl == 600.0
    assert snapshot.sources["job_ttl"] == "env"
    assert snapshot.runtime.log_level == "DEBUG"
    # What the UI did save still wins.
    assert snapshot.runtime.render_timeout == 45.0


def test_reset_on_a_cleared_field_brings_the_environment_back(
    settings: Settings, pg_conninfo: str
) -> None:
    """Review focus (#322): the reset drops the "cleared" row, and env is back."""
    deployed = settings.model_copy(update={"default_plate": "H2C"})
    store = _store_over(deployed)
    try:
        store.save(SettingsPatch(default_plate=None))
        assert store.snapshot().sources["default_plate"] == "cleared"
        assert store.load().default_plate is None

        store.save(SettingsPatch(reset=["default_plate"]))
        snapshot = store.snapshot()
    finally:
        store.close()
    assert snapshot.stored.default_plate == "H2C"
    assert snapshot.sources["default_plate"] == "env"
    with psycopg.connect(pg_conninfo) as conn:
        row = conn.execute("SELECT 1 FROM settings WHERE name = 'default_plate'").fetchone()
    assert row is None


def test_reset_on_a_stored_value_follows_the_environment_then_the_default(
    settings: Settings,
) -> None:
    deployed = settings.model_copy(update={"render_timeout": 30.0})
    store = _store_over(deployed)
    try:
        store.save(SettingsPatch(render_timeout=10.0, lsp_sessions=2))
        store.save(SettingsPatch(reset=["render_timeout", "lsp_sessions"]))
        snapshot = store.snapshot()
    finally:
        store.close()
    assert (snapshot.runtime.render_timeout, snapshot.sources["render_timeout"]) == (30.0, "env")
    assert (snapshot.runtime.lsp_sessions, snapshot.sources["lsp_sessions"]) == (4, "default")


def test_a_stored_value_this_version_refuses_falls_back_rather_than_failing(
    store: SettingsStore, pg_conninfo: str
) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(
            "INSERT INTO settings (name, value) VALUES ('render_concurrency', '0'),"
            " ('render_timeout', '\"soon\"')"
        )
    snapshot = store.snapshot()
    assert snapshot.runtime.render_concurrency == 2
    assert snapshot.sources["render_concurrency"] == "default"
    assert snapshot.runtime.render_timeout == 120.0


def test_the_google_fonts_key_is_stored_and_an_empty_one_clears_it(store: SettingsStore) -> None:
    store.save(SettingsPatch(google_fonts_api_key="g-key"))
    assert store.snapshot().runtime.google_fonts_api_key == "g-key"
    store.save(SettingsPatch(google_fonts_api_key=""))
    snapshot = store.snapshot()
    assert snapshot.runtime.google_fonts_api_key is None
    assert snapshot.sources["google_fonts_api_key"] == "cleared"


def test_the_patch_refuses_what_the_ui_may_not_set() -> None:
    with pytest.raises(ValueError, match="render_concurrency"):
        SettingsPatch.model_validate({"render_concurrency": 0})
    with pytest.raises(ValueError, match="cannot be cleared"):
        SettingsPatch.model_validate({"render_timeout": None})
    with pytest.raises(ValueError, match="openscad"):
        SettingsPatch.model_validate({"openscad": "/bin/sh"})
    with pytest.raises(ValueError, match="not an env-seeded setting"):
        SettingsPatch.model_validate({"reset": ["data_dir"]})
    with pytest.raises(ValueError, match="both set and reset"):
        SettingsPatch.model_validate({"render_timeout": 5, "reset": ["render_timeout"]})


def test_forget_all_clears_the_remembered_choices_and_nothing_else(
    store: SettingsStore,
) -> None:
    store.save(SettingsPatch(pipeline_id=4, render_timeout=33.0))
    store.set_model_pipeline("gear", 2)
    store.set_model_choices("gear", ModelPrintChoices(tier="fine"))
    store.set_printer_bed_type(1, "Cool Plate")
    store.save_print_options("global", None, PrintOptions(timelapse=True))
    store.save_print_options("printer", "1", PrintOptions(vibration_cali=False))
    store.save_print_options("model", "gear", PrintOptions(use_ams=True))

    store.forget_remembered()

    loaded = store.load()
    assert loaded.model_pipelines == {}
    assert loaded.model_print_choices == {}
    assert loaded.printer_bed_types == {}
    assert loaded.print_options.is_empty()
    assert loaded.printer_print_options == {}
    assert loaded.model_print_options == {}
    assert loaded.pipeline_id == 4
    assert store.snapshot().runtime.render_timeout == 33.0
