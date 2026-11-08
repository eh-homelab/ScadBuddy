"""`/api/v1/orders/{order_id}/bank-deposits` (#4961): the order page's suggested deposits, and
the shop's "Record this deposit as payment" and "Not this order". The deposit and its
suggestion are written as the sync writes them (``tests/payments/test_deposits.py`` runs
the sync itself against Plaid's recorded payloads)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.auth.service_credentials import TokenReader
from scadbuddy.core.settings import Settings
from scadbuddy.payments.component import PAYMENTS
from tests.api.test_order_payments import payable_order, with_payments
from tests.api.test_payment_webhooks import enforced_app
from tests.auth.fake_oidc import FakeIssuer

pytestmark = pytest.mark.requires_postgres

PROBLEMS = "https://scadbuddy.dev/problems/orders/"


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    return with_payments(settings, tmp_path)


def suggest(client: TestClient, order_id: str, tx_id: str = "t1", amount: int = 1000) -> None:
    """A deposit the sync kept and suggested for ``order_id``."""
    app = client.app
    assert isinstance(app, FastAPI)
    store = getattr(app.state, STATE_ATTR).components.get(PAYMENTS).bank.store
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO bank_deposits (transaction_id, item_id, amount_minor, currency,"
            " posted_on, description) VALUES (%s, 'item-1', %s, 'USD', '2026-11-06',"
            " 'ACH ACCT •••')",
            (tx_id, amount),
        )
        conn.execute(
            "INSERT INTO bank_deposit_matches (transaction_id, order_id, reason)"
            " VALUES (%s, %s, 'amount')",
            (tx_id, order_id),
        )


def test_a_suggestion_is_listed_then_recorded_once(client: TestClient) -> None:
    order = payable_order(client)
    deposits = f"/api/v1/orders/{order['id']}/bank-deposits"
    assert client.get(deposits).json() == []
    suggest(client, order["id"])
    [shown] = client.get(deposits).json()
    assert shown == {
        "transaction_id": "t1",
        "amount_minor": 1000,
        "currency": "USD",
        "posted_on": "2026-11-06",
        "description": "ACH ACCT •••",
        "reason": "amount",
    }
    # Listing records nothing.
    assert client.get(f"/api/v1/orders/{order['id']}").json()["payments"] == []
    recorded = client.post(f"{deposits}/t1/record")
    assert recorded.status_code == 201, recorded.text
    payment = recorded.json()
    assert (payment["amount_minor"], payment["method"], payment["reference"]) == (
        1000,
        "bank_transfer",
        "t1",
    )
    got = client.get(f"/api/v1/orders/{order['id']}").json()
    assert [p["id"] for p in got["payments"]] == [payment["id"]]
    assert got["totals"]["balance_minor"] == 0
    assert client.get(deposits).json() == []
    again = client.post(f"{deposits}/t1/record")
    assert (again.status_code, again.json()["type"]) == (409, PROBLEMS + "deposit_recorded")


def test_not_this_order_hides_it(client: TestClient) -> None:
    order = payable_order(client)
    deposits = f"/api/v1/orders/{order['id']}/bank-deposits"
    suggest(client, order["id"])
    assert client.post(f"{deposits}/t1/dismiss").status_code == 204
    assert client.get(deposits).json() == []
    refused = client.post(f"{deposits}/t1/record")
    assert (refused.status_code, refused.json()["type"]) == (
        404,
        PROBLEMS + "deposit_not_suggested",
    )


def test_an_unknown_order_or_deposit_is_404(client: TestClient) -> None:
    missing = client.get("/api/v1/orders/nowhere/bank-deposits")
    assert (missing.status_code, missing.json()["type"]) == (404, PROBLEMS + "order_not_found")
    order = payable_order(client)
    unknown = client.post(f"/api/v1/orders/{order['id']}/bank-deposits/none/dismiss")
    assert (unknown.status_code, unknown.json()["type"]) == (
        404,
        PROBLEMS + "deposit_not_suggested",
    )


def test_an_agent_reads_suggestions_and_records_none(settings: Settings) -> None:
    with enforced_app(settings, FakeIssuer()) as client:
        app = client.app
        assert isinstance(app, FastAPI)
        kek = getattr(app.state, STATE_ATTR).settings_store.kek
        token = TokenReader("agent", settings.database_url, kek).token()
        agent = {"Authorization": f"Bearer {token}"}
        deposits = "/api/v1/orders/o1/bank-deposits"
        for answer in (
            client.post(f"{deposits}/t1/record", headers=agent),
            client.post(f"{deposits}/t1/dismiss", headers=agent),
        ):
            assert answer.status_code == 403, answer.text
            assert answer.json()["reason"] == "not_a_person"
        assert client.get(deposits, headers=agent).status_code == 404
