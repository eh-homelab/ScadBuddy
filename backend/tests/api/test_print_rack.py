"""The rack nozzle routes (#836): the remembered algorithm, the preview, the manual pick."""

from __future__ import annotations

from fastapi.testclient import TestClient


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
