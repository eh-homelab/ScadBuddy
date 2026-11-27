"""#7170: the label printer's settings. Only a person changes the address."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.requires_postgres

AGENT = {"X-ScadBuddy-Agent-Author": "token:p1", "X-ScadBuddy-Agent-Author-Session": "s1"}
SETTINGS = "/api/v1/settings"
NAMES = (
    "filament_label_printer_host",
    "filament_label_printer_port",
    "filament_label_printer_media",
    "filament_label_printer_darkness",
)


def printer(client: TestClient) -> tuple[object, ...]:
    view = client.get(SETTINGS).json()
    return tuple(view[name] for name in NAMES)


def test_the_printer_settings_default(client: TestClient) -> None:
    assert printer(client) == (None, 9100, "direct", 10)


def test_a_person_sets_the_printer(client: TestClient) -> None:
    body = {
        "filament_label_printer_host": "zebra.lan",
        "filament_label_printer_port": 6101,
        "filament_label_printer_media": "transfer",
        "filament_label_printer_darkness": 22,
    }
    assert client.put(SETTINGS, json=body).status_code == 200
    assert printer(client) == ("zebra.lan", 6101, "transfer", 22)
    cleared = client.put(SETTINGS, json=dict.fromkeys(NAMES))
    assert cleared.status_code == 200
    assert printer(client) == (None, 9100, "direct", 10)


@pytest.mark.parametrize(
    "body",
    [
        {"filament_label_printer_host": "other.lan"},
        {"filament_label_printer_host": None},
        {"filament_label_printer_port": 6101},
        {"filament_label_printer_port": None},
    ],
)
def test_an_agent_cannot_move_the_printer(client: TestClient, body: dict[str, object]) -> None:
    client.put(SETTINGS, json={"filament_label_printer_host": "zebra.lan"})
    refused = client.put(SETTINGS, json=body, headers=AGENT)
    assert refused.status_code == 403
    assert refused.json()["type"].endswith("person_only")
    assert client.get(SETTINGS).json()["filament_label_printer_host"] == "zebra.lan"


def test_an_agent_may_set_media_and_darkness(client: TestClient) -> None:
    body = {"filament_label_printer_media": "transfer", "filament_label_printer_darkness": 5}
    assert client.put(SETTINGS, json=body, headers=AGENT).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"filament_label_printer_port": 0},
        {"filament_label_printer_port": 65536},
        {"filament_label_printer_darkness": 31},
        {"filament_label_printer_media": "ribbon"},
        {"filament_label_printer_host": "http://zebra"},
    ],
)
def test_bad_printer_values_are_a_422(client: TestClient, body: dict[str, object]) -> None:
    assert client.put(SETTINGS, json=body).status_code == 422
