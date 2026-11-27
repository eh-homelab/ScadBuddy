"""The routes of a dry box (#7167, Task 2): a full box is a 409 `box_full`, a place with
two spools cannot become a box (422 `box_has_many`)."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from tests.api.test_filament import LOCATIONS, SPOOLS, _kind_id, _location, _spool, _state

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


def _asked(client: TestClient, spool: dict[str, object]) -> None:
    """The spool came out of a tray and is asked about its home (as capture parks it)."""
    with _state(client).settings_store.pool.connection() as conn:
        conn.execute(
            "UPDATE filament_spools SET location_id = NULL, home_asked_at = now() WHERE id = %s",
            (spool["id"],),
        )


def test_awaiting_home_lists_only_the_asked_spools(client: TestClient) -> None:
    box = _box(client, "Box W")
    asked, plain = _spool(client), _spool(client)
    client.post(f"{SPOOLS}/{asked['id']}/move", json={"location_id": box["id"]})
    _asked(client, asked)
    listed = client.get(SPOOLS, params={"awaiting_home": "true"})
    assert listed.status_code == 200, listed.text
    assert [s["id"] for s in listed.json()] == [asked["id"]]
    assert listed.json()[0]["home_asked_at"] is not None
    rest = [s["id"] for s in client.get(SPOOLS, params={"awaiting_home": "false"}).json()]
    assert plain["id"] in rest and asked["id"] not in rest


def test_a_replace_puts_the_identified_spool_in_and_archives_the_old(
    client: TestClient,
) -> None:
    """The unloaded spool is asked; the person names another spool and replaces the box's."""
    box = _box(client, "Box V")
    unloaded, held, named = _spool(client), _spool(client, grams_left=80), _spool(client)
    client.post(f"{SPOOLS}/{unloaded['id']}/move", json={"location_id": box["id"]})
    _asked(client, unloaded)
    client.post(f"{SPOOLS}/{held['id']}/move", json={"location_id": box["id"]})
    moved = client.post(
        f"{SPOOLS}/{named['id']}/move",
        json={
            "location_id": box["id"],
            "if_occupied": "replace",
            "answers": unloaded["id"],
            "expected_occupant_id": held["id"],
        },
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["location_id"] == box["id"]
    gone = client.get(f"{SPOOLS}/{held['id']}").json()
    assert gone["archived_at"] is not None and gone["grams_left"] == 0
    events = client.get(f"{SPOOLS}/{held['id']}/events").json()
    waste = [e for e in events if e["kind"] == "non_print_waste"]
    assert [(e["data"]["cause"], e["data"]["waste_g"]) for e in waste] == [("replaced", 80)]
    answered = client.get(f"{SPOOLS}/{unloaded['id']}").json()
    assert answered["home_asked_at"] is None and answered["location_id"] is None
    assert answered["archived_at"] is None
    assert client.get(SPOOLS, params={"awaiting_home": "true"}).json() == []


def test_an_answer_for_another_home_is_a_422(client: TestClient) -> None:
    box, elsewhere = _box(client, "Box U"), _box(client, "Box T")
    asked, other = _spool(client), _spool(client)
    client.post(f"{SPOOLS}/{asked['id']}/move", json={"location_id": box["id"]})
    _asked(client, asked)
    refused = client.post(
        f"{SPOOLS}/{other['id']}/move",
        json={"location_id": elsewhere["id"], "answers": asked["id"]},
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "not_its_home"
    assert client.get(f"{SPOOLS}/{other['id']}").json()["location_id"] is None


@pytest.mark.parametrize("value", ["assume_new", "keep", "", "REPLACE"])
def test_an_unknown_if_occupied_is_a_422(client: TestClient, value: str) -> None:
    box = _box(client, "Box S")
    spool = _spool(client)
    refused = client.post(
        f"{SPOOLS}/{spool['id']}/move", json={"location_id": box["id"], "if_occupied": value}
    )
    assert refused.status_code == 422, refused.text
    assert client.get(f"{SPOOLS}/{spool['id']}").json()["location_id"] is None


def test_an_answer_that_names_no_spool_is_a_404_naming_it(client: TestClient) -> None:
    box = _box(client, "Box Q")
    spool = _spool(client)
    missing = "00000000-0000-4000-8000-00000000717a"
    refused = client.post(
        f"{SPOOLS}/{spool['id']}/move", json={"location_id": box["id"], "answers": missing}
    )
    assert refused.status_code == 404, refused.text
    assert missing in refused.json()["detail"]
    assert client.get(f"{SPOOLS}/{spool['id']}").json()["location_id"] is None


def test_a_replace_of_another_occupant_than_expected_is_a_409(client: TestClient) -> None:
    """#7167 Task 6 review (M4): the caller saw `seen` in the box; `other` is there now."""
    box, shelf = _box(client, "Box W"), _location(client, "Shelf W")
    seen, other, new = _spool(client), _spool(client), _spool(client)
    client.post(f"{SPOOLS}/{seen['id']}/move", json={"location_id": box["id"]})
    client.post(f"{SPOOLS}/{seen['id']}/move", json={"location_id": shelf["id"]})
    client.post(f"{SPOOLS}/{other['id']}/move", json={"location_id": box["id"]})
    refused = client.post(
        f"{SPOOLS}/{new['id']}/move",
        json={
            "location_id": box["id"],
            "if_occupied": "replace",
            "expected_occupant_id": seen["id"],
        },
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "box_full"
    assert refused.json()["occupant_id"] == other["id"]
    assert client.get(f"{SPOOLS}/{other['id']}").json()["archived_at"] is None
    assert client.get(f"{SPOOLS}/{new['id']}").json()["location_id"] is None


def test_a_replace_that_names_no_occupant_is_a_422(client: TestClient) -> None:
    """#7167 fix round 2: a replace names the box's spool it expects, or nothing happens."""
    box = _box(client, "Box X")
    held, new = _spool(client), _spool(client)
    client.post(f"{SPOOLS}/{held['id']}/move", json={"location_id": box["id"]})
    refused = client.post(
        f"{SPOOLS}/{new['id']}/move", json={"location_id": box["id"], "if_occupied": "replace"}
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "expected_occupant_required"
    assert client.get(f"{SPOOLS}/{held['id']}").json()["archived_at"] is None
    assert client.get(f"{SPOOLS}/{new['id']}").json()["location_id"] is None
