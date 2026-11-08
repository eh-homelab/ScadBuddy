"""`PUT /orders/{id}/charges/after-acceptance` (#5351): the charges of an accepted, in
production or ready order, refused before acceptance, once shipped or ended, and on hold;
the audit row holds the charges before and after. The agreed-total case: a zero-price item
or a discount line keeps the total, and job cost shows the item's real cost against zero
revenue."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.test_customers import create as create_customer
from tests.api.test_order_add_after_acceptance import accepted_order, after, custom, detail
from tests.api.test_orders import create_order
from tests.api.test_orders_lifecycle import fulfil, problem

pytestmark = pytest.mark.requires_git

POST = {"kind": "shipping", "label": "Post", "amount_minor": 500}


def put(client: TestClient, order_id: str, *charges: dict[str, Any]) -> Any:
    return client.put(
        f"/api/v1/orders/{order_id}/charges/after-acceptance", json={"charges": list(charges)}
    )


def accept_quote(client: TestClient, order_id: str, item_id: str) -> None:
    item = next(i for i in detail(client, order_id)["items"] if i["id"] == item_id)
    quote_id = item["quotes"][0]["id"]
    accepted = client.post(f"/api/v1/orders/{order_id}/items/{item_id}/quotes/{quote_id}/accept")
    assert accepted.json()["status"] == "approved", accepted.text


def test_a_discount_absorbs_an_added_item_and_the_total_stays(client: TestClient) -> None:
    order_id, _ = accepted_order(client)
    # The agreed total: the item's 1000 and the 500 post.
    assert detail(client, order_id)["totals"]["total_minor"] == 1500
    # The plain edit refuses charges past `quoted` and names the route.
    refused = problem(
        client.patch(f"/api/v1/orders/{order_id}", json={"charges": [POST]}),
        "order_not_editable",
    )
    assert f"PUT /orders/{order_id}/charges/after-acceptance" in refused["detail"]

    added = after(client, order_id, kind="custom", title="Tag 49", quantity=1, unit_price_minor=300)
    assert added.status_code == 201, added.text
    accept_quote(client, order_id, added.json()["id"])
    assert detail(client, order_id)["totals"]["total_minor"] == 1800

    cap = {"kind": "discount", "label": "Agreed cap", "amount_minor": -300}
    changed = put(client, order_id, POST, cap)
    assert changed.status_code == 200, changed.text
    assert changed.json()["status"] == "accepted"

    seen = detail(client, order_id)
    assert [(c["kind"], c["amount_minor"]) for c in seen["charges"]] == [
        ("shipping", 500),
        ("discount", -300),
    ]
    totals = seen["totals"]
    assert (totals["subtotal_minor"], totals["charges_minor"], totals["total_minor"]) == (
        1300,
        200,
        1500,
    )
    events = client.get(f"/api/v1/orders/{order_id}/events").json()
    rows = [e for e in events if e["kind"] == "charges.replaced_after_acceptance"]
    assert [e["detail"] for e in rows] == [
        {"order_status": "accepted", "before": [POST], "after": [POST, cap]}
    ]
    assert rows[0]["by"]["kind"] == "browser"


@pytest.mark.parametrize("start", ["in_production", "ready"])
def test_in_production_and_ready_take_the_change(client: TestClient, start: str) -> None:
    order_id, first = accepted_order(client)
    if start == "ready":
        fulfil(client, order_id, first, 1)
    else:
        failed = client.post(
            f"/api/v1/orders/{order_id}/items/{first}/fulfilments",
            json={"quantity": 1, "outcome": "failed"},
        )
        assert failed.status_code == 201, failed.text
    assert detail(client, order_id)["order"]["status"] == start
    changed = put(client, order_id)
    assert changed.status_code == 200, changed.text
    assert changed.json()["status"] == start
    assert detail(client, order_id)["charges"] == []


def test_the_refusals(client: TestClient) -> None:
    customer = create_customer(client)
    draft = create_order(client, customer["id"])
    problem(put(client, draft["id"], POST), "order_not_accepted")
    custom(client, draft["id"])
    assert detail(client, draft["id"])["order"]["status"] == "quoted"
    problem(put(client, draft["id"], POST), "order_not_accepted")
    bad = client.put(
        f"/api/v1/orders/{draft['id']}/charges/after-acceptance", json={"charges": [], "notes": ""}
    )
    assert bad.status_code == 422, bad.text

    order_id, first = accepted_order(client)
    client.post(f"/api/v1/orders/{order_id}/hold")
    problem(put(client, order_id), "order_on_hold")
    client.post(f"/api/v1/orders/{order_id}/release")
    fulfil(client, order_id, first, 1)
    shipped = client.post(
        f"/api/v1/orders/{order_id}/shipments",
        json={"carrier": "Post", "items": [{"order_item_id": first, "quantity": 1}]},
    )
    assert shipped.status_code == 201, shipped.text
    problem(put(client, order_id), "order_not_editable")
    assert client.post(f"/api/v1/orders/{order_id}/close").status_code == 200
    problem(put(client, order_id), "order_closed")
    assert [c["amount_minor"] for c in detail(client, order_id)["charges"]] == [500]

    cancelled, _ = accepted_order(client)
    assert client.post(f"/api/v1/orders/{cancelled}/cancel").status_code == 200
    problem(put(client, cancelled), "order_cancelled")
    assert put(client, "nope").status_code == 404


def test_a_no_charge_item_keeps_the_total_and_shows_its_cost_as_a_loss(
    client: TestClient,
) -> None:
    """A zero-price custom item on #5540's route needs no new path. Job cost counts its
    real cost against zero revenue, and a discount charge lowers the order's revenue."""
    order_id, _ = accepted_order(client)
    added = after(
        client, order_id, kind="custom", title="Extra tag", quantity=2, unit_price_minor=0
    )
    assert added.status_code == 201, added.text
    item_id = added.json()["id"]
    assert added.json()["status"] == "quoted"
    accept_quote(client, order_id, item_id)
    seen = detail(client, order_id)
    assert seen["order"]["status"] == "accepted"
    assert (seen["totals"]["total_minor"], seen["totals"]["pending_minor"]) == (1500, 0)

    made = client.post(
        f"/api/v1/orders/{order_id}/cost-entries",
        json={"order_item_id": item_id, "kind": "material", "amount_minor": 120},
    )
    assert made.status_code == 201, made.text
    costs = client.get(f"/api/v1/orders/{order_id}/costs").json()
    line = next(i for i in costs["items"] if i["item_id"] == item_id)
    assert (line["revenue_minor"], line["cost_minor"]) == (0, 120)
    assert (line["margin_minor"], line["margin_state"], line["margin_percent"]) == (
        -120,
        "loss",
        None,
    )
    before = costs["revenue_minor"]
    # The items' 1000 and the 500 post (not tax).
    assert (costs["charges_minor"], before) == (500, 1500)

    cap = {"kind": "discount", "label": "Goodwill", "amount_minor": -200}
    tax = {"kind": "tax", "label": "VAT", "amount_minor": 90}
    assert put(client, order_id, POST, cap, tax).status_code == 200
    costs = client.get(f"/api/v1/orders/{order_id}/costs").json()
    # A discount counts, signed; tax never counts as revenue.
    assert (costs["charges_minor"], costs["revenue_minor"]) == (300, 1300)


def test_a_charge_other_than_shipping_names_no_shipping_option(client: TestClient) -> None:
    order_id = create_order(client, create_customer(client)["id"])["id"]
    fee = {"kind": "fee", "label": "Rush", "amount_minor": 100, "shipping_option_id": "post"}
    refused = put(client, order_id, fee)
    assert refused.status_code == 422, refused.text
    assert "shipping_option_id" in refused.text
