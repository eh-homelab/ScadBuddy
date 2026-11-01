"""Printing an order item end to end (#4719, spec 2026-10-18 §5.4):
``POST /orders/{id}/items/{item}/print`` answers with the ``order_item_print``
operation, whose run starts the item's output's print run as ``POST .../run`` starts one
(Bambuddy's slice and queue over the ``respx`` recordings ``tests/api/test_print.py``
uses), records the attempt and queues the item; the item then follows the print's
recorded outcome."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.core.events import PrintOutcome
from scadbuddy.orders.lifecycle import FULFILMENT_HOLDER
from tests.api.conftest import follow_operation
from tests.api.test_customers import create as create_customer
from tests.api.test_order_quote import COPY, BambuddySlices, sliced_plate, two_colours
from tests.api.test_order_quote import Shop as QuoteShop
from tests.api.test_orders import create_order
from tests.api.test_orders_lifecycle import add_item, problem, quote_and_accept
from tests.api.test_print_filaments import queue_route, slice_routes
from tests.api.test_print_run_choices import body, follow_run, run_routes
from tests.api.test_send import configure, make_output, upload_route
from tests.api.test_settings_runtime import _state
from tests.api.test_versions import SLUG, upload
from tests.support.operations import press

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_git]


@pytest.fixture
def bambuddy(client: TestClient, fake_openscad: str) -> Iterator[TestClient]:
    with respx.mock as router:
        router.route(host="testserver").pass_through()
        two_colours(fake_openscad)
        configure(client)
        upload_route(COPY)
        run_routes()
        yield client


class Item:
    """One item of an order from an output, quoted by hand and accepted."""

    def __init__(self, client: TestClient, *, quantity: int = 2, accept: bool = True) -> None:
        self.client = client
        upload(client)
        customer = create_customer(client)
        self.order = create_order(client, customer["id"])
        self.output_id = make_output(client, SLUG)
        self.item = add_item(client, self.order["id"], output_id=self.output_id, quantity=quantity)
        quote_and_accept(client, self.order["id"], self.item["id"])
        if accept:
            accepted = client.post(f"/api/v1/orders/{self.order['id']}/accept")
            assert accepted.status_code == 200, accepted.text
        self.path = f"/api/v1/orders/{self.order['id']}/items/{self.item['id']}"

    def post(self, key: str | None = None, **extra: Any) -> Any:
        headers = {"Idempotency-Key": key} if key else press()
        return self.client.post(f"{self.path}/print", json={**body(), **extra}, headers=headers)

    def get(self) -> dict[str, Any]:
        detail = self.client.get(f"/api/v1/orders/{self.order['id']}").json()
        found: dict[str, Any] = next(i for i in detail["items"] if i["id"] == self.item["id"])
        return found

    def order_status(self) -> str:
        detail = self.client.get(f"/api/v1/orders/{self.order['id']}").json()
        status: str = detail["order"]["status"]
        return status


def answered(client: TestClient, started: httpx.Response) -> dict[str, Any]:
    """The attempt a print answered with, followed past a 202."""
    assert started.status_code in (200, 202), started.text
    if started.status_code == 200:
        answer: dict[str, Any] = started.json()
        return answer
    op = follow_operation(client, started.json(), timeout=120)
    assert op["status"] == "succeeded", op
    result: dict[str, Any] = op["result"]
    return result


def printed(item: Item, key: str | None = None) -> dict[str, Any]:
    return answered(item.client, item.post(key))


def test_an_item_prints_from_its_quote_and_follows_the_print(bambuddy: TestClient) -> None:
    slice_routes()
    queued = queue_route()
    item = Item(bambuddy)
    attempt = printed(item, key="print-1")
    assert (attempt["attempt"], attempt["outcome"], attempt["quantity"]) == (1, "unknown", 0)
    assert attempt["copies"] == 2 and attempt["print_run_id"]
    # A manual quote names no sliced file.
    assert attempt["sliced_sha256"] is None
    assert item.get()["status"] == "queued"
    assert item.order_status() == "in_production"

    run = follow_run(bambuddy, attempt["print_run_id"])
    assert run["status"] == "succeeded", run
    assert run["subject"] == f"output:{item.output_id}"
    sent = json.loads(queued.calls.last.request.content)
    # §5.4: the quantity still to print.
    assert sent["quantity"] == 2

    # The same press again repeats its run and records nothing twice.
    assert printed(item, key="print-1")["id"] == attempt["id"]
    assert len(item.get()["fulfilments"]) == 1

    # The print's follow records its outcome (#2665): the item follows it.
    state = _state(bambuddy)
    with state.projection.pool.connection() as conn:
        conn.execute(
            "INSERT INTO print_outcomes (run_id, subject, slug, outcome)"
            " VALUES (%s, %s, %s, 'finished')",
            (run["id"], item.output_id, SLUG),
        )
    state.events.publish(
        PrintOutcome(output_id=item.output_id, slug=SLUG, run_id=run["id"], outcome="finished")
    )
    deadline = time.monotonic() + 30
    while item.get()["status"] != "printed" and time.monotonic() < deadline:
        time.sleep(0.05)
    current = item.get()
    assert current["status"] == "printed"
    [done] = current["fulfilments"]
    assert (done["outcome"], done["quantity"]) == ("succeeded", 2)
    accepted = bambuddy.post(f"{item.path}/fulfilments/{done['id']}/accept")
    assert accepted.status_code == 200, accepted.text
    assert item.get()["status"] == "fulfilled"
    assert item.order_status() == "ready"


def test_a_print_is_refused_before_anything_starts(bambuddy: TestClient) -> None:
    item = Item(bambuddy, accept=False)
    problem(item.post(), "order_not_accepted")
    # Nothing was started or recorded.
    assert item.get()["fulfilments"] == []
    assert item.get()["status"] == "approved"


def test_a_slice_quote_prints_and_holds_its_sliced_file(bambuddy: TestClient) -> None:
    """The attempt names and holds the accepted slice quote's file (§6.1); a file that is
    no longer the one the quote priced wants a new quote."""
    shop = QuoteShop(bambuddy)
    BambuddySlices({1: sliced_plate(1, 3600, [12.5, 3.0])})
    op = shop.quote()
    assert op["status"] == "succeeded", op
    quote = op["result"]
    assert bambuddy.post(f"{shop.path}/quotes/{quote['id']}/accept").status_code == 200
    assert bambuddy.post(f"/api/v1/orders/{shop.order['id']}/accept").status_code == 200

    sliced = _state(bambuddy).store.blobs.dir_for(quote["sliced_blob_key"]) / "sliced.3mf"
    kept = sliced.read_bytes()
    sliced.write_bytes(b"not the file it priced")
    problem(bambuddy.post(f"{shop.path}/print", json=body(), headers=press()), "requote_needed")
    sliced.write_bytes(kept)

    queue_route()
    attempt = answered(bambuddy, bambuddy.post(f"{shop.path}/print", json=body(), headers=press()))
    assert attempt["sliced_sha256"] == quote["sliced_sha256"]
    assert attempt["sliced_blob_key"] == quote["sliced_blob_key"]
    refs = _state(bambuddy).refs
    with refs.pool.connection() as conn:
        held = conn.execute(
            "SELECT key FROM blob_refs WHERE holder_kind = %s AND holder_id = %s",
            (FULFILMENT_HOLDER, attempt["id"]),
        ).fetchall()
    assert [row["key"] for row in held] == [quote["sliced_blob_key"]]
