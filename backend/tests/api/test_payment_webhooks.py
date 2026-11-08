"""`POST /api/v1/payments/webhooks/{provider}` (#4957): the one back-office route the
public reaches. A bad signature records nothing; a replayed event records one payment."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from scadbuddy.payments.fake import FAKE, signed_event
from scadbuddy.payments.provider import ProviderUnavailableError
from tests.api.test_auth_enforce import ENABLED, HEADER, PUBLIC, _app, _sign_in
from tests.api.test_order_payments import (
    connect_fake,
    payable_order,
    request_payment,
    with_payments,
)
from tests.auth.fake_oidc import CLIENT_ID, ISSUER, FakeIssuer


def fake_status(client: TestClient) -> dict[str, Any]:
    found = client.get("/api/v1/payments/providers").json()
    return next(s for s in found if s["id"] == "fake")


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
    # #5681: the refund fails; a payment cancels it and the balance is paid again.
    failed = {
        "id": "e3", "type": "refund_failed", "provider_ref": req["provider_ref"],
        "currency": "USD", "reverses": "e2",
    }  # fmt: skip
    body, headers = signed_event("s3cret", failed)
    assert client.post(HOOK, content=body, headers=headers).json() == {"outcome": "recorded"}
    detail = client.get(f"/api/v1/orders/{order_id}").json()
    assert [p["amount_minor"] for p in detail["payments"]] == [req["amount_minor"], -400, 400]
    assert detail["totals"]["balance_minor"] == 0
    assert detail["payment_requests"][0]["status"] == "paid"
    status = fake_status(client)
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
    status = fake_status(client)
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


def test_a_provider_that_does_not_answer_after_the_signature_is_503_and_records_nothing(
    client: TestClient, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#4958: the adapter may call its provider once the signature holds. When that call
    fails, nothing is recorded, so the provider sends the event again."""
    order_id, req = linked_request(client)

    async def down(*args: object, **kwargs: object) -> object:
        raise ProviderUnavailableError("the provider did not answer")

    monkeypatch.setattr(FAKE, "parse_webhook", down)
    body, headers = signed_event("s3cret", paid(req))
    refused = client.post(HOOK, content=body, headers=headers)
    assert refused.status_code == 503
    assert refused.json()["type"] == "https://scadbuddy.dev/problems/payments/provider_unavailable"
    with psycopg.connect(pg_conninfo) as conn:
        row = conn.execute("SELECT count(*) FROM payment_events").fetchone()
    assert row is not None and row[0] == 0
    assert client.get(f"/api/v1/orders/{order_id}").json()["payments"] == []


# -- the last webhook failure in Settings (#5684) ------------------------------------------


def _webhook_error(client: TestClient) -> tuple[str | None, bool]:
    status = fake_status(client)
    return status["last_webhook_error"], status["last_webhook_error_at"] is not None


def test_a_failed_webhook_is_shown_by_its_category_and_cleared_by_the_next(
    client: TestClient,
) -> None:
    _, req = linked_request(client)
    assert _webhook_error(client) == (None, False)
    body, headers = signed_event("wrong", paid(req))
    assert client.post(HOOK, content=body, headers=headers).status_code == 400
    assert _webhook_error(client) == ("signature", True)
    body, headers = signed_event("s3cret", paid(req))
    assert client.post(HOOK, content=body, headers=headers).status_code == 200
    assert _webhook_error(client) == (None, False)


def test_a_malformed_event_is_a_handler_error_and_stores_no_text(
    client: TestClient, pg_conninfo: str
) -> None:
    connect_fake(client)
    body, headers = signed_event("s3cret", {"type": "paid", "secret_marker": "zz9"})
    assert client.post(HOOK, content=body, headers=headers).status_code == 400
    assert _webhook_error(client) == ("handler_error", True)
    with psycopg.connect(pg_conninfo) as conn:
        row = conn.execute("SELECT payment_credentials::text FROM payment_credentials").fetchone()
    assert row is not None and "zz9" not in row[0] and "malformed" not in row[0]


def test_an_unreadable_credential_is_a_handler_error(client: TestClient, pg_conninfo: str) -> None:
    _, req = linked_request(client)
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute("UPDATE payment_credentials SET secret_sealed = '\\x00'")
    body, headers = signed_event("s3cret", paid(req))
    assert client.post(HOOK, content=body, headers=headers).status_code == 503
    assert _webhook_error(client) == ("handler_error", True)


def test_a_provider_that_does_not_answer_is_provider_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, req = linked_request(client)

    async def down(*args: object, **kwargs: object) -> object:
        raise ProviderUnavailableError("the provider did not answer: sk_test_zz9")

    monkeypatch.setattr(FAKE, "parse_webhook", down)
    body, headers = signed_event("s3cret", paid(req))
    assert client.post(HOOK, content=body, headers=headers).status_code == 503
    assert _webhook_error(client) == ("provider_unavailable", True)


def test_a_fee_before_its_payment_is_provider_unavailable(client: TestClient) -> None:
    """The event is sent back for a retry (``EventTooEarlyError``), as a provider outage."""
    _, req = linked_request(client)
    fee = {
        "id": "f1", "type": "fee", "provider_ref": req["provider_ref"], "fee_minor": 31,
        "created_at": datetime.now(UTC).isoformat(),
    }  # fmt: skip
    body, headers = signed_event("s3cret", fee)
    assert client.post(HOOK, content=body, headers=headers).status_code == 503
    assert _webhook_error(client) == ("provider_unavailable", True)


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
