"""`POST /orders/{id}/items/batch` (#4777, order operations spec 2026-11-07 §3.1): several
items in one request, at most 50, all or nothing in one transaction; each goes through
the add as a single item does, after acceptance too."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.lifecycle import LifecycleError
from scadbuddy.orders.store import OrderStore
from tests.api.test_customers import create as create_customer
from tests.api.test_order_add_after_acceptance import accepted_order, detail
from tests.api.test_orders import create_order
from tests.api.test_send import make_output
from tests.api.test_versions import SLUG, upload

pytestmark = pytest.mark.requires_git

CUSTOM = {"kind": "custom", "title": "Gift box", "quantity": 1, "unit_price_minor": 250}


def batch(client: TestClient, order_id: str, items: list[dict[str, Any]], *, after: bool = False):  # type: ignore[no-untyped-def]
    path = "items/after-acceptance/batch" if after else "items/batch"
    return client.post(f"/api/v1/orders/{order_id}/{path}", json={"items": items})


def test_several_outputs_are_added_in_one_request(client: TestClient) -> None:
    upload(client)
    order = create_order(client, create_customer(client)["id"])
    first, second = make_output(client, SLUG, "A"), make_output(client, SLUG, "B")
    added = batch(
        client,
        order["id"],
        [{"output_id": first, "quantity": 2}, {"output_id": second, "quantity": 1}, CUSTOM],
    )
    assert added.status_code == 201, added.text
    items = added.json()
    assert [(i["kind"], i["output_id"], i["quantity"]) for i in items] == [
        ("model", first, 2),
        ("model", second, 1),
        ("custom", None, 1),
    ]
    # Each as the single add makes it: a model item's first revision, a custom item's
    # accepted price.
    assert [i["status"] for i in items] == ["draft", "draft", "approved"]
    shown = detail(client, order["id"])
    assert [i["id"] for i in shown["items"]] == [i["id"] for i in items]
    assert all(i["revisions"][0]["number"] == 1 for i in shown["items"][:2])
    kinds = [e["kind"] for e in client.get(f"/api/v1/orders/{order['id']}/events").json()]
    assert kinds.count("item.added") == 3


def test_a_bad_item_adds_none_and_names_its_index(client: TestClient) -> None:
    upload(client)
    order = create_order(client, create_customer(client)["id"])
    good = make_output(client, SLUG)
    refused = batch(
        client,
        order["id"],
        [{"output_id": good, "quantity": 1}, {"output_id": "f" * 32, "quantity": 1}],
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["item_index"] == 1
    shape = batch(client, order["id"], [CUSTOM, {"kind": "custom", "quantity": 1}])
    assert shape.status_code == 422, shape.text
    assert detail(client, order["id"])["items"] == []


def test_a_refusal_part_way_rolls_the_whole_batch_back(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    order = create_order(client, create_customer(client)["id"])
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    store = state.components.get(ORDERS).store
    insert = OrderStore.insert_custom_item
    calls: list[int] = []

    def failing_second(self: OrderStore, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 2:
            raise LifecycleError("item_locked", "refused part way")
        return insert(self, *args, **kwargs)

    monkeypatch.setattr(type(store), "insert_custom_item", failing_second)
    refused = batch(client, order["id"], [CUSTOM, CUSTOM, CUSTOM])
    assert refused.status_code == 409, refused.text
    assert len(calls) == 2
    assert detail(client, order["id"])["items"] == []


def test_the_batch_is_bounded(client: TestClient) -> None:
    order = create_order(client, create_customer(client)["id"])
    assert batch(client, order["id"], []).status_code == 422
    assert batch(client, order["id"], [CUSTOM] * 51).status_code == 422
    assert batch(client, order["id"], [CUSTOM] * 50).status_code == 201


def test_after_acceptance_the_batch_follows_the_single_rules(client: TestClient) -> None:
    upload(client)
    order_id, _ = accepted_order(client)
    output = make_output(client, SLUG)
    # The plain batch is refused on an accepted order, as the single add is.
    locked = batch(client, order_id, [{"output_id": output, "quantity": 1}])
    assert locked.status_code == 409, locked.text
    added = batch(client, order_id, [{"output_id": output, "quantity": 1}, CUSTOM], after=True)
    assert added.status_code == 201, added.text
    # Unpriced: a custom item's price waits for its own accept.
    assert [i["status"] for i in added.json()] == ["draft", "quoted"]
    kinds = [e["kind"] for e in client.get(f"/api/v1/orders/{order_id}/events").json()]
    assert kinds.count("item.added_after_acceptance") == 2
    accept = batch(client, order_id, [{**CUSTOM, "accept": True}], after=True)
    assert accept.status_code == 422, accept.text
