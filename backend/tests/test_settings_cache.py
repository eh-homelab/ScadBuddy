"""`SettingsStore.get` and the per-process cache of the plain settings (#7384)."""

from __future__ import annotations

import asyncio
import re
import typing
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from temporalio.testing import ActivityEnvironment

from scadbuddy.core.pg_listener import PgListener
from scadbuddy.core.settings import Settings
from scadbuddy.core.settings_cache import SETTINGS_CHANNEL, SettingsCache, settings_written
from scadbuddy.library import settings_store
from scadbuddy.library.settings_store import (
    DEFAULT_PLATE,
    LAN_REQUIRE_PLATE_CLEAR,
    OPERATION_RETENTION_SECONDS,
    PUBLIC_URL,
    Setting,
    SettingsPatch,
    SettingsStore,
    StoredSettings,
)
from scadbuddy.operations.store import OperationStore
from scadbuddy.printers import store as printer_store
from scadbuddy.printers.models import PrinterCreate
from scadbuddy.printers.store import PrinterStore
from scadbuddy.workflows.operation_activities import operation_activities
from scadbuddy.workflows.operation_models import InsertOp, OperationInput
from tests.conftest import UNUSED_TEMPORAL_ADDRESS
from tests.printers.conftest import KEK


@pytest.fixture
def settings(tmp_path: Path, pg_conninfo: str) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_url=pg_conninfo,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        # Env-seeded: the environment's value until the UI stores or clears one.
        public_url="https://env.example",
        lan_require_plate_clear=True,
    )


def _opened(settings: Settings, **kwargs: Any) -> SettingsStore:
    store = SettingsStore(settings, **kwargs)
    store.open()
    return store


@pytest.fixture
def store(settings: Settings) -> Iterator[SettingsStore]:
    opened = _opened(settings)
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def other(settings: Settings) -> Iterator[SettingsStore]:
    """A second store with a pool of its own: another API replica."""
    opened = _opened(settings)
    try:
        yield opened
    finally:
        opened.close()


@asynccontextmanager
async def _listening(conninfo: str, store: SettingsStore) -> AsyncIterator[asyncio.Event]:
    """``store`` following a running listener, as the API's does; yields an event set at
    each drop the listener's notification makes."""
    listener = PgListener(conninfo, check_interval=1.0)
    dropped = asyncio.Event()
    store.follow(listener)
    cache = store.cache
    drop = cache.drop

    def heard() -> None:
        drop()
        dropped.set()

    cache.drop = heard  # type: ignore[method-assign]
    up = asyncio.Event()
    listener.on_state(lambda connected: up.set() if connected else None)
    task = asyncio.create_task(listener.run())
    try:
        await asyncio.wait_for(up.wait(), 10)
        # `on_state` runs before the channels' `on_connect`; both run before the
        # listener's task next waits, which is when this one resumes.
        await asyncio.sleep(0)
        assert cache.live
        yield dropped
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


# ── get matches load ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("cached", [False, True])
async def test_get_answers_what_load_does_for_env_seeded_defaulted_and_stored_values(
    store: SettingsStore, settings: Settings, cached: bool
) -> None:
    async def check() -> None:
        loaded = store.load()
        runtime = store.snapshot().runtime
        assert store.get(PUBLIC_URL) == loaded.public_url
        assert store.get(DEFAULT_PLATE) == loaded.default_plate
        assert store.get(OPERATION_RETENTION_SECONDS) == loaded.operation_retention_seconds
        assert store.get(LAN_REQUIRE_PLATE_CLEAR) is runtime.lan_require_plate_clear

    async def steps() -> None:
        # The environment's value, and the defaults.
        await check()
        assert store.get(PUBLIC_URL) == "https://env.example"
        assert store.get(DEFAULT_PLATE) is None
        assert store.get(OPERATION_RETENTION_SECONDS) is None
        # Stored values win.
        store.save(
            SettingsPatch(
                public_url="https://stored.example",
                default_plate="X1C",
                operation_retention_seconds=86400.0,
                lan_require_plate_clear=False,
            )
        )
        await check()
        assert store.get(PUBLIC_URL) == "https://stored.example"
        assert store.get(OPERATION_RETENTION_SECONDS) == 86400.0
        assert store.get(LAN_REQUIRE_PLATE_CLEAR) is False
        # A clear beats the environment; a reset follows it again.
        store.save(SettingsPatch(public_url=None))
        await check()
        assert store.get(PUBLIC_URL) is None
        store.save(SettingsPatch(reset=["public_url"]))
        await check()
        assert store.get(PUBLIC_URL) == "https://env.example"

    if cached:
        async with _listening(settings.database_url, store):
            await steps()
    else:
        await steps()


def test_a_stored_value_this_version_refuses_follows_the_environment_as_load_does(
    store: SettingsStore, settings: Settings
) -> None:
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO settings (name, value) VALUES ('lan_require_plate_clear', %s)",
            [Jsonb("not a switch")],
        )
    assert store.get(LAN_REQUIRE_PLATE_CLEAR) is store.snapshot().runtime.lan_require_plate_clear
    assert store.get(LAN_REQUIRE_PLATE_CLEAR) is True


def test_retention_is_bounded_as_load_bounds_it(store: SettingsStore, settings: Settings) -> None:
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO settings (name, value) VALUES ('operation_retention_seconds', %s)",
            [Jsonb(1e30)],
        )
    assert store.get(OPERATION_RETENTION_SECONDS) == settings_store.RETENTION_MAX_SECONDS
    assert store.get(OPERATION_RETENTION_SECONDS) == store.load().operation_retention_seconds


@pytest.mark.parametrize(
    "name",
    [
        "bambuddy_api_key",
        "store_s3_secret_access_key",
        "git_remote_token",
        "model_print_choices",
        "printer_plates",
        "printer_id",
        "bambuddy_links",
        "no_such_setting",
    ],
)
def test_a_secret_another_table_or_no_setting_is_refused(name: str) -> None:
    with pytest.raises(ValueError):
        Setting[Any](name)


def test_each_setting_key_is_typed_as_its_field() -> None:
    hints = typing.get_type_hints(settings_store)
    keys = {
        name: hint
        for name, hint in hints.items()
        if typing.get_origin(hint) is Setting and isinstance(getattr(settings_store, name), Setting)
    }
    assert keys, "no Setting keys found"
    for name, hint in keys.items():
        field = getattr(settings_store, name).name
        model = StoredSettings if field in StoredSettings.model_fields else Settings
        assert typing.get_args(hint) == (model.model_fields[field].annotation,), name


# ── the cache across replicas ────────────────────────────────────────────────────


async def test_a_write_on_another_replica_is_seen_after_its_notification(
    store: SettingsStore, other: SettingsStore, settings: Settings
) -> None:
    async with _listening(settings.database_url, store) as dropped:
        assert store.get(PUBLIC_URL) == "https://env.example"
        # Blocking the loop: the listener cannot hear it yet, so the cache answers.
        other.save(SettingsPatch(public_url="https://other.example"))
        assert store.get(PUBLIC_URL) == "https://env.example"
        await asyncio.wait_for(dropped.wait(), 10)
        assert store.get(PUBLIC_URL) == "https://other.example"
        assert store.load().public_url == "https://other.example"


async def test_any_writer_of_a_settings_row_drops_every_replicas_cache(
    store: SettingsStore, settings: Settings
) -> None:
    async with _listening(settings.database_url, store) as dropped:
        assert store.get(DEFAULT_PLATE) is None
        # Not through a store: the printer registry, a migration, a hand-run fix.
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO settings (name, value) VALUES ('default_plate', %s)", [Jsonb("A1")]
            )
        assert store.get(DEFAULT_PLATE) is None
        await asyncio.wait_for(dropped.wait(), 10)
        assert store.get(DEFAULT_PLATE) == "A1"


async def test_a_write_in_this_process_is_read_back_at_once(
    store: SettingsStore, settings: Settings
) -> None:
    async with _listening(settings.database_url, store):
        assert store.get(PUBLIC_URL) == "https://env.example"
        store.save(SettingsPatch(public_url="https://mine.example"))
        assert store.get(PUBLIC_URL) == "https://mine.example"
        store.save_print_options("global", None, store.load().print_options)
        # Another writer of the rows in this process (the printer registry).
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            conn.execute("UPDATE settings SET value = %s WHERE name = 'public_url'", [Jsonb("x")])
        settings_written()
        assert store.get(PUBLIC_URL) == "x"


async def test_an_entry_older_than_the_ttl_is_read_again(
    settings: Settings, other: SettingsStore
) -> None:
    store = _opened(settings, cache_ttl=0.0)
    try:
        async with _listening(settings.database_url, store):
            assert store.get(PUBLIC_URL) == "https://env.example"
            other.save(SettingsPatch(public_url="https://other.example"))
            # No notification heard yet (the loop is held), but the entry is too old.
            assert store.get(PUBLIC_URL) == "https://other.example"
    finally:
        store.close()


async def test_nothing_is_cached_without_a_listener(
    store: SettingsStore, other: SettingsStore, settings: Settings
) -> None:
    assert not store.cache.live
    assert store.get(PUBLIC_URL) == "https://env.example"
    other.save(SettingsPatch(public_url="https://other.example"))
    assert store.get(PUBLIC_URL) == "https://other.example"
    # And once a listener that ran stops, nothing is cached again.
    async with _listening(settings.database_url, store):
        assert store.get(PUBLIC_URL) == "https://other.example"
    assert not store.cache.live
    other.save(SettingsPatch(public_url="https://later.example"))
    assert store.get(PUBLIC_URL) == "https://later.example"


#: A plain setting the printer registry writes (`printers/store.py`
#: `PRINTER_LISTED_SETTINGS`).
_SKIP_LAN: Setting[list[str] | None] = Setting("print_auto_import_skip_lan_printers")


def _skip_lan(settings: Settings, ids: list[str]) -> None:
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO settings (name, value) VALUES ('print_auto_import_skip_lan_printers', %s)",
            [Jsonb(ids)],
        )


async def test_a_printer_delete_drops_the_cache_after_its_commit(
    store: SettingsStore, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    printers = PrinterStore(store.pool, KEK)
    printer = printers.create(PrinterCreate(serial="01P00A000000001", name="One")).id
    _skip_lan(settings, [printer])
    async with _listening(settings.database_url, store):
        assert store.get(_SKIP_LAN) == [printer]
        forget = printer_store._forget_settings

        def forget_then_read(conn: Any, printer_id: str) -> None:
            forget(conn, printer_id)
            # Another request reads before the delete commits: it sees the old rows.
            assert store.get(_SKIP_LAN) == [printer]

        monkeypatch.setattr(printer_store, "_forget_settings", forget_then_read)
        printers.delete(printer)
        # Blocking the loop: no notification heard, so only the registry's drop helps.
        assert store.get(_SKIP_LAN) == []


async def test_a_printer_merge_drops_the_cache_after_its_commit(
    store: SettingsStore, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    printers = PrinterStore(store.pool, KEK)
    synthetic = printers.ensure_bambuddy(7, serial=None, name="Seven", model=None).id
    kept = printers.create(PrinterCreate(serial="01P00A000000001", name="One")).id
    _skip_lan(settings, [synthetic])
    async with _listening(settings.database_url, store):
        assert store.get(_SKIP_LAN) == [synthetic]

        def read(conn: Any, gone: str, into: str) -> None:
            # Another request reads before the merge commits: it sees the old rows.
            assert store.get(_SKIP_LAN) == [synthetic]

        monkeypatch.setattr(
            printer_store, "PRINTER_REFERENCES", (*printer_store.PRINTER_REFERENCES, read)
        )
        printers.link_bambuddy(kept, 7)
        assert store.get(_SKIP_LAN) == [kept]


async def test_a_write_while_the_listener_reconnects_is_never_served_stale(
    store: SettingsStore, other: SettingsStore, settings: Settings
) -> None:
    """`on_state(False)`, a write whose notification nobody hears, then `on_connect`:
    the cache never answers the entry from before the gap."""
    listener = PgListener(settings.database_url, check_interval=0.2, backoff=0.1)
    store.follow(listener)
    cache = store.cache
    states: asyncio.Queue[bool] = asyncio.Queue()
    listener.on_state(states.put_nowait)
    task = asyncio.create_task(listener.run())
    try:
        assert await asyncio.wait_for(states.get(), 10) is True
        await asyncio.sleep(0)
        assert cache.live
        assert store.get(PUBLIC_URL) == "https://env.example"
        pid = listener.backend_pid
        assert pid is not None
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            conn.execute("SELECT pg_terminate_backend(%s)", [pid])
        assert await asyncio.wait_for(states.get(), 10) is False
        assert not cache.live
        # Written while nothing listens: its notification is lost.
        other.save(SettingsPatch(public_url="https://gap.example"))
        assert store.get(PUBLIC_URL) == "https://gap.example"
        assert await asyncio.wait_for(states.get(), 10) is True
        await asyncio.sleep(0)
        assert cache.live
        assert listener.connects == 2
        assert store.get(PUBLIC_URL) == "https://gap.example"
        assert store.load().public_url == "https://gap.example"
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def test_a_read_begun_before_a_reconnect_keeps_nothing() -> None:
    """A read that began while live, across a drop and a reconnect, keeps nothing: its
    rows may predate a write whose notification was lost."""
    cache: SettingsCache[str] = SettingsCache()
    channels: dict[str, Any] = {}
    states: list[Any] = []

    class Listener:
        def listen(self, channel: str, *, on_notify: Any, on_connect: Any) -> None:
            channels[channel] = on_connect

        def on_state(self, callback: Any) -> None:
            states.append(callback)

    cache.follow(Listener())  # type: ignore[arg-type]
    channels[SETTINGS_CHANNEL](False)
    _, generation = cache.fresh()
    assert generation is not None
    at = cache.clock()
    for callback in states:
        callback(False)
    assert cache.fresh() == (None, None)
    channels[SETTINGS_CHANNEL](True)
    cache.keep(generation, at, "read before the gap")
    assert cache.fresh()[0] is None


def test_the_trigger_notifies_the_channel_the_cache_listens_on(settings: Settings) -> None:
    store = _opened(settings)
    try:
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            conn.execute(f"LISTEN {SETTINGS_CHANNEL}")
            store.save(SettingsPatch(public_url="https://n.example"))
            heard = list(conn.notifies(timeout=5, stop_after=1))
        assert [n.channel for n in heard] == [SETTINGS_CHANNEL]
    finally:
        store.close()


# ── op_insert ────────────────────────────────────────────────────────────────────

#: Any statement on the settings, the print choices, the plates or the printers.
_SETTINGS_TABLES = re.compile(
    r"\b(settings|model_print_choices|printer_bed_types|printers)\b", re.IGNORECASE
)


async def test_an_operation_insert_runs_no_settings_or_printer_query(
    store: SettingsStore, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save(SettingsPatch(operation_retention_seconds=86400.0))
    ops = OperationStore(store.pool)
    insert, _finish = operation_activities(ops, store, {})
    async with _listening(settings.database_url, store):
        # Warm, as every insert after a process's first finds it.
        assert store.get(OPERATION_RETENTION_SECONDS) == 86400.0
        statements: list[str] = []
        execute = psycopg.Cursor.execute

        def recording(self: psycopg.Cursor[Any], query: Any, *args: Any, **kwargs: Any) -> Any:
            statements.append(query if isinstance(query, str) else repr(query))
            return execute(self, query, *args, **kwargs)

        monkeypatch.setattr(psycopg.Cursor, "execute", recording)
        op = await ActivityEnvironment().run(
            insert, InsertOp(input=OperationInput(kind="demo", subject="s", key="k", request={}))
        )
        monkeypatch.undo()
    assert op.kind == "demo"
    assert [s for s in statements if _SETTINGS_TABLES.search(s)] == []
    # The retention was read: the insert pruned past it.
    assert any(s.startswith("DELETE FROM operations WHERE finished_at") for s in statements)
