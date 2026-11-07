"""`POST /api/v1/payments/webhooks/{provider}` (#4957): the one back-office route the
public reaches. A bad signature records nothing; a replayed event records one payment."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from scadbuddy.payments.fake import signed_event
from tests.api.test_auth_enforce import ENABLED, HEADER, PUBLIC, _app, _sign_in
from tests.api.test_order_payments import (
    connect_fake,
    payable_order,
    request_payment,
    with_payments,
)
from tests.auth.fake_oidc import CLIENT_ID, ISSUER, FakeIssuer

pytestmark = pytest.mark.requires_postgres

HOOK = "/api/v1/payments/webhooks/fake"


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    return with_payments(settings, tmp_path)


def paid(req: dict[str, Any], event_id: str = "e1", **fields: Any) -> dict[str, Any]:
    return {
        "id": event_id,
        "type": "paid",
        "provider_ref": req["provider_ref"],
        "amount_minor": req["amount_minor"],
        "currency": req["currency"],
        **fields,
    }


def linked_request(client: TestClient) -> tuple[str, dict[str, Any]]:
    connect_fake(client)
    order = payable_order(client)
    return order["id"], request_payment(client, order["id"])


def test_the_fake_provider_drives_a_request_through_paid_and_refunded(
    client: TestClient,
) -> None:
    order_id, req = linked_request(client)
    assert req["provider"] == "fake"
    assert req["hosted_url"].startswith("https://payments.fake.invalid/pay/")
    body, headers = signed_event("s3cret", paid(req, fee_minor=31))
    answer = client.post(HOOK, content=body, headers=headers)
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"outcome": "recorded"}
    detail = client.get(f"/api/v1/orders/{order_id}").json()
    assert detail["payment_requests"][0]["status"] == "paid"
    assert detail["totals"]["balance_minor"] == 0
    refund = {
        "id": "e2",
        "type": "refunded",
        "provider_ref": req["provider_ref"],
        "amount_minor": 400,
        "currency": "USD",
    }
    body, headers = signed_event("s3cret", refund)
    assert client.post(HOOK, content=body, headers=headers).json() == {"outcome": "recorded"}
    payments = client.get(f"/api/v1/orders/{order_id}").json()["payments"]
    assert [p["amount_minor"] for p in payments] == [req["amount_minor"], -400]
    [status] = client.get("/api/v1/payments/providers").json()
    assert status["last_webhook_at"] is not None


def test_a_bad_signature_is_400_and_records_nothing(client: TestClient, pg_conninfo: str) -> None:
    order_id, req = linked_request(client)
    body, headers = signed_event("wrong", paid(req))
    refused = client.post(HOOK, content=body, headers=headers)
    assert refused.status_code == 400
    assert refused.json()["type"] == "https://scadbuddy.dev/problems/payments/bad_signature"
    assert "s3cret" not in refused.text
    with psycopg.connect(pg_conninfo) as conn:
        row = conn.execute("SELECT count(*) FROM payment_events").fetchone()
    assert row is not None and row[0] == 0
    assert client.get(f"/api/v1/orders/{order_id}").json()["payments"] == []
    [status] = client.get("/api/v1/payments/providers").json()
    assert status["last_webhook_at"] is None


def test_a_replayed_event_records_one_payment(client: TestClient) -> None:
    order_id, req = linked_request(client)
    body, headers = signed_event("s3cret", paid(req))
    first = client.post(HOOK, content=body, headers=headers)
    second = client.post(HOOK, content=body, headers=headers)
    assert [r.json()["outcome"] for r in (first, second)] == ["recorded", "replayed"]
    assert len(client.get(f"/api/v1/orders/{order_id}").json()["payments"]) == 1


def test_an_event_for_no_request_is_200_unmatched(client: TestClient) -> None:
    connect_fake(client)
    body, headers = signed_event(
        "s3cret", {"id": "e9", "type": "paid", "provider_ref": "fake_none", "currency": "USD"}
    )
    assert client.post(HOOK, content=body, headers=headers).json() == {"outcome": "unmatched"}


def test_a_signed_but_malformed_body_is_400(client: TestClient) -> None:
    connect_fake(client)
    body, headers = signed_event("s3cret", {"type": "paid"})
    refused = client.post(HOOK, content=body, headers=headers)
    assert refused.status_code == 400
    assert refused.json()["type"] == "https://scadbuddy.dev/problems/payments/malformed_event"


def test_an_unknown_provider_is_404_and_a_huge_body_413(client: TestClient) -> None:
    body, headers = signed_event("s3cret", {"id": "e1", "type": "paid"})
    # Offered but not connected, then not offered at all.
    assert client.post(HOOK, content=body, headers=headers).status_code == 404
    unknown = client.post("/api/v1/payments/webhooks/stripe", content=body, headers=headers)
    assert unknown.status_code == 404
    assert unknown.json()["type"] == "https://scadbuddy.dev/problems/payments/provider_not_found"
    connect_fake(client)
    huge = b"x" * (256 * 1024 + 1)
    too_big = client.post(HOOK, content=huge, headers={"content-type": "application/json"})
    assert too_big.status_code == 413


def test_a_huge_body_with_no_length_is_413(client: TestClient) -> None:
    """A streamed body states no Content-Length: it is counted as it arrives."""
    connect_fake(client)

    def chunks() -> Iterator[bytes]:
        for _ in range(5):
            yield b"x" * (64 * 1024)

    too_big = client.post(HOOK, content=chunks(), headers={"content-type": "application/json"})
    assert too_big.request.headers.get("content-length") is None
    assert too_big.status_code == 413


def test_a_missing_kek_is_503_and_says_nothing_of_the_setup(
    client: TestClient, settings: Settings
) -> None:
    order_id, req = linked_request(client)
    body, headers = signed_event("s3cret", paid(req))
    with TestClient(create_app(settings.model_copy(update={"secret_key_file": None}))) as bare:
        refused = bare.post(HOOK, content=body, headers=headers)
    assert refused.status_code == 503
    assert refused.json()["type"] == "https://scadbuddy.dev/problems/payments/credential_unreadable"
    assert refused.json()["detail"] == "the provider's credential is unavailable"
    assert "SCADBUDDY" not in refused.text
    assert client.get(f"/api/v1/orders/{order_id}").json()["payments"] == []


def test_an_unreadable_credential_is_503(client: TestClient, pg_conninfo: str) -> None:
    order_id, req = linked_request(client)
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute("UPDATE payment_credentials SET secret_sealed = '\\x00'")
    body, headers = signed_event("s3cret", paid(req))
    refused = client.post(HOOK, content=body, headers=headers)
    assert refused.status_code == 503
    assert "s3cret" not in refused.text
    assert client.get(f"/api/v1/orders/{order_id}").json()["payments"] == []


# -- enforce ------------------------------------------------------------------------------


@pytest.fixture
def issuer() -> FakeIssuer:
    return FakeIssuer()


def enforce_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"public_url": PUBLIC, "auth_oidc_issuer": ISSUER, "auth_oidc_client_id": CLIENT_ID}
    )


@contextmanager
def enforced_app(settings: Settings, issuer: FakeIssuer) -> Iterator[TestClient]:
    """An app in `enforce`, with sign-in set up in `off` first, as `test_auth_enforce.py`
    `_configure` does."""
    running = enforce_settings(settings)
    with TestClient(_app(running, "off", issuer.transport()), base_url=PUBLIC) as client:
        assert client.put("/api/v1/auth/config", json=ENABLED).status_code == 200
    with TestClient(_app(running, "enforce", issuer.transport()), base_url=PUBLIC) as client:
        yield client


@pytest.fixture
def enforced(settings: Settings, issuer: FakeIssuer) -> Iterator[TestClient]:
    with enforced_app(settings, issuer) as client:
        yield client


def test_the_webhook_needs_no_session_in_enforce(enforced: TestClient) -> None:
    _sign_in(enforced)
    assert (
        enforced.put(
            "/api/v1/payments/providers/fake",
            json={"settings": {"account": "a"}, "secrets": {"signing_secret": "s3cret"}},
            headers=HEADER,
        ).status_code
        == 200
    )
    enforced.cookies.clear()
    body, headers = signed_event("s3cret", {"id": "e1", "type": "paid", "provider_ref": "x"})
    answer = enforced.post(HOOK, content=body, headers=headers)
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"outcome": "unmatched"}
    # Every other payments route still wants a person.
    assert enforced.get("/api/v1/payments/providers").status_code == 401
