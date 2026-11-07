"""`POST /orders/{id}/items/after-acceptance` (#5349): an item added to an accepted, in
production or ready order, refused before acceptance, once shipped or ended, and on hold;
the item counts as pending in the totals, and the audit line names it."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.test_customers import create as create_customer
from tests.api.test_orders import create_order
from tests.api.test_orders_lifecycle import add_item, fulfil, problem, quote_and_accept
from tests.api.test_versions import SLUG, upload

pytestmark = pytest.mark.requires_git


def custom(client: TestClient, order_id: str, price: int = 1000, **body: Any) -> dict[str, Any]:
    return add_item(
        client, order_id, kind="custom", title="Tag", quantity=1, unit_price_minor=price, **body
    )


def after(client: TestClient, order_id: str, **body: Any) -> Any:
    return client.post(f"/api/v1/orders/{order_id}/items/after-acceptance", json=body)


def accepted_order(client: TestClient) -> tuple[str, str]:
    """An accepted order with one priced custom item and a 500 shipping charge."""
    customer = create_customer(client)
    order = create_order(client, customer["id"])
    patched = client.patch(
        f"/api/v1/orders/{order['id']}",
        json={"charges": [{"kind": "shipping", "label": "Post", "amount_minor": 500}]},
    )
    assert patched.status_code == 200, patched.text
    item = custom(client, order["id"])
    assert item["status"] == "approved"
    accepted = client.post(f"/api/v1/orders/{order['id']}/accept")
    assert accepted.json()["status"] == "accepted", accepted.text
    return order["id"], item["id"]


def detail(client: TestClient, order_id: str) -> dict[str, Any]:
    body: dict[str, Any] = client.get(f"/api/v1/orders/{order_id}").json()
    return body


def test_an_item_added_after_acceptance_is_pending_and_audited(client: TestClient) -> None:
    order_id, first = accepted_order(client)
    # The plain route refuses, and names this one.
    body = problem(
        client.post(
            f"/api/v1/orders/{order_id}/items",
            json={"kind": "custom", "title": "Tag", "quantity": 1, "unit_price_minor": 1},
        ),
        "item_locked",
    )
    assert "/items/after-acceptance" in body["detail"]
    # Its price is never accepted as it is saved.
    refused = after(
        client, order_id, kind="custom", title="Rush", quantity=1, unit_price_minor=1, accept=True
    )
    assert refused.status_code == 422, refused.text

    added = after(client, order_id, kind="custom", title="Tag 49", quantity=2, unit_price_minor=300)
    assert added.status_code == 201, added.text
    assert added.json()["status"] == "quoted"
    request = after(client, order_id, kind="request", title="A dragon", quantity=1)
    assert request.status_code == 201, request.text
    assert request.json()["status"] == "draft"

    seen = detail(client, order_id)
    assert seen["order"]["status"] == "accepted"
    totals = seen["totals"]
    # The first item's accepted 1000, the new one's unaccepted 2 x 300, the request unpriced.
    assert totals["subtotal_minor"] == 1600
    assert totals["pending_minor"] == 600
    assert totals["unpriced_items"] == [request.json()["id"]]
    assert totals["charges_minor"] == 500
    first_quotes = next(i for i in seen["items"] if i["id"] == first)["quotes"]
    assert [(q["accepted"], q["total_minor"]) for q in first_quotes] == [(True, 1000)]

    events = client.get(f"/api/v1/orders/{order_id}/events").json()
    added_rows = [e for e in events if e["kind"] == "item.added_after_acceptance"]
    assert [(e["detail"]["item_id"], e["detail"]["order_status"]) for e in added_rows] == [
        (added.json()["id"], "accepted"),
        (request.json()["id"], "accepted"),
    ]

    # The new item's quote is accepted on its own route; then it is made like the rest.
    quote_id = seen["items"][1]["quotes"][0]["id"]
    accepted = client.post(
        f"/api/v1/orders/{order_id}/items/{added.json()['id']}/quotes/{quote_id}/accept"
    )
    assert accepted.json()["status"] == "approved", accepted.text
    assert detail(client, order_id)["totals"]["pending_minor"] == 0
    fulfil(client, order_id, first, 1)
    assert detail(client, order_id)["order"]["status"] == "in_production"


def test_a_ready_order_goes_back_to_in_production(client: TestClient) -> None:
    order_id, first = accepted_order(client)
    fulfil(client, order_id, first, 1)
    assert detail(client, order_id)["order"]["status"] == "ready"
    added = after(client, order_id, kind="request", title="One more", quantity=1)
    assert added.status_code == 201, added.text
    assert detail(client, order_id)["order"]["status"] == "in_production"


def test_a_model_item_is_added_from_a_reference(client: TestClient) -> None:
    upload(client)
    order_id, _ = accepted_order(client)
    commit = client.get(f"/api/v1/models/{SLUG}").json()["version"]
    added = after(client, order_id, slug=SLUG, commit=commit, inputs={}, quantity=1)
    assert added.status_code == 201, added.text
    assert (added.json()["status"], added.json()["reference"]["slug"]) == ("draft", SLUG)
    quote_and_accept(client, order_id, added.json()["id"])
    assert detail(client, order_id)["order"]["status"] == "accepted"


def test_the_refusals(client: TestClient) -> None:
    customer = create_customer(client)
    draft = create_order(client, customer["id"])
    problem(after(client, draft["id"], kind="request", title="X", quantity=1), "order_not_accepted")
    custom(client, draft["id"])
    assert detail(client, draft["id"])["order"]["status"] == "quoted"
    problem(after(client, draft["id"], kind="request", title="X", quantity=1), "order_not_accepted")

    order_id, first = accepted_order(client)
    client.post(f"/api/v1/orders/{order_id}/hold")
    problem(after(client, order_id, kind="request", title="X", quantity=1), "order_on_hold")
    client.post(f"/api/v1/orders/{order_id}/release")
    fulfil(client, order_id, first, 1)
    shipped = client.post(
        f"/api/v1/orders/{order_id}/shipments",
        json={"carrier": "Post", "items": [{"order_item_id": first, "quantity": 1}]},
    )
    assert shipped.status_code == 201, shipped.text
    assert detail(client, order_id)["order"]["status"] == "shipped"
    problem(after(client, order_id, kind="request", title="X", quantity=1), "item_locked")
    assert client.post(f"/api/v1/orders/{order_id}/close").status_code == 200
    problem(after(client, order_id, kind="request", title="X", quantity=1), "order_closed")

    cancelled, _ = accepted_order(client)
    assert client.post(f"/api/v1/orders/{cancelled}/cancel").status_code == 200
    problem(after(client, cancelled, kind="request", title="X", quantity=1), "order_cancelled")
    missing = after(client, "nope", kind="request", title="X", quantity=1)
    assert missing.status_code == 404
