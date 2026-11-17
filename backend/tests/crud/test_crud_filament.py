"""The print worker's reads of SB spools and its print uses through its CRUD routes
(#6262 Task 6, area ``spools``): the trays and presets answer as the API's own reads
do, only to the print worker's credential, and a LAN start's uses and their settle
answer once per ``Idempotency-Key`` (428 without one). Invented tags only (spec
2026-10-01 §7)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from psycopg.types.json import Jsonb

from scadbuddy.api.crud_areas import CRUD_AREAS
from scadbuddy.api.crud_print import MAX_ASSIGNMENTS
from scadbuddy.auth.service_credentials import ServiceCredentialStore
from scadbuddy.bambuddy.models import SpoolAssignment
from scadbuddy.core.settings import Settings
from scadbuddy.crud.client import CrudClient, CrudRefusedError
from scadbuddy.crud.idempotency import KEY_REQUIRED_PROBLEM
from scadbuddy.crud.print import BASE, RemoteFilamentTrays, RemotePrintUses
from scadbuddy.filament import ledger
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.inventory import FilamentInventory
from scadbuddy.filament.models import Material, SpoolCreate, SpoolPresetCreate, TrayView
from scadbuddy.filament.presets import PoolSpoolPresets
from scadbuddy.filament.print_uses import SlotUse
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.spools.models import SeenTray
from scadbuddy.worker import PRINT_CRUD_AREAS
from tests.conftest import UNUSED_TEMPORAL_ADDRESS
from tests.crud.crud_support import print_crud_app, served_print_crud, threaded_print_crud

pg = pytest.mark.requires_postgres

PRINTER = UUID("5d0c8f1e-0000-4000-8000-000000006262")
#: An invented tag, as a tray reports one.
TAG = "5CADB0DDF1A7E5000000000000006262"
RUN = "run-6262-crud"
STARTED = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)


def test_the_print_worker_waits_for_the_filament_area() -> None:
    """The routes are an area of their own, appended last, so a new worker waits for an
    API that serves them (`wait_for_areas`) rather than booking into a 404 mid-roll."""
    assert "filament" in PRINT_CRUD_AREAS
    assert CRUD_AREAS["print"][-1] == "filament"


@pytest.fixture
def store(tmp_path: Path, pg_conninfo: str) -> Iterator[SettingsStore]:
    opened = SettingsStore(
        Settings(
            data_dir=tmp_path,
            database_url=pg_conninfo,
            temporal_address=UNUSED_TEMPORAL_ADDRESS,
            auth_mode="enforce",
        )
    )
    opened.open()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
async def crud(store: SettingsStore) -> AsyncIterator[CrudClient]:
    async with served_print_crud(store) as client:
        yield client


def _spool(store: SettingsStore, grams: float = 100.0) -> UUID:
    material = next(
        r.id
        for r in CatalogStore(store.pool).list("materials")
        if isinstance(r, Material) and r.name == "PLA"
    )
    spools = FilamentSpoolStore(store.pool)
    return spools.create(SpoolCreate(material_id=material, grams_left=grams), actor="test").id


def _tagged(store: SettingsStore) -> UUID:
    spool = _spool(store)
    FilamentSpoolStore(store.pool).bind_tag(spool, tray_uuid=TAG, kind="bambu_mifare", actor="test")
    return spool


def _seen() -> list[SeenTray]:
    return [
        SeenTray(ams_id=0, tray_id=0, tray_uuid=TAG, material="PLA", colour="#F5547C"),
        SeenTray(ams_id=0, tray_id=1, material="PLA", colour="#FFFFFF"),
    ]


def _trays_body() -> dict[str, Any]:
    return {"printer_id": str(PRINTER), "seen": [t.model_dump(mode="json") for t in _seen()]}


def _run(store: SettingsStore, starts: list[str]) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO print_runs (id, output_id, idempotency_key, status, result)"
            " VALUES (%s, 'out', 'k', 'succeeded', %s)",
            (RUN, Jsonb({"lan": [{"subtask_name": name} for name in starts]})),
        )


def _print_events(store: SettingsStore, spool: UUID) -> list[str]:
    return [
        e.kind
        for e in FilamentSpoolStore(store.pool).events(spool)
        if e.ref_kind == ledger.PRINT_RUN
    ]


@pg
async def test_the_trays_route_needs_the_print_credential(store: SettingsStore) -> None:
    """No credential is 401, another caller's is 403; the print worker's answers the
    `TrayView` the API's own inventory reads."""
    held = _tagged(store)
    on_shelf = _spool(store)
    app = print_crud_app(store, ServiceCredentialStore(store.pool))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as bare:
        refused = await bare.post(f"{BASE}/filament/trays", json=_trays_body())
        assert refused.status_code == 401, refused.text
        assert (
            await bare.post(f"{BASE}/filament/presets", json={"numbers": []})
        ).status_code == 401

    with threaded_print_crud(store, caller="projects") as other:
        for path in (
            "/filament/trays",
            "/filament/presets",
            "/filament/print-uses",
            "/filament/print-uses/settle",
            "/filament/rows",
            "/filament/bb-to-sb",
        ):
            with pytest.raises(CrudRefusedError) as wrong:
                other.request("POST", f"{BASE}{path}", json={})
            assert wrong.value.status == 403, path
            assert wrong.value.problem["reason"] == "wrong_caller", path

    async with served_print_crud(store) as crud:
        response = await crud.request("POST", f"{BASE}/filament/trays", json=_trays_body())
        assert response.status_code == 200
        view = TrayView.model_validate(response.json())
        own = FilamentInventory(store.pool).trays(PRINTER, _seen())
        assert view.trays == own.trays
        assert view.trays[0].spool is not None and view.trays[0].spool.id == held
        # The trays only: a plan's choices are the trays, so the shelf is not sent.
        assert [spool.id for spool in own.shelf] == [on_shelf]
        assert view.shelf == []
        assert await RemoteFilamentTrays(crud).trays(PRINTER, _seen()) == view


@pg
async def test_the_presets_answer_as_the_apis_own(store: SettingsStore, crud: CrudClient) -> None:
    spool = _tagged(store)
    spools = FilamentSpoolStore(store.pool)
    own = SpoolPresetCreate(
        printer_model="H2C", nozzle_diameter="0.4", slicer_filament="Generic PLA @BBL H2C"
    )
    assert spools.set_presets(spool, [own], actor="test") is not None
    found = spools.get(spool)
    assert found is not None
    numbers = [found.number, 999_999]

    remote = await RemoteFilamentTrays(crud).presets(numbers)

    assert remote == await PoolSpoolPresets(store.pool).presets(numbers)
    assert [p.slicer_filament for p in remote[found.number]] == ["Generic PLA @BBL H2C"]
    assert await RemoteFilamentTrays(crud).presets([]) == {}


@pg
async def test_the_rows_and_bb_links_answer_as_the_apis_own(
    store: SettingsStore, crud: CrudClient
) -> None:
    """#6262 Task 7: the Bambuddy path's picker rows and its plan's id mapping, read by
    the print worker through the CRUD service."""
    linked = _spool(store)
    _spool(store)
    now = datetime.now(UTC)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO filament_bb_links (spool_id, bb_spool_id, bb_updated_at, linked_at)"
            " VALUES (%s, 42, %s, %s)",
            (linked, now, now),
        )
    assignments = [
        SpoolAssignment(id=1, spool_id=42, printer_id=7, printer_name="H2C", ams_id=0, tray_id=2)
    ]
    remote = RemoteFilamentTrays(crud)

    rows = await remote.rows(assignments)

    assert rows == FilamentInventory(store.pool).rows(bb_assignments=assignments)
    [row] = [r for r in rows if r.spool.id == linked]
    assert row.bb_spool_id == 42
    assert row.loaded is not None and (row.loaded.ams_id, row.loaded.tray_id) == (0, 2)
    assert await remote.bb_to_sb([42, 43]) == {42: row.spool.number}
    assert await remote.bb_to_sb([]) == {}


@pg
async def test_bambuddys_history_rows_never_pass_the_routes_cap(
    store: SettingsStore, crud: CrudClient
) -> None:
    """#6262 Task 7 review M3: Bambuddy keeps an assignment's history rows, so one spool
    may have more than the route takes (`MAX_ASSIGNMENTS`); only its last is sent, which
    is the one `FilamentInventory.rows` reads."""
    linked = _spool(store)
    now = datetime.now(UTC)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO filament_bb_links (spool_id, bb_spool_id, bb_updated_at, linked_at)"
            " VALUES (%s, 42, %s, %s)",
            (linked, now, now),
        )
    history = [
        SpoolAssignment(
            id=n, spool_id=42, printer_id=7, printer_name="H2C", ams_id=0, tray_id=n % 4
        )
        for n in range(1, MAX_ASSIGNMENTS + 3)
    ]

    rows = await RemoteFilamentTrays(crud).rows(history)

    [row] = [r for r in rows if r.spool.id == linked]
    assert row.loaded is not None
    assert (row.loaded.ams_id, row.loaded.tray_id) == (0, history[-1].tray_id)


@pg
async def test_a_retried_print_uses_call_writes_once(
    store: SettingsStore, crud: CrudClient
) -> None:
    """A resend under one ``Idempotency-Key`` answers as the first did and books nothing
    more; a call with no key is 428 and books nothing."""
    spool = _spool(store)
    _run(store, ["a"])
    use = SlotUse(slot_id=1, spool_id=spool, grams=12, plate_started_at=STARTED)
    body = {"run_id": RUN, "subtask": "a", "uses": [use.model_dump(mode="json")]}
    url = f"{BASE}/filament/print-uses"
    with pytest.raises(CrudRefusedError) as keyless:
        await crud.request("POST", url, json=body)
    assert keyless.value.status == 428
    assert keyless.value.problem["type"] == KEY_REQUIRED_PROBLEM
    assert _print_events(store, spool) == []

    first = await crud.request("POST", url, json=body, idempotency_key="uses-1")
    again = await crud.request("POST", url, json=body, idempotency_key="uses-1")
    assert first.json() == again.json() == {"recorded": 1}
    assert _print_events(store, spool) == ["print_started"]

    settle = {"run_id": RUN, "stages": ["done"]}
    settle_url = f"{BASE}/filament/print-uses/settle"
    with pytest.raises(CrudRefusedError) as keyless:
        await crud.request("POST", settle_url, json=settle)
    assert keyless.value.status == 428
    first = await crud.request("POST", settle_url, json=settle, idempotency_key="settle-1")
    again = await crud.request("POST", settle_url, json=settle, idempotency_key="settle-1")
    assert first.json() == again.json() == {"settled": True}
    assert _print_events(store, spool) == ["print_started", "print_ended"]

    remote = RemotePrintUses(crud)
    assert await remote.record_uses(RUN, "a", [use]) == 0
    assert await remote.record_uses(RUN, "a", []) == 0
    assert await remote.settle_uses(RUN, ["done"]) is False
    assert _print_events(store, spool) == ["print_started", "print_ended"]
    other = _spool(store)
    fresh = SlotUse(slot_id=2, spool_id=other, grams=3, plate_started_at=STARTED)
    assert await remote.record_uses(RUN, "a", [fresh, use.model_copy(update={"slot_id": 3})]) == 2
