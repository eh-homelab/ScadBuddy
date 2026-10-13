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
def test_the_queue_passes_its_filters(client: TestClient) -> None:
    configure(client)
    route = respx.get(f"{API}/queue/").mock(
        return_value=httpx.Response(200, json=recording("queue.json"))
    )

    response = client.get("/api/v1/farm/queue", params={"printer_id": 1, "status": "failed"})

    assert response.status_code == 200
    params = route.calls.last.request.url.params
    assert (params["printer_id"], params["status"]) == ("1", "failed")
    items = response.json()
    assert [item["id"] for item in items] == [257, 259, 199, 205]
    assert items[0]["archive_name"] == "Carrot Garden"
    assert items[0]["printer_name"] == "3DP-31B-598"


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


def test_an_archive_window_is_at_most_two_hundred(client: TestClient) -> None:
    assert client.get("/api/v1/farm/archives", params={"limit": 201}).status_code == 422


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


@respx.mock
@pytest.mark.parametrize("status", ["queued", "pending&printer_id=2", "../archives"])
def test_a_queue_status_bambuddy_does_not_have_is_refused_before_bambuddy(
    client: TestClient, status: str
) -> None:
    """The route forwards only a fixed set of values to its one fixed endpoint, never
    the caller's free text (security review on #1912)."""
    configure(client)
    route = respx.get(f"{API}/queue/").mock(return_value=httpx.Response(200, json=[]))

    response = client.get("/api/v1/farm/queue", params={"status": status})

    assert response.status_code == 422
    assert not route.called


# --- #785: one spool's summary, for the filament hover card ------------------------


def mock_inventory() -> tuple[respx.Route, respx.Route]:
    return (
        respx.get(f"{API}/inventory/spools").mock(
            return_value=httpx.Response(200, json=recording("inventory-spools.json"))
        ),
        respx.get(f"{API}/inventory/assignments").mock(
            return_value=httpx.Response(200, json=recording("inventory-assignments.json"))
        ),
    )


@respx.mock
def test_a_spool_summary_counts_its_filament_and_prices_it(client: TestClient) -> None:
    configure(client)
    spools, _ = mock_inventory()

    response = client.get("/api/v1/farm/spools/15/summary", params={"grams": 10})

    assert response.status_code == 200, response.text
    # The archived ones too, so an archived spool's own card still resolves.
    assert spools.calls.last.request.url.params["include_archived"] == "true"
    body = response.json()
    # Insignia Clear PETG: spool 15 has 400 g left, spool 14 is full; both loaded.
    assert body["spool"]["remaining_g"] == 400.0
    assert body["spool"]["cost_per_kg"] == 15.96
    assert body["spool"]["loaded"] == {
        "printer_id": 1,
        "printer_name": "3DP-31B-598",
        "ams_id": 0,
        "tray_id": 3,
    }
    assert body["price_per_g"] == pytest.approx(0.01596)
    assert body["remaining_value"] == pytest.approx(6.384)
    assert (body["grams"], body["grams_cost"]) == (10.0, pytest.approx(0.1596))
    filament = body["filament"]
    assert (filament["material"], filament["brand"], filament["color_name"]) == (
        "PETG",
        "Insignia",
        "Clear",
    )
    assert filament["spool_count"] == 2
    assert sorted(filament["spool_ids"]) == [14, 15]
    assert filament["remaining_g"] == 1400.0
    assert sorted((row["spool_id"], row["tray_id"]) for row in filament["loaded"]) == [
        (14, 2),
        (15, 3),
    ]


@respx.mock
def test_without_a_price_every_cost_is_null_not_a_guess(client: TestClient) -> None:
    configure(client)
    mock_inventory()

    body = client.get("/api/v1/farm/spools/1/summary", params={"grams": 10}).json()

    assert body["spool"]["cost_per_kg"] is None
    assert (body["price_per_g"], body["remaining_value"], body["grams_cost"]) == (None, None, None)
    assert body["filament"]["spool_count"] == 1
    assert body["filament"]["loaded"] == []
    assert body["spool"]["loaded"] is None


@respx.mock
def test_an_archived_spool_is_summarised_but_not_counted_as_stock(client: TestClient) -> None:
    configure(client)
    rows = recording("inventory-spools.json")
    for row in rows:
        if row["id"] == 14:
            row["archived_at"] = "2026-10-01T00:00:00"
    respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(200, json=rows))
    respx.get(f"{API}/inventory/assignments").mock(return_value=httpx.Response(200, json=[]))

    archived = client.get("/api/v1/farm/spools/14/summary").json()

    assert archived["spool"]["archived"] is True
    assert archived["filament"]["spool_ids"] == [15]
    assert archived["filament"]["remaining_g"] == 400.0


@respx.mock
def test_an_unknown_spool_is_a_404(client: TestClient) -> None:
    configure(client)
    mock_inventory()

    assert client.get("/api/v1/farm/spools/999/summary").status_code == 404


def test_grams_cannot_be_negative(client: TestClient) -> None:
    assert client.get("/api/v1/farm/spools/1/summary", params={"grams": -1}).status_code == 422
