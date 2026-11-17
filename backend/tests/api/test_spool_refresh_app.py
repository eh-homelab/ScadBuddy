"""The app's ``library`` worker runs the spool refresh on its Schedule (#3818 tier A, plan
2026-10-18 Task 6b), and only when one is set: the api fixture turns it off, since its
trigger at start reads Bambuddy beside a test's own reads (#4180 CI). Since #6262 Task 12
a run pulls Bambuddy's spools into ScadBuddy's own (#6261); no provider is refreshed."""

from __future__ import annotations

import asyncio

import respx
from fastapi.testclient import TestClient
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from scadbuddy.workflows.spool_refresh import spool_schedule_id_for
from tests.bambuddy.conftest import recording

BAMBUDDY = "https://bambuddy.test"


def _state(client: TestClient) -> AppState:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state


async def _pulled(state: AppState, want: int) -> list[int]:
    """The Bambuddy spool ids the pull linked, once it linked ``want`` of them."""

    def linked() -> list[int]:
        with state.settings_store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT bb_spool_id FROM filament_bb_links ORDER BY bb_spool_id"
            ).fetchall()
        return [row["bb_spool_id"] for row in rows]

    for _ in range(600):
        if len(found := await asyncio.to_thread(linked)) >= want:
            return found
        await asyncio.sleep(0.05)
    raise AssertionError("the spool refresh never pulled")


async def _schedule_status(client: Client, schedule_id: str) -> RPCStatusCode | None:
    try:
        await client.get_schedule_handle(schedule_id).describe()
    except RPCError as error:
        return error.status
    return None


def test_the_api_fixture_runs_no_spool_refresh(settings: Settings) -> None:
    """No Schedule, so nothing reads Bambuddy's inventory unasked."""
    assert settings.spool_overlay_refresh_seconds == 0
    with respx.mock(assert_all_called=False) as bambuddy:
        inventory = bambuddy.get(f"{BAMBUDDY}/api/v1/inventory/spools").respond(json=[])
        bambuddy.route(host="testserver").pass_through()
        with TestClient(create_app(settings)) as client:
            client.put(
                "/api/v1/settings",
                json={"bambuddy_url": BAMBUDDY, "bambuddy_api_key": "k"},
            )
            state = _state(client)
            assert state.temporal is not None
            schedule = spool_schedule_id_for(settings.temporal_task_queue_library)
            status = client.portal.call(_schedule_status, state.temporal, schedule)  # type: ignore[union-attr]
    assert status == RPCStatusCode.NOT_FOUND
    assert not inventory.called


def test_a_schedule_pulls_bambuddys_spools(settings: Settings) -> None:
    """With an interval, the Schedule's run at start reads Bambuddy's inventory and pulls
    each spool into an SB spool linked to it."""
    configured = settings.model_copy(
        update={
            "bambuddy_url": BAMBUDDY,
            "bambuddy_api_key": "k",
            "spool_overlay_refresh_seconds": 300,
        }
    )
    spools = recording("inventory-spools.json")
    with respx.mock(assert_all_called=False) as bambuddy:
        bambuddy.get(f"{BAMBUDDY}/api/v1/inventory/spools").respond(json=spools)
        bambuddy.get(f"{BAMBUDDY}/api/v1/inventory/assignments").respond(json=[])
        bambuddy.get(f"{BAMBUDDY}/api/v1/inventory/catalog").respond(json=[])
        bambuddy.route(host="testserver").pass_through()
        with TestClient(create_app(configured)) as client:
            state = _state(client)
            assert state.temporal is not None
            handle = state.temporal.get_schedule_handle(
                spool_schedule_id_for(configured.temporal_task_queue_library)
            )
            try:
                pulled = client.portal.call(_pulled, state, len(spools))  # type: ignore[union-attr]
            finally:
                client.portal.call(handle.delete)  # type: ignore[union-attr]
    assert pulled == sorted(spool["id"] for spool in spools)
