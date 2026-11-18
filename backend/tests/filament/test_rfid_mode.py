"""The RFID override mode (#6264 Task 5): `filament/rfid_mode.py`. Bambuddy is respx;
every route and body is the one the recordings name
(`tests/bambuddy/recordings/openapi/rfid-routes.json`)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.errors import UNAVAILABLE_PROBLEM
from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament.rfid_mode import RfidModeStore, reconcile_mode
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.spools.activities import filament_rfid_activity
from tests.conftest import PgPool
from tests.filament.test_rfid_push import _bind, _filament, _settings, _spool

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
    assert status.detail is not None and "auth is on" in status.detail
    assert status.override_mode is True
    assert status.saved_auto_add is True
    assert store.state().saved_auto_add is True


@respx.mock
async def test_a_blocked_restore_keeps_the_saved_value(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    store = RfidModeStore(pg_pool)
    store.save_found(True)
    _reads(False)
    respx.patch(SETTINGS).mock(return_value=httpx.Response(403, json={"detail": "Forbidden"}))

    status = await reconcile_mode(store, client)

    assert status.state == "blocked"
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
    run = filament_rfid_activity(None, f, _settings, on)  # type: ignore[arg-type]
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
    run = filament_rfid_activity(None, f, load, lambda: FilamentSync(tags="sb_to_bb"))  # type: ignore[arg-type]

    assert await run() == "failed"
    assert link.call_count == 1


def test_save_found_writes_only_when_nothing_is_saved(pg_pool: PgPool) -> None:
    store = RfidModeStore(pg_pool)

    assert store.save_found(True) is True
    assert store.save_found(False) is False
    assert store.state().saved_auto_add is True
    store.clear_saved()
    assert store.state().saved_auto_add is None
