"""#7170: the label print settings. Only a person changes the server."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.requires_postgres

AGENT = {"X-ScadBuddy-Agent-Author": "token:p1", "X-ScadBuddy-Agent-Author-Session": "s1"}
SETTINGS = "/api/v1/settings"
SERVER = "filament_label_server_url"
DEFAULT_SERVER = "http://cups.cups.svc:8631"


def test_the_defaults(client: TestClient) -> None:
    view = client.get(SETTINGS).json()
    assert view[SERVER] == DEFAULT_SERVER
    assert view["filament_label_queue"] is None
    assert view["filament_label_job_options"] == {
        "media_type": "Saved",
        "darkness": -1,
        "print_rate": "Default",
    }


def test_a_person_sets_and_resets_the_server(client: TestClient) -> None:
    assert client.put(SETTINGS, json={SERVER: "https://cups.example.lan/"}).status_code == 200
    assert client.get(SETTINGS).json()[SERVER] == "https://cups.example.lan"
    assert client.put(SETTINGS, json={SERVER: None}).status_code == 200
    assert client.get(SETTINGS).json()[SERVER] == DEFAULT_SERVER


@pytest.mark.parametrize(
    "body",
    [{SERVER: "http://other"}, {SERVER: None}],
)
def test_an_agent_cannot_move_the_server(client: TestClient, body: dict[str, object]) -> None:
    client.put(SETTINGS, json={SERVER: "https://cups.example.lan"})
    refused = client.put(SETTINGS, json=body, headers=AGENT)
    assert refused.status_code == 403
    assert refused.json()["type"].endswith("person_only")
    assert client.get(SETTINGS).json()[SERVER] == "https://cups.example.lan"


def test_an_agent_may_pick_the_queue_and_options(client: TestClient) -> None:
    options = {"media_type": "Thermal", "darkness": 20, "print_rate": "4"}
    body = {"filament_label_queue": "S-EDCDU03-2x1", "filament_label_job_options": options}
    assert client.put(SETTINGS, json=body, headers=AGENT).status_code == 200
    view = client.get(SETTINGS).json()
    assert view["filament_label_queue"] == "S-EDCDU03-2x1"
    assert view["filament_label_job_options"] == options


@pytest.mark.parametrize(
    "body",
    [
        {SERVER: "http://cups/printers"},
        {"filament_label_job_options": {"darkness": 31}},
        {"filament_label_job_options": {"media_type": "Ribbon"}},
        {"filament_label_job_options": {"print_rate": "13"}},
        {"filament_label_job_options": {"extra": 1}},
        {"filament_label_queue": "a b"},
        {"filament_label_queue": ""},
        {"filament_label_queue": "a" * 128},
        {"filament_label_queue": "q/../x"},
    ],
)
def test_bad_values_are_a_422(client: TestClient, body: dict[str, object]) -> None:
    assert client.put(SETTINGS, json=body).status_code == 422
