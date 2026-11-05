"""Design revisions through the API (#5280): request items, the revision routes, the order
read's `revisions`, and the approval mode."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.api.test_customers import create as create_customer
from tests.api.test_orders import create_order
from tests.api.test_orders_lifecycle import add_item, problem
from tests.api.test_versions import SLUG, upload

REQUEST = {"kind": "request", "title": "A dragon", "quantity": 1, "notes": "green"}


def a_request(client: TestClient) -> tuple[dict[str, Any], dict[str, Any]]:
    order = create_order(client, create_customer(client)["id"])
    return order, add_item(client, order["id"], **REQUEST)


def a_reference(client: TestClient) -> dict[str, Any]:
    upload(client)
    commit = client.get(f"/api/v1/models/{SLUG}").json()["version"]
    return {"slug": SLUG, "commit": commit, "inputs": {"params": {"width": 12}}}


def test_a_request_item_takes_its_design_as_revision_one(client: TestClient) -> None:
    order, item = a_request(client)
    assert item["kind"] == "request" and item["reference"] is None
    assert item["approval"] == "design_first"
    rev = client.post(
        f"/api/v1/orders/{order['id']}/items/{item['id']}/revisions",
        json={"reference": a_reference(client), "note": "first go"},
    )
    assert rev.status_code == 201, rev.text
    assert rev.json()["number"] == 1 and rev.json()["note"] == "first go"


def test_the_order_read_lists_each_items_revisions(client: TestClient) -> None:
    order, item = a_request(client)
    base = f"/api/v1/orders/{order['id']}/items/{item['id']}/revisions"
    assert client.post(base, json={"reference": a_reference(client)}).status_code == 201
    assert client.post(base, json={"note": "again"}).status_code == 201
    detail = client.get(f"/api/v1/orders/{order['id']}").json()
    assert [r["number"] for r in detail["items"][0]["revisions"]] == [1, 2]


def test_send_and_answer_are_refused_in_order(client: TestClient) -> None:
    order, item = a_request(client)
    base = f"/api/v1/orders/{order['id']}/items/{item['id']}/revisions"
    rev = client.post(base, json={"reference": a_reference(client)}).json()
    answer = client.post(f"{base}/{rev['id']}/answer", json={"answer": "accepted"})
    problem(answer, "revision_not_sent")
    problem(client.post(f"{base}/{rev['id']}/send"), "revision_empty")


def test_a_request_item_with_no_design_is_not_quoted(client: TestClient) -> None:
    order, item = a_request(client)
    answer = client.post(
        f"/api/v1/orders/{order['id']}/items/{item['id']}/quotes", json={"unit_price_minor": 500}
    )
    problem(answer, "item_has_no_design")


def test_approval_is_patched(client: TestClient) -> None:
    order, item = a_request(client)
    url = f"/api/v1/orders/{order['id']}/items/{item['id']}"
    patched = client.patch(url, json={"approval": "together"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["approval"] == "together"
    assert client.patch(url, json={"approval": "design_first"}).json()["approval"] == "design_first"


def test_a_request_names_no_source(client: TestClient) -> None:
    order, _ = a_request(client)
    url = f"/api/v1/orders/{order['id']}/items"
    named = client.post(url, json={"kind": "request", "title": "x", "quantity": 1, "slug": "demo"})
    assert named.status_code == 422
    untitled = client.post(url, json={"kind": "request", "title": "  ", "quantity": 1})
    assert untitled.status_code == 422
