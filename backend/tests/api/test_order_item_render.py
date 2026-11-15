"""An item's render end to end (#6324): ``POST /orders/{id}/items/{item}/render`` answers
202 with the ``order_item_render`` operation, whose run on the in-process ``library``
worker renders the item's design on the normal pipeline (the fake openscad), saves the
output and sets it on the item, with no new revision and no quote dropped. The print
dialog then opens on that output, and the slice quote slices it as it slices any."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from scadbuddy.orders import operations as order_operations
from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.lifecycle import Lifecycle
from scadbuddy.orders.models import Actor
from scadbuddy.orders.rendering import output_for_reference
from tests.api.conftest import FAIL_WIDTH, follow_operation
from tests.api.test_order_quote import (
    BambuddySlices,
    Shop,
    output_ids,
    sliced_plate,
    worker_stopped,
)
from tests.api.test_order_quote import (
    bambuddy as quote_bambuddy,
)
from tests.api.test_orders_lifecycle import add_item
from tests.api.test_settings_runtime import _state
from tests.api.test_versions import SLUG
from tests.support.operations import press

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_git]

#: The slice quote's Bambuddy, configured with a two-colour fake openscad.
bambuddy = quote_bambuddy


def render(client: TestClient, path: str) -> httpx.Response:
    response: httpx.Response = client.post(f"{path}/render", headers=press())
    return response


def rendered(client: TestClient, path: str) -> dict[str, Any]:
    started = render(client, path)
    assert started.status_code == 202, started.text
    return follow_operation(client, started.json(), timeout=120, poll=0.2)


def revisions(client: TestClient, item_id: str) -> list[str]:
    store = _state(client).components.get(ORDERS).store
    return [revision.id for revision in store.revisions(item_id)]


def test_an_items_render_attaches_its_output_and_keeps_its_revision_and_quotes(
    bambuddy: TestClient,
) -> None:
    shop = Shop(bambuddy, by_hand=True)
    assert shop.item["output_id"] is None
    typed = bambuddy.post(f"{shop.path}/quotes", json={"unit_price_minor": 1500})
    assert typed.status_code == 201, typed.text
    before = revisions(bambuddy, shop.item["id"])
    quotes = [quote["id"] for quote in shop.get_item()["quotes"]]

    op = rendered(bambuddy, shop.path)
    assert op["status"] == "succeeded", op
    item = shop.get_item()
    assert op["result"]["output_id"] == item["output_id"] is not None
    output = bambuddy.get(f"/api/v1/outputs/{item['output_id']}").json()
    assert output["model_version"] == item["reference"]["commit"]
    assert output["name"] == f"{shop.order['number']} {item['title']}"
    # The render of the item's own design: no new revision, and its quote stays.
    assert revisions(bambuddy, shop.item["id"]) == before
    assert [quote["id"] for quote in item["quotes"]] == quotes
    assert [slot["slot"] for slot in item["colours"]] == [0, 1]

    # The slice quote then slices that output, with no second render.
    outputs = output_ids(bambuddy)
    BambuddySlices({1: sliced_plate(1, 600, [1.0, 1.0])})
    quoted = shop.quote()
    assert quoted["status"] == "succeeded", quoted
    assert shop.get_item()["output_id"] == item["output_id"]
    assert output_ids(bambuddy) == outputs


def test_an_items_render_reuses_a_finished_render_of_its_key(bambuddy: TestClient) -> None:
    shop = Shop(bambuddy, by_hand=True)
    assert rendered(bambuddy, shop.path)["status"] == "succeeded"
    first = shop.get_item()
    other = add_item(
        bambuddy,
        shop.order["id"],
        slug=SLUG,
        commit=first["reference"]["commit"],
        inputs={"params": {"width": 12}},
        quantity=1,
    )
    path = f"/api/v1/orders/{shop.order['id']}/items/{other['id']}"
    op = rendered(bambuddy, path)
    assert op["status"] == "succeeded", op
    job_of = {
        output_id: bambuddy.get(f"/api/v1/outputs/{output_id}").json()["job_id"]
        for output_id in (first["output_id"], op["result"]["output_id"])
    }
    # One render of the key: the second item's output is saved from the same job.
    assert len(set(job_of.values())) == 1, job_of


def test_a_failed_render_is_the_operations_error(bambuddy: TestClient) -> None:
    shop = Shop(bambuddy, by_hand=True)
    commit = shop.item["reference"]["commit"]
    item = add_item(
        bambuddy,
        shop.order["id"],
        slug=SLUG,
        commit=commit,
        inputs={"params": {"width": FAIL_WIDTH}},
        quantity=1,
    )
    path = f"/api/v1/orders/{shop.order['id']}/items/{item['id']}"
    op = rendered(bambuddy, path)
    assert op["status"] == "failed", op
    assert "failed" in op["error"]["detail"]
    detail = bambuddy.get(f"/api/v1/orders/{shop.order['id']}").json()
    assert next(i for i in detail["items"] if i["id"] == item["id"])["output_id"] is None


def test_an_items_render_is_refused_before_anything_runs(bambuddy: TestClient) -> None:
    shop = Shop(bambuddy)
    missing = render(bambuddy, f"/api/v1/orders/{shop.order['id']}/items/nobody")
    assert missing.status_code == 404, missing.text
    custom = bambuddy.post(
        f"/api/v1/orders/{shop.order['id']}/items",
        json={"kind": "custom", "title": "Rush fee", "quantity": 1, "unit_price_minor": 100},
    )
    assert custom.status_code == 201, custom.text
    refused = render(bambuddy, f"/api/v1/orders/{shop.order['id']}/items/{custom.json()['id']}")
    assert refused.status_code == 409, refused.text
    assert refused.json()["type"].endswith("/item_custom")


def test_an_items_render_whose_pod_stopped_after_saving_the_output_saves_it_once(
    bambuddy: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pod stops after the item's output was saved and before the item's
    ``output_id`` write. The next attempt finds that output by the id the operation saves
    under (``render_output_id``), sets it on the item, and saves no second output."""
    shop = Shop(bambuddy, by_hand=True)
    before = output_ids(bambuddy)
    real = Lifecycle.update_item
    stops: list[str] = []

    def update_item(self: Lifecycle, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("output_id") is not None and not stops:
            stops.append(kwargs["output_id"])
            raise worker_stopped()
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Lifecycle, "update_item", update_item)
    op = rendered(bambuddy, shop.path)
    assert op["status"] == "succeeded", op
    assert len(stops) == 1
    added = [output for output in output_ids(bambuddy) if output not in before]
    assert added == stops
    assert shop.get_item()["output_id"] == stops[0]


def test_an_items_render_that_ends_after_the_item_settled_writes_nothing(
    bambuddy: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run checks the item again before it sets the output (``render_check``): an
    item cancelled while it rendered is refused ``item_not_open``, its output unset."""
    shop = Shop(bambuddy, by_hand=True)
    real = output_for_reference
    lifecycle = _state(bambuddy).components.get(ORDERS).lifecycle

    async def cancelled_meanwhile(*args: Any, **kwargs: Any) -> Any:
        await asyncio.to_thread(
            lifecycle.cancel_item, shop.order["id"], shop.item["id"], Actor(kind="browser")
        )
        return await real(*args, **kwargs)

    monkeypatch.setattr(order_operations, "output_for_reference", cancelled_meanwhile)
    op = rendered(bambuddy, shop.path)
    assert op["status"] == "failed", op
    assert op["error"]["type"].endswith("/item_not_open")
    assert shop.get_item()["output_id"] is None
