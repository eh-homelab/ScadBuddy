"""Stations (#8062, spec D3): a location flagged for flow tasks."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.api.test_filament import LOCATIONS, _location


def test_station_flag(client: TestClient) -> None:
    room = _location(client, "Room")
    zed = _location(client, "Zed", parent_id=room["id"])
    alpha = _location(client, "alpha")
    plain = _location(client, "Plain")
    assert plain["station"] is False
    patched = client.patch(f"{LOCATIONS}/{zed['id']}", json={"station": True})
    assert patched.status_code == 200, patched.text
    assert patched.json()["station"] is True
    assert client.patch(f"{LOCATIONS}/{alpha['id']}", json={"station": True}).status_code == 200
    stations = client.get(f"{LOCATIONS}?station=true").json()
    assert [(s["name"], s["station"], s["children"]) for s in stations] == [
        ("alpha", True, []),
        ("Zed", True, []),
    ]
    assert [n["name"] for n in client.get(LOCATIONS).json()] == ["alpha", "Plain", "Room"]
    made = client.post(
        LOCATIONS,
        json={"name": "Made", "kind_id": zed["kind_id"], "station": True},
    )
    assert made.json()["station"] is True
