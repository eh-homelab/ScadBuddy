"""Undoing an archive, a close and a cancel: a customer back in the pickers, a closed
order reopened, a cancelled line item reopened, each on the record."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.test_customers import create as create_customer
from tests.api.test_orders import create_order
from tests.api.test_orders_lifecycle import add_item, fulfil, problem, quote_and_accept
from tests.api.test_versions import SLUG, upload

pytestmark = pytest.mark.requires_git


def item_of(client: TestClient, order_id: str, **body: Any) -> dict[str, Any]:
    commit = client.get(f"/api/v1/models/{SLUG}").json()["version"]
    return add_item(
        client,
        order_id,
        slug=SLUG,
        commit=commit,
        inputs={"params": {"width": 12}},
        quantity=1,
        **body,
    )


def detail(client: TestClient, order_id: str) -> dict[str, Any]:
    body: dict[str, Any] = client.get(f"/api/v1/orders/{order_id}").json()
    return body


def test_an_archived_customer_is_unarchived(client: TestClient) -> None:
    customer = create_customer(client)
    client.post(f"/api/v1/customers/{customer['id']}/archive")
    assert client.post("/api/v1/orders", json={"customer_id": customer["id"]}).status_code == 409
    back = client.post(f"/api/v1/customers/{customer['id']}/unarchive")
    assert back.status_code == 200, back.text
    assert back.json()["archived_at"] is None
    assert [c["id"] for c in client.get("/api/v1/customers").json()] == [customer["id"]]
    assert create_order(client, customer["id"])["customer_id"] == customer["id"]
    # Never archived: as it is.
    assert client.post(f"/api/v1/customers/{customer['id']}/unarchive").status_code == 200
    assert client.post("/api/v1/customers/nobody/unarchive").status_code == 404


def test_a_closed_order_reopens_to_what_its_rows_say(client: TestClient) -> None:
    upload(client)
    order = create_order(client, create_customer(client)["id"])
    item = item_of(client, order["id"])
    quote_and_accept(client, order["id"], item["id"])
    assert client.post(f"/api/v1/orders/{order['id']}/accept").status_code == 200
    fulfil(client, order["id"], item["id"], 1)
    shipped = client.post(
        f"/api/v1/orders/{order['id']}/shipments",
        json={"items": [{"order_item_id": item["id"], "quantity": 1}]},
    )
    assert shipped.status_code == 201, shipped.text
    # Not closed yet: nothing to reopen.
    problem(client.post(f"/api/v1/orders/{order['id']}/reopen"), "order_not_ended")
    assert client.post(f"/api/v1/orders/{order['id']}/close").json()["status"] == "closed"

    reopened = client.post(f"/api/v1/orders/{order['id']}/reopen")
    assert reopened.status_code == 200, reopened.text
    assert (reopened.json()["status"], reopened.json()["closed_at"]) == ("shipped", None)
    last = client.get(f"/api/v1/orders/{order['id']}/events").json()[-1]
    assert (last["kind"], last["detail"]["to"], last["detail"]["reopened"]) == (
        "order.status",
        "shipped",
        True,
    )
    # It closes again as before.
    assert client.post(f"/api/v1/orders/{order['id']}/close").json()["status"] == "closed"


def test_only_an_ended_order_reopens(client: TestClient) -> None:
    order = create_order(client, create_customer(client)["id"])
    problem(client.post(f"/api/v1/orders/{order['id']}/reopen"), "order_not_ended")
    assert client.post("/api/v1/orders/nothing/reopen").status_code == 404


def test_a_cancelled_draft_reopens_with_the_items_it_cancelled(client: TestClient) -> None:
    upload(client)
    order = create_order(client, create_customer(client)["id"])
    draft = item_of(client, order["id"])
    priced = item_of(client, order["id"], title="Priced")
    earlier = item_of(client, order["id"], title="Cancelled before")
    quote_and_accept(client, order["id"], priced["id"])
    # Cancelled on its own, before the order: the order's reopen leaves it cancelled.
    client.post(f"/api/v1/orders/{order['id']}/items/{earlier['id']}/cancel")
    cancelled = client.post(f"/api/v1/orders/{order['id']}/cancel")
    assert cancelled.json()["status"] == "cancelled"

    reopened = client.post(f"/api/v1/orders/{order['id']}/reopen")
    assert reopened.status_code == 200, reopened.text
    assert (reopened.json()["status"], reopened.json()["cancelled_at"]) == ("draft", None)
    statuses = {i["id"]: i["status"] for i in detail(client, order["id"])["items"]}
    assert statuses == {
        draft["id"]: "draft",
        priced["id"]: "approved",
        earlier["id"]: "cancelled",
    }
    event = [
        e["detail"]
        for e in client.get(f"/api/v1/orders/{order['id']}/events").json()
        if e["detail"].get("reopened")
    ][-1]
    assert (event["from_status"], event["to"]) == ("cancelled", "draft")
    assert sorted(event["items"]) == sorted([draft["id"], priced["id"]])


def test_a_cancelled_accepted_order_reopens_accepted(client: TestClient) -> None:
    upload(client)
    order = create_order(client, create_customer(client)["id"])
    item = item_of(client, order["id"])
    quote_and_accept(client, order["id"], item["id"])
    assert client.post(f"/api/v1/orders/{order['id']}/accept").status_code == 200
    client.post(f"/api/v1/orders/{order['id']}/cancel")
    reopened = client.post(f"/api/v1/orders/{order['id']}/reopen")
    assert reopened.json()["status"] == "accepted"
    assert detail(client, order["id"])["items"][0]["status"] == "approved"
    assert detail(client, order["id"])["totals"]["subtotal_minor"] == 1200


def test_a_cancelled_item_reopens_to_what_its_quotes_say(client: TestClient) -> None:
    upload(client)
    order = create_order(client, create_customer(client)["id"])
    draft = item_of(client, order["id"])
    priced = item_of(client, order["id"], title="Priced")
    quoted = item_of(client, order["id"], title="Quoted")
    quote_and_accept(client, order["id"], priced["id"])
    typed = client.post(
        f"/api/v1/orders/{order['id']}/items/{quoted['id']}/quotes",
        json={"unit_price_minor": 700},
    )
    assert typed.status_code == 201, typed.text
    for each in (draft, priced, quoted):
        cancelled = client.post(f"/api/v1/orders/{order['id']}/items/{each['id']}/cancel")
        assert cancelled.json()["status"] == "cancelled"

    reopen = f"/api/v1/orders/{order['id']}/items/{{}}/reopen"
    assert client.post(reopen.format(draft["id"])).json()["status"] == "draft"
    assert client.post(reopen.format(priced["id"])).json()["status"] == "approved"
    assert client.post(reopen.format(quoted["id"])).json()["status"] == "quoted"
    statuses = {i["id"]: i["status"] for i in detail(client, order["id"])["items"]}
    assert statuses == {draft["id"]: "draft", priced["id"]: "approved", quoted["id"]: "quoted"}
    assert detail(client, order["id"])["order"]["status"] == "draft"
    reopened = [
        e["detail"]
        for e in client.get(f"/api/v1/orders/{order['id']}/events").json()
        if e["kind"] == "item.reopened"
    ]
    assert [(e["item_id"], e["to"]) for e in reopened] == [
        (draft["id"], "draft"),
        (priced["id"], "approved"),
        (quoted["id"], "quoted"),
    ]
    # Only a cancelled item reopens.
    problem(client.post(reopen.format(draft["id"])), "item_not_cancelled")


def test_reopening_an_item_moves_the_order_back_to_draft(client: TestClient) -> None:
    upload(client)
    order = create_order(client, create_customer(client)["id"])
    kept = item_of(client, order["id"])
    dropped = item_of(client, order["id"], title="Dropped")
    quote_and_accept(client, order["id"], kept["id"])
    client.post(f"/api/v1/orders/{order['id']}/items/{dropped['id']}/cancel")
    # Every open item priced: quoted.
    assert detail(client, order["id"])["order"]["status"] == "quoted"
    client.post(f"/api/v1/orders/{order['id']}/items/{dropped['id']}/reopen")
    assert detail(client, order["id"])["order"]["status"] == "draft"


def test_an_accepted_order_takes_back_a_priced_item_only(client: TestClient) -> None:
    upload(client)
    order = create_order(client, create_customer(client)["id"])
    kept = item_of(client, order["id"])
    unpriced = item_of(client, order["id"], title="Unpriced")
    priced = item_of(client, order["id"], title="Priced")
    quote_and_accept(client, order["id"], kept["id"])
    quote_and_accept(client, order["id"], priced["id"])
    client.post(f"/api/v1/orders/{order['id']}/items/{unpriced['id']}/cancel")
    client.post(f"/api/v1/orders/{order['id']}/items/{priced['id']}/cancel")
    assert client.post(f"/api/v1/orders/{order['id']}/accept").status_code == 200

    body = problem(
        client.post(f"/api/v1/orders/{order['id']}/items/{unpriced['id']}/reopen"),
        "item_not_priced",
    )
    assert body["items"] == [unpriced["id"]]
    back = client.post(f"/api/v1/orders/{order['id']}/items/{priced['id']}/reopen")
    assert back.json()["status"] == "approved"
    assert detail(client, order["id"])["order"]["status"] == "accepted"
    assert detail(client, order["id"])["totals"]["subtotal_minor"] == 2400


def test_an_item_of_an_ended_order_does_not_reopen(client: TestClient) -> None:
    upload(client)
    order = create_order(client, create_customer(client)["id"])
    item = item_of(client, order["id"])
    client.post(f"/api/v1/orders/{order['id']}/cancel")
    problem(
        client.post(f"/api/v1/orders/{order['id']}/items/{item['id']}/reopen"), "order_cancelled"
    )


def test_a_draft_adds_up_before_anything_is_accepted(client: TestClient) -> None:
    upload(client)
    order = create_order(client, create_customer(client)["id"])
    accepted = item_of(client, order["id"])
    pending = item_of(client, order["id"], title="Pending")
    unpriced = item_of(client, order["id"], title="Unpriced")
    quote_and_accept(client, order["id"], accepted["id"], price=1000)
    typed = client.post(
        f"/api/v1/orders/{order['id']}/items/{pending['id']}/quotes",
        json={"unit_price_minor": 250},
    )
    assert typed.status_code == 201, typed.text
    # A quantity change after the quote: the pending price follows the item.
    client.patch(f"/api/v1/orders/{order['id']}/items/{pending['id']}", json={"quantity": 3})
    totals = detail(client, order["id"])["totals"]
    assert totals["subtotal_minor"] == 1000 + 750
    assert totals["pending_minor"] == 750
    assert totals["unpriced_items"] == [unpriced["id"]]
    assert totals["total_minor"] == totals["balance_minor"] == 1750
    # A cancelled item is in no sum.
    client.post(f"/api/v1/orders/{order['id']}/items/{pending['id']}/cancel")
    totals = detail(client, order["id"])["totals"]
    assert (totals["subtotal_minor"], totals["pending_minor"]) == (1000, 0)


def test_an_accepted_order_with_nothing_open_reopens_as_a_draft(client: TestClient) -> None:
    upload(client)
    order = create_order(client, create_customer(client)["id"])
    item = item_of(client, order["id"])
    quote_and_accept(client, order["id"], item["id"])
    assert client.post(f"/api/v1/orders/{order['id']}/accept").status_code == 200
    # Its one item cancelled on its own: an accepted order with nothing open...
    client.post(f"/api/v1/orders/{order['id']}/items/{item['id']}/cancel")
    client.post(f"/api/v1/orders/{order['id']}/cancel")
    # ...reopens open for edits, as `accept_order` would refuse it accepted.
    reopened = client.post(f"/api/v1/orders/{order['id']}/reopen")
    assert reopened.json()["status"] == "draft"
    # Its item, cancelled before the order, reopens on its own.
    back = client.post(f"/api/v1/orders/{order['id']}/items/{item['id']}/reopen")
    assert back.json()["status"] == "approved"
    assert detail(client, order["id"])["order"]["status"] == "quoted"


def test_an_item_whose_latest_attempt_failed_reopens_failed(client: TestClient) -> None:
    upload(client)
    order = create_order(client, create_customer(client)["id"])
    item = item_of(client, order["id"])
    quote_and_accept(client, order["id"], item["id"])
    assert client.post(f"/api/v1/orders/{order['id']}/accept").status_code == 200
    failed = client.post(
        f"/api/v1/orders/{order['id']}/items/{item['id']}/fulfilments",
        json={"quantity": 0, "outcome": "failed", "failure_reason": "spaghetti"},
    )
    assert failed.status_code == 201, failed.text
    assert detail(client, order["id"])["order"]["status"] == "in_production"
    client.post(f"/api/v1/orders/{order['id']}/items/{item['id']}/cancel")
    back = client.post(f"/api/v1/orders/{order['id']}/items/{item['id']}/reopen")
    assert back.json()["status"] == "failed"
    assert detail(client, order["id"])["order"]["status"] == "in_production"
