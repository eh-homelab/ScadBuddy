"""`/api/v1/payments/bank` (#4961): the shop's bank account linked through Plaid. No
answer carries a Plaid key or the access token, and only a person changes the link.
Plaid is answered by respx with the sandbox payloads of ``tests/payments/plaid_payloads``."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.auth.service_credentials import TokenReader
from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from tests.api.test_auth_enforce import HEADER, _sign_in
from tests.api.test_order_payments import with_payments
from tests.api.test_payment_webhooks import enforced_app
from tests.auth.fake_oidc import FakeIssuer
from tests.payments.test_bank import ACCESS, CHECKING, CREDIT, SANDBOX, payload, plaid_answers

pytestmark = pytest.mark.requires_postgres

PROBLEMS = "https://scadbuddy.dev/problems/payments/"
BANK = "/api/v1/payments/bank"
KEYS = {"environment": "sandbox", "client_id": "client-1", "secret": "plaid-s3cret"}
LINK = {"public_token": "public-sandbox-1", "account_id": CHECKING}


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    return with_payments(settings, tmp_path)


@pytest.fixture
def plaid() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        # The app's own requests go to the test client, never through respx.
        router.route(host="testserver").pass_through()
        plaid_answers(router)
        yield router


def test_link_show_and_unlink(client: TestClient, plaid: respx.MockRouter) -> None:
    assert client.get(BANK).json() == {
        "configured": False,
        "environment": None,
        "account": None,
        "linked_at": None,
    }
    saved = client.put(f"{BANK}/keys", json=KEYS)
    assert saved.status_code == 200, saved.text
    assert saved.json()["configured"] is True and "plaid-s3cret" not in saved.text
    token = client.post(f"{BANK}/link-token")
    assert token.status_code == 200, token.text
    assert token.json() == {"link_token": "link-sandbox-af1a0311-da53-4636-b754-dd15cc058176"}
    linked = client.post(f"{BANK}/link", json=LINK)
    assert linked.status_code == 200, linked.text
    account = {"name": "Plaid Checking", "type": "checking", "mask": "0000"}
    assert linked.json()["account"] == account
    shown = client.get(BANK)
    assert ACCESS not in shown.text and "plaid-s3cret" not in shown.text
    assert shown.json()["account"]["mask"] == "0000"
    assert client.delete(f"{BANK}/link").status_code == 204
    assert client.get(BANK).json()["account"] is None
    assert client.delete(f"{BANK}/keys").status_code == 204
    assert client.get(BANK).json()["configured"] is False


def test_plaid_refusing_the_keys_is_422_with_its_text(
    client: TestClient, plaid: respx.MockRouter
) -> None:
    plaid.post(f"{SANDBOX}/link/token/create").mock(
        return_value=httpx.Response(400, json=payload("error.invalid_api_keys.json"))
    )
    refused = client.put(f"{BANK}/keys", json=KEYS)
    assert refused.status_code == 422
    assert refused.json()["detail"] == "Plaid refused: invalid client_id or secret provided"
    assert "plaid-s3cret" not in refused.text


def test_a_credit_card_is_refused(client: TestClient, plaid: respx.MockRouter) -> None:
    client.put(f"{BANK}/keys", json=KEYS)
    refused = client.post(f"{BANK}/link", json={**LINK, "account_id": CREDIT})
    assert refused.status_code == 422
    assert "checking or savings" in refused.json()["detail"]


def test_the_order_of_steps_is_409(client: TestClient, plaid: respx.MockRouter) -> None:
    for answer in (client.post(f"{BANK}/link-token"), client.post(f"{BANK}/link", json=LINK)):
        assert answer.status_code == 409
        assert answer.json()["type"] == PROBLEMS + "bank_not_configured"
    unlinked = client.delete(f"{BANK}/link")
    assert (unlinked.status_code, unlinked.json()["type"]) == (409, PROBLEMS + "bank_not_linked")
    client.put(f"{BANK}/keys", json=KEYS)
    client.post(f"{BANK}/link", json=LINK)
    for answer in (
        client.put(f"{BANK}/keys", json=KEYS),
        client.delete(f"{BANK}/keys"),
        client.post(f"{BANK}/link", json=LINK),
    ):
        assert answer.status_code == 409
        assert answer.json()["type"] == PROBLEMS + "bank_linked"


def test_an_unknown_field_or_environment_is_422(client: TestClient) -> None:
    development = {**KEYS, "environment": "development"}
    assert client.put(f"{BANK}/keys", json=development).status_code == 422
    assert client.put(f"{BANK}/keys", json={**KEYS, "routing": "1"}).status_code == 422


def test_saving_keys_without_a_kek_is_409(settings: Settings) -> None:
    with TestClient(create_app(settings.model_copy(update={"secret_key_file": None}))) as client:
        refused = client.put(f"{BANK}/keys", json=KEYS)
        assert refused.status_code == 409
        assert refused.json()["type"] == PROBLEMS + "kek_missing"


def test_an_agent_cannot_change_the_link(settings: Settings) -> None:
    with enforced_app(settings, FakeIssuer()) as client:
        app = client.app
        assert isinstance(app, FastAPI)
        kek = getattr(app.state, STATE_ATTR).settings_store.kek
        token = TokenReader("agent", settings.database_url, kek).token()
        agent = {"Authorization": f"Bearer {token}"}
        for answer in (
            client.put(f"{BANK}/keys", json=KEYS, headers=agent),
            client.delete(f"{BANK}/keys", headers=agent),
            client.post(f"{BANK}/link-token", headers=agent),
            client.post(f"{BANK}/link", json=LINK, headers=agent),
            client.delete(f"{BANK}/link", headers=agent),
        ):
            assert answer.status_code == 403, answer.text
            assert answer.json()["reason"] == "not_a_person"
        assert client.get(BANK, headers=agent).status_code == 200
        _sign_in(client)
        assert client.get(BANK, headers=HEADER).status_code == 200
