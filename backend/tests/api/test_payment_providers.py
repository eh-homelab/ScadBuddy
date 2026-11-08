"""`/api/v1/payments/providers` (#4957): connect, disconnect and choose the default. No
answer carries a secret; only a person changes them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.auth.service_credentials import TokenReader
from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from scadbuddy.payments.fake import FAKE
from scadbuddy.payments.provider import ProviderError
from tests.api.test_auth_enforce import HEADER, _sign_in
from tests.api.test_order_payments import (
    FAKE_CONNECT,
    connect_fake,
    payable_order,
    request_payment,
    with_payments,
)
from tests.api.test_payment_webhooks import enforced_app
from tests.auth.fake_oidc import FakeIssuer

pytestmark = pytest.mark.requires_postgres

PROBLEMS = "https://scadbuddy.dev/problems/payments/"
PROVIDER = "/api/v1/payments/providers/fake"


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    return with_payments(settings, tmp_path)


def fake_status(client: TestClient) -> dict[str, Any]:
    """The fake provider's row; Stripe (#4958) is offered beside it."""
    found = client.get("/api/v1/payments/providers").json()
    return next(s for s in found if s["id"] == "fake")


def test_stripe_is_offered_with_its_two_secrets(client: TestClient) -> None:
    [stripe] = [s for s in client.get("/api/v1/payments/providers").json() if s["id"] == "stripe"]
    assert (stripe["label"], stripe["connected"]) == ("Stripe", False)
    assert [(f["name"], f["secret"]) for f in stripe["fields"]] == [
        ("secret_key", True),
        ("webhook_secret", True),
    ]


def test_the_page_never_sees_a_secret(client: TestClient) -> None:
    before = fake_status(client)
    assert (before["id"], before["connected"], before["is_default"]) == ("fake", False, False)
    assert [f["name"] for f in before["fields"]] == ["account", "signing_secret"]
    connected = client.put(PROVIDER, json=FAKE_CONNECT)
    assert connected.status_code == 200, connected.text
    assert "s3cret" not in connected.text
    status = fake_status(client)
    assert status["account_label"] == "Fake account a"
    assert (status["connected"], status["is_default"]) == (True, True)
    assert status["connected_at"] is not None and status["last_webhook_at"] is None
    assert "s3cret" not in json.dumps(status)


def test_a_field_not_declared_or_missing_is_422_naming_it(client: TestClient) -> None:
    extra = client.put(
        PROVIDER,
        json={"settings": {"account": "a", "colour": "red"}, "secrets": {"signing_secret": "s"}},
    )
    assert extra.status_code == 422
    assert extra.json()["errors"][0]["loc"] == ["body", "settings", "colour"]
    missing = client.put(PROVIDER, json={"settings": {"account": "a"}, "secrets": {}})
    assert missing.status_code == 422
    assert missing.json()["errors"][0]["loc"] == ["body", "secrets", "signing_secret"]
    # A secret sent as a setting is refused, not stored in the clear.
    swapped = client.put(
        PROVIDER, json={"settings": {"account": "a", "signing_secret": "s"}, "secrets": {}}
    )
    assert swapped.status_code == 422
    assert swapped.json()["errors"][0]["loc"] == ["body", "settings", "signing_secret"]
    too_long = client.put(
        PROVIDER,
        json={"settings": {"account": "a" * 2001}, "secrets": {"signing_secret": "s"}},
    )
    assert too_long.status_code == 422
    secret = "s" * 2001
    long_secret = client.put(
        PROVIDER, json={"settings": {"account": "a"}, "secrets": {"signing_secret": secret}}
    )
    assert long_secret.status_code == 422
    assert secret not in long_secret.text
    assert fake_status(client)["connected"] is False


def test_an_unknown_provider_is_404(client: TestClient) -> None:
    for answer in (
        client.put("/api/v1/payments/providers/square", json=FAKE_CONNECT),
        client.delete(PROVIDER),
        client.post(f"{PROVIDER}/default"),
    ):
        assert answer.status_code == 404
        assert answer.json()["type"] == PROBLEMS + "provider_not_found"


def test_a_failed_verify_is_422_with_its_text(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(*args: object) -> str:
        raise ProviderError("the account is closed")

    monkeypatch.setattr(FAKE, "verify", refuse)
    refused = client.put(PROVIDER, json=FAKE_CONNECT)
    assert refused.status_code == 422
    assert refused.json()["detail"] == "the account is closed"
    assert fake_status(client)["connected"] is False


def test_connecting_without_a_kek_is_409(settings: Settings) -> None:
    with TestClient(create_app(settings.model_copy(update={"secret_key_file": None}))) as client:
        refused = client.put(PROVIDER, json=FAKE_CONNECT)
        assert refused.status_code == 409
        assert refused.json()["type"] == PROBLEMS + "kek_missing"


def test_disconnect_with_an_open_request_is_409(client: TestClient) -> None:
    connect_fake(client)
    order = payable_order(client)
    req = request_payment(client, order["id"])
    refused = client.delete(PROVIDER)
    assert refused.status_code == 409
    assert refused.json()["type"] == PROBLEMS + "provider_has_open_requests"
    client.post(f"/api/v1/orders/{order['id']}/payment-requests/{req['id']}/cancel")
    assert client.delete(PROVIDER).status_code == 204
    assert fake_status(client)["connected"] is False


def test_the_default_is_chosen(client: TestClient) -> None:
    connect_fake(client)
    assert client.post(f"{PROVIDER}/default").status_code == 204
    assert fake_status(client)["is_default"] is True


def test_an_agent_cannot_connect_a_provider(settings: Settings) -> None:
    with enforced_app(settings, FakeIssuer()) as client:
        app = client.app
        assert isinstance(app, FastAPI)
        kek = getattr(app.state, STATE_ATTR).settings_store.kek
        token = TokenReader("agent", settings.database_url, kek).token()
        agent = {"Authorization": f"Bearer {token}"}
        for answer in (
            client.put(PROVIDER, json=FAKE_CONNECT, headers=agent),
            client.delete(PROVIDER, headers=agent),
            client.post(f"{PROVIDER}/default", headers=agent),
        ):
            assert answer.status_code == 403, answer.text
            assert answer.json()["reason"] == "not_a_person"
        # The agent reads the providers, as any api route.
        assert client.get("/api/v1/payments/providers", headers=agent).status_code == 200
        _sign_in(client)
        assert client.put(PROVIDER, json=FAKE_CONNECT, headers=HEADER).status_code == 200
