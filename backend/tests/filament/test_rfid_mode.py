"""The RFID override mode (#6264 Task 5): `filament/rfid_mode.py`. Bambuddy is respx;
every route and body is the one the recordings name
(`tests/bambuddy/recordings/openapi/rfid-routes.json`)."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Iterator

import httpx
import psycopg
import pytest
import respx
from psycopg.types.json import Jsonb

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.errors import UNAVAILABLE_PROBLEM
from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament.rfid_mode import (
    BUSY,
    NO_BAMBUDDY,
    RFID_MODE_LOCK,
    RfidModeStore,
    reconcile_mode,
    reconcile_mode_now,
)
from scadbuddy.library.settings_store import StoredSettings
from tests.conftest import PgPool
from tests.filament.test_rfid_push import (
    _bind,
    _filament,
    _printers,
    _rfid_activity,
    _settings,
    _spool,
)

pytestmark = pytest.mark.requires_postgres

BASE = "https://bambuddy.test"
API = f"{BASE}/api/v1"
SETTINGS = f"{API}/settings/"


@pytest.fixture
async def client() -> AsyncIterator[BambuddyClient]:
    async with BambuddyClient(BambuddyConfig(base_url=BASE, api_key="s3cret")) as bambuddy:
        yield bambuddy


def _reads(*values: bool) -> respx.Route:
    return respx.get(SETTINGS).mock(
        side_effect=[httpx.Response(200, json={"auto_add_unknown_rfid": v}) for v in values]
    )


def _patch() -> respx.Route:
    return respx.patch(SETTINGS).mock(return_value=httpx.Response(200, json={}))


def _patches() -> list[object]:
    return [json.loads(c.request.content) for c in respx.calls if c.request.method == "PATCH"]


@respx.mock
async def test_turning_the_mode_on_saves_the_found_value_then_turns_auto_add_off(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    store = RfidModeStore(pg_pool)
    store.set_mode(True)
    _reads(True, False)
    seen_at_patch: list[bool | None] = []

    def patched(request: httpx.Request) -> httpx.Response:
        seen_at_patch.append(store.state().saved_auto_add)
        return httpx.Response(200, json={})

    respx.patch(SETTINGS).mock(side_effect=patched)

    status = await reconcile_mode(store, client)

    assert seen_at_patch == [True]  # saved before the write
    assert _patches() == [{"auto_add_unknown_rfid": False}]
    assert [c.request.method for c in respx.calls] == ["GET", "PATCH", "GET"]
    assert (status.state, status.bambuddy_auto_add, status.saved_auto_add) == (
        "applied",
        False,
        True,
    )


@respx.mock
async def test_a_second_reconcile_never_overwrites_the_saved_value(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    store = RfidModeStore(pg_pool)
    store.set_mode(True)
    store.save_found(True)
    _reads(False)
    _patch()

    status = await reconcile_mode(store, client)

    assert store.state().saved_auto_add is True
    assert _patches() == []
    assert status.state == "applied"


@respx.mock
async def test_a_flip_back_in_bambuddy_is_turned_off_again(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    store = RfidModeStore(pg_pool)
    store.set_mode(True)
    store.save_found(True)
    _reads(True, False)
    _patch()

    status = await reconcile_mode(store, client)

    assert _patches() == [{"auto_add_unknown_rfid": False}]
    assert store.state().saved_auto_add is True
    assert status.state == "applied"


@respx.mock
async def test_turning_the_mode_off_restores_the_original_value(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    store = RfidModeStore(pg_pool)
    store.set_mode(True)
    store.save_found(True)
    store.set_mode(False)
    _reads(False)
    _patch()

    status = await reconcile_mode(store, client)

    assert _patches() == [{"auto_add_unknown_rfid": True}]
    assert store.state().saved_auto_add is None
    assert status.state == "off"


@respx.mock
async def test_turning_the_mode_off_writes_nothing_when_the_person_had_it_off(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    store = RfidModeStore(pg_pool)
    store.set_mode(True)
    store.save_found(False)
    store.set_mode(False)
    _reads(False)
    _patch()

    status = await reconcile_mode(store, client)

    assert _patches() == []
    assert store.state().saved_auto_add is None
    assert status.state == "off"


@respx.mock
async def test_a_mode_that_is_off_with_nothing_saved_calls_nothing(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    status = await reconcile_mode(RfidModeStore(pg_pool), client)

    assert len(respx.calls) == 0
    assert status.state == "off"


@respx.mock
async def test_a_blocked_write_keeps_the_mode_and_the_saved_value(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    store = RfidModeStore(pg_pool)
    store.set_mode(True)
    _reads(True)
    respx.patch(SETTINGS).mock(return_value=httpx.Response(403, json={"detail": "Forbidden"}))

    status = await reconcile_mode(store, client)

    assert status.state == "blocked"
    # The value Bambuddy answered, never a claim that auto-add is off.
    assert status.bambuddy_auto_add is True
    assert status.detail is not None and "auth is on" in status.detail
    assert status.override_mode is True
    assert status.saved_auto_add is True
    assert store.state().saved_auto_add is True


@respx.mock
async def test_a_blocked_restore_keeps_the_saved_value(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    store = RfidModeStore(pg_pool)
    store.set_mode(True)
    store.save_found(True)
    store.set_mode(False)
    _reads(False)
    respx.patch(SETTINGS).mock(return_value=httpx.Response(403, json={"detail": "Forbidden"}))

    status = await reconcile_mode(store, client)

    assert status.state == "blocked"
    assert status.bambuddy_auto_add is False
    assert store.state().saved_auto_add is True


@respx.mock
async def test_bambuddy_down_leaves_the_mode_pending(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    store = RfidModeStore(pg_pool)
    before = store.set_mode(True)
    respx.get(SETTINGS).mock(side_effect=httpx.ConnectError("down"))

    status = await reconcile_mode(store, client)

    assert status.state == "pending"
    assert status.detail == UNAVAILABLE_PROBLEM
    assert store.state() == before


@respx.mock
async def test_the_activity_reconciles_the_mode_first_and_each_step_fails_alone(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    f.rfid.set_mode(True)
    on = lambda: FilamentSync(tags="sb_to_bb")  # noqa: E731
    run = _rfid_activity(_printers(pg_pool), f, _settings, on)
    _reads(True, False)
    patch = _patch()
    respx.get(f"{API}/inventory/spools/by-tag").mock(return_value=httpx.Response(404, json={}))
    _bind(f, _spool(f, pg_pool, 11), uid="AABBCCDD11223344")
    link11 = respx.patch(f"{API}/inventory/spools/11/link-tag").mock(
        return_value=httpx.Response(200, json={"id": 11, "material": "PLA"})
    )

    assert await run() == "done"

    assert (patch.call_count, link11.call_count) == (1, 1)
    assert respx.calls[0].request.url.path == "/api/v1/settings/"  # the mode comes first

    # The mode step cannot reach Bambuddy: the activity says "failed", and the tag step
    # still ran.
    respx.get(SETTINGS).mock(side_effect=httpx.ConnectError("down"))
    _bind(f, _spool(f, pg_pool, 12), uid="0102030405060708")
    link12 = respx.patch(f"{API}/inventory/spools/12/link-tag").mock(
        return_value=httpx.Response(200, json={"id": 12, "material": "PLA"})
    )
    assert await run() == "failed"
    assert link12.call_count == 1

    # A blocked write is shown on the status, not a failure of the activity.
    respx.get(SETTINGS).mock(return_value=httpx.Response(200, json={"auto_add_unknown_rfid": True}))
    respx.patch(SETTINGS).mock(return_value=httpx.Response(403, json={"detail": "no"}))
    assert await run() == "done"


@respx.mock
async def test_the_activity_survives_a_broken_mode_step_and_still_pushes_tags(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    f.rfid.set_mode(True)
    calls = {"n": 0}

    def load() -> StoredSettings:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("settings are gone")
        return _settings()

    _bind(f, _spool(f, pg_pool, 11), uid="AABBCCDD11223344")
    respx.get(f"{API}/inventory/spools/by-tag").mock(return_value=httpx.Response(404, json={}))
    link = respx.patch(f"{API}/inventory/spools/11/link-tag").mock(
        return_value=httpx.Response(200, json={"id": 11, "material": "PLA"})
    )
    run = _rfid_activity(_printers(pg_pool), f, load, lambda: FilamentSync(tags="sb_to_bb"))

    assert await run() == "failed"
    assert link.call_count == 1


def test_save_found_writes_only_when_nothing_is_saved_and_the_mode_is_on(
    pg_pool: PgPool,
) -> None:
    store = RfidModeStore(pg_pool)

    assert store.save_found(True) is False  # the mode is off
    store.set_mode(True)
    assert store.save_found(True) is True
    assert store.save_found(False) is False
    assert store.state().saved_auto_add is True


def test_clear_saved_needs_the_mode_off_and_the_value_it_restored(pg_pool: PgPool) -> None:
    store = RfidModeStore(pg_pool)
    store.set_mode(True)
    store.save_found(True)

    assert store.clear_saved(True) is False  # the mode is on
    store.set_mode(False)
    assert store.clear_saved(False) is False  # another value than the saved one
    assert store.clear_saved(True) is True
    assert store.state().saved_auto_add is None


@respx.mock
async def test_a_mode_turned_on_during_the_restore_keeps_the_saved_value(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    store = RfidModeStore(pg_pool)
    store.set_mode(True)
    store.save_found(True)
    store.set_mode(False)
    _reads(False)

    def patched(request: httpx.Request) -> httpx.Response:
        store.set_mode(True)  # a PUT lands between the restore's write and its clear
        return httpx.Response(200, json={})

    respx.patch(SETTINGS).mock(side_effect=patched)

    status = await reconcile_mode(store, client)

    assert store.state().saved_auto_add is True
    assert store.state().override_mode is True
    assert status.state == "pending"


@respx.mock
async def test_a_mode_turned_off_before_the_write_skips_the_write(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    store = RfidModeStore(pg_pool)
    store.set_mode(True)

    def read(request: httpx.Request) -> httpx.Response:
        store.set_mode(False)  # a PUT off lands after the mode was read
        return httpx.Response(200, json={"auto_add_unknown_rfid": True})

    respx.get(SETTINGS).mock(side_effect=read)
    patch = _patch()

    status = await reconcile_mode(store, client)

    assert patch.call_count == 0
    assert store.state().saved_auto_add is None
    assert status.state == "off"


@respx.mock
async def test_a_connect_error_during_the_restore_keeps_the_saved_value(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    store = RfidModeStore(pg_pool)
    store.set_mode(True)
    store.save_found(True)
    store.set_mode(False)
    _reads(False)
    respx.patch(SETTINGS).mock(side_effect=httpx.ConnectError("down"))

    status = await reconcile_mode(store, client)

    assert status.state == "pending"
    assert status.detail == UNAVAILABLE_PROBLEM
    assert store.state().saved_auto_add is True


@respx.mock
async def test_no_bambuddy_leaves_the_mode_pending_and_the_activity_not_failed(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    f.rfid.set_mode(True)
    none = lambda: StoredSettings()  # noqa: E731

    status = await reconcile_mode_now(f.rfid, none)
    run = _rfid_activity(_printers(pg_pool), f, none, lambda: FilamentSync(tags="sb_to_bb"))

    assert (status.state, status.detail) == ("pending", NO_BAMBUDDY)
    assert await run() == "done"
    assert len(respx.calls) == 0


@pytest.fixture
def hold_lock(pg_pool: PgPool) -> Iterator[None]:
    """Another process holding the mode lock."""
    conn = psycopg.connect(RfidModeStore(pg_pool).conninfo, autocommit=True)
    conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (RFID_MODE_LOCK,))
    try:
        yield
    finally:
        conn.close()


def _lock_is_free(store: RfidModeStore) -> bool:
    with psycopg.connect(store.conninfo, autocommit=True) as other:
        row = other.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (RFID_MODE_LOCK,)
        ).fetchone()
    return row is not None and bool(row[0])


@respx.mock
async def test_the_activity_skips_the_mode_step_while_another_holds_the_lock(
    pg_pool: PgPool, hold_lock: None
) -> None:
    f = _filament(pg_pool)
    f.rfid.set_mode(True)
    run = _rfid_activity(_printers(pg_pool), f, _settings, lambda: FilamentSync(tags="off"))

    assert await run() == "done"
    assert len(respx.calls) == 0


@respx.mock
async def test_a_wait_for_the_lock_times_out_as_busy(pg_pool: PgPool, hold_lock: None) -> None:
    store = RfidModeStore(pg_pool)
    store.set_mode(True)

    began = time.monotonic()
    status = await reconcile_mode_now(store, _settings, wait=0.3)

    assert 0.25 <= time.monotonic() - began < 3
    assert (status.state, status.detail) == ("pending", BUSY)
    assert store.state().override_mode is True
    assert len(respx.calls) == 0


@respx.mock
async def test_one_reconcile_holds_the_lock_from_its_first_call_to_its_last_write(
    pg_pool: PgPool,
) -> None:
    """The reviewer's interleaving (a PUT off finishing between the activity's re-read and
    its PATCH) needs a second reconcile to run inside the first: it cannot, and one that
    tries finds the lock held and calls nothing."""
    store = RfidModeStore(pg_pool)
    store.set_mode(True)
    free_during: list[bool] = []

    def read(request: httpx.Request) -> httpx.Response:
        free_during.append(_lock_is_free(store))
        return httpx.Response(200, json={"auto_add_unknown_rfid": True})

    def patched(request: httpx.Request) -> httpx.Response:
        free_during.append(_lock_is_free(store))
        store.set_mode(False)  # the row changes mid-run, as a PUT's write does
        return httpx.Response(200, json={})

    respx.get(SETTINGS).mock(side_effect=read)
    respx.patch(SETTINGS).mock(side_effect=patched)

    await reconcile_mode_now(store, _settings)

    assert free_during == [False, False, False]
    assert store.state().saved_auto_add is True  # the saved value survived the mid-run flip
    assert _lock_is_free(store)  # and the lock is released


def _auto_add(pg_pool: PgPool, on: bool) -> None:
    """The stored-only setting `filament_auto_add_unknown_rfid` (#7156), as `PUT
    /settings` stores it: a JSON boolean."""
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO settings (name, value) VALUES (%s, %s)"
            " ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value",
            ("filament_auto_add_unknown_rfid", Jsonb(on)),
        )


@respx.mock
async def test_the_setting_off_holds_auto_add_off_with_the_mode_off(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    """R-7156-2: with SB's own setting off, Bambuddy makes no spool of an unknown tag
    either; the value it had is saved first, as the mode saves it."""
    store = RfidModeStore(pg_pool)
    _auto_add(pg_pool, False)
    _reads(True, False)
    _patch()

    status = await reconcile_mode(store, client)

    assert _patches() == [{"auto_add_unknown_rfid": False}]
    assert store.state().saved_auto_add is True
    assert (status.override_mode, status.auto_add_unknown_rfid, status.state) == (
        False,
        False,
        "applied",
    )


@respx.mock
async def test_the_setting_on_again_restores_the_saved_value(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    store = RfidModeStore(pg_pool)
    _auto_add(pg_pool, False)
    store.save_found(True)
    _auto_add(pg_pool, True)
    _reads(False)
    _patch()

    status = await reconcile_mode(store, client)

    assert _patches() == [{"auto_add_unknown_rfid": True}]
    assert store.state().saved_auto_add is None
    assert (status.auto_add_unknown_rfid, status.state) == (True, "off")


@respx.mock
async def test_the_mode_off_keeps_the_hold_while_the_setting_is_off(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    """R-7156-3: two holds on one value; the saved value goes back only when neither
    holds."""
    store = RfidModeStore(pg_pool)
    store.set_mode(True)
    store.save_found(True)
    _auto_add(pg_pool, False)
    store.set_mode(False)
    _reads(False)
    _patch()

    status = await reconcile_mode(store, client)

    assert _patches() == []
    assert store.state().saved_auto_add is True
    assert status.state == "applied"
    assert store.clear_saved(True) is False  # the setting still holds it


def test_save_found_takes_the_setting_as_a_hold(pg_pool: PgPool) -> None:
    store = RfidModeStore(pg_pool)
    _auto_add(pg_pool, False)

    assert store.save_found(True) is True
    assert store.state().auto_add_off is True


@respx.mock
async def test_reconcile_now_reaches_bambuddy_for_the_setting_alone(pg_pool: PgPool) -> None:
    """A mode that is off with nothing saved calls nothing, unless the setting holds."""
    store = RfidModeStore(pg_pool)
    _auto_add(pg_pool, False)
    _reads(True, False)
    _patch()

    status = await reconcile_mode_now(store, _settings)

    assert _patches() == [{"auto_add_unknown_rfid": False}]
    assert status.state == "applied"
