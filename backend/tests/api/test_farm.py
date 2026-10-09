"""Issue #1912 — ``/api/v1/farm/…``: the queue, every archive's outcome, the stats and the
spool inventory, read from Bambuddy and never written to it."""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.api.test_send import API, configure
from tests.bambuddy.conftest import recording


@respx.mock
def test_the_queue_passes_its_filters_and_cuts_to_the_limit(client: TestClient) -> None:
    configure(client)
    route = respx.get(f"{API}/queue/").mock(
        return_value=httpx.Response(200, json=recording("queue.json"))
    )

    response = client.get(
        "/api/v1/farm/queue", params={"printer_id": 1, "status": "failed", "limit": 2}
    )

    assert response.status_code == 200
    params = route.calls.last.request.url.params
    assert (params["printer_id"], params["status"]) == ("1", "failed")
    body = response.json()
    assert body["total"] == 4
    assert [item["id"] for item in body["items"]] == [257, 259]
    assert body["items"][0]["archive_name"] == "Carrot Garden"
    assert body["items"][0]["printer_name"] == "3DP-31B-598"


@respx.mock
@pytest.mark.parametrize(
    ("route", "bambuddy_path"),
    [
        ("/api/v1/farm/queue", "/queue/"),
        ("/api/v1/farm/stats", "/archives/stats"),
        ("/api/v1/farm/archives", "/archives/"),
        ("/api/v1/farm/inventory", "/inventory/spools"),
    ],
)
def test_a_key_without_read_status_names_the_scope(
    client: TestClient, route: str, bambuddy_path: str
) -> None:
    configure(client)
    respx.get(f"{API}{bambuddy_path}").mock(
        return_value=httpx.Response(403, json={"detail": "Missing permission"})
    )

    response = client.get(route)

    assert response.status_code == 409
    assert response.json()["required_scope"] == "Read Status"


def test_without_bambuddy_the_farm_is_not_configured(client: TestClient) -> None:
    response = client.get("/api/v1/farm/stats")

    assert response.status_code == 409
    assert response.json()["type"].endswith("/bambuddy-not-configured")


@respx.mock
def test_stats_take_a_date_window(client: TestClient) -> None:
    configure(client)
    route = respx.get(f"{API}/archives/stats").mock(
        return_value=httpx.Response(200, json=recording("archive-stats.json"))
    )

    response = client.get(
        "/api/v1/farm/stats", params={"date_from": "2026-09-01", "date_to": "2026-09-30"}
    )

    assert response.status_code == 200
    params = route.calls.last.request.url.params
    assert (params["date_from"], params["date_to"]) == ("2026-09-01", "2026-09-30")
    body = response.json()
    assert body["total_prints"] == 104
    assert body["printer_names"] == {"1": "3DP-31B-598"}


@respx.mock
def test_archives_are_every_archive_with_its_outcome_and_no_file_paths(
    client: TestClient,
) -> None:
    configure(client)
    route = respx.get(f"{API}/archives/").mock(
        return_value=httpx.Response(200, json=recording("archives-full.json"))
    )

    response = client.get(
        "/api/v1/farm/archives",
        params={
            "printer_id": 1,
            "project_id": 4,
            "date_from": "2026-10-01",
            "limit": 3,
            "offset": 3,
        },
    )

    assert response.status_code == 200
    params = route.calls.last.request.url.params
    assert dict(params) == {
        "printer_id": "1",
        "project_id": "4",
        "date_from": "2026-10-01",
        "limit": "3",
        "offset": "3",
    }
    rows = response.json()
    assert [row["status"] for row in rows] == ["printing", "completed", "archived"]
    assert rows[1]["print_name"] == "Bambu Bed Scraper"
    assert "file_path" not in rows[0] and "thumbnail_path" not in rows[0]


def test_an_archive_page_is_at_most_a_hundred(client: TestClient) -> None:
    assert client.get("/api/v1/farm/archives", params={"limit": 101}).status_code == 422


@respx.mock
def test_the_inventory_lists_spools_and_loaded_slots(client: TestClient) -> None:
    configure(client)
    spools = respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(200, json=recording("inventory-spools.json"))
    )
    respx.get(f"{API}/inventory/assignments").mock(
        return_value=httpx.Response(200, json=recording("inventory-assignments.json"))
    )
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(200, json=recording("printers.json"))
    )
    respx.get(f"{API}/printers/1/inventory-remain").mock(
        return_value=httpx.Response(200, json=recording("inventory-remain.json"))
    )

    response = client.get("/api/v1/farm/inventory", params={"include_archived": True})

    assert response.status_code == 200
    assert spools.calls.last.request.url.params["include_archived"] == "true"
    body = response.json()
    assert len(body["spools"]) == 12
    assert [slot["global_tray_id"] for slot in body["slots"]] == [1, 2, 3, 4, 8]
    assert body["slots"][3]["spool_id"] == 7
