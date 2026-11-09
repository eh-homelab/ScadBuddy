"""The back office's readers meet a storefront draft (#5282, Review Focus 2): a visitor's
cart has no customer and no number, and every reader answers it with both null."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.lifecycle import NewModelItem
from scadbuddy.orders.storefront.visitors import VisitorStore, visitor_actor
from tests.api.test_customers import create as create_customer
from tests.api.test_orders import create_order
from tests.api.test_settings_runtime import _state
from tests.conftest import PgPool
from tests.orders.test_lifecycle import resolved


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
