"""Bambuddy's inventory as a spool provider, and the refresh that reads every provider
(#3818 tier A, plan 2026-10-18 Task 6b). Only the refresh reads a provider."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
import respx

import scadbuddy
from scadbuddy.bambuddy.spool_providers import SPOOL_PROVIDERS, BambuddyProvider
from scadbuddy.core.components import Components, Core, feature_exports
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.spools import activities
from scadbuddy.spools.activities import record_seen, refresh_provider
from scadbuddy.spools.component import Spools
from scadbuddy.spools.models import SeenTray, SpoolLocation, SpoolSnapshot, tray_fingerprint
from scadbuddy.spools.providers import PROVIDER_ATTR, PROVIDER_MODULE, TraySource
from scadbuddy.spools.store import SpoolStore
from tests.bambuddy.conftest import recording

URL = "https://bambuddy.test"
SPOOLS = f"{URL}/api/v1/inventory/spools"
ASSIGNMENTS = f"{URL}/api/v1/inventory/assignments"
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def provider(url: str | None = URL) -> BambuddyProvider:
    return BambuddyProvider(lambda: StoredSettings(bambuddy_url=url, bambuddy_api_key="k"))


@pytest.fixture
def bambuddy() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get(ASSIGNMENTS).respond(json=recording("inventory-assignments.json"))
        yield router


def test_bambuddy_is_a_discovered_provider() -> None:
    assert SPOOL_PROVIDERS in feature_exports(PROVIDER_MODULE, PROVIDER_ATTR, scadbuddy)
    store = SimpleNamespace(load=lambda: StoredSettings())
    (build,) = SPOOL_PROVIDERS
    built = build(cast(Core, SimpleNamespace(settings_store=store)), cast(Components, None))
    assert built is not None
    assert (built.name, built.label) == ("bambuddy", "Bambuddy")


async def test_the_recorded_inventory_maps_to_snapshots(bambuddy: respx.MockRouter) -> None:
    route = bambuddy.get(SPOOLS).respond(json=recording("inventory-spools.json"))
    records = await provider().records()
    assert route.call_count == 1
    assert route.calls[0].request.url.params["include_archived"] == "false"
    assert len(records) == 12
    first = next(r for r in records if r.record_id == "10")
    assert first == SpoolSnapshot(
        record_id="10",
        material="PETG",
        colour="#0086D6",
        brand="Bambu Lab",
        name="Navy Blue",
        remaining_g=980.0,
        label_weight_g=1000,
        # The recording's tray_uuid is all zeros (redacted), so it is no key; its tag_uid
        # is the recording's placeholder, cut to its hex digits as Bambuddy cuts it.
        tray_uuid=None,
        tag_uid="AD0000000000",
        preset="GFG00",
        location=SpoolLocation(
            printer="bambuddy:1",
            ams_id=2,
            tray_id=0,
            fingerprint=tray_fingerprint("PETG", "0086D6FF"),
        ),
    )
    untagged = next(r for r in records if r.record_id == "13")
    assert (untagged.tray_uuid, untagged.tag_uid, untagged.preset) == (None, None, "2")


async def test_a_tagged_spool_carries_its_keys(bambuddy: respx.MockRouter) -> None:
    row = {
        **recording("inventory-spools.json")[0],
        "tray_uuid": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
        "tag_uid": "1A2B3C4D5E6F7081",
    }
    bambuddy.get(SPOOLS).respond(json=[row])
    (only,) = await provider().records()
    assert (only.tray_uuid, only.tag_uid) == (
        "A1B2C3D4E5F60718293A4B5C6D7E8F90",
        "1A2B3C4D5E6F7081",
    )


async def test_without_a_url_it_is_not_configured() -> None:
    assert await provider(None).configured() is False
    assert await provider().configured() is True


async def test_bambuddys_own_assignment_is_the_records_location(
    bambuddy: respx.MockRouter,
) -> None:
    """I5: `GET /inventory/assignments`, the tray Bambuddy says a spool is in."""
    bambuddy.get(SPOOLS).respond(json=recording("inventory-spools.json"))
    records = {r.record_id: r for r in await provider().records()}
    assert records["9"].location == SpoolLocation(
        printer="bambuddy:1",
        ams_id=0,
        tray_id=1,
        fingerprint=tray_fingerprint("PETG", "688197FF"),
    )
    assert records["13"].location is None


class FakeStore:
    def __init__(self, *, fail_write: str | None = None) -> None:
        self.replaced: list[tuple[str, list[SpoolSnapshot]]] = []
        self.failures: list[tuple[str, str]] = []
        self.seen_trays: list[SeenTray] = []
        self.fail_write = fail_write

    async def replace_records(
        self, name: str, snapshots: list[SpoolSnapshot], at: datetime
    ) -> None:
        if name == self.fail_write:
            raise RuntimeError("the store is down")
        self.replaced.append((name, list(snapshots)))

    async def failed(self, name: str, at: datetime, failure: str) -> None:
        self.failures.append((name, failure))

    async def seen(self, trays: list[SeenTray]) -> None:
        self.seen_trays.extend(trays)


def spools(
    store: FakeStore, *providers: BambuddyProvider, trays: TraySource | None = None
) -> Spools:
    return Spools(store=cast(SpoolStore, store), providers=list(providers), trays=trays)


async def test_a_refresh_replaces_the_providers_records(bambuddy: respx.MockRouter) -> None:
    bambuddy.get(SPOOLS).respond(json=recording("inventory-spools.json"))
    store = FakeStore()
    assert await refresh_provider(spools(store, provider()), "bambuddy", now=lambda: NOW) == (
        "refreshed"
    )
    ((name, records),) = store.replaced
    assert name == "bambuddy" and len(records) == 12


async def test_a_failing_provider_is_recorded_and_its_rows_kept(
    bambuddy: respx.MockRouter,
) -> None:
    bambuddy.get(SPOOLS).respond(503)
    store = FakeStore()
    outcome = await refresh_provider(spools(store, provider()), "bambuddy", now=lambda: NOW)
    assert outcome == "failed"
    assert store.replaced == []
    assert store.failures == [("bambuddy", "ApiError")]


async def test_a_failed_write_is_the_providers_failure(bambuddy: respx.MockRouter) -> None:
    """I3: the whole step is the provider's, its write too."""
    bambuddy.get(SPOOLS).respond(json=[])
    store = FakeStore(fail_write="bambuddy")
    outcome = await refresh_provider(spools(store, provider()), "bambuddy", now=lambda: NOW)
    assert outcome == "failed"
    assert store.failures == [("bambuddy", "RuntimeError")]


async def test_an_unconfigured_provider_is_skipped_and_nothing_is_read(
    bambuddy: respx.MockRouter,
) -> None:
    route = bambuddy.route().respond(500)
    store = FakeStore()
    outcome = await refresh_provider(spools(store, provider(None)), "bambuddy", now=lambda: NOW)
    assert outcome == "skipped"
    assert route.call_count == 0
    assert store.replaced == [] and store.failures == []


async def test_the_refresh_records_what_the_printers_trays_show() -> None:
    seen = SeenTray(ams_id=0, tray_id=0, tray_uuid="A1", material="PLA", colour="#FF0000")

    class Trays:
        async def latest(self) -> list[SeenTray]:
            return [seen]

    store = FakeStore()
    await record_seen(spools(store, trays=Trays()))
    assert store.seen_trays == [seen]


@pytest.mark.parametrize("hangs", ["configured", "records"])
async def test_a_provider_that_hangs_fails(hangs: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """I3: `configured()` too is under the provider's timeout."""
    monkeypatch.setattr(activities, "PROVIDER_TIMEOUT", 0.01)

    class Slow(BambuddyProvider):
        async def configured(self) -> bool:
            if hangs == "configured":
                await asyncio.sleep(1)
            return True

        async def records(self) -> list[SpoolSnapshot]:
            await asyncio.sleep(1)
            return []

    store = FakeStore()
    slow = Slow(lambda: StoredSettings(bambuddy_url=URL))
    assert await refresh_provider(spools(store, slow), "bambuddy", now=lambda: NOW) == "failed"
    assert store.failures == [("bambuddy", "TimeoutError")]
