"""`/api/v1/filament/catalog/{kind}` (#6261): the filament catalogs."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.requires_postgres

URL = "/api/v1/filament/catalog"
MISSING = "00000000-0000-4000-8000-000000000001"


def test_a_catalog_is_listed_made_changed_and_archived(client: TestClient) -> None:
    names = [row["name"] for row in client.get(f"{URL}/materials").json()]
    assert "PLA" in names
    made = client.post(f"{URL}/brands", json={"name": "Acme"})
    assert made.status_code == 201, made.text
    brand = made.json()
    assert client.get(f"{URL}/brands").json() == [brand]
    patched = client.patch(f"{URL}/brands/{brand['id']}", json={"name": "Acme 2"})
    assert patched.json()["name"] == "Acme 2"
    archived = client.patch(f"{URL}/brands/{brand['id']}", json={"archived": True})
    assert archived.json()["archived_at"] is not None
    assert client.get(f"{URL}/brands").json() == []
    assert len(client.get(f"{URL}/brands", params={"include_archived": True}).json()) == 1


def test_a_second_material_with_the_same_name_is_refused(client: TestClient) -> None:
    taken = client.post(f"{URL}/materials", json={"name": "pla"})
    assert taken.status_code == 409, taken.text


def test_an_unknown_kind_or_id_is_404(client: TestClient) -> None:
    assert client.get(f"{URL}/nope").status_code == 404
    assert client.post(f"{URL}/nope", json={"name": "x"}).status_code == 404
    assert client.patch(f"{URL}/brands/{MISSING}", json={"name": "x"}).status_code == 404


def test_a_bad_body_is_422(client: TestClient) -> None:
    assert client.post(f"{URL}/brands", json={"name": ""}).status_code == 422
    assert client.post(f"{URL}/brands", json={"name": "x", "bogus": 1}).status_code == 422
    bad_ref = client.post(f"{URL}/subtypes", json={"name": "S", "material_id": MISSING})
    assert bad_ref.status_code == 422, bad_ref.text
