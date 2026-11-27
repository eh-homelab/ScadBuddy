"""The routes of a dry box (#7167, Task 2): a full box is a 409 `box_full`, a place with
two spools cannot become a box (422 `box_has_many`)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.api.test_filament import LOCATIONS, SPOOLS, _kind_id, _location, _spool

pytestmark = pytest.mark.requires_postgres


def _box(client: TestClient, name: str) -> dict[str, object]:
    return _location(client, name, kind_id=_kind_id(client, "Dry box"), capacity=1)


def test_a_move_into_a_full_box_is_a_409_naming_the_occupant(client: TestClient) -> None:
    box = _box(client, "Box A")
    first, second = _spool(client), _spool(client)
    ok = client.post(f"{SPOOLS}/{first['id']}/move", json={"location_id": box["id"]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["home_location_id"] == box["id"]
    refused = client.post(f"{SPOOLS}/{second['id']}/move", json={"location_id": box["id"]})
    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["code"] == "box_full"
    assert body["occupant_id"] == first["id"]
    assert client.get(f"{SPOOLS}/{second['id']}").json()["location_id"] is None


def test_a_bulk_create_into_a_full_box_is_a_409(client: TestClient) -> None:
    box = _box(client, "Box B")
    first = _spool(client)
    client.post(f"{SPOOLS}/{first['id']}/move", json={"location_id": box["id"]})
    before = len(client.get(SPOOLS).json())
    made = client.post(
        f"{SPOOLS}/bulk",
        json={"spool": {"material_id": first["material_id"]}, "location_id": box["id"]},
        headers={"Idempotency-Key": "box-full-1"},
    )
    assert made.status_code == 409, made.text
    assert made.json()["code"] == "box_full"
    assert len(client.get(SPOOLS).json()) == before


def test_a_place_with_two_spools_cannot_become_a_box(client: TestClient) -> None:
    drawer = _location(client, "Drawer", kind_id=_kind_id(client, "Dry box"), capacity=3)
    for _ in range(2):
        spool = _spool(client)
        moved = client.post(f"{SPOOLS}/{spool['id']}/move", json={"location_id": drawer["id"]})
        assert moved.status_code == 200, moved.text
    refused = client.patch(f"{LOCATIONS}/{drawer['id']}", json={"capacity": 1})
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "box_has_many"
    kept = client.get(f"{LOCATIONS}/{drawer['id']}")
    assert kept.json()["capacity"] == 3
