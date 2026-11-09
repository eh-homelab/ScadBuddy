"""The back office's readers meet a storefront draft (#5282, Review Focus 2): a visitor's
cart has no customer and no number, and every reader answers it with both null."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.lifecycle import NewModelItem
from scadbuddy.orders.store import OrderStore
from scadbuddy.orders.storefront.visitors import VisitorStore, visitor_actor
from scadbuddy.store.refs import BlobRefs
from tests.api.test_customers import create as create_customer
from tests.api.test_orders import create_order
from tests.api.test_orders_lifecycle import problem
from tests.api.test_settings_runtime import _state
from tests.conftest import PgPool
from tests.orders.storefront.test_customer_actor import storefront_order
from tests.orders.test_lifecycle import Shop, resolved


@pytest.fixture
def draft_with_item(client: TestClient, pg_pool: PgPool) -> str:
    orders = _state(client).components.get(ORDERS)
    visitor = VisitorStore(pg_pool).ensure("v" * 43)
    by = visitor_actor(visitor)
    draft = orders.lifecycle.ensure_draft(visitor.id, by, currency="USD")
    orders.lifecycle.add_items(draft.id, by, [NewModelItem(resolved=resolved(), quantity=1)])
    return draft.id


def test_reading_a_draft_never_fails(client: TestClient, draft_with_item: str) -> None:
    placed = create_order(client, create_customer(client)["id"])
    listed = client.get("/api/v1/orders", params={"source": "storefront", "unsubmitted": "true"})
    assert listed.status_code == 200, listed.text
    rows = listed.json()["items"]
    assert [(r["id"], r["number"], r["customer_id"]) for r in rows] == [
        (draft_with_item, None, None)
    ]
    assert (rows[0]["source"], rows[0]["submitted_at"]) == ("storefront", None)
    for path in (
        f"/api/v1/orders/{draft_with_item}",
        f"/api/v1/orders/{draft_with_item}/costs",
        "/api/v1/orders/reports/profit",
    ):
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
    order = client.get(f"/api/v1/orders/{draft_with_item}").json()["order"]
    assert (order["number"], order["customer_id"], order["source"]) == (None, None, "storefront")
    # The list leaves drafts out unless asked for them.
    assert [r["id"] for r in client.get("/api/v1/orders").json()["items"]] == [placed["id"]]
    back_office = client.get("/api/v1/orders", params={"source": "back_office"}).json()
    assert [r["id"] for r in back_office["items"]] == [placed["id"]]
    assert placed["source"] == "back_office"


def test_the_list_refuses_an_unknown_source(client: TestClient) -> None:
    assert client.get("/api/v1/orders", params={"source": "shop"}).status_code == 422


def test_a_past_prints_quote_is_refused_on_a_cart(client: TestClient, draft_with_item: str) -> None:
    item = client.get(f"/api/v1/orders/{draft_with_item}").json()["items"][0]
    refused = client.post(
        f"/api/v1/orders/{draft_with_item}/items/{item['id']}/quotes/archive",
        json={"archive_id": 1},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["type"] == "https://scadbuddy.dev/problems/orders/order_not_submitted"
    assert refused.json()["detail"] == f"cart {draft_with_item} is not yet submitted"


def test_a_slice_with_new_inputs_is_refused_on_a_cart(
    client: TestClient, draft_with_item: str
) -> None:
    """#4673 PR 3: a visitor's cart refuses a slice quote before its new inputs are read
    or any revision made, so nothing of a revision reaches a visitor-facing path."""
    item = client.get(f"/api/v1/orders/{draft_with_item}").json()["items"][0]
    refused = client.post(
        f"/api/v1/orders/{draft_with_item}/items/{item['id']}/quote",
        json={
            "filament_plan": {"slots": []},
            "choices": {"nozzles": [{"size": "0.4"}]},
            "inputs": {"params": {"width": 99}},
        },
        headers={"Idempotency-Key": "cart-slice"},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["type"] == "https://scadbuddy.dev/problems/orders/order_not_submitted"
    assert "revision" not in refused.text
    after = client.get(f"/api/v1/orders/{draft_with_item}").json()["items"][0]
    assert after["current_revision_id"] == item["current_revision_id"]


def test_the_shop_accepts_for_the_customer_only_with_a_reason(
    client: TestClient, pg_pool: PgPool
) -> None:
    """#5283, storefront spec §6.1: on a storefront order the shop's own accept is
    refused; "Accept for the customer" takes a reason and keeps it in the audit line."""
    order = storefront_order(Shop(OrderStore(pg_pool), BlobRefs(pg_pool)), pg_pool, 1)
    item = client.get(f"/api/v1/orders/{order.id}").json()["items"][0]
    rev = item["revisions"][-1]
    url = f"/api/v1/orders/{order.id}/items/{item['id']}"
    problem(
        client.post(f"{url}/revisions/{rev['id']}/answer", json={"answer": "accepted"}),
        "customer_accepts",
    )
    [quote] = client.get(f"/api/v1/orders/{order.id}").json()["items"][0]["quotes"]
    problem(client.post(f"{url}/quotes/{quote['id']}/accept"), "customer_accepts")
    blank = {"what": "revision", "target_id": rev["id"], "reason": ""}
    assert client.post(f"{url}/accept-for-customer", json=blank).status_code == 422
    spaces = client.post(f"{url}/accept-for-customer", json={**blank, "reason": "  "})
    assert spaces.status_code == 422, spaces.text
    accepted = client.post(
        f"{url}/accept-for-customer",
        json={"what": "revision", "target_id": rev["id"], "reason": "By phone, 9 Nov"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "approved"
    detail = client.get(f"/api/v1/orders/{order.id}").json()
    assert detail["order"]["status"] == "accepted"
    events = client.get(f"/api/v1/orders/{order.id}/events").json()
    [event] = [e for e in events if e["kind"] == "item.accepted_for_customer"]
    assert event["detail"]["reason"] == "By phone, 9 Nov"
    assert event["by"]["kind"] == "browser"


def test_a_storefront_custom_price_waits_for_the_customer(
    client: TestClient, pg_pool: PgPool
) -> None:
    """#5283, storefront spec §4.5: an unset `accept` leaves a storefront order's custom
    price `quoted`; an explicit true is the customer's to give."""
    order = storefront_order(Shop(OrderStore(pg_pool), BlobRefs(pg_pool)), pg_pool, 1)
    items = f"/api/v1/orders/{order.id}/items"
    custom = {"kind": "custom", "title": "Gift wrap", "quantity": 1, "unit_price_minor": 1500}
    added = client.post(items, json=custom)
    assert added.status_code == 201, added.text
    assert added.json()["status"] == "quoted"
    problem(client.post(items, json={**custom, "accept": True}), "customer_accepts")
    url = f"{items}/{added.json()['id']}"
    patched = client.patch(url, json={"unit_price_minor": 2000})
    assert patched.status_code == 200, patched.text
    assert patched.json()["status"] == "quoted"
    problem(client.patch(url, json={"unit_price_minor": 2500, "accept": True}), "customer_accepts")
    copy = client.post(f"{url}/duplicate", json={})
    assert copy.status_code == 201, copy.text
    assert copy.json()["status"] == "quoted"


def test_a_back_office_custom_price_is_accepted_when_unset(client: TestClient) -> None:
    order = create_order(client, create_customer(client)["id"])
    items = f"/api/v1/orders/{order['id']}/items"
    custom = {"kind": "custom", "title": "Gift wrap", "quantity": 1, "unit_price_minor": 1500}
    added = client.post(items, json=custom).json()
    assert added["status"] == "approved"
    patched = client.patch(f"{items}/{added['id']}", json={"unit_price_minor": 2000})
    assert patched.json()["status"] == "approved"
    copy = client.post(f"{items}/{added['id']}/duplicate", json={})
    assert copy.json()["status"] == "approved"


def test_the_reason_is_trimmed_before_its_length_is_checked(
    client: TestClient, pg_pool: PgPool
) -> None:
    order = storefront_order(Shop(OrderStore(pg_pool), BlobRefs(pg_pool)), pg_pool, 1)
    item = client.get(f"/api/v1/orders/{order.id}").json()["items"][0]
    rev = item["revisions"][-1]
    padded = {"what": "revision", "target_id": rev["id"], "reason": "  " + "r" * 500 + "  "}
    url = f"/api/v1/orders/{order.id}/items/{item['id']}/accept-for-customer"
    assert client.post(url, json=padded).status_code == 200
