"""`/api/v1/payments/bank` (#4961): the shop's bank account linked through Plaid. No
answer carries a Plaid key or the access token, and only a person changes the link.
Plaid is answered by respx with the sandbox payloads of ``tests/payments/plaid_payloads``."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.auth.service_credentials import TokenReader
from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from scadbuddy.payments import bank as bank_module
from tests.api.test_auth_enforce import HEADER, _sign_in
from tests.api.test_order_payments import with_payments
from tests.api.test_payment_webhooks import enforced_app
from tests.auth.fake_oidc import FakeIssuer
from tests.payments.test_bank import ACCESS, CHECKING, CREDIT, SANDBOX, payload, plaid_answers
from tests.payments.test_bank_webhook import (
    body_of,
    lookups_unspaced,  # noqa: F401 (an autouse fixture)
    plaid_key,
    signed,
    unknown_key,
)

pytestmark = pytest.mark.requires_postgres

PROBLEMS = "https://scadbuddy.dev/problems/payments/"
BANK = "/api/v1/payments/bank"
KEYS = {"environment": "sandbox", "client_id": "client-1", "secret": "plaid-s3cret"}
LINK = {"public_token": "public-sandbox-1", "account_ids": [CHECKING]}
OTHER_KID = "6c5516e1-92dc-479e-a8ff-5a51992e0002"


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
        "synced_at": None,
        "sync_error": None,
        "login_required": False,
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
    unlinked = client.delete(f"{BANK}/link")
    assert unlinked.status_code == 200, unlinked.text
    assert unlinked.json() == {"removed_at_plaid": True, "note": None}
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
    refused = client.post(f"{BANK}/link", json={**LINK, "account_ids": [CREDIT]})
    assert refused.status_code == 422
    assert "checking or savings" in refused.json()["detail"]


def test_two_accounts_chosen_is_422_and_the_item_is_removed(
    client: TestClient, plaid: respx.MockRouter
) -> None:
    client.put(f"{BANK}/keys", json=KEYS)
    refused = client.post(f"{BANK}/link", json={**LINK, "account_ids": [CHECKING, CREDIT]})
    assert refused.status_code == 422
    assert refused.json()["detail"] == "Choose one checking or savings account in Plaid."
    assert any(call.request.url.path == "/item/remove" for call in plaid.calls)


def test_plaid_not_answering_is_503_plaid_unavailable(
    client: TestClient, plaid: respx.MockRouter
) -> None:
    plaid.post(f"{SANDBOX}/link/token/create").mock(side_effect=httpx.ConnectError("down"))
    down = client.put(f"{BANK}/keys", json=KEYS)
    assert down.status_code == 503, down.text
    assert down.json()["type"] == PROBLEMS + "plaid_unavailable"


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
        # Unlink too: the row is kept for when the key file is back.
        unlink = client.delete(f"{BANK}/link")
        assert unlink.status_code == 409
        assert unlink.json()["type"] == PROBLEMS + "kek_missing"


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
            client.post(f"{BANK}/reconnected", headers=agent),
        ):
            assert answer.status_code == 403, answer.text
            assert answer.json()["reason"] == "not_a_person"
        assert client.get(BANK, headers=agent).status_code == 200
        _sign_in(client)
        assert client.get(BANK, headers=HEADER).status_code == 200
        # Plaid's webhook needs no session: an unsigned one is refused by its JWT check.
        client.cookies.clear()
        unsigned = client.post(f"{BANK}/webhook", content=b"{}")
        assert unsigned.status_code == 400, unsigned.text
        assert unsigned.json()["type"] == PROBLEMS + "bad_signature"


def linked(client: TestClient) -> None:
    assert client.put(f"{BANK}/keys", json=KEYS).status_code == 200
    assert client.post(f"{BANK}/link", json=LINK).status_code == 200


def test_login_required_shows_reconnect_until_update_mode_finishes(
    client: TestClient, plaid: respx.MockRouter
) -> None:
    plaid_key(plaid)
    linked(client)
    body = body_of("webhook.item_login_required.json")
    hook = client.post(f"{BANK}/webhook", content=body, headers=signed(body))
    assert hook.status_code == 200, hook.text
    assert hook.json() == {"outcome": "login_required"}
    assert client.get(BANK).json()["login_required"] is True
    token = client.post(f"{BANK}/link-token")
    assert token.status_code == 200, token.text
    assert client.post(f"{BANK}/reconnected").status_code == 204
    assert client.get(BANK).json()["login_required"] is False


def test_sync_updates_available_syncs_after_the_answer(
    client: TestClient, plaid: respx.MockRouter
) -> None:
    plaid_key(plaid)
    synced = plaid.post(f"{SANDBOX}/transactions/sync").mock(
        return_value=httpx.Response(200, json=payload("transactions.sync.json"))
    )
    linked(client)
    body = body_of("webhook.sync_updates_available.json")
    hook = client.post(f"{BANK}/webhook", content=body, headers=signed(body))
    assert hook.json() == {"outcome": "sync"}
    assert synced.called
    assert client.get(BANK).json()["synced_at"] is not None


def test_a_bad_signature_or_a_missing_key_changes_nothing(
    client: TestClient, plaid: respx.MockRouter
) -> None:
    linked(client)
    body = body_of("webhook.item_login_required.json")
    plaid.post(f"{SANDBOX}/webhook_verification_key/get").mock(
        side_effect=httpx.ConnectError("down")
    )
    unavailable = client.post(f"{BANK}/webhook", content=body, headers=signed(body))
    assert (unavailable.status_code, unavailable.json()["type"]) == (
        503,
        PROBLEMS + "provider_unavailable",
    )
    plaid.routes.clear()
    unknown_key(plaid)
    unknown = client.post(f"{BANK}/webhook", content=body, headers=signed(body))
    # Plaid does not know the kid: a 400, which Plaid does not retry, not a 503.
    assert (unknown.status_code, unknown.json()["type"]) == (400, PROBLEMS + "bad_signature")
    plaid.routes.clear()
    plaid_key(plaid)
    forged = client.post(f"{BANK}/webhook", content=body, headers=signed(b"other", kid=OTHER_KID))
    assert (forged.status_code, forged.json()["type"]) == (400, PROBLEMS + "bad_signature")
    assert client.get(BANK).json()["login_required"] is False


def test_a_lookup_over_the_limit_is_a_429_plaid_retries_after(
    client: TestClient, plaid: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bank_module, "_KEY_LOOKUPS", bank_module.KeyLookups(burst=1, every=3600.0))
    plaid_key(plaid)
    linked(client)
    body = body_of("webhook.item_login_required.json")
    assert client.post(f"{BANK}/webhook", content=body, headers=signed(body)).status_code == 200
    limited = client.post(f"{BANK}/webhook", content=body, headers=signed(body, kid=OTHER_KID))
    assert limited.status_code == 429, limited.text
    # Plaid waits for Retry-After before it sends the webhook again
    # (https://plaid.com/docs/api/webhooks/#webhook-retries).
    assert 1 <= int(limited.headers["retry-after"]) <= 3600


def test_the_lookup_limit_counts_the_peer_not_a_raw_forwarded_for(
    client: TestClient, plaid: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources: list[str] = []
    real = bank_module.BankLinks.handle_webhook

    async def spy(self: bank_module.BankLinks, *args: Any, **kwargs: Any) -> Any:
        sources.append(kwargs["source"])
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(bank_module.BankLinks, "handle_webhook", spy)
    plaid_key(plaid)
    linked(client)
    body = body_of("webhook.item_login_required.json")
    headers = {**signed(body), "X-Forwarded-For": "203.0.113.7"}
    assert client.post(f"{BANK}/webhook", content=body, headers=headers).status_code == 200
    # The test client is no trusted proxy, so its X-Forwarded-For is not believed.
    assert sources == ["testclient"]


def test_reconnecting_syncs_after_the_answer(client: TestClient, plaid: respx.MockRouter) -> None:
    """The deposits the bank held back while signed out show up at once."""
    synced = plaid.post(f"{SANDBOX}/transactions/sync").mock(
        return_value=httpx.Response(200, json=payload("transactions.sync.json"))
    )
    linked(client)
    assert client.post(f"{BANK}/reconnected").status_code == 204
    assert synced.called
    assert client.get(BANK).json()["synced_at"] is not None


def test_a_link_with_no_access_token_is_a_503_not_a_500(
    client: TestClient, plaid: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A linked row whose token is gone cannot read a webhook: 503, so Plaid sends it
    again, with no traceback per request."""
    plaid_key(plaid)
    linked(client)

    def missing(self: bank_module.BankLinks) -> Any:
        raise bank_module.AccessTokenMissingError("item")

    monkeypatch.setattr(bank_module.BankLinks, "access", missing)
    body = body_of("webhook.sync_updates_available.json")
    hook = client.post(f"{BANK}/webhook", content=body, headers=signed(body))
    assert (hook.status_code, hook.json()["type"]) == (503, PROBLEMS + "credential_unreadable")
