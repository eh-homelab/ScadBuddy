"""Square through the real routes (#5666): connect, request, pay with the fee told later,
a bank payment that settles or fails, a cancel, two part refunds and a failed one, from
the recorded payloads in ``tests/payments/square_payloads``. respx answers every Square
call; nothing reaches the network."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.payments.component import PAYMENTS
from scadbuddy.payments.square import API
from tests.api.test_order_payments import (
    _due,
    _state,
    payable_order,
    request_payment,
    with_payments,
)
from tests.payments.test_square import (
    LINK,
    LOCATION,
    NOTIFY,
    ORDER,
    PAYMENT,
    SIGNATURE_KEY,
    TOKEN,
    payload,
    signed,
)

pytestmark = [pytest.mark.requires_postgres, pytest.mark.usefixtures("app_payment_sweep_off")]

HOOK = "/api/v1/payments/webhooks/square"
CONNECT = {
    "settings": {"environment": "sandbox", "location_id": LOCATION, "notification_url": NOTIFY},
    "secrets": {"access_token": TOKEN, "signature_key": SIGNATURE_KEY},
}


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    return with_payments(settings, tmp_path)


@pytest.fixture
def square() -> Iterator[respx.MockRouter]:
    """Square's sandbox API as the recorded answers. Only connect.squareupsandbox.com is
    mocked; the test client's own requests never leave the process."""
    with respx.mock(base_url=API["sandbox"], assert_all_called=False) as router:
        router.get(f"/v2/locations/{LOCATION}").respond(json=payload("api.location.json"))
        router.post("/v2/online-checkout/payment-links", name="create").respond(
            json=payload("api.payment_link.created.json")
        )
        router.get("/v2/online-checkout/payment-links").respond(
            json=payload("api.payment_links.page2.json")
        )
        router.delete(f"/v2/online-checkout/payment-links/{LINK}", name="delete").respond(
            json=payload("api.payment_link.deleted.json")
        )
        router.get(f"/v2/payments/{PAYMENT}", name="payment").respond(
            json=payload("api.payment.json")
        )
        yield router


def send(client: TestClient, name: str) -> httpx.Response:
    body, headers = signed(name)
    answer: httpx.Response = client.post(HOOK, content=body, headers=headers)
    return answer


def square_request(client: TestClient) -> tuple[str, dict[str, Any]]:
    connected = client.put("/api/v1/payments/providers/square", json=CONNECT)
    assert connected.status_code == 200, connected.text
    # The settings page shows this as "Connected as …": sandbox or production.
    assert connected.json()["account_label"] == "Square location Print shop (sandbox)"
    assert connected.json()["currency"] == "USD"
    assert TOKEN not in connected.text and SIGNATURE_KEY not in connected.text
    order = payable_order(client, 700)
    req = request_payment(client, order["id"], provider="square")
    assert (req["provider_ref"], req["status"]) == (ORDER, "open")
    assert req["hosted_url"] == "https://sandbox.square.link/u/kpTBBMfZ"
    return order["id"], req


def detail(client: TestClient, order_id: str) -> dict[str, Any]:
    found: dict[str, Any] = client.get(f"/api/v1/orders/{order_id}").json()
    return found


def fees(pg_conninfo: str) -> list[tuple[int, str]]:
    with psycopg.connect(pg_conninfo) as conn:
        rows = conn.execute(
            "SELECT amount_minor, source FROM order_cost_entries WHERE kind = 'payment_fee'"
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def test_pay_with_the_fee_told_later_and_two_part_refunds(
    client: TestClient, square: respx.MockRouter, pg_conninfo: str
) -> None:
    order_id, req = square_request(client)
    created = json.loads(square["create"].calls.last.request.content)
    assert created["idempotency_key"] == req["id"]
    assert created["quick_pay"]["location_id"] == LOCATION

    assert send(client, "payment.updated.completed.json").json() == {"outcome": "recorded"}
    found = detail(client, order_id)
    assert found["payment_requests"][0]["status"] == "paid"
    assert found["totals"]["balance_minor"] == 0
    [paid] = found["payments"]
    assert (paid["amount_minor"], paid["method"], paid["reference"]) == (700, "square", ORDER)
    assert fees(pg_conninfo) == []
    assert send(client, "payment.updated.completed.json").json() == {"outcome": "replayed"}

    # Square adds the processing fee to the payment and tells it in another update.
    assert send(client, "payment.updated.completed.fee.json").json() == {"outcome": "recorded"}
    assert fees(pg_conninfo) == [(50, f"payment:{paid['id']}")]
    assert send(client, "payment.updated.completed.fee.json").json() == {"outcome": "replayed"}
    assert [p["amount_minor"] for p in detail(client, order_id)["payments"]] == [700]

    assert send(client, "refund.created.pending.json").json() == {"outcome": "unmatched"}
    assert send(client, "refund.updated.completed.json").json() == {"outcome": "recorded"}
    assert send(client, "refund.updated.completed.again.json").json() == {"outcome": "replayed"}
    assert send(client, "refund.updated.second.json").json() == {"outcome": "recorded"}
    payments = detail(client, order_id)["payments"]
    assert [p["amount_minor"] for p in payments] == [700, -400, -300]

    # The first refund fails afterwards: the money came back.
    assert send(client, "refund.updated.failed.json").json() == {"outcome": "recorded"}
    payments = detail(client, order_id)["payments"]
    assert [p["amount_minor"] for p in payments] == [700, -400, -300, 400]


def test_a_payment_whose_first_update_has_its_fee_records_it_at_once(
    client: TestClient, square: respx.MockRouter, pg_conninfo: str
) -> None:
    order_id, _ = square_request(client)
    assert send(client, "payment.updated.completed.fee.json").json() == {"outcome": "recorded"}
    assert send(client, "payment.updated.completed.json").json() == {"outcome": "ignored"}
    [paid] = detail(client, order_id)["payments"]
    assert fees(pg_conninfo) == [(50, f"payment:{paid['id']}")]


def test_a_bank_payment_is_pending_until_it_settles(
    client: TestClient, square: respx.MockRouter
) -> None:
    order_id, _ = square_request(client)
    assert send(client, "payment.created.pending.json").json() == {"outcome": "recorded"}
    assert detail(client, order_id)["payment_requests"][0]["status"] == "pending"
    assert detail(client, order_id)["payments"] == []
    assert send(client, "payment.updated.settled.json").json() == {"outcome": "recorded"}
    assert detail(client, order_id)["payment_requests"][0]["status"] == "paid"


def test_a_failed_bank_payment_records_no_money(
    client: TestClient, square: respx.MockRouter
) -> None:
    order_id, _ = square_request(client)
    send(client, "payment.created.pending.json")
    assert send(client, "payment.updated.failed.json").json() == {"outcome": "recorded"}
    assert detail(client, order_id)["payment_requests"][0]["status"] == "failed"
    assert detail(client, order_id)["payments"] == []


def test_cancelling_a_request_deletes_its_payment_link(
    client: TestClient, square: respx.MockRouter
) -> None:
    order_id, req = square_request(client)
    cancelled = client.post(f"/api/v1/orders/{order_id}/payment-requests/{req['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert square["delete"].called


def test_a_location_in_another_currency_is_refused(
    client: TestClient, square: respx.MockRouter
) -> None:
    """A Square location takes one currency (the Location object's ``currency``); a
    location that is not in the shop's ``orders_currency`` could take none of its links."""
    location = payload("api.location.json")
    location["location"]["currency"] = "CAD"
    square.get(f"/v2/locations/{LOCATION}").respond(json=location)
    refused = client.put("/api/v1/payments/providers/square", json=CONNECT)
    assert refused.status_code == 409, refused.text
    assert refused.json()["type"] == "https://scadbuddy.dev/problems/payments/currency_mismatch"
    assert TOKEN not in refused.text
    [square_row] = [
        s for s in client.get("/api/v1/payments/providers").json() if s["id"] == "square"
    ]
    assert square_row["connected"] is False


def test_a_forged_event_records_nothing(
    client: TestClient, square: respx.MockRouter, pg_conninfo: str
) -> None:
    order_id, _ = square_request(client)
    body, _ = signed("payment.updated.completed.json")
    forged = signed("payment.updated.completed.json", key="not the key")[1]
    refused = client.post(HOOK, content=body, headers=forged)
    assert refused.status_code == 400
    assert refused.json()["type"] == "https://scadbuddy.dev/problems/payments/bad_signature"
    assert detail(client, order_id)["payments"] == []
    with psycopg.connect(pg_conninfo) as conn:
        row = conn.execute("SELECT count(*) FROM payment_events").fetchone()
    assert row is not None and row[0] == 0


def test_square_down_while_reading_a_refund_is_503_and_square_retries(
    client: TestClient, square: respx.MockRouter, pg_conninfo: str
) -> None:
    order_id, _ = square_request(client)
    send(client, "payment.updated.completed.json")
    square["payment"].respond(
        503, json={"errors": [{"category": "API_ERROR", "code": "SERVICE_UNAVAILABLE"}]}
    )
    refused = send(client, "refund.updated.completed.json")
    assert refused.status_code == 503
    assert refused.json()["type"] == "https://scadbuddy.dev/problems/payments/provider_unavailable"
    with psycopg.connect(pg_conninfo) as conn:
        row = conn.execute("SELECT count(*) FROM payment_events").fetchone()
    assert row is not None and row[0] == 1
    square["payment"].respond(json=payload("api.payment.json"))
    assert send(client, "refund.updated.completed.json").json() == {"outcome": "recorded"}
    assert [p["amount_minor"] for p in detail(client, order_id)["payments"]] == [700, -400]


def pay_by_hand(client: TestClient, order_id: str, request_id: str) -> None:
    paid = client.post(
        f"/api/v1/orders/{order_id}/payments",
        json={"amount_minor": 700, "method": "cash", "payment_request_id": request_id},
    )
    assert paid.status_code == 201, paid.text


def test_a_link_paid_after_cash_paid_its_request_records_the_money_once(
    client: TestClient, square: respx.MockRouter, pg_conninfo: str
) -> None:
    """#5725 review: cash paid the request first, so its ``payment_id`` is the cash. Each
    later ``payment.updated`` of the Square payment is still the same money."""
    order_id, req = square_request(client)
    pay_by_hand(client, order_id, req["id"])
    assert send(client, "payment.updated.completed.json").json() == {"outcome": "recorded"}
    assert send(client, "payment.updated.completed.fee.json").json() == {"outcome": "recorded"}
    assert send(client, "refund.updated.completed.json").json() == {"outcome": "recorded"}
    payments = detail(client, order_id)["payments"]
    assert [(p["amount_minor"], p["method"]) for p in payments] == [
        (700, "cash"),
        (700, "square"),
        (-400, "square"),
    ]
    assert fees(pg_conninfo) == [(50, f"payment:{payments[1]['id']}")]


def test_the_sweep_deletes_the_link_of_a_request_paid_by_hand(
    client: TestClient, square: respx.MockRouter
) -> None:
    """#5700: a manual payment marks the Square page to close; when the delete fails, the
    housekeeping sweep calls `cancel_request` again until Square deletes the link."""
    square["delete"].respond(
        500, json={"errors": [{"category": "API_ERROR", "code": "INTERNAL_SERVER_ERROR"}]}
    )
    order_id, req = square_request(client)
    pay_by_hand(client, order_id, req["id"])
    assert square["delete"].call_count == 1
    [marked] = client.get(f"/api/v1/orders/{order_id}/payment-requests").json()
    assert marked["close_pending_at"] is not None

    square["delete"].respond(json=payload("api.payment_link.deleted.json"))
    _due(client, req["id"])
    service = _state(client).components.get(PAYMENTS).service
    assert asyncio.run(service.retry_closes()) == 1
    assert square["delete"].call_count == 2
    [closed] = client.get(f"/api/v1/orders/{order_id}/payment-requests").json()
    assert closed["close_pending_at"] is None
