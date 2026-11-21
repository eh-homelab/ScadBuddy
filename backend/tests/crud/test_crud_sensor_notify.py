"""The sensor notifications' CRUD routes (#6646, plan Decision 40): the print worker claims
the queued rows and ends each through them, only with the print credential, and
`RemoteSensorNotifications` answers as `PoolSensorNotifications` does."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from scadbuddy.api.crud_areas import CRUD_AREAS
from scadbuddy.auth.service_credentials import ServiceCredentialStore
from scadbuddy.core.settings import Settings
from scadbuddy.crud.client import CrudClient, CrudRefusedError
from scadbuddy.crud.print import BASE, RemoteSensorNotifications
from scadbuddy.filament.models import LocationCreate, LocationKind, NotificationEnd, SensorIn
from scadbuddy.filament.sensors import SensorStore
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.worker import PRINT_CRUD_AREAS
from tests.conftest import UNUSED_TEMPORAL_ADDRESS
from tests.crud.crud_support import (
    filament_on,
    print_crud_app,
    served_print_crud,
    threaded_print_crud,
)

pg = pytest.mark.requires_postgres

CLAIM = f"{BASE}/filament/sensor-notifications/claim"
T0 = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def _end(row: UUID | str) -> str:
    return f"{BASE}/filament/sensor-notifications/{row}/end"


def test_the_print_areas_list_sensor_notify() -> None:
    """An area of its own, appended last, so a new worker waits for an API that serves it."""
    assert "sensor-notify" in PRINT_CRUD_AREAS
    assert CRUD_AREAS["print"][-1] == "sensor-notify"
    assert set(CRUD_AREAS["print"]) == PRINT_CRUD_AREAS


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


def _alerting(store: SettingsStore) -> UUID:
    filament = filament_on(store.pool)
    kind = next(
        r.id
        for r in filament.catalog.list("location-kinds")
        if isinstance(r, LocationKind) and r.name == "Shelf"
    )
    place = filament.locations.create(LocationCreate(name="Dry box", kind_id=kind)).id
    sensor = (
        SensorStore(store.pool)
        .create(
            SensorIn.model_validate(
                {
                    "location_id": place,
                    "source": "home_assistant",
                    "kind": "humidity",
                    "entity_id": "sensor.box",
                    "name": "Box",
                    "unit": "%",
                    "alert_above": 50,
                    "notify": True,
                }
            ),
            actor="ann",
        )
        .id
    )
    with store.pool.connection() as conn, conn.transaction():
        SensorStore.record_on(conn, sensor, 60.0, T0)
    return sensor


def _state(store: SettingsStore) -> tuple[str, int | None, str | None]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT state, channels, error FROM filament_sensor_notifications"
        ).fetchone()
    assert row is not None
    return row["state"], row["channels"], row["error"]


@pg
async def test_the_routes_need_the_print_credential(store: SettingsStore) -> None:
    """No credential is 401, another caller's is 403; nothing is claimed or ended."""
    _alerting(store)
    app = print_crud_app(store, ServiceCredentialStore(store.pool))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as bare:
        for path in (CLAIM, _end(uuid4())):
            assert (await bare.post(path, json={})).status_code == 401, path
    with threaded_print_crud(store, caller="render") as other:
        for path in (CLAIM, _end(uuid4())):
            with pytest.raises(CrudRefusedError) as wrong:
                other.request("POST", path, json={})
            assert wrong.value.status == 403, path
            assert wrong.value.problem["reason"] == "wrong_caller", path
    assert _state(store) == ("queued", None, None)


@pg
async def test_the_worker_claims_and_ends_a_row(store: SettingsStore, crud: CrudClient) -> None:
    _alerting(store)
    remote = RemoteSensorNotifications(crud)
    (claimed,) = await remote.claim(10)
    assert claimed.title == "Box: above the limit"
    assert _state(store) == ("sending", None, None)
    assert await remote.claim(10) == []
    await remote.end(claimed.id, NotificationEnd(channels=3))
    # Natural: sent again, the end finds the row ended and changes nothing.
    await remote.end(claimed.id, NotificationEnd(error="unavailable"))
    assert _state(store) == ("sent", 3, None)


@pg
async def test_a_claim_sent_again_answers_the_same_rows(
    store: SettingsStore, crud: CrudClient
) -> None:
    """A claim whose answer was lost is sent again under its key and gets those rows."""
    _alerting(store)
    first = await crud.request("POST", CLAIM, json={"limit": 5}, idempotency_key="k1")
    again = await crud.request("POST", CLAIM, json={"limit": 5}, idempotency_key="k1")
    assert first.json() == again.json() and len(first.json()) == 1


@pg
async def test_the_bodies_are_bounded(crud: CrudClient) -> None:
    with pytest.raises(CrudRefusedError) as big:
        await crud.request("POST", CLAIM, json={"limit": 1000}, idempotency_key="k2")
    assert big.value.status == 422
    with pytest.raises(CrudRefusedError) as only_the_claim:
        await crud.request("POST", _end(uuid4()), json={"error": "interrupted"})
    assert only_the_claim.value.status == 422
