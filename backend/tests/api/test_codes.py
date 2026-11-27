"""`GET /api/v1/codes/{code}` (#6955)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.api.test_filament import LOCATIONS, _location, _spool

pytestmark = pytest.mark.requires_postgres

CODES = "/api/v1/codes"


def test_a_location_resolves(client: TestClient) -> None:
    place = _location(client, "Shelf 9")
    got = client.get(f"{CODES}/L-{place['number']}")
    assert got.status_code == 200, got.text
    assert got.json() == {
        "kind": "location",
        "id": place["id"],
        "code": f"L-{place['number']}",
        "name": "Shelf 9",
        "archived": False,
    }
    assert client.get(f"{CODES}/l{place['number']}").json()["id"] == place["id"]


def test_a_spool_resolves_with_no_name(client: TestClient) -> None:
    spool = _spool(client)
    got = client.get(f"{CODES}/S-{spool['number']}")
    assert got.status_code == 200, got.text
    assert got.json() == {
        "kind": "spool",
        "id": spool["id"],
        "code": f"S-{spool['number']}",
        "name": None,
        "archived": False,
    }


def test_a_seeded_kind_resolves_by_its_k_code(client: TestClient) -> None:
    kinds = client.get("/api/v1/inventory/kinds")
    assert kinds.status_code == 200, kinds.text
    got = client.get(f"{CODES}/K-1")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["kind"] == "item_kind" and body["code"] == "K-1"
    assert body["id"] in {k["id"] for k in kinds.json()["kinds"]}
    assert client.get(f"{CODES}/k1").json()["id"] == body["id"]


def test_an_archived_location_still_resolves(client: TestClient) -> None:
    place = _location(client, "Old")
    client.patch(f"{LOCATIONS}/{place['id']}", json={"archived": True})
    got = client.get(f"{CODES}/L-{place['number']}")
    assert got.status_code == 200, got.text
    assert got.json()["archived"] is True


@pytest.mark.parametrize(
    "code", ["Q-1", "L-999999", "nonsense", "L-0", "L-99999999999", "L-" + "1" * 5000]
)
def test_an_unknown_code_is_404_code_unknown(client: TestClient, code: str) -> None:
    got = client.get(f"{CODES}/{code}")
    assert got.status_code == 404, got.text
    assert got.json()["code"] == "code_unknown"
