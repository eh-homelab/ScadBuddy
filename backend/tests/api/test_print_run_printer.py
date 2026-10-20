"""``POST /print/outputs/{id}/run`` names a registry printer (#3823, plan 2026-10-16 Task
8): the run is recorded with the row it printed on, by identity, whichever field named
it; a ``printer`` that disagrees with ``printer_id`` is refused before anything is
uploaded. ``prepare_run``'s own cases are `tests/printers/test_run_printer.py`."""

from __future__ import annotations

import json
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.printers.component import PRINTERS
from scadbuddy.printers.store import PrinterStore
from tests.api.test_print_filaments import prepared, queue_route, slice_routes
from tests.api.test_print_run_choices import body, follow_run, run_routes
from tests.api.test_send import upload_route

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]


def registry(client: TestClient) -> PrinterStore:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state.components.get(PRINTERS).store


def run(client: TestClient, output_id: str, request: dict[str, Any]) -> dict[str, Any]:
    started = client.post(f"/api/v1/print/outputs/{output_id}/run", json=request)
    assert started.status_code in (200, 202), started.text
    return follow_run(client, started.json()["id"])


@respx.mock
def test_a_run_by_bambuddy_id_is_recorded_with_its_registry_printer(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    slice_routes()
    queue_route()

    ended = run(client, output_id, body())

    assert ended["status"] == "succeeded", ended
    row = registry(client).by_bambuddy_id(1)
    assert row is not None
    assert ended["printer"] == row.id
    activity = client.get(f"/api/v1/activity/print_run:{ended['id']}?advanced=1").json()
    assert activity["row"]["facets"]["printer"] == row.id


@respx.mock
def test_a_run_naming_a_printer_queues_on_its_bambuddy_printer(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    slice_routes()
    queued = queue_route()
    row = registry(client).ensure_bambuddy(1, serial="01P00A0000000AA", name="H2C", model="H2C")
    request = {**body(), "printer": row.id}
    del request["printer_id"]

    ended = run(client, output_id, request)

    assert ended["status"] == "succeeded", ended
    assert json.loads(queued.calls.last.request.content)["printer_id"] == 1
    assert ended["printer"] == row.id


@respx.mock
def test_run_refuses_disagreeing_printer_fields(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    uploaded = upload_route()
    run_routes()
    row = registry(client).ensure_bambuddy(1, serial="01P00A0000000AA", name="H2C", model="H2C")

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json={**body(), "printer": row.id, "printer_id": 2},
    )

    assert response.status_code == 422, response.text
    assert "disagreeing printer" in response.json()["detail"]
    assert not uploaded.called
