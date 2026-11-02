"""`/api/v1/orders/price-book` (#4250, spec 2026-10-18 §5.1, plan Ruling 4): the policy,
the filament records and the minor unit's exponent on the wire (Ruling 5)."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.api.test_customers import create as create_customer
from tests.api.test_orders import create_order

PLA: dict[str, Any] = {
    "material": "PLA",
    "subtype": "Basic",
    "brand": "Bambu",
    "colour_name": "Jade White",
    "colour": "#FFFFFF",
    "cost_per_kg": "25.50",
    "density_g_cm3": "1.24",
}
BOOK = "/api/v1/orders/price-book"


def add_filament(client: TestClient, **fields: Any) -> dict[str, Any]:
    response = client.post(f"{BOOK}/filaments", json={**PLA, **fields})
    assert response.status_code == 201, response.text
    made: dict[str, Any] = response.json()
    return made


def test_the_book_answers_the_policy_and_the_filaments(client: TestClient) -> None:
    book = client.get(BOOK).json()
    assert book == {
        "currency": "USD",
        "minor_exponent": 2,
        "policy": {
            "machine_rate_per_hour": "0",
            "setup_fee": "0",
            "labour_per_item": "0",
            "markup_percent": "0",
            "rounding": "to_minor_unit",
        },
        "rough_estimate_factor": "1.15",
        "filaments": [],
        "spool_import": False,
    }
    made = add_filament(client)
    assert (made["cost_per_kg"], made["density_g_cm3"]) == ("25.5", "1.24")
    assert [f["id"] for f in client.get(BOOK).json()["filaments"]] == [made["id"]]


def test_the_policy_is_saved_as_the_settings_it_is(client: TestClient) -> None:
    policy = {
        "machine_rate_per_hour": "1.5",
        "setup_fee": "2",
        "labour_per_item": "3.25",
        "markup_percent": "15",
        "rounding": "to_major",
    }
    saved = client.put(BOOK, json={**policy, "rough_estimate_factor": "1.2"})
    assert saved.status_code == 200, saved.text
    assert saved.json()["policy"] == policy
    assert saved.json()["rough_estimate_factor"] == "1.2"
    settings = client.get("/api/v1/settings").json()
    assert settings["pricing_markup_percent"] == "15"
    assert settings["pricing_rounding"] == "to_major"
    assert settings["rough_estimate_factor"] == "1.2"

    refused = client.put(BOOK, json={**policy, "setup_fee": "-1"})
    assert refused.status_code == 422
    assert refused.json()["errors"][0]["loc"] == ["body", "setup_fee"]
    assert client.put(BOOK, json={**policy, "rough_estimate_factor": "0"}).status_code == 422
    # A body naming an author or anything else is refused.
    assert client.put(BOOK, json={**policy, "created_by": "me"}).status_code == 422
    assert client.get(BOOK).json()["policy"] == policy


def test_a_duplicate_filament_is_409_naming_the_record(client: TestClient) -> None:
    made = add_filament(client)
    again = client.post(f"{BOOK}/filaments", json={**PLA, "material": "pla", "colour": "#ffffff"})
    assert again.status_code == 409
    assert again.json()["type"] == "https://scadbuddy.dev/problems/orders/filament_exists"
    assert again.json()["filament_id"] == made["id"]


def test_a_filament_is_edited_and_archived(client: TestClient) -> None:
    made = add_filament(client)
    edited = client.patch(f"{BOOK}/filaments/{made['id']}", json={"cost_per_kg": "30"})
    assert edited.status_code == 200, edited.text
    assert (edited.json()["cost_per_kg"], edited.json()["colour_name"]) == ("30", "Jade White")
    assert client.patch(f"{BOOK}/filaments/nobody", json={"cost_per_kg": "1"}).status_code == 404
    assert client.patch(f"{BOOK}/filaments/{made['id']}", json={"x": 1}).status_code == 422

    archived = client.post(f"{BOOK}/filaments/{made['id']}/archive")
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None
    assert client.get(BOOK).json()["filaments"] == []
    assert client.post(f"{BOOK}/filaments/nobody/archive").status_code == 404
    # An archived record frees its identity: the same filament can be added again.
    again = client.post(f"{BOOK}/filaments", json=PLA)
    assert again.status_code == 201, again.text
    assert again.json()["id"] != made["id"]
    assert [f["id"] for f in client.get(BOOK).json()["filaments"]] == [again.json()["id"]]


def test_an_archived_filament_is_listed_and_unarchived(client: TestClient) -> None:
    """#4977: ``include_archived`` lists the archived records beside the book's, and
    ``…/unarchive`` puts one back with its id; an identity the book holds again is 409
    ``filament_exists`` naming that record."""
    made = add_filament(client)
    assert client.post(f"{BOOK}/filaments/{made['id']}/archive").status_code == 200
    assert client.get(BOOK).json()["filaments"] == []
    listed = client.get(BOOK, params={"include_archived": "true"}).json()["filaments"]
    assert [(f["id"], f["archived_at"] is not None) for f in listed] == [(made["id"], True)]

    back = client.post(f"{BOOK}/filaments/{made['id']}/unarchive")
    assert back.status_code == 200, back.text
    assert (back.json()["id"], back.json()["archived_at"]) == (made["id"], None)
    assert [f["id"] for f in client.get(BOOK).json()["filaments"]] == [made["id"]]
    assert client.post(f"{BOOK}/filaments/{made['id']}/unarchive").status_code == 200
    assert client.post(f"{BOOK}/filaments/nobody/unarchive").status_code == 404

    client.post(f"{BOOK}/filaments/{made['id']}/archive")
    newer = add_filament(client)
    refused = client.post(f"{BOOK}/filaments/{made['id']}/unarchive")
    assert refused.status_code == 409
    assert refused.json()["type"] == "https://scadbuddy.dev/problems/orders/filament_exists"
    assert refused.json()["filament_id"] == newer["id"]


def test_the_exponent_is_on_the_order_and_the_book(client: TestClient) -> None:
    """Ruling 5: the browser takes the digits it is given, so every order and the book
    carry ``minor_exponent`` from the backend's one table."""
    customer = create_customer(client)
    for code, exponent in (("JPY", 0), ("KWD", 3), ("CLF", 4), ("ZZZ", 2)):
        assert client.put("/api/v1/settings", json={"orders_currency": code}).status_code == 200
        order = create_order(client, customer["id"])
        assert order["minor_exponent"] == exponent, code
        assert client.get(f"/api/v1/orders/{order['id']}").json()["order"]["minor_exponent"] == (
            exponent
        )
        listed = client.get("/api/v1/orders").json()["items"]
        assert next(o for o in listed if o["id"] == order["id"])["minor_exponent"] == exponent
        assert client.get(BOOK).json()["minor_exponent"] == exponent
