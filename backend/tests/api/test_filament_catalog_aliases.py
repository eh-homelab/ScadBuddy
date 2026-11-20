"""Catalog aliases over the API (#6706): `aliases` on every row of the keyed catalogs
(materials, subtypes, brands, categories, effects), `PUT .../aliases`, the 409 that names
the row holding a key, `GET .../resolve`, and the name lookups of the Bambuddy pull, the
CSV import and the price book, which resolve by key."""

from __future__ import annotations

import httpx
import psycopg
import pytest
import respx
from fastapi.testclient import TestClient

from tests.api.test_send import API, configure

pytestmark = pytest.mark.requires_postgres

CATALOG = "/api/v1/filament/catalog"
URL = f"{CATALOG}/effects"
TYPES = "/api/v1/filament/types"
SPOOLS = "/api/v1/filament/spools"


def _effect(client: TestClient, name: str) -> dict[str, object]:
    found: dict[str, object] = next(r for r in client.get(URL).json() if r["name"] == name)
    return found


def _effect_count(client: TestClient) -> int:
    return len(client.get(URL, params={"include_archived": True}).json())


def test_aliases_are_put_listed_and_trimmed(client: TestClient) -> None:
    dual = _effect(client, "Dual Color")
    assert dual["aliases"] == []
    put = client.put(f"{URL}/{dual['id']}/aliases", json={"aliases": [" Two Tone ", "Bicolor"]})
    assert put.status_code == 200, put.text
    assert put.json()["aliases"] == ["Bicolor", "Two Tone"]
    assert _effect(client, "Dual Color")["aliases"] == ["Bicolor", "Two Tone"]


def test_a_clash_is_409_and_names_the_holder(client: TestClient) -> None:
    dual = _effect(client, "Dual Color")
    tri = _effect(client, "Tri Color")
    client.put(f"{URL}/{dual['id']}/aliases", json={"aliases": ["Bicolor"]})
    for method, path, body in (
        ("PUT", f"{URL}/{tri['id']}/aliases", {"aliases": ["bi-color"]}),
        ("PATCH", f"{URL}/{tri['id']}", {"name": "BICOLOR"}),
        ("POST", URL, {"name": "dual color"}),
    ):
        refused = client.request(method, path, json=body)
        assert refused.status_code == 409, (path, refused.text)
        problem = refused.json()
        assert (problem["effect_id"], problem["effect_name"]) == (dual["id"], "Dual Color")
        assert "Dual Color" in problem["detail"]


def test_an_empty_alias_is_422_and_a_missing_effect_404(client: TestClient) -> None:
    dual = _effect(client, "Dual Color")
    for alias in ("", "   ", "--"):
        refused = client.put(f"{URL}/{dual['id']}/aliases", json={"aliases": [alias]})
        assert refused.status_code == 422, (alias, refused.text)
    missing = client.put(
        f"{URL}/00000000-0000-4000-8000-000000000001/aliases", json={"aliases": ["x"]}
    )
    assert missing.status_code == 404, missing.text


def test_resolve_finds_any_spelling(client: TestClient) -> None:
    dual = _effect(client, "Dual Color")
    client.put(f"{URL}/{dual['id']}/aliases", json={"aliases": ["Bicolor"]})
    for name in ("dual-color", "BI COLOR"):
        found = client.get(f"{URL}/resolve", params={"name": name})
        assert found.status_code == 200, found.text
        assert found.json()["id"] == dual["id"]
    assert client.get(f"{URL}/resolve", params={"name": "Holo"}).status_code == 404


@respx.mock
def test_the_bambuddy_pull_resolves_an_effect_by_key(client: TestClient) -> None:
    configure(client)
    before = _effect_count(client)
    bb = {
        "id": 41,
        "material": "PLA",
        "rgba": "00FF00",
        "label_weight": 1000,
        "weight_used": 0.0,
        "effect_type": "dual-color",
        "updated_at": "2026-11-14T10:00:00",
    }
    respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(200, json=[bb]))
    respx.get(f"{API}/inventory/catalog").mock(return_value=httpx.Response(200, json=[]))
    pulled = client.post("/api/v1/filament/bambuddy/import")
    assert pulled.status_code == 200, pulled.text
    (spool_id,) = pulled.json()["created_ids"]
    spool = client.get(f"{SPOOLS}/{spool_id}").json()
    assert spool["names"]["effect"] == "Dual Color"
    assert _effect_count(client) == before


def test_the_csv_import_resolves_an_effect_by_key_and_adds_a_new_one(client: TestClient) -> None:
    before = _effect_count(client)
    rows = [{"material": "PLA", "effect": "tri-color"}, {"material": "PLA", "effect": "Holo"}]
    preview = client.post(f"{SPOOLS}/import", json={"rows": rows, "dry_run": True}).json()
    known, new = preview["rows"]
    assert known["changes"]["effect"] == [None, "Tri Color"]
    assert new["changes"]["effect"] == [None, "Holo", {"new": True}]
    done = client.post(
        f"{SPOOLS}/import",
        json={"rows": rows, "dry_run": False},
        headers={"Idempotency-Key": "k-6706"},
    )
    assert done.status_code == 200, done.text
    assert _effect_count(client) == before + 1
    holo = _effect(client, "Holo")
    again = client.post(URL, json={"name": "HOLO"})
    assert again.status_code == 409 and again.json()["effect_id"] == holo["id"]


def test_the_price_book_effect_resolves_through_an_alias(
    client: TestClient, pg_conninfo: str
) -> None:
    made = client.post(URL, json={"name": "Two Tone"}).json()
    client.put(f"{URL}/{made['id']}/aliases", json={"aliases": ["Dual-Colour", "dual color 2"]})
    # "Dual Color" holds DUALCOLOR; move it aside so the alias can take that key.
    dual = _effect(client, "Dual Color")
    client.patch(f"{URL}/{dual['id']}", json={"name": "Dual Colour Classic"})
    put = client.put(f"{URL}/{made['id']}/aliases", json={"aliases": ["dual-color"]})
    assert put.status_code == 200, put.text
    material = next(
        r["id"]
        for r in client.get("/api/v1/filament/catalog/materials").json()
        if r["name"] == "PLA"
    )
    kind = client.post(
        TYPES,
        json={
            "code": "PLA-TWO",
            "material_id": material,
            "effect_id": made["id"],
            "rgba": "#ff0000",
            "label_weight_g": 1000,
            "cost_per_kg": "20",
        },
    )
    assert kind.status_code == 201, kind.text
    with psycopg.connect(pg_conninfo) as conn:
        row = conn.execute(
            "SELECT effect FROM filaments WHERE id = %s", (kind.json()["filament_id"],)
        ).fetchone()
    assert row is not None and row[0] == "dual-color"


def test_a_material_and_a_subtype_take_aliases_and_resolve_in_scope(client: TestClient) -> None:
    pla = next(r for r in client.get(f"{CATALOG}/materials").json() if r["name"] == "PLA")
    put = client.put(f"{CATALOG}/materials/{pla['id']}/aliases", json={"aliases": ["PLA Basic"]})
    assert put.status_code == 200, put.text
    clash = client.post(f"{CATALOG}/materials", json={"name": "pla-basic"})
    assert clash.status_code == 409, clash.text
    assert (clash.json()["material_id"], clash.json()["material_name"]) == (pla["id"], "PLA")
    found = client.get(f"{CATALOG}/materials/resolve", params={"name": "p.l.a"})
    assert found.json()["id"] == pla["id"]

    mine = client.post(f"{CATALOG}/subtypes", json={"name": "Glossy", "material_id": pla["id"]})
    assert mine.status_code == 201, mine.text
    resolved = client.get(
        f"{CATALOG}/subtypes/resolve", params={"name": "GLOSSY", "material_id": pla["id"]}
    )
    assert resolved.json()["id"] == mine.json()["id"]
    elsewhere = client.get(f"{CATALOG}/subtypes/resolve", params={"name": "GLOSSY"})
    assert elsewhere.status_code == 404, elsewhere.text


def test_a_catalog_without_aliases_refuses_them(client: TestClient) -> None:
    row = client.post(f"{CATALOG}/insert-materials", json={"name": "Foam"}).json()
    assert "aliases" not in row
    put = client.put(f"{CATALOG}/insert-materials/{row['id']}/aliases", json={"aliases": ["x"]})
    assert put.status_code == 404, put.text
    assert client.get(f"{CATALOG}/colours/resolve", params={"name": "x"}).status_code == 404
