"""`POST /orders/{id}/items/{item}/revisions/after-acceptance` (#5350): an accepted item's
new design on an accepted, in production or ready order. The order's total keeps the old
accepted price until a quote of the new design is accepted. Refused before acceptance, on
hold, on an ended order and on a fulfilled item; the plain route names this one."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.api.test_customers import create as create_customer
from tests.api.test_order_add_after_acceptance import detail
from tests.api.test_orders import create_order
from tests.api.test_orders_lifecycle import add_item, fulfil, problem, quote_and_accept
from tests.api.test_send import make_output
from tests.api.test_versions import SLUG, upload

pytestmark = pytest.mark.requires_git


def revise(client: TestClient, order_id: str, item_id: str, **body: Any) -> Any:
    return client.post(
        f"/api/v1/orders/{order_id}/items/{item_id}/revisions/after-acceptance", json=body
    )


def model_order(client: TestClient, *, accept: bool = True) -> tuple[str, str]:
    """An order with one model item at an accepted 1000 a unit, two of them; accepted
    unless ``accept`` is false."""
    upload(client)
    customer = create_customer(client)
    order = create_order(client, customer["id"])
    item = add_item(client, order["id"], output_id=make_output(client, SLUG), quantity=2)
    quote_and_accept(client, order["id"], item["id"], price=1000)
    if accept:
        accepted = client.post(f"/api/v1/orders/{order['id']}/accept")
        assert accepted.json()["status"] == "accepted", accepted.text
    return order["id"], item["id"]


def item_of(client: TestClient, order_id: str, item_id: str) -> dict[str, Any]:
    found: dict[str, Any] = next(i for i in detail(client, order_id)["items"] if i["id"] == item_id)
    return found


def test_the_old_price_stands_until_the_new_quote_is_accepted(client: TestClient) -> None:
    order_id, item_id = model_order(client)
    assert detail(client, order_id)["totals"]["subtotal_minor"] == 2000
    # The plain route refuses an accepted item and names this one.
    refused = problem(
        client.post(f"/api/v1/orders/{order_id}/items/{item_id}/revisions", json={}),
        "item_locked",
    )
    assert f"/items/{item_id}/revisions/after-acceptance" in refused["detail"]

    made = revise(client, order_id, item_id, note="A bigger loop")
    assert made.status_code == 201, made.text
    assert made.json()["note"] == "A bigger loop"

    item = item_of(client, order_id, item_id)
    assert (item["status"], item["revised_after_acceptance"]) == ("draft", True)
    assert item["current_revision_id"] == made.json()["id"]
    seen = detail(client, order_id)
    assert seen["order"]["status"] == "accepted"
    # The agreed price stands.
    assert (seen["totals"]["subtotal_minor"], seen["totals"]["pending_minor"]) == (2000, 0)
    events = client.get(f"/api/v1/orders/{order_id}/events").json()
    rows = [e for e in events if e["kind"] == "revision.added_after_acceptance"]
    assert len(rows) == 1
    assert rows[0]["detail"]["order_status"] == "accepted"
    assert rows[0]["by"]["kind"] == "browser"

    # A new quote is recorded; the total still holds the accepted one.
    quote = client.post(
        f"/api/v1/orders/{order_id}/items/{item_id}/quotes",
        json={"unit_price_minor": 1300, "label": "new design"},
    )
    assert quote.status_code == 201, quote.text
    assert detail(client, order_id)["totals"]["subtotal_minor"] == 2000
    # The quantity stays pinned.
    problem(
        client.patch(f"/api/v1/orders/{order_id}/items/{item_id}", json={"quantity": 3}),
        "item_locked",
    )

    accepted = client.post(
        f"/api/v1/orders/{order_id}/items/{item_id}/quotes/{quote.json()['id']}/accept"
    )
    assert accepted.status_code == 200, accepted.text
    assert (accepted.json()["status"], accepted.json()["revised_after_acceptance"]) == (
        "approved",
        False,
    )
    assert detail(client, order_id)["totals"]["subtotal_minor"] == 2600


def test_before_acceptance_is_refused(client: TestClient) -> None:
    order_id, item_id = model_order(client, accept=False)
    body = problem(revise(client, order_id, item_id), "order_not_accepted")
    assert f"/items/{item_id}/revisions" in body["detail"]


def test_on_hold_is_refused(client: TestClient) -> None:
    order_id, item_id = model_order(client)
    held = client.post(f"/api/v1/orders/{order_id}/hold")
    assert held.status_code == 200, held.text
    problem(revise(client, order_id, item_id), "order_on_hold")
    assert item_of(client, order_id, item_id)["revised_after_acceptance"] is False


def test_a_fulfilled_item_is_refused(client: TestClient) -> None:
    order_id, item_id = model_order(client)
    fulfil(client, order_id, item_id, 2)
    assert detail(client, order_id)["order"]["status"] == "ready"
    problem(revise(client, order_id, item_id), "item_not_open")


def test_a_cancelled_order_is_refused(client: TestClient) -> None:
    order_id, item_id = model_order(client)
    cancelled = client.post(f"/api/v1/orders/{order_id}/cancel", json={})
    assert cancelled.status_code == 200, cancelled.text
    problem(revise(client, order_id, item_id), "order_cancelled")


def test_an_item_with_an_accepted_attempt_is_refused(client: TestClient) -> None:
    """#5592: with 1 of 2 units accepted, the route answers `item_partly_fulfilled`, its
    detail says "1 of 2", and the item stays `approved` and not revised after acceptance."""
    order_id, item_id = model_order(client)
    fulfil(client, order_id, item_id, 1)
    body = problem(revise(client, order_id, item_id), "item_partly_fulfilled")
    assert "1 of 2" in body["detail"]
    item = item_of(client, order_id, item_id)
    assert (item["status"], item["revised_after_acceptance"]) == ("approved", False)


def test_the_detail_marks_the_old_designs_attempt_stale(
    client: TestClient, pg_conninfo: str
) -> None:
    """#5592: the order detail marks an attempt older than the accepted quote's revision
    `stale`, by the rule its accept answers `fulfilment_stale` by."""
    order_id, item_id = model_order(client)
    base = f"/api/v1/orders/{order_id}/items/{item_id}"
    old = client.post(f"{base}/fulfilments", json={"quantity": 1, "outcome": "succeeded"})
    assert old.status_code == 201, old.text
    # Pinned an hour back: before the revision below, whatever the clock does between.
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            "UPDATE order_item_fulfilments SET created_at = %s WHERE id = %s",
            (datetime.now(UTC) - timedelta(hours=1), old.json()["id"]),
        )
    assert revise(client, order_id, item_id).status_code == 201
    quote = client.post(f"{base}/quotes", json={"unit_price_minor": 1300, "label": "new"})
    assert quote.status_code == 201, quote.text
    assert client.post(f"{base}/quotes/{quote.json()['id']}/accept").status_code == 200
    new = client.post(f"{base}/fulfilments", json={"quantity": 1, "outcome": "succeeded"})
    assert new.status_code == 201, new.text

    stale = {a["id"]: a["stale"] for a in item_of(client, order_id, item_id)["fulfilments"]}
    assert stale == {old.json()["id"]: True, new.json()["id"]: False}
    problem(client.post(f"{base}/fulfilments/{old.json()['id']}/accept"), "fulfilment_stale")
    accepted = client.post(f"{base}/fulfilments/{new.json()['id']}/accept")
    assert accepted.status_code == 200, accepted.text


def test_the_body_is_the_plain_routes(client: TestClient) -> None:
    order_id, item_id = model_order(client)
    unknown = revise(client, order_id, item_id, price=1)
    assert unknown.status_code == 422, unknown.text
    long_note = revise(client, order_id, item_id, note="x" * 4001)
    assert long_note.status_code == 422, long_note.text
