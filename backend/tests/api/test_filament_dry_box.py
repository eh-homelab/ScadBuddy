"""The routes of a dry box (#7167, Task 2): a full box is a 409 `box_full`, a place with
two spools cannot become a box (422 `box_has_many`)."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from httpx import Response

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


def _boxed_pair(client: TestClient) -> tuple[dict[str, object], dict[str, object]]:
    """An archived spool that keeps its box, and a live spool that now holds it."""
    box = _box(client, "Box R")
    old, new = _spool(client), _spool(client)
    client.post(f"{SPOOLS}/{old['id']}/move", json={"location_id": box["id"]})
    done = client.post(f"{SPOOLS}/{old['id']}/archive", json={"archived": True, "reason": "other"})
    assert done.status_code == 200, done.text
    client.post(f"{SPOOLS}/{new['id']}/move", json={"location_id": box["id"]})
    return old, new


def test_a_restore_into_a_taken_box_is_a_409(client: TestClient) -> None:
    old, new = _boxed_pair(client)
    calls: list[Callable[[], Response]] = [
        lambda: client.patch(f"{SPOOLS}/{old['id']}", json={"archived": False}),
        lambda: client.post(f"{SPOOLS}/{old['id']}/archive", json={"archived": False}),
        lambda: client.post(f"{SPOOLS}/bulk-restore", json={"spool_ids": [old["id"]]}),
    ]
    for call in calls:
        refused = call()
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "box_full"
        assert refused.json()["occupant_id"] == new["id"]
    assert client.get(f"{SPOOLS}/{old['id']}").json()["archived_at"] is not None


def test_a_kind_flip_over_a_place_with_two_spools_is_a_422(client: TestClient) -> None:
    made = client.post("/api/v1/filament/catalog/location-kinds", json={"name": "Cabinet"})
    assert made.status_code == 201, made.text
    kind = made.json()
    cabinet = _location(client, "Cab", kind_id=kind["id"], capacity=1)
    for _ in range(2):
        spool = _spool(client)
        moved = client.post(f"{SPOOLS}/{spool['id']}/move", json={"location_id": cabinet["id"]})
        assert moved.status_code == 200, moved.text
    path = f"/api/v1/filament/catalog/location-kinds/{kind['id']}"
    refused = client.patch(path, json={"dry_storage": True})
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "box_has_many"
    assert client.get("/api/v1/filament/catalog/location-kinds").json()
    kept = next(
        r
        for r in client.get("/api/v1/filament/catalog/location-kinds").json()
        if r["id"] == kind["id"]
    )
    assert kept["dry_storage"] is False
