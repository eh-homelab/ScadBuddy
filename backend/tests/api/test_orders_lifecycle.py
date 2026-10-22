"""The whole order through the API (#4249, spec 2026-10-18 §3.3-3.5, §6.1, §6.3, §4),
and the lifecycle's refusals as 409 problems naming their ids."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import wait_for_job
from tests.api.test_customers import create as create_customer
from tests.api.test_orders import create_order
from tests.api.test_send import make_output
from tests.api.test_versions import SECOND, SLUG, put_source, upload
from tests.support.operations import press

# A committed template: an upload is a revision of the models repository, where the
# `model` fixture's files, written after the app boots, are not.
pytestmark = pytest.mark.requires_git

PROBLEMS = "https://scadbuddy.dev/problems/orders/"


def problem(response: Any, slug: str, status: int = 409) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    assert body["type"] == PROBLEMS + slug
    return body


def add_item(client: TestClient, order_id: str, **body: Any) -> dict[str, Any]:
    response = client.post(f"/api/v1/orders/{order_id}/items", json=body)
    assert response.status_code == 201, response.text
    item: dict[str, Any] = response.json()
    return item


def quote_and_accept(client: TestClient, order_id: str, item_id: str, price: int = 1200) -> None:
    quote = client.post(
        f"/api/v1/orders/{order_id}/items/{item_id}/quotes",
        json={"unit_price_minor": price, "label": "typed"},
    )
    assert quote.status_code == 201, quote.text
    accepted = client.post(
        f"/api/v1/orders/{order_id}/items/{item_id}/quotes/{quote.json()['id']}/accept"
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "approved"


def fulfil(client: TestClient, order_id: str, item_id: str, quantity: int) -> dict[str, Any]:
    attempt = client.post(
        f"/api/v1/orders/{order_id}/items/{item_id}/fulfilments",
        json={"quantity": quantity, "outcome": "succeeded"},
    )
    assert attempt.status_code == 201, attempt.text
    accepted = client.post(
        f"/api/v1/orders/{order_id}/items/{item_id}/fulfilments/{attempt.json()['id']}/accept"
    )
    assert accepted.status_code == 200, accepted.text
    body: dict[str, Any] = accepted.json()
    return body


def test_the_happy_path_through_the_api(client: TestClient) -> None:
    upload(client)
    customer = create_customer(client)
    order = create_order(client, customer["id"])
    output_id = make_output(client, SLUG)

    item = add_item(client, order["id"], output_id=output_id, quantity=2)
    reference = item["reference"]
    assert reference["slug"] == SLUG
    assert len(reference["commit"]) == 40
    assert reference["inputs"]["params"] == {"width": 12}
    assert (item["status"], item["quantity"], item["output_id"]) == ("draft", 2, output_id)
    assert item["colours"] == [{"slot": 0, "colour": "#FF0000", "filament_id": None}]
    assert item["title"] == SLUG

    quote = client.post(
        f"/api/v1/orders/{order['id']}/items/{item['id']}/quotes",
        json={"unit_price_minor": 1500, "label": "typed"},
    )
    assert quote.status_code == 201, quote.text
    assert (quote.json()["source"], quote.json()["total_minor"]) == ("manual", 3000)
    detail = client.get(f"/api/v1/orders/{order['id']}").json()
    assert detail["items"][0]["status"] == "quoted"
    assert detail["items"][0]["quotes"][0]["id"] == quote.json()["id"]
    accepted = client.post(
        f"/api/v1/orders/{order['id']}/items/{item['id']}/quotes/{quote.json()['id']}/accept"
    )
    assert accepted.json()["status"] == "approved"
    assert client.get(f"/api/v1/orders/{order['id']}").json()["order"]["status"] == "quoted"
    assert client.get(f"/api/v1/orders/{order['id']}").json()["totals"]["subtotal_minor"] == 3000

    order_accepted = client.post(f"/api/v1/orders/{order['id']}/accept")
    assert order_accepted.status_code == 200, order_accepted.text
    assert order_accepted.json()["status"] == "accepted"
    assert order_accepted.json()["shipping_address"]["recipient"] == "Ada Lovelace"

    fulfil(client, order["id"], item["id"], 2)
    detail = client.get(f"/api/v1/orders/{order['id']}").json()
    assert detail["order"]["status"] == "ready"
    assert detail["items"][0]["status"] == "fulfilled"
    assert detail["items"][0]["fulfilments"][0]["accepted_at"] is not None

    shipment = client.post(
        f"/api/v1/orders/{order['id']}/shipments",
        json={
            "carrier": "Post",
            "tracking": "T1",
            "items": [{"order_item_id": item["id"], "quantity": 2}],
        },
    )
    assert shipment.status_code == 201, shipment.text
    assert shipment.json()["address"]["recipient"] == "Ada Lovelace"
    assert client.get(f"/api/v1/orders/{order['id']}").json()["order"]["status"] == "shipped"
    # A PATCH carrying only the tracking number keeps the carrier and the lines, so
    # the order stays shipped.
    updated = client.patch(
        f"/api/v1/orders/{order['id']}/shipments/{shipment.json()['id']}",
        json={"tracking": "T2"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["tracking"] == "T2" and updated.json()["carrier"] == "Post"
    assert updated.json()["items"] == [{"order_item_id": item["id"], "quantity": 2}]
    assert client.get(f"/api/v1/orders/{order['id']}").json()["order"]["status"] == "shipped"
    twice = client.post(
        f"/api/v1/orders/{order['id']}/shipments",
        json={"items": [{"order_item_id": item["id"], "quantity": 1}] * 2},
    )
    assert twice.status_code == 422, twice.text

    closed = client.post(f"/api/v1/orders/{order['id']}/close")
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "closed"
    kinds = [e["kind"] for e in client.get(f"/api/v1/orders/{order['id']}/events").json()]
    assert kinds[0] == "order.created" and kinds[-1] == "order.status"
    assert "shipment.updated" in kinds


def test_an_item_by_hand_and_its_edits(client: TestClient) -> None:
    upload(client)
    customer = create_customer(client)
    order = create_order(client, customer["id"])
    commit = client.get(f"/api/v1/models/{SLUG}").json()["version"]
    item = add_item(
        client,
        order["id"],
        slug=SLUG,
        commit=commit[:7],
        inputs={"params": {"width": 7}},
        quantity=1,
        title="Seven",
        notes="n",
    )
    assert item["reference"]["commit"] == commit
    assert item["colours"] == []
    assert item["title"] == "Seven"
    quote_and_accept(client, order["id"], item["id"])
    assert client.get(f"/api/v1/orders/{order['id']}").json()["order"]["status"] == "quoted"

    repinned = client.patch(
        f"/api/v1/orders/{order['id']}/items/{item['id']}",
        json={"reference": {"slug": SLUG, "commit": commit, "inputs": {"params": {"width": 8}}}},
    )
    assert repinned.status_code == 200, repinned.text
    assert repinned.json()["status"] == "draft"
    assert repinned.json()["reference"]["inputs"]["params"] == {"width": 8}
    assert client.get(f"/api/v1/orders/{order['id']}").json()["order"]["status"] == "draft"
    quantity = client.patch(
        f"/api/v1/orders/{order['id']}/items/{item['id']}", json={"quantity": 3}
    )
    assert quantity.json()["quantity"] == 3
    renamed = client.patch(
        f"/api/v1/orders/{order['id']}/items/{item['id']}", json={"title": "Eight"}
    )
    assert renamed.json()["title"] == "Eight"
    assert (
        client.patch(
            f"/api/v1/orders/{order['id']}/items/{item['id']}", json={"status": "approved"}
        ).status_code
        == 422
    )

    deleted = client.delete(f"/api/v1/orders/{order['id']}/items/{item['id']}")
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/orders/{order['id']}").json()["items"] == []
    assert client.delete(f"/api/v1/orders/{order['id']}/items/{item['id']}").status_code == 404


def test_a_bad_reference_is_422_naming_the_field(client: TestClient) -> None:
    upload(client)
    customer = create_customer(client)
    order = create_order(client, customer["id"])
    commit = client.get(f"/api/v1/models/{SLUG}").json()["version"]
    cases = [
        ({"slug": "nothing", "commit": commit, "inputs": {}}, "slug"),
        ({"slug": SLUG, "commit": "0" * 40, "inputs": {}}, "commit"),
        ({"slug": SLUG, "commit": "HEAD", "inputs": {}}, "commit"),
        ({"slug": SLUG, "commit": commit, "inputs": {"params": {"depth": 1}}}, "inputs"),
        ({"output_id": "missing"}, "output_id"),
        ({}, "slug"),
    ]
    for body, field in cases:
        response = client.post(f"/api/v1/orders/{order['id']}/items", json={**body, "quantity": 1})
        assert response.status_code == 422, (body, response.text)
        assert response.json()["errors"][0]["loc"] == ["body", field], body
    assert (
        client.post(
            f"/api/v1/orders/{order['id']}/items", json={"output_id": "x", "quantity": 0}
        ).status_code
        == 422
    )


def test_a_commit_or_inputs_beside_an_output_must_be_its_own(client: TestClient) -> None:
    """#4458: on an add and on a repin, a commit or inputs that describe another render
    than the output named beside them are 422 on that field."""
    upload(client)
    customer = create_customer(client)
    order = create_order(client, customer["id"])
    output_id = make_output(client, SLUG)
    rendered = client.get(f"/api/v1/models/{SLUG}").json()["version"]
    assert put_source(client, SECOND).status_code == 200
    later = client.get(f"/api/v1/models/{SLUG}").json()["version"]
    assert later != rendered
    item = add_item(client, order["id"], output_id=output_id, quantity=1)
    inputs = item["reference"]["inputs"]
    # The output's own, given back whole, agree.
    agreed = add_item(
        client, order["id"], output_id=output_id, commit=rendered, inputs=inputs, quantity=1
    )
    assert agreed["reference"]["commit"] == rendered
    cases = [
        ({"commit": later}, "commit"),
        ({"inputs": {**inputs, "params": {"width": 20}}}, "inputs"),
    ]
    for reference, field in cases:
        added = client.post(
            f"/api/v1/orders/{order['id']}/items",
            json={**reference, "output_id": output_id, "quantity": 1},
        )
        assert added.status_code == 422, (reference, added.text)
        assert added.json()["errors"][0]["loc"][-1] == field, reference
        repinned = client.patch(
            f"/api/v1/orders/{order['id']}/items/{item['id']}",
            json={"reference": reference, "output_id": output_id},
        )
        assert repinned.status_code == 422, (reference, repinned.text)
        assert repinned.json()["errors"][0]["loc"][-1] == field, reference


def test_inputs_beside_an_output_are_given_whole(client: TestClient) -> None:
    """#4485: the output's own params without its `v` read as `v` 0, so beside an
    output rendered at `v` 1 they are 422 on `inputs`, on an add and on a repin."""
    upload(client)
    customer = create_customer(client)
    order = create_order(client, customer["id"])
    inputs = {"params": {"width": 12}, "v": 1}
    job_id = client.post(f"/api/v1/models/{SLUG}/render", json={"inputs": inputs}).json()["job_id"]
    wait_for_job(client, job_id)
    output_id = client.post(
        f"/api/v1/models/{SLUG}/outputs", json={"job_id": job_id, "name": "v1"}, headers=press()
    ).json()["id"]
    item = add_item(client, order["id"], output_id=output_id, inputs=inputs, quantity=1)
    assert item["reference"]["inputs"]["v"] == 1
    reference = {"inputs": {"params": inputs["params"]}}
    added = client.post(
        f"/api/v1/orders/{order['id']}/items",
        json={**reference, "output_id": output_id, "quantity": 1},
    )
    assert added.status_code == 422, added.text
    assert added.json()["errors"][0]["loc"][-1] == "inputs"
    assert "`v` included" in added.text
    repinned = client.patch(
        f"/api/v1/orders/{order['id']}/items/{item['id']}",
        json={"reference": reference, "output_id": output_id},
    )
    assert repinned.status_code == 422, repinned.text
    assert repinned.json()["errors"][0]["loc"][-1] == "inputs"


def test_a_deleted_template_is_409(client: TestClient) -> None:
    upload(client)
    customer = create_customer(client)
    order = create_order(client, customer["id"])
    commit = client.get(f"/api/v1/models/{SLUG}").json()["version"]
    assert client.delete(f"/api/v1/models/{SLUG}", headers=press()).status_code in (
        200,
        202,
        204,
    )
    response = client.post(
        f"/api/v1/orders/{order['id']}/items",
        json={"slug": SLUG, "commit": commit, "inputs": {}, "quantity": 1},
    )
    body = problem(response, "template_deleted")
    assert body["slug"] == SLUG


def test_the_refusals_are_409_problems_naming_their_ids(client: TestClient) -> None:
    upload(client)
    customer = create_customer(client)
    order = create_order(client, customer["id"])
    commit = client.get(f"/api/v1/models/{SLUG}").json()["version"]
    reference = {"slug": SLUG, "commit": commit, "inputs": {}}
    done = add_item(client, order["id"], **reference, quantity=1)
    pending = add_item(client, order["id"], **reference, quantity=1)

    # a quote that is not the item's is not there (while the order is still open)
    missing = client.post(f"/api/v1/orders/{order['id']}/items/{done['id']}/quotes/nope/accept")
    assert missing.status_code == 404, missing.text
    # accept needs every open item quoted, and names the ones that are not
    body = problem(client.post(f"/api/v1/orders/{order['id']}/accept"), "items_not_quoted")
    assert body["items"] == [done["id"], pending["id"]]
    quote_and_accept(client, order["id"], done["id"])
    quote_and_accept(client, order["id"], pending["id"])

    # on hold, what advances the order is refused; release goes
    client.post(f"/api/v1/orders/{order['id']}/hold")
    problem(client.post(f"/api/v1/orders/{order['id']}/accept"), "order_on_hold")
    client.post(f"/api/v1/orders/{order['id']}/release")
    assert client.post(f"/api/v1/orders/{order['id']}/accept").json()["status"] == "accepted"

    # the reference and quantity are pinned once accepted
    problem(
        client.patch(f"/api/v1/orders/{order['id']}/items/{done['id']}", json={"quantity": 2}),
        "item_locked",
    )
    # a non-draft item does not delete: the route tells, the caller decides
    body = problem(
        client.delete(f"/api/v1/orders/{order['id']}/items/{pending['id']}"), "item_not_draft"
    )
    assert "cancel" in body["detail"]

    fulfil(client, order["id"], done["id"], 1)
    # close names every open item and every fulfilled item not shipped
    body = problem(client.post(f"/api/v1/orders/{order['id']}/close"), "order_not_closable")
    assert set(body["items"]) == {pending["id"], done["id"]}
    # a shipment carries fulfilled items only
    body = problem(
        client.post(
            f"/api/v1/orders/{order['id']}/shipments",
            json={"items": [{"order_item_id": pending["id"], "quantity": 1}]},
        ),
        "items_not_fulfilled",
    )
    assert body["items"] == [pending["id"]]
    assert client.post(f"/api/v1/orders/{order['id']}/shipments/nope", json={}).status_code == 405
    assert client.patch(f"/api/v1/orders/{order['id']}/shipments/nope", json={}).status_code == 404

    # cancel keeps the fulfilled item and its attempt, cancels the open one
    cancelled = client.post(f"/api/v1/orders/{order['id']}/items/{pending['id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    problem(
        client.post(f"/api/v1/orders/{order['id']}/items/{pending['id']}/cancel"), "item_not_open"
    )
    whole = client.post(f"/api/v1/orders/{order['id']}/cancel", json={"reason": "changed mind"})
    assert whole.status_code == 200 and whole.json()["status"] == "cancelled"
    detail = client.get(f"/api/v1/orders/{order['id']}").json()
    assert [i["status"] for i in detail["items"]] == ["fulfilled", "cancelled"]
    assert len(detail["items"][0]["fulfilments"]) == 1
    problem(client.post(f"/api/v1/orders/{order['id']}/cancel"), "order_not_cancellable")
    problem(client.post(f"/api/v1/orders/{order['id']}/hold"), "order_cancelled")
    assert client.post("/api/v1/orders/nope/accept").status_code == 404
