"""The rack nozzle routes (#836): the remembered algorithm, the preview, the manual pick."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.rack.component import RACK_USAGE
from scadbuddy.rack.usage import RackUsageStore
from tests.api.test_print_filaments import prepared
from tests.api.test_print_run_choices import API, body, run_routes
from tests.api.test_send import upload_route
from tests.rack.helpers import invented_status, serial

pytestmark = pytest.mark.requires_postgres


def test_the_rack_algorithm_is_remembered_per_printer_and_forgotten(client: TestClient) -> None:
    put = client.put("/api/v1/print/printers/1/rack-algorithm", json={"algorithm": "newest_first"})
    assert put.status_code == 200, put.text
    assert put.json() == {"printer_id": 1, "algorithm": "newest_first"}
    remembered = client.get("/api/v1/settings/remembered").json()
    assert remembered["printer_rack_algorithms"] == {"1": "newest_first"}

    forgot = client.put("/api/v1/print/printers/1/rack-algorithm", json={"algorithm": None})
    assert forgot.json() == {"printer_id": 1, "algorithm": "least_used"}
    assert client.get("/api/v1/settings/remembered").json().get("printer_rack_algorithms", {}) == {}


def test_an_unknown_algorithm_is_refused(client: TestClient) -> None:
    response = client.put("/api/v1/print/printers/1/rack-algorithm", json={"algorithm": "random"})
    assert response.status_code == 422


def rack_usage(client: TestClient) -> RackUsageStore:
    state = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    store: RackUsageStore = state.components.get(RACK_USAGE)
    return store


def invented_rack_route() -> None:
    """The recorded rack with invented serials; replaces ``hardware_routes``' answer
    (respx re-uses a route registered again with the same pattern)."""
    respx.get(f"{API}/printers/1/status").mock(
        return_value=httpx.Response(200, json=invented_status())
    )


@respx.mock
def test_the_check_records_the_racks_hotends_as_seen(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    invented_rack_route()

    response = client.post(f"/api/v1/print/outputs/{output_id}/check", json=body())

    assert response.status_code == 200, response.text
    usage = asyncio.run(rack_usage(client).usage([serial(17), serial(0), serial(1)]))
    assert usage[serial(17)].first_seen_at is not None
    assert usage[serial(0)].first_seen_at is not None
    assert usage[serial(1)].first_seen_at is None  # the left hotend is not a rack hotend
