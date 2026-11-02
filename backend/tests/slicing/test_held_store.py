"""The kept copy of Bambuddy's local presets in Postgres, and its refresh (#4645): read
off the hot path, on the spool refresh's Schedule, and only what changed."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.dispatch import SlicePlan
from scadbuddy.bambuddy.models import LocalPresetDetail, PresetRef
from scadbuddy.slicing.component import COMPONENT as SLICING_COMPONENT
from scadbuddy.slicing.held_presets import HeldPresetStore, refresh_held_presets
from scadbuddy.slicing.presets import build_presets
from scadbuddy.slicing.profiles import Profiles
from tests.conftest import PgPool
from tests.slicing.conftest import PROFILES, local_preset

pytestmark = pytest.mark.requires_postgres

BASE = "https://bambuddy.test"
API = f"{BASE}/api/v1"


def detail(preset_id: int, updated_at: str, temperature: str = "215") -> dict[str, Any]:
    return local_preset(
        preset_id,
        f"PLA {preset_id}",
        "filament",
        {"name": f"PLA {preset_id}", "inherits": "Test PLA", "nozzle_temperature": [temperature]},
        compatible_printers=json.dumps(["Bambu Lab H2C 0.4 nozzle"]),
        updated_at=updated_at,
    ).model_dump()


def listing(*rows: dict[str, Any]) -> dict[str, Any]:
    return {
        "filament": [{k: v for k, v in row.items() if k != "setting"} for row in rows],
        "printer": [],
        "process": [],
    }


@pytest.fixture
def store(pg_pool: PgPool) -> HeldPresetStore:
    return HeldPresetStore(pg_pool)


@pytest.fixture
async def client() -> AsyncIterator[BambuddyClient]:
    async with BambuddyClient(BambuddyConfig(base_url=BASE, api_key="k")) as client:
        yield client


@respx.mock
async def test_a_refresh_keeps_every_local_preset_with_its_document(
    store: HeldPresetStore, client: BambuddyClient
) -> None:
    one, two = detail(1, "2026-10-22T10:00:00"), detail(2, "2026-10-22T11:00:00")
    respx.get(f"{API}/local-presets/").respond(200, json=listing(one, two))
    respx.get(f"{API}/local-presets/1").respond(200, json=one)
    respx.get(f"{API}/local-presets/2").respond(200, json=two)
    assert await refresh_held_presets(store, client) == 2
    rows = {row.preset.id: row for row in await store.rows()}
    assert set(rows) == {1, 2} and rows[1].preset.name == "PLA 1"
    docs = await store.documents([1, 3])
    assert set(docs) == {1} and docs[1].setting["nozzle_temperature"] == ["215"]


@respx.mock
async def test_a_refresh_reads_only_what_changed_and_forgets_what_is_gone(
    store: HeldPresetStore, client: BambuddyClient
) -> None:
    one, two = detail(1, "2026-10-22T10:00:00"), detail(2, "2026-10-22T11:00:00")
    respx.get(f"{API}/local-presets/").respond(200, json=listing(one, two))
    respx.get(f"{API}/local-presets/1").respond(200, json=one)
    respx.get(f"{API}/local-presets/2").respond(200, json=two)
    await refresh_held_presets(store, client)

    respx.reset()
    edited = detail(1, "2026-10-23T09:00:00", temperature="220")
    respx.get(f"{API}/local-presets/").respond(200, json=listing(edited))
    read = respx.get(f"{API}/local-presets/1").respond(200, json=edited)
    # Preset 2 is gone; preset 1 was edited (its updated_at moved), so only it is read.
    assert await refresh_held_presets(store, client) == 1
    assert read.call_count == 1
    docs = await store.documents([1, 2])
    assert set(docs) == {1} and docs[1].setting["nozzle_temperature"] == ["220"]

    respx.reset()
    respx.get(f"{API}/local-presets/").respond(200, json=listing(edited))
    assert await refresh_held_presets(store, client) == 0


@respx.mock
async def test_a_preset_gone_between_the_list_and_its_read_keeps_what_was_kept(
    store: HeldPresetStore, client: BambuddyClient
) -> None:
    one = detail(1, "2026-10-22T10:00:00")
    respx.get(f"{API}/local-presets/").respond(200, json=listing(one))
    respx.get(f"{API}/local-presets/1").respond(200, json=one)
    await refresh_held_presets(store, client)

    respx.reset()
    edited, new = detail(1, "2026-10-23T09:00:00"), detail(3, "2026-10-23T09:00:00")
    respx.get(f"{API}/local-presets/").respond(200, json=listing(edited, new))
    respx.get(f"{API}/local-presets/1").respond(404, json={"detail": "Local preset not found"})
    respx.get(f"{API}/local-presets/3").respond(404, json={"detail": "Local preset not found"})
    assert await refresh_held_presets(store, client) == 0
    # The kept one stays as it was, and is read again next time (its stamp is the old).
    assert {row.preset.id: row.preset.updated_at for row in await store.rows()} == {
        1: "2026-10-22T10:00:00"
    }


@respx.mock
async def test_a_failed_list_changes_nothing(
    store: HeldPresetStore, client: BambuddyClient
) -> None:
    one = detail(1, "2026-10-22T10:00:00")
    respx.get(f"{API}/local-presets/").respond(200, json=listing(one))
    respx.get(f"{API}/local-presets/1").respond(200, json=one)
    await refresh_held_presets(store, client)
    respx.reset()
    respx.get(f"{API}/local-presets/").respond(500)
    with pytest.raises(Exception):  # noqa: B017 - whatever the client raises, nothing moves
        await refresh_held_presets(store, client)
    assert [row.preset.id for row in await store.rows()] == [1]


@respx.mock
async def test_a_refresh_cut_short_keeps_what_it_read(
    store: HeldPresetStore, client: BambuddyClient
) -> None:
    """Each preset is kept as it is read (#4645 review): a refresh that fails part way
    keeps the ones before, and the next reads only the rest."""
    one, two = detail(1, "2026-10-22T10:00:00"), detail(2, "2026-10-22T11:00:00")
    respx.get(f"{API}/local-presets/").respond(200, json=listing(one, two))
    respx.get(f"{API}/local-presets/1").respond(200, json=one)
    respx.get(f"{API}/local-presets/2").respond(500)
    with pytest.raises(Exception):  # noqa: B017 - whatever the client raises
        await refresh_held_presets(store, client)
    assert [row.preset.id for row in await store.rows()] == [1]

    respx.reset()
    respx.get(f"{API}/local-presets/").respond(200, json=listing(one, two))
    read = respx.get(f"{API}/local-presets/2").respond(200, json=two)
    assert await refresh_held_presets(store, client) == 1 and read.call_count == 1


@pytest.mark.parametrize(("interval", "kept"), [(0, False), (300, True)])
async def test_with_the_refresh_off_the_start_forgets_the_copy(
    store: HeldPresetStore, interval: int, kept: bool
) -> None:
    """#4645 review: with ``spool_overlay_refresh_seconds`` 0 nothing keeps the copy
    current, so the API's start forgets it, and a ``local`` preset is read from Bambuddy
    again rather than from a copy that only gets older."""
    await store.keep(LocalPresetDetail.model_validate(detail(1, "2026-10-22T10:00:00")))
    slicing = SimpleNamespace(
        held=store,
        profiles=None,
        core=SimpleNamespace(
            settings=SimpleNamespace(spool_overlay_refresh_seconds=interval),
            settings_store=SimpleNamespace(load=lambda: SimpleNamespace(slicer_backend="native")),
        ),
    )
    run = SLICING_COMPONENT.run
    assert run is not None
    async with run(cast(Any, slicing)):
        pass
    assert bool(await store.rows()) is kept


@respx.mock
async def test_with_the_copy_forgotten_a_slice_reads_bambuddys_current_document(
    store: HeldPresetStore, client: BambuddyClient
) -> None:
    """What the forgotten copy leaves: Bambuddy's document as it is now, not the kept one."""
    await store.keep(LocalPresetDetail.model_validate(detail(1, "2026-10-22T10:00:00")))
    await store.clear()
    now = detail(1, "2026-10-23T09:00:00", temperature="230")
    route = respx.get(f"{API}/local-presets/1").respond(200, json=now)
    plan = SlicePlan(
        printer_preset=PresetRef(source="standard", id="Test Printer 0.4 nozzle"),
        process_preset=PresetRef(source="standard", id="leaf"),
        filament_presets=[PresetRef(source="local", id="1")],
        filament_colours=["#000000"],
        bed_type="Textured PEI Plate",
    )
    files = await build_presets(plan, Profiles(PROFILES, held=store), client)
    assert json.loads(files.filaments[0])["nozzle_temperature"] == ["230"]
    assert route.call_count == 1
