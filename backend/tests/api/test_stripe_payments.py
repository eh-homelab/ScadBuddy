"""Stripe through the real routes (#4958): connect, request, pay, a bank debit that settles
or fails, a late fee and a refund, from the recorded payloads in
``tests/payments/stripe_payloads``. respx answers every Stripe call; nothing reaches the
network."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.payments.stripe import API
from tests.api.test_order_payments import payable_order, request_payment, with_payments
from tests.payments.test_stripe import CS, KEY, PAYLOADS, PI, TXN, WHSEC, payload, signed

pytestmark = pytest.mark.requires_postgres

HOOK = "/api/v1/payments/webhooks/stripe"
CONNECT = {"settings": {}, "secrets": {"secret_key": KEY, "webhook_secret": WHSEC}}


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    return with_payments(settings, tmp_path)


@pytest.fixture
def stripe() -> Iterator[respx.MockRouter]:
    """Stripe's API as the recorded answers. Only api.stripe.com is mocked; the test
    client's own requests never leave the process."""
    with respx.mock(base_url=API, assert_all_called=False) as router:
        # The session lookup first: it sends limit=1 too.
        router.get("/v1/checkout/sessions", params={"payment_intent": PI}).respond(
            json=payload("api.checkout_sessions.list.json")
        )
        router.get("/v1/checkout/sessions", params={"limit": "1"}).respond(
            json=payload("api.checkout_sessions.list.empty.json")
        )
        router.post("/v1/checkout/sessions").respond(
            json=payload("api.checkout_session.created.json")
        )
        router.get(f"/v1/payment_intents/{PI}", name="intent").respond(
            json=payload("api.payment_intent.no_balance_transaction.json")
        )
        router.get(f"/v1/balance_transactions/{TXN}").respond(
            json=payload("api.balance_transaction.json")
        )
        router.post(f"/v1/checkout/sessions/{CS}/expire", name="expire").respond(
            json=payload("api.checkout_session.expired.json")
        )
        yield router


def send(client: TestClient, name: str, *, now: bool = False) -> httpx.Response:
    """The recorded event ``name``; with ``now``, made by Stripe just now."""
    body = (PAYLOADS / name).read_bytes()
    if now:
        body = json.dumps(json.loads(body) | {"created": int(time.time())}).encode()
    answer: httpx.Response = client.post(HOOK, content=body, headers=signed(body))
    return answer


def stripe_request(client: TestClient) -> tuple[str, dict[str, Any]]:
    connected = client.put("/api/v1/payments/providers/stripe", json=CONNECT)
    assert connected.status_code == 200, connected.text
    assert connected.json()["account_label"] == "Stripe account (test mode)"
    assert KEY not in connected.text and WHSEC not in connected.text
    order = payable_order(client, 700)
    req = request_payment(client, order["id"], provider="stripe")
    assert (req["provider_ref"], req["status"]) == (CS, "open")
    assert req["hosted_url"].startswith("https://checkout.stripe.com/c/pay/")
    return order["id"], req


def request_status(client: TestClient, order_id: str) -> str:
    status: str = client.get(f"/api/v1/orders/{order_id}").json()["payment_requests"][0]["status"]
    return status


def fees(pg_conninfo: str) -> list[tuple[int, str]]:
    with psycopg.connect(pg_conninfo) as conn:
        rows = conn.execute(
            "SELECT amount_minor, source FROM order_cost_entries WHERE kind = 'payment_fee'"
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def test_a_card_payment_a_late_fee_and_two_part_refunds(
    client: TestClient, stripe: respx.MockRouter, pg_conninfo: str
) -> None:
    order_id, _ = stripe_request(client)
    sent = stripe.calls[-1].request
    assert (
        sent.headers["idempotency-key"]
        == client.get(f"/api/v1/orders/{order_id}").json()["payment_requests"][0]["id"]
    )

    assert send(client, "checkout.session.completed.card.json").json() == {"outcome": "recorded"}
    detail = client.get(f"/api/v1/orders/{order_id}").json()
    assert detail["payment_requests"][0]["status"] == "paid"
    assert detail["totals"]["balance_minor"] == 0
    [paid] = detail["payments"]
    assert (paid["amount_minor"], paid["method"], paid["reference"]) == (700, "stripe", CS)
    # Stripe had no balance transaction yet: the fee comes with charge.updated.
    assert fees(pg_conninfo) == []
    assert send(client, "charge.updated.balance_transaction.json").json() == {"outcome": "recorded"}
    assert fees(pg_conninfo) == [(50, f"payment:{paid['id']}")]
    assert send(client, "charge.updated.balance_transaction.json").json() == {"outcome": "replayed"}

    assert send(client, "refund.created.json").json() == {"outcome": "recorded"}
    assert send(client, "refund.created.second.json").json() == {"outcome": "recorded"}
    # The same Refund told again by refund.updated records nothing more.
    assert send(client, "refund.updated.metadata.json").json() == {"outcome": "replayed"}
    payments = client.get(f"/api/v1/orders/{order_id}").json()["payments"]
    assert [p["amount_minor"] for p in payments] == [700, -400, -300]
    assert fees(pg_conninfo) == [(50, f"payment:{paid['id']}")]


def test_a_bank_debit_is_pending_until_it_settles(
    client: TestClient, stripe: respx.MockRouter
) -> None:
    order_id, _ = stripe_request(client)
    assert send(client, "checkout.session.completed.ach.json").json() == {"outcome": "recorded"}
    assert request_status(client, order_id) == "pending"
    assert client.get(f"/api/v1/orders/{order_id}").json()["payments"] == []
    assert send(client, "checkout.session.async_payment_succeeded.json").json() == {
        "outcome": "recorded"
    }
    assert request_status(client, order_id) == "paid"
    assert [
        p["amount_minor"] for p in client.get(f"/api/v1/orders/{order_id}").json()["payments"]
    ] == [700]


def test_a_bank_debit_that_fails_records_no_money(
    client: TestClient, stripe: respx.MockRouter
) -> None:
    order_id, _ = stripe_request(client)
    send(client, "checkout.session.completed.ach.json")
    assert send(client, "checkout.session.async_payment_failed.json").json() == {
        "outcome": "recorded"
    }
    assert request_status(client, order_id) == "failed"
    assert client.get(f"/api/v1/orders/{order_id}").json()["payments"] == []
    # A failed request no longer needs the webhook secret, so Stripe can be disconnected.
    assert client.delete("/api/v1/payments/providers/stripe").status_code == 204


def test_a_pending_request_keeps_stripe_connected(
    client: TestClient, stripe: respx.MockRouter
) -> None:
    stripe_request(client)
    send(client, "checkout.session.completed.ach.json")
    refused = client.delete("/api/v1/payments/providers/stripe")
    assert refused.status_code == 409


def test_cancelling_a_request_expires_its_session(
    client: TestClient, stripe: respx.MockRouter
) -> None:
    order_id, req = stripe_request(client)
    cancelled = client.post(f"/api/v1/orders/{order_id}/payment-requests/{req['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert stripe["expire"].called


def test_stripe_down_after_the_signature_is_503_and_stripe_retries(
    client: TestClient, stripe: respx.MockRouter, pg_conninfo: str
) -> None:
    stripe_request(client)
    stripe["intent"].respond(status_code=500, json={})
    refused = send(client, "checkout.session.completed.card.json")
    assert refused.status_code == 503
    with psycopg.connect(pg_conninfo) as conn:
        row = conn.execute("SELECT count(*) FROM payment_events").fetchone()
    assert row is not None and row[0] == 0
    stripe["intent"].respond(json=payload("api.payment_intent.expanded.json"))
    assert send(client, "checkout.session.completed.card.json").json() == {"outcome": "recorded"}
    assert fees(pg_conninfo)[0][0] == 50


def test_a_fee_that_arrives_before_its_payment_is_sent_again(
    client: TestClient, stripe: respx.MockRouter, pg_conninfo: str
) -> None:
    """``charge.updated`` can land before the paid event commits: it answers 503 with
    nothing recorded, and Stripe's retry records the fee."""
    stripe_request(client)
    early = send(client, "charge.updated.balance_transaction.json", now=True)
    assert early.status_code == 503
    assert early.json()["type"] == "https://scadbuddy.dev/problems/payments/provider_unavailable"
    assert send(client, "checkout.session.completed.card.json").json() == {"outcome": "recorded"}
    assert send(client, "charge.updated.balance_transaction.json", now=True).json() == {
        "outcome": "recorded"
    }
    assert [f[0] for f in fees(pg_conninfo)] == [50]


def test_a_fee_whose_payment_never_came_is_ignored_after_a_day(
    client: TestClient, stripe: respx.MockRouter, pg_conninfo: str
) -> None:
    """The recorded event was made long ago: no 503 for ever, it is recorded as ignored."""
    stripe_request(client)
    late = send(client, "charge.updated.balance_transaction.json")
    assert late.json() == {"outcome": "ignored"}
    assert fees(pg_conninfo) == []


def test_a_forged_event_records_nothing(client: TestClient, stripe: respx.MockRouter) -> None:
    order_id, _ = stripe_request(client)
    body = (PAYLOADS / "checkout.session.completed.card.json").read_bytes()
    refused = client.post(HOOK, content=body, headers=signed(body, secret="whsec_forged"))
    assert refused.status_code == 400
    assert client.get(f"/api/v1/orders/{order_id}").json()["payments"] == []


def send_refund(client: TestClient, kind: str, **refund: Any) -> httpx.Response:
    """``refund.failed.json`` as ``kind``, made just now, its Refund's fields replaced."""
    event = payload("refund.failed.json")
    event["type"] = kind
    event["created"] = int(time.time())
    event["data"]["object"] |= refund
    body = json.dumps(event).encode()
    answer: httpx.Response = client.post(HOOK, content=body, headers=signed(body))
    return answer


def test_a_refund_that_failed_after_it_succeeded_is_reversed(
    client: TestClient, stripe: respx.MockRouter
) -> None:
    """#5681: the money went back to the balance (``failure_balance_transaction``), so a
    payment cancels the refund and the balance is paid again."""
    order_id, _ = stripe_request(client)
    send(client, "checkout.session.completed.card.json")
    assert send(client, "refund.updated.succeeded.json").json() == {"outcome": "recorded"}
    assert send_refund(client, "refund.failed").json() == {"outcome": "recorded"}
    detail = client.get(f"/api/v1/orders/{order_id}").json()
    assert [p["amount_minor"] for p in detail["payments"]] == [700, -700, 700]
    assert detail["totals"]["balance_minor"] == 0


@pytest.mark.parametrize(
    ("kind", "status", "failure_txn"),
    [
        ("refund.failed", "failed", None),
        ("refund.updated", "failed", None),
        ("refund.updated", "canceled", "txn_3refundfail"),
        ("refund.updated", "canceled", None),
    ],
)
def test_a_refund_that_failed_while_pending_answers_200_and_writes_nothing(
    client: TestClient, stripe: respx.MockRouter, kind: str, status: str, failure_txn: str | None
) -> None:
    """A refund that never took money (pending, then failed or cancelled) reverses
    nothing; the webhook answers 200 at once, not a 503 for a day."""
    order_id, _ = stripe_request(client)
    send(client, "checkout.session.completed.card.json")
    assert send(client, "refund.created.pending.json").status_code == 200
    answer = send_refund(client, kind, status=status, failure_balance_transaction=failure_txn)
    assert answer.status_code == 200, answer.text
    payments = client.get(f"/api/v1/orders/{order_id}").json()["payments"]
    assert [p["amount_minor"] for p in payments] == [700]
    provider = next(
        s for s in client.get("/api/v1/payments/providers").json() if s["id"] == "stripe"
    )
    assert provider["last_webhook_error"] is None
