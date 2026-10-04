"""``SettingsStore`` in Postgres, under concurrent writers (PR #335 review 1).

The print dialog fires two remember PUTs back to back on every print and FastAPI runs
each sync handler on its own threadpool thread, so two setters really do overlap. A
store that read everything, changed one entry and wrote everything back would lose one
of them; each write here touches only its own row, and a map-valued setting merges its
one key inside the row's upsert.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import PoolTimeout

from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.core.events import Event, InProcessEventBus, SettingsChanged
from scadbuddy.core.settings import Settings
from scadbuddy.library import settings_store
from scadbuddy.library.settings_store import (
    STORE_READINESS_LOCK,
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
    store.save(SettingsPatch(printer_id=4, public_url="https://scad.example"))

    loaded = store.load()
    assert loaded.model_print_choices["gear"].tier == "fine"
    assert loaded.printer_bed_types == {"1": "Cool Plate"}
    assert loaded.printer_id == 4


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
        store.save(SettingsPatch(render_timeout=45.0, printer_id=3))
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
    store.save(SettingsPatch(printer_id=4, render_timeout=33.0))
    store.set_model_choices("gear", ModelPrintChoices(tier="fine"))
    store.set_printer_bed_type(1, "Cool Plate")
    store.save_print_options("global", None, PrintOptions(timelapse=True))
    store.save_print_options("printer", "1", PrintOptions(vibration_cali=False))
    store.save_print_options("model", "gear", PrintOptions(use_ams=True))

    store.forget_remembered()

    loaded = store.load()
    assert loaded.model_print_choices == {}
    assert loaded.printer_bed_types == {}
    assert loaded.print_options.is_empty()
    assert loaded.printer_print_options == {}
    assert loaded.model_print_options == {}
    assert loaded.printer_id == 4
    assert store.snapshot().runtime.render_timeout == 33.0


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
    store.save(SettingsPatch(reset=["store_backend"]))
    assert store.load().store_backend == "local"


def test_a_printers_rack_algorithm_round_trips_and_is_forgotten(
    store: SettingsStore, settings: Settings
) -> None:
    """#836: kept in the jsonb ``settings`` row, one printer at a time, no own table."""
    store.set_printer_rack_algorithm(1, "oldest_first")
    store.set_printer_rack_algorithm(2, "bambuddy")
    loaded = _fresh_load(settings)
    assert loaded.printer_rack_algorithms == {"1": "oldest_first", "2": "bambuddy"}
    assert (loaded.rack_algorithm(1), loaded.rack_algorithm(3), loaded.rack_algorithm(None)) == (
        "oldest_first",
        "least_used",
        "least_used",
    )

    assert store.set_printer_rack_algorithm(2, None) == "least_used"
    assert _fresh_load(settings).printer_rack_algorithms == {"1": "oldest_first"}

    store.forget_remembered()
    assert _fresh_load(settings).printer_rack_algorithms == {}


def test_a_rack_algorithm_write_held_up_gives_up_and_never_lands_later(
    store: SettingsStore, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1129: the print dialog gives up on a save after 25 s and sends the next choice.
    A save that is still waiting in Postgres must give up first, or it could commit
    after the one that replaced it and leave the printer on the older choice."""
    monkeypatch.setattr(settings_store, "RACK_ALGORITHM_WRITE_TIMEOUT", 0.2)
    store.set_printer_rack_algorithm(1, "oldest_first")
    failed: list[BaseException] = []

    def save() -> None:
        try:
            store.set_printer_rack_algorithm(1, "bambuddy")
        except Exception as exc:
            failed.append(exc)

    with psycopg.connect(settings.database_url) as holder, holder.transaction():
        holder.execute("SELECT 1 FROM settings WHERE name = 'printer_rack_algorithms' FOR UPDATE")
        took = _timed_in_thread(save)
    assert took < 2.0
    assert [type(exc) for exc in failed] == [psycopg.errors.QueryCanceled]
    assert _fresh_load(settings).printer_rack_algorithms == {"1": "oldest_first"}


def test_a_rack_algorithm_save_gives_up_waiting_for_a_connection(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1129: the pool wait is bounded too, not only the write."""
    monkeypatch.setattr(settings_store, "RACK_ALGORITHM_WRITE_TIMEOUT", 0.2)
    store = SettingsStore(settings.model_copy(update={"database_pool_size": 1}))
    store.open()
    store.set_printer_rack_algorithm(1, "oldest_first")
    failed: list[BaseException] = []

    def save() -> None:
        try:
            store.set_printer_rack_algorithm(1, "bambuddy")
        except Exception as exc:
            failed.append(exc)

    try:
        with store._pool.connection():
            took = _timed_in_thread(save)
    finally:
        store.close()
    assert took < 2.0
    assert [type(exc) for exc in failed] == [PoolTimeout]
    assert _fresh_load(settings).printer_rack_algorithms == {"1": "oldest_first"}


def test_a_committed_rack_algorithm_save_answers_without_reading_everything_back(
    store: SettingsStore, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1129 review: a save that committed must not then stall on an unbounded read of
    every setting, or the dialog gives up on a value that is stored."""
    monkeypatch.setattr(settings_store, "RACK_ALGORITHM_WRITE_TIMEOUT", 0.2)
    answered: list[str] = []
    with psycopg.connect(settings.database_url) as holder, holder.transaction():
        holder.execute("LOCK TABLE printer_bed_types IN ACCESS EXCLUSIVE MODE")
        took = _timed_in_thread(
            lambda: answered.append(store.set_printer_rack_algorithm(1, "bambuddy"))
        )
    assert took < 2.0
    assert answered == ["bambuddy"]
    assert _fresh_load(settings).printer_rack_algorithms == {"1": "bambuddy"}


def _timed_in_thread(call: Callable[[], object]) -> float:
    """Run ``call`` in a thread, give it 5 s, and return how long it took."""
    started = time.monotonic()
    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    return time.monotonic() - started


def test_an_unknown_stored_rack_algorithm_is_dropped_not_fatal() -> None:
    """A newer version's algorithm must not stop this one loading its settings."""
    loaded = StoredSettings.model_validate(
        {"printer_rack_algorithms": {"1": "newest_first", "2": "from-the-future"}}
    )
    assert loaded.printer_rack_algorithms == {"1": "newest_first"}


def test_a_save_that_would_leave_the_store_unready_with_another_waits_and_is_refused(
    store: SettingsStore, settings: Settings
) -> None:
    """Clearing the URL and switching to Bambuddy are each safe alone. While one save
    (here, by hand) holds the readiness lock and has cleared the URL, the switch waits,
    then sees the clear and is refused: the store is never Bambuddy without a URL."""
    store.save(SettingsPatch(bambuddy_url="https://b.test", library_folder_id=7))
    errors: list[Exception] = []

    def switch() -> None:
        try:
            store.save(SettingsPatch(store_backend="bambuddy"))
        except Exception as error:
            errors.append(error)

    with psycopg.connect(settings.database_url) as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (STORE_READINESS_LOCK,)
        )
        conn.execute(
            "INSERT INTO settings (name, value) VALUES ('bambuddy_url', 'null'::jsonb)"
            " ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value"
        )
        thread = threading.Thread(target=switch)
        thread.start()
        thread.join(0.5)
        assert thread.is_alive()  # the switch waits on the lock
        conn.commit()
    thread.join(10)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], StoreNotReadyError)
    loaded = _fresh_load(settings)
    assert (loaded.store_backend, loaded.bambuddy_url) == ("local", None)
