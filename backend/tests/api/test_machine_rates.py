"""`/api/v1/printers/{id}/machine-rate` (#4625, order operations spec 2026-11-07 §6): a
printer's machine rate inputs, replaced whole, and the rate they give with each part."""

from __future__ import annotations

from typing import Any

import httpx
import respx
from fastapi.testclient import TestClient

from tests.api.test_printers import bambuddy_printer, bambuddy_printers, create
from tests.api.test_send import API, configure

INPUTS: dict[str, Any] = {
    "profile": {
        "purchase_price": "1440",
        "life_hours": "3000",
        "watts": "120",
        "kwh_price": "0.25",
        "labour_rate_per_hour": "30",
    },
    "wear_parts": [{"name": "Nozzle", "cost": "14", "life_hours": "200"}],
    "maintenance": [
        {"task": "Lubricate rods", "minutes": "20", "consumables": "2", "interval_hours": "100"}
    ],
}


def path(printer: str) -> str:
    return f"/api/v1/printers/{printer}/machine-rate"


def test_a_printer_with_no_profile_is_the_books_rate(client: TestClient) -> None:
    row = create(client)
    view = client.get(path(row["id"]))
    assert view.status_code == 200, view.text
    body = view.json()
    assert body["inputs"] is None
    assert (body["printer"], body["printer_name"]) == (row["id"], "H2C")
    assert body["breakdown"]["source"] == "price_book"
    assert body["breakdown"]["rate_per_hour"] == "0"


def test_inputs_are_replaced_whole_and_priced(client: TestClient) -> None:
    row = create(client)
    put = client.put(path(row["id"]), json=INPUTS)
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["inputs"]["wear_parts"] == INPUTS["wear_parts"]
    breakdown = body["breakdown"]
    assert breakdown["source"] == "profile"
    assert breakdown["intervals"] == "typed"
    assert (
        breakdown["maintenance_per_hour"],
        breakdown["wear_per_hour"],
        breakdown["power_per_hour"],
        breakdown["depreciation_per_hour"],
        breakdown["rate_per_hour"],
    ) == ("0.12", "0.07", "0.03", "0.48", "0.7")
    assert client.get(path(row["id"])).json() == body
    assert client.delete(path(row["id"])).status_code == 204
    assert client.get(path(row["id"])).json()["inputs"] is None
    assert client.delete(path(row["id"])).status_code == 404


def test_an_unknown_printer_is_404(client: TestClient) -> None:
    assert client.get(path("nope")).status_code == 404
    assert client.put(path("nope"), json=INPUTS).status_code == 404
    assert client.delete(path("nope")).status_code == 404


def test_bad_inputs_are_422(client: TestClient) -> None:
    row = create(client)
    zero_life = {**INPUTS, "wear_parts": [{"name": "Nozzle", "cost": "14", "life_hours": "0"}]}
    assert client.put(path(row["id"]), json=zero_life).status_code == 422
    twice = {**INPUTS, "wear_parts": INPUTS["wear_parts"] * 2}
    assert client.put(path(row["id"]), json=twice).status_code == 422


@respx.mock
def test_a_linked_printer_reads_bambuddys_intervals(client: TestClient) -> None:
    configure(client)
    bambuddy_printers(bambuddy_printer(1, "01P00A000000001"))
    row = create(client)
    assert (
        client.put(
            f"/api/v1/printers/{row['id']}/bambuddy", json={"bambuddy_printer_id": 1}
        ).status_code
        == 200
    )
    maintenance = respx.get(f"{API}/maintenance/printers/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "printer_id": 1,
                "maintenance_items": [
                    {
                        "maintenance_type_name": "Lubricate rods",
                        "enabled": True,
                        "interval_hours": 50.0,
                        "interval_type": "hours",
                    }
                ],
            },
        )
    )
    body = client.put(path(row["id"]), json=INPUTS).json()
    assert maintenance.called
    assert body["bambuddy_printer_id"] == 1
    assert body["breakdown"]["intervals"] == "bambuddy"
    assert body["breakdown"]["maintenance_per_hour"] == "0.24"

    maintenance.mock(return_value=httpx.Response(503, json={"detail": "down"}))
    stored = client.get(path(row["id"])).json()["breakdown"]
    assert stored["intervals"] == "stored"
    assert stored["maintenance_per_hour"] == "0.24"
