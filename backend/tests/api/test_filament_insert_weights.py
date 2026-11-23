"""`…/filament/spools/{id}/insert-weight-offer` and `…/insert-weight` (#7280)."""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.requires_postgres

CATALOG = "/api/v1/filament/catalog/insert-types"
SPOOLS = "/api/v1/filament/spools"
MISSING = "00000000-0000-4000-8000-000000000001"


def _pla(client: TestClient) -> str:
    rows = client.get("/api/v1/filament/catalog/materials").json()
    found: str = next(row["id"] for row in rows if row["name"] == "PLA")
    return found


def _insert(client: TestClient, **over: object) -> dict[str, object]:
    made = client.post(CATALOG, json={"name": "Cardboard", "empty_weight_g": 200} | over)
    assert made.status_code == 201, made.text
    row: dict[str, object] = made.json()
    return row


def _spool(client: TestClient, insert: object, **over: object) -> str:
    body = {
        "material_id": _pla(client),
        "label_weight_g": 1000,
        "insert_type_id": insert,
        "grams_left": 1000,
    } | over
    made = client.post(SPOOLS, json=body)
    assert made.status_code == 201, made.text
    spool: str = made.json()["id"]
    return spool


def test_a_new_spool_is_offered_and_accepted(client: TestClient) -> None:
    insert = _insert(client, note="Placeholder weight, weigh an empty spool")
    assert insert["empty_weight_basis"] == "person"
    assert (insert["placeholder"], insert["derived_count"]) == (True, 0)
    spool = _spool(client, insert["id"])
    events = client.get(f"{SPOOLS}/{spool}/events").json()

    check = client.get(f"{SPOOLS}/{spool}/insert-weight-offer", params={"reading_g": 1150})
    assert check.status_code == 200, check.text
    body = check.json()
    assert (body["reason"], body["empty_weight_g"], body["filament_g"]) == (None, 200, 950)
    offer = body["offer"]
    assert (offer["derived_g"], offer["new_empty_weight_g"], offer["sample_count"]) == (150, 150, 1)
    assert (offer["placeholder"], offer["checked_by_default"]) == (True, True)

    accepted = client.post(f"{SPOOLS}/{spool}/insert-weight", json={"reading_g": 1150})
    assert accepted.status_code == 200, accepted.text
    done = accepted.json()
    assert (done["empty_weight_g"], done["sample_count"], done["filament_g"]) == (150, 1, 1000)
    assert done["insert_type"]["empty_weight_basis"] == "derived"
    assert done["insert_type"]["derived_count"] == 1
    assert client.get(f"{SPOOLS}/{spool}/events").json() == events
    listed = client.get(CATALOG).json()
    assert [(r["empty_weight_g"], r["empty_weight_basis"]) for r in listed] == [(150, "derived")]


def test_no_offer_is_a_reason_and_an_accept_without_one_is_409(
    client: TestClient, pg_conninfo: str
) -> None:
    insert = _insert(client)
    bare = client.get(
        f"{SPOOLS}/{_spool(client, None)}/insert-weight-offer", params={"reading_g": 1150}
    )
    assert bare.status_code == 200, bare.text
    assert (bare.json()["reason"], bare.json()["filament_g"]) == ("no_insert", None)

    used = _spool(client, insert["id"])
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO filament_spool_events (spool_id, kind, source, delta_g)"
            " VALUES (%s, 'print_ended', 'print_actual', -12)",
            (used,),
        )
    weighed = client.post(f"{SPOOLS}/{used}/events", json={"kind": "weighed", "grams": 1000})
    assert weighed.status_code == 201, weighed.text
    refused = client.post(f"{SPOOLS}/{used}/insert-weight", json={"reading_g": 1150})
    assert refused.status_code == 409, refused.text
    assert (refused.json()["code"], refused.json()["reason"]) == ("no_insert_weight_offer", "used")


def test_an_unknown_spool_is_404_and_a_bad_reading_422(client: TestClient) -> None:
    offer = f"{SPOOLS}/{MISSING}/insert-weight-offer"
    assert client.get(offer, params={"reading_g": 1150}).status_code == 404
    accept = client.post(f"{SPOOLS}/{MISSING}/insert-weight", json={"reading_g": 1150})
    assert accept.status_code == 404
    spool = _spool(client, _insert(client)["id"])
    assert client.get(f"{SPOOLS}/{spool}/insert-weight-offer").status_code == 422
    zero = client.get(f"{SPOOLS}/{spool}/insert-weight-offer", params={"reading_g": 0})
    assert zero.status_code == 422
    body = client.post(f"{SPOOLS}/{spool}/insert-weight", json={"reading_g": 0})
    assert body.status_code == 422


def test_a_catalog_write_sets_the_basis(client: TestClient) -> None:
    insert = _insert(client, empty_weight_basis="placeholder")
    assert (insert["empty_weight_basis"], insert["placeholder"]) == ("placeholder", True)
    one = f"{CATALOG}/{insert['id']}"
    noted = client.patch(one, json={"note": "kept"})
    assert noted.json()["empty_weight_basis"] == "placeholder"
    weighed = client.patch(one, json={"empty_weight_g": 190})
    assert (weighed.json()["empty_weight_basis"], weighed.json()["placeholder"]) == (
        "person",
        False,
    )
    marked = client.patch(one, json={"empty_weight_g": 180, "empty_weight_basis": "placeholder"})
    assert marked.json()["empty_weight_basis"] == "placeholder"
    assert client.patch(one, json={"empty_weight_basis": "derived"}).status_code == 422
    assert (
        client.post(
            CATALOG, json={"name": "X", "empty_weight_g": 1, "empty_weight_basis": "catalog"}
        ).status_code
        == 422
    )
