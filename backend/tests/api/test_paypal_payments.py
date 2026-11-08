"""PayPal through the real routes (#4959): connect, request, approve and capture, pay, a
pending capture that settles, a declined one, a cancel and two part refunds, from the
recorded payloads in ``tests/payments/paypal_payloads``. respx answers every PayPal call;
nothing reaches the network."""

from __future__ import annotations

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
from scadbuddy.payments.paypal import API, CANCELLED
from tests.api.test_order_payments import payable_order, request_payment, with_payments
from tests.payments.test_paypal import (
    CAPTURE,
    CLIENT_ID,
    HEADERS,
    ORDER,
    PAYLOADS,
    SECRET,
    WEBHOOK_ID,
    payload,
)

pytestmark = pytest.mark.requires_postgres

HOOK = "/api/v1/payments/webhooks/paypal"
CONNECT = {
    "settings": {"environment": "sandbox", "client_id": CLIENT_ID, "webhook_id": WEBHOOK_ID},
    "secrets": {"client_secret": SECRET},
}


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    return with_payments(settings, tmp_path)


@pytest.fixture
def paypal() -> Iterator[respx.MockRouter]:
    """PayPal's sandbox API as the recorded answers. Only api-m.sandbox.paypal.com is
    mocked; the test client's own requests never leave the process."""
    with respx.mock(base_url=API["sandbox"], assert_all_called=False) as router:
        router.post("/v1/oauth2/token").respond(json=payload("api.oauth2.token.json"))
        router.get(f"/v1/notifications/webhooks/{WEBHOOK_ID}").respond(
            json=payload("api.webhook.json")
        )
        router.post("/v1/notifications/verify-webhook-signature", name="verify").respond(
            json=payload("api.verify.success.json")
        )
        router.post("/v2/checkout/orders", name="create").respond(
            201, json=payload("api.order.created.json")
        )
        router.post(f"/v2/checkout/orders/{ORDER}/capture", name="capture").respond(
            201, json=payload("api.order.captured.json")
        )
        router.patch(f"/v2/checkout/orders/{ORDER}", name="patch").respond(204)
        router.get(f"/v2/payments/captures/{CAPTURE}").respond(json=payload("api.capture.json"))
        yield router


def send(client: TestClient, name: str) -> httpx.Response:
    body = (PAYLOADS / name).read_bytes()
    answer: httpx.Response = client.post(HOOK, content=body, headers=HEADERS)
    return answer


def paypal_request(client: TestClient) -> tuple[str, dict[str, Any]]:
    connected = client.put("/api/v1/payments/providers/paypal", json=CONNECT)
    assert connected.status_code == 200, connected.text
    # The settings page shows this as "Connected as …": sandbox or live.
    assert connected.json()["account_label"] == "PayPal app APP-80W284485P519543T (sandbox)"
    assert SECRET not in connected.text
    order = payable_order(client, 700)
    req = request_payment(client, order["id"], provider="paypal")
    assert (req["provider_ref"], req["status"]) == (ORDER, "open")
    assert req["hosted_url"] == f"https://www.sandbox.paypal.com/checkoutnow?token={ORDER}"
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


def test_approve_capture_pay_with_its_fee_and_two_part_refunds(
    client: TestClient, paypal: respx.MockRouter, pg_conninfo: str
) -> None:
    order_id, req = paypal_request(client)
    created = paypal["create"].calls.last.request
    assert created.headers["paypal-request-id"] == req["id"]
    assert json.loads(created.content)["purchase_units"][0]["custom_id"] == req["id"]

    assert send(client, "checkout.order.approved.json").json() == {"outcome": "ignored"}
    assert paypal["capture"].called
    assert detail(client, order_id)["payments"] == []

    assert send(client, "payment.capture.completed.json").json() == {"outcome": "recorded"}
    found = detail(client, order_id)
    assert found["payment_requests"][0]["status"] == "paid"
    assert found["totals"]["balance_minor"] == 0
    [paid] = found["payments"]
    assert (paid["amount_minor"], paid["method"], paid["reference"]) == (700, "paypal", ORDER)
    assert fees(pg_conninfo) == [(69, f"payment:{paid['id']}")]
    assert send(client, "payment.capture.completed.json").json() == {"outcome": "replayed"}

    assert send(client, "payment.capture.refunded.json").json() == {"outcome": "recorded"}
    assert send(client, "payment.capture.refunded.second.json").json() == {"outcome": "recorded"}
    assert send(client, "payment.capture.refunded.json").json() == {"outcome": "replayed"}
    payments = detail(client, order_id)["payments"]
    assert [p["amount_minor"] for p in payments] == [700, -400, -300]


def test_a_pending_capture_is_pending_until_it_completes(
    client: TestClient, paypal: respx.MockRouter
) -> None:
    order_id, _ = paypal_request(client)
    assert send(client, "payment.capture.pending.json").json() == {"outcome": "recorded"}
    assert detail(client, order_id)["payment_requests"][0]["status"] == "pending"
    assert detail(client, order_id)["payments"] == []
    assert send(client, "payment.capture.completed.json").json() == {"outcome": "recorded"}
    assert detail(client, order_id)["payment_requests"][0]["status"] == "paid"


def test_a_declined_capture_records_no_money(client: TestClient, paypal: respx.MockRouter) -> None:
    order_id, _ = paypal_request(client)
    send(client, "payment.capture.pending.json")
    assert send(client, "payment.capture.declined.json").json() == {"outcome": "recorded"}
    assert detail(client, order_id)["payment_requests"][0]["status"] == "failed"
    assert detail(client, order_id)["payments"] == []


def test_cancelling_a_request_marks_its_order(client: TestClient, paypal: respx.MockRouter) -> None:
    order_id, req = paypal_request(client)
    cancelled = client.post(f"/api/v1/orders/{order_id}/payment-requests/{req['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    [patch] = json.loads(paypal["patch"].calls.last.request.content)
    assert patch["value"] == CANCELLED
    # PayPal's approval of the marked order is never captured.
    send(client, "checkout.order.approved.cancelled.json")
    assert not paypal["capture"].called


def test_paypal_down_while_verifying_is_503_and_paypal_retries(
    client: TestClient, paypal: respx.MockRouter, pg_conninfo: str
) -> None:
    paypal_request(client)
    paypal["verify"].respond(500, json={"name": "INTERNAL_SERVICE_ERROR"})
    refused = send(client, "payment.capture.completed.json")
    assert refused.status_code == 503
    assert refused.json()["type"] == "https://scadbuddy.dev/problems/payments/provider_unavailable"
    with psycopg.connect(pg_conninfo) as conn:
        row = conn.execute("SELECT count(*) FROM payment_events").fetchone()
    assert row is not None and row[0] == 0
    paypal["verify"].respond(json=payload("api.verify.success.json"))
    assert send(client, "payment.capture.completed.json").json() == {"outcome": "recorded"}


def test_a_forged_event_records_nothing(client: TestClient, paypal: respx.MockRouter) -> None:
    order_id, _ = paypal_request(client)
    paypal["verify"].respond(json=payload("api.verify.failure.json"))
    refused = send(client, "payment.capture.completed.json")
    assert refused.status_code == 400
    assert refused.json()["type"] == "https://scadbuddy.dev/problems/payments/bad_signature"
    assert detail(client, order_id)["payments"] == []
