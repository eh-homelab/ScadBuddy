"""The app's ``library`` worker refreshes the spool providers on their Schedule (#3818
tier A, plan 2026-10-18 Task 6b), and only when one is set: the api fixture turns it
off, since its trigger at start reads Bambuddy beside a test's own reads (#4180 CI)."""

from __future__ import annotations

import asyncio
from typing import Any

import respx
from fastapi.testclient import TestClient
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from scadbuddy.spools.component import SPOOLS
from scadbuddy.workflows.spool_refresh import spool_schedule_id_for
from tests.bambuddy.conftest import recording

BAMBUDDY = "https://bambuddy.test"


def _state(client: TestClient) -> AppState:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state


async def _refreshed(state: AppState) -> Any:
    spools = state.components.get(SPOOLS)
    for _ in range(600):
        found = {r.provider: r for r in await spools.store.refreshes()}
        if "bambuddy" in found and found["bambuddy"].refreshed_at is not None:
            return found["bambuddy"]
        await asyncio.sleep(0.05)
    raise AssertionError("the spool refresh never ran")


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


def test_a_schedule_refreshes_bambuddys_records(settings: Settings) -> None:
    """With an interval, the Schedule's run at start reads Bambuddy's inventory and
    records it."""
    configured = settings.model_copy(
        update={
            "bambuddy_url": BAMBUDDY,
            "bambuddy_api_key": "k",
            "spool_overlay_refresh_seconds": 300,
        }
    )
    with respx.mock(assert_all_called=False) as bambuddy:
        bambuddy.get(f"{BAMBUDDY}/api/v1/inventory/spools").respond(
            json=recording("inventory-spools.json")
        )
        bambuddy.get(f"{BAMBUDDY}/api/v1/inventory/assignments").respond(json=[])
        bambuddy.route(host="testserver").pass_through()
        with TestClient(create_app(configured)) as client:
            state = _state(client)
            assert state.temporal is not None
            handle = state.temporal.get_schedule_handle(
                spool_schedule_id_for(configured.temporal_task_queue_library)
            )
            try:
                refresh = client.portal.call(_refreshed, state)  # type: ignore[union-attr]
            finally:
                client.portal.call(handle.delete)  # type: ignore[union-attr]
    assert refresh.failure is None
