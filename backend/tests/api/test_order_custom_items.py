"""Custom line items through the API (#4954): `POST /orders/{id}/items` with
``kind: "custom"``, their totals, their inline edits, and the refusals."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.test_customers import create as create_customer
from tests.api.test_orders import create_order
from tests.api.test_orders_lifecycle import add_item, fulfil, problem
from tests.api.test_print_run_choices import body as dialog_body
from tests.api.test_send import make_output
from tests.api.test_versions import SLUG, upload
from tests.support.operations import press

CUSTOM = {"kind": "custom", "title": "Rush fee", "quantity": 2, "unit_price_minor": 750}


def an_order(client: TestClient) -> dict[str, Any]:
    return create_order(client, create_customer(client)["id"])


def test_a_custom_item_is_saved_priced_and_counted(client: TestClient) -> None:
    order = an_order(client)
    item = add_item(client, order["id"], **CUSTOM, notes="same day", sku="RUSH")
    assert (item["kind"], item["reference"], item["status"]) == ("custom", None, "approved")
    assert (item["title"], item["notes"], item["sku"]) == ("Rush fee", "same day", "RUSH")
    assert item["requires_printing"] is False
    detail = client.get(f"/api/v1/orders/{order['id']}").json()
    assert detail["order"]["status"] == "quoted"
    [quote] = detail["items"][0]["quotes"]
    assert (quote["source"], quote["accepted"], quote["total_minor"]) == ("manual", True, 1500)
    assert detail["totals"]["subtotal_minor"] == 1500
    assert detail["totals"]["pending_minor"] == 0
    assert detail["totals"]["unpriced_items"] == []

    # Edited inline while the order is open: priced again at once.
    patched = client.patch(
        f"/api/v1/orders/{order['id']}/items/{item['id']}",
        json={"quantity": 3, "unit_price_minor": 500, "title": "Rush", "sku": None},
    )
    assert patched.status_code == 200, patched.text
    assert (patched.json()["status"], patched.json()["sku"]) == ("approved", None)
    assert client.get(f"/api/v1/orders/{order['id']}").json()["totals"]["subtotal_minor"] == 1500

    # It is made by hand and the order runs to the end as any other.
    assert client.post(f"/api/v1/orders/{order['id']}/accept").status_code == 200
    fulfil(client, order["id"], item["id"], 3)
    shipped = client.post(
        f"/api/v1/orders/{order['id']}/shipments",
        json={"items": [{"order_item_id": item["id"], "quantity": 3}]},
    )
    assert shipped.status_code == 201, shipped.text
    assert client.post(f"/api/v1/orders/{order['id']}/close").json()["status"] == "closed"


def test_a_custom_item_needs_its_own_shape(client: TestClient) -> None:
    order = an_order(client)
    url = f"/api/v1/orders/{order['id']}/items"
    for body in (
        {**CUSTOM, "title": " "},
        {k: v for k, v in CUSTOM.items() if k != "title"},
        {k: v for k, v in CUSTOM.items() if k != "unit_price_minor"},
        {**CUSTOM, "unit_price_minor": -1},
        {**CUSTOM, "slug": SLUG},
        {**CUSTOM, "output_id": "out-1"},
        {"quantity": 1, "output_id": "out-1", "unit_price_minor": 5},
        {"quantity": 1, "output_id": "out-1", "requires_printing": False},
        {**CUSTOM, "kind": "other"},
    ):
        assert client.post(url, json=body).status_code == 422, body


def test_a_custom_item_has_nothing_to_slice(client: TestClient) -> None:
    order = an_order(client)
    item = add_item(client, order["id"], **CUSTOM, requires_printing=True)
    assert item["requires_printing"] is True
    dialog = {name: value for name, value in dialog_body().items() if name != "copies"}
    refused = client.post(
        f"/api/v1/orders/{order['id']}/items/{item['id']}/quote", json=dialog, headers=press()
    )
    assert problem(refused, "item_custom")["items"] == [item["id"]]


def test_a_hold_refuses_a_custom_items_price(client: TestClient) -> None:
    order = an_order(client)
    item = add_item(client, order["id"], **CUSTOM)
    client.post(f"/api/v1/orders/{order['id']}/hold")
    problem(client.post(f"/api/v1/orders/{order['id']}/items", json=CUSTOM), "order_on_hold")
    edited = client.patch(
        f"/api/v1/orders/{order['id']}/items/{item['id']}", json={"unit_price_minor": 1}
    )
    problem(edited, "order_on_hold")


@pytest.mark.requires_git
def test_a_source_attached_to_a_custom_item_makes_it_a_model_item(client: TestClient) -> None:
    upload(client)
    order = an_order(client)
    item = add_item(client, order["id"], **CUSTOM)
    output_id = make_output(client, SLUG)
    patched = client.patch(
        f"/api/v1/orders/{order['id']}/items/{item['id']}", json={"output_id": output_id}
    )
    assert patched.status_code == 200, patched.text
    moved = patched.json()
    assert (moved["kind"], moved["status"], moved["output_id"]) == ("model", "draft", output_id)
    assert moved["reference"]["slug"] == SLUG and moved["requires_printing"] is True
    refused = client.patch(
        f"/api/v1/orders/{order['id']}/items/{item['id']}", json={"unit_price_minor": 5}
    )
    assert refused.status_code == 422
    assert refused.json()["errors"][0]["loc"] == ["body", "unit_price_minor"]
