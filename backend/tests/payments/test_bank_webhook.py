"""Plaid's webhooks for the linked account (#4961). Each is checked by its
``Plaid-Verification`` JWT before it is read
(https://plaid.com/docs/api/webhooks/webhook-verification/): ES256, the key from
``/webhook_verification_key/get``, ``iat`` at most 5 minutes old, and the body's SHA-256 in
``request_body_sha256``. The tests sign with a key of their own, served by respx in the
JWK shape Plaid documents; the bodies are Plaid's examples
(https://plaid.com/docs/api/products/transactions/#sync_updates_available,
https://plaid.com/docs/api/items/#error)."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from joserfc import jwt
from joserfc.jwk import ECKey, OctKey

from scadbuddy.orders.store import OrderStore
from scadbuddy.payments import plaid
from scadbuddy.payments.bank import BankLinks
from scadbuddy.payments.plaid import PlaidError
from scadbuddy.payments.provider import BadSignatureError
from tests.payments.test_bank import ACCESS, KEK, KEYS, SANDBOX, linked, payload

pytestmark = pytest.mark.requires_postgres

PAYLOADS = Path(__file__).parent / "plaid_payloads"
KEY = ECKey.generate_key("P-256")
OTHER = ECKey.generate_key("P-256")
KID = "6c5516e1-92dc-479e-a8ff-5a51992e0001"
VERIFY = f"{SANDBOX}/webhook_verification_key/get"


def body_of(name: str) -> bytes:
    return (PAYLOADS / name).read_bytes()


def signed(
    body: bytes,
    *,
    key: ECKey = KEY,
    alg: str = "ES256",
    iat: float | None = None,
    digest: str | None = None,
) -> dict[str, str]:
    claims = {
        "iat": int(time.time() if iat is None else iat),
        "request_body_sha256": digest or hashlib.sha256(body).hexdigest(),
    }
    token = jwt.encode({"alg": alg, "kid": KID, "typ": "JWT"}, claims, key)
    return {"Plaid-Verification": token}


def plaid_key(router: respx.MockRouter) -> respx.Route:
    """``/webhook_verification_key/get`` as Plaid answers it: the public JWK."""
    jwk: dict[str, Any] = {
        **KEY.as_dict(private=False),
        "alg": "ES256",
        "kid": KID,
        "use": "sig",
        "created_at": 1560466150,
        "expired_at": None,
    }
    return router.post(VERIFY).mock(
        return_value=httpx.Response(200, json={"key": jwk, "request_id": "RZ6Omi1bzzwDaLo"})
    )


@pytest.fixture
async def bank(rows: OrderStore) -> BankLinks:
    made = BankLinks(rows, KEK)
    with respx.mock as router:
        await linked(made, router)
    return made


async def test_sync_updates_available_asks_for_a_sync(bank: BankLinks) -> None:
    body = body_of("webhook.sync_updates_available.json")
    with respx.mock as router:
        key = plaid_key(router)
        assert await bank.handle_webhook(signed(body), body) == "sync"
        assert json.loads(key.calls.last.request.content) == {"key_id": KID}
        # The key is kept: a second webhook asks Plaid for nothing.
        assert await bank.handle_webhook(signed(body), body) == "sync"
        assert key.call_count == 1


async def test_item_login_required_marks_the_link_and_login_repaired_clears_it(
    bank: BankLinks,
) -> None:
    required = body_of("webhook.item_login_required.json")
    repaired = body_of("webhook.login_repaired.json")
    with respx.mock as router:
        plaid_key(router)
        assert await bank.handle_webhook(signed(required), required) == "login_required"
        assert bank.status().login_required is True
        assert await bank.handle_webhook(signed(repaired), repaired) == "login_repaired"
    assert bank.status().login_required is False


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Plaid-Verification": "not-a-jwt"},
        signed(b"x", key=OTHER),
        signed(b"x", iat=time.time() - 301),
        signed(b"x", digest="0" * 64),
    ],
    ids=["missing", "garbage", "other key", "too old", "other body"],
)
async def test_a_webhook_that_does_not_verify_changes_nothing(
    bank: BankLinks, headers: dict[str, str]
) -> None:
    body = body_of("webhook.item_login_required.json")
    with respx.mock as router:
        plaid_key(router)
        with pytest.raises(BadSignatureError):
            await bank.handle_webhook(headers, body)
    assert bank.status().login_required is False


async def test_only_es256_is_taken(bank: BankLinks) -> None:
    body = body_of("webhook.sync_updates_available.json")
    with respx.mock as router:
        key = plaid_key(router)
        token = jwt.encode(
            {"alg": "HS256", "kid": KID},
            {"iat": int(time.time()), "request_body_sha256": hashlib.sha256(body).hexdigest()},
            OctKey.import_key(b"0" * 32),
        )
        with pytest.raises(BadSignatureError):
            await bank.handle_webhook({"plaid-verification": token}, body)
        assert not key.called


async def test_a_webhook_for_another_item_is_ignored(bank: BankLinks) -> None:
    found = json.loads(body_of("webhook.item_login_required.json"))
    body = json.dumps({**found, "item_id": "another-item"}).encode()
    with respx.mock as router:
        plaid_key(router)
        assert await bank.handle_webhook(signed(body), body) == "ignored"
    assert bank.status().login_required is False


async def test_a_key_plaid_cannot_give_is_a_plaid_error(bank: BankLinks) -> None:
    body = body_of("webhook.sync_updates_available.json")
    with respx.mock as router:
        router.post(VERIFY).mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(PlaidError):
            await bank.handle_webhook(signed(body), body)


async def test_reconnect_link_token_is_update_mode(bank: BankLinks) -> None:
    with respx.mock as router:
        route = router.post(f"{SANDBOX}/link/token/create").mock(
            return_value=httpx.Response(200, json=payload("link_token.create.json"))
        )
        await bank.link_token()
    sent = json.loads(route.calls.last.request.content)
    # Update mode: the Item's token and no products (https://plaid.com/docs/link/update-mode/).
    assert sent["access_token"] == ACCESS and "products" not in sent


async def test_the_link_token_carries_the_webhook_url(rows: OrderStore) -> None:
    bank = BankLinks(
        rows, KEK, webhook_url=lambda: "https://shop.example/api/v1/payments/bank/webhook"
    )
    with respx.mock as router:
        routes = {
            "token": router.post(f"{SANDBOX}/link/token/create").mock(
                return_value=httpx.Response(200, json=payload("link_token.create.json"))
            )
        }
        await bank.save_keys(KEYS)
        await bank.link_token()
    sent = json.loads(routes["token"].calls.last.request.content)
    assert sent["webhook"] == "https://shop.example/api/v1/payments/bank/webhook"
    assert sent["products"] == ["transactions"]


async def test_reconnected_clears_the_mark(bank: BankLinks) -> None:
    body = body_of("webhook.item_login_required.json")
    with respx.mock as router:
        plaid_key(router)
        await bank.handle_webhook(signed(body), body)
    bank.reconnected()
    assert bank.status().login_required is False


def test_the_header_name_is_plaids() -> None:
    assert plaid.VERIFICATION_HEADER == "plaid-verification"
