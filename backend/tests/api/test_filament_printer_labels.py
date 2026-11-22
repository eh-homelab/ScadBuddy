"""`GET /filament/locations/{id}` and `POST /filament/printer-labels` (#6955)."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.filament.component import FILAMENT

pytestmark = pytest.mark.requires_postgres

LABELS = "/api/v1/filament/printer-labels"
LOCATIONS = "/api/v1/filament/locations"


def _state(client: TestClient) -> AppState:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state


def _printer(client: TestClient, name: str) -> str:
    printer = uuid4()
    with _state(client).settings_store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO printers (id, serial, name) VALUES (%s, %s, %s)",
            (printer, f"SN{printer.hex[:8]}", name),
        )
    return str(printer)


def _tray(client: TestClient, printer: str, ams: int, tray: int) -> None:
    _state(client).components.get(FILAMENT).locations.ensure_ams(
        UUID(printer), ams, tray, label=f"t{ams}{tray}"
    )


def _roots(client: TestClient, printer: str) -> int:
    with _state(client).settings_store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM filament_locations"
            " WHERE printer_id = %s AND printer_root AND archived_at IS NULL",
            (printer,),
        ).fetchone()
    assert row is not None
    return int(row["n"])


def test_printer_labels_in_tree_order(client: TestClient) -> None:
    printer = _printer(client, "H2C")
    _printer(client, "A first")
    for tray in (3, 0, 1, 2):
        _tray(client, printer, 0, tray)
    _tray(client, printer, 255, 0)
    _tray(client, printer, 1, 0)
    body = client.post(LABELS, json={"printer_ids": [printer]}).json()
    rows = [(label["name"], label["depth"]) for label in body["labels"]]
    assert [d for _, d in rows] == [0, 1, 2, 2, 2, 2, 1, 2, 1]
    assert rows[0][0] == "H2C"
    assert rows[1][0] == "H2C AMS-A"
    assert rows[-1][0] == "t2550"
    assert all(label["code"].startswith("L-") for label in body["labels"])
    everyone = client.post(LABELS, json={"printer_ids": None}).json()
    assert everyone["labels"][0]["name"] == "A first"


def test_external_holder_is_last(client: TestClient) -> None:
    printer = _printer(client, "H2C")
    _tray(client, printer, 255, 0)
    _tray(client, printer, 0, 0)
    labels = client.post(LABELS, json={"printer_ids": [printer]}).json()["labels"]
    assert [(x["depth"]) for x in labels] == [0, 1, 2, 1]
    assert labels[-1]["name"] == "t2550"


def test_printer_labels_make_the_printer_place(client: TestClient) -> None:
    printer = _printer(client, "H2C")
    assert _roots(client, printer) == 0
    first = client.post(LABELS, json={"printer_ids": [printer]}).json()
    assert _roots(client, printer) == 1
    second = client.post(LABELS, json={"printer_ids": [printer]}).json()
    assert _roots(client, printer) == 1
    assert first == second


def test_printer_labels_carry_the_base_url(client: TestClient) -> None:
    printer = _printer(client, "H2C")
    assert client.post(LABELS, json={"printer_ids": [printer]}).json()["base_url"] is None
    saved = client.put(
        "/api/v1/settings", json={"filament_label_base_url": "https://sb.example.com"}
    )
    assert saved.status_code == 200, saved.text
    body = client.post(LABELS, json={"printer_ids": [printer]}).json()
    assert body["base_url"] == "https://sb.example.com"


def test_printer_labels_unknown_printer_is_422(client: TestClient) -> None:
    assert client.post(LABELS, json={"printer_ids": [str(uuid4())]}).status_code == 422


def test_printer_labels_without_a_printer_kind_is_409(client: TestClient) -> None:
    printer = _printer(client, "H2C")
    with _state(client).settings_store.pool.connection() as conn:
        conn.execute("UPDATE filament_location_kinds SET printer = false")
    refused = client.post(LABELS, json={"printer_ids": [printer]})
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "no_printer_kind"


def test_location_page(client: TestClient) -> None:
    printer = _printer(client, "H2C")
    labels = client.post(LABELS, json={"printer_ids": [printer]}).json()["labels"]
    page = client.get(f"{LOCATIONS}/{labels[0]['location_id']}")
    assert page.status_code == 200, page.text
    assert page.json()["code"] == labels[0]["code"]
    assert client.get(f"{LOCATIONS}/{uuid4()}").status_code == 404
