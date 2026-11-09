"""The Plaid calls (#4961). No test calls Plaid: respx answers every request, and the
payloads in ``plaid_payloads/`` are the sandbox examples of Plaid's API reference
(https://plaid.com/docs/api/link/#linktokencreate,
https://plaid.com/docs/api/items/#itempublic_tokenexchange,
https://plaid.com/docs/api/accounts/#accountsget,
https://plaid.com/docs/api/items/#itemremove, https://plaid.com/docs/errors/)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from scadbuddy.payments import plaid
from scadbuddy.payments.plaid import (
    AccountSubtype,
    PlaidAccount,
    PlaidError,
    PlaidKeys,
    PlaidUnavailableError,
)

PAYLOADS = Path(__file__).parent / "plaid_payloads"
SANDBOX = "https://sandbox.plaid.com"
KEYS = PlaidKeys(environment="sandbox", client_id="client-1", secret="plaid-s3cret")
ACCESS = "access-sandbox-de3ce8ef-33f8-452c-a685-8671031fc0f6"
CHECKING = "BxBXxLj1m4HMXBm9WZZmCWVbPjX16EHwv99vp"
SAVINGS = "dVzbVMLjrxTnLjX4G66XUp5GLklm4oiZy88yK"
CREDIT = "Pp1Vpkl9w8sajvK6oEEKtr7vZxBnGpf7LxxLE"


def payload(name: str) -> Any:
    return json.loads((PAYLOADS / name).read_text())


def sent(route: respx.Route) -> dict[str, Any]:
    request = route.calls.last.request
    assert request.headers["plaid-client-id"] == "client-1"
    assert request.headers["plaid-secret"] == "plaid-s3cret"
    body: dict[str, Any] = json.loads(request.content)
    # The keys go in the headers only, never in a body a log might print.
    assert "secret" not in body and "client_id" not in body
    return body


def test_the_keys_stay_out_of_repr() -> None:
    assert "plaid-s3cret" not in repr(KEYS) and "client-1" not in repr(KEYS)


@respx.mock
async def test_a_link_token_asks_for_transactions_on_checking_or_savings() -> None:
    route = respx.post(f"{SANDBOX}/link/token/create").mock(
        return_value=httpx.Response(200, json=payload("link_token.create.json"))
    )
    token = await plaid.link_token(KEYS, user_id="link-row-1", webhook=None)
    assert token == "link-sandbox-af1a0311-da53-4636-b754-dd15cc058176"
    body = sent(route)
    assert body["user"] == {"client_user_id": "link-row-1"}
    assert body["products"] == ["transactions"]
    assert body["country_codes"] == ["US"] and body["language"] == "en"
    assert body["account_filters"] == {"depository": {"account_subtypes": ["checking", "savings"]}}
    assert len(body["client_name"]) <= 30
    assert "webhook" not in body


@respx.mock
async def test_production_keys_call_production() -> None:
    route = respx.post("https://production.plaid.com/link/token/create").mock(
        return_value=httpx.Response(200, json=payload("link_token.create.json"))
    )
    live = PlaidKeys(environment="production", client_id="client-1", secret="plaid-s3cret")
    await plaid.link_token(live, user_id="u", webhook="https://shop.example/hook")
    assert sent(route)["webhook"] == "https://shop.example/hook"


@respx.mock
async def test_a_refusal_is_plaids_text_and_code_without_the_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    respx.post(f"{SANDBOX}/link/token/create").mock(
        return_value=httpx.Response(400, json=payload("error.invalid_api_keys.json"))
    )
    with caplog.at_level(logging.DEBUG), pytest.raises(PlaidError) as err:
        await plaid.link_token(KEYS, user_id="u", webhook=None)
    assert err.value.code == "INVALID_API_KEYS"
    assert str(err.value) == "Plaid refused: invalid client_id or secret provided"
    assert "plaid-s3cret" not in caplog.text


@respx.mock
async def test_no_answer_is_a_plaid_error_with_no_code() -> None:
    respx.post(f"{SANDBOX}/item/remove").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(PlaidUnavailableError) as err:
        await plaid.remove_item(KEYS, ACCESS)
    assert err.value.code is None and str(err.value) == "Plaid did not answer"
    # Still a PlaidError, so every caller that handles one handles it.
    assert isinstance(err.value, PlaidError)


@respx.mock
async def test_the_exchange_gives_the_access_token_and_item() -> None:
    route = respx.post(f"{SANDBOX}/item/public_token/exchange").mock(
        return_value=httpx.Response(200, json=payload("item.public_token.exchange.json"))
    )
    found = await plaid.exchange(KEYS, "public-sandbox-1")
    assert sent(route) == {"public_token": "public-sandbox-1"}
    assert found.access_token == ACCESS
    assert found.item_id == "M5eVJqLnv3tbzdngLDp9FL5OlDNxlNhlE55op"
    assert ACCESS not in repr(found)


@respx.mock
@pytest.mark.parametrize(
    ("account_id", "expected"),
    [
        (CHECKING, ("Plaid Checking", "checking", "0000")),
        (SAVINGS, ("Plaid Saving", "savings", "1111")),
    ],
)
async def test_the_chosen_account_is_its_name_subtype_and_mask(
    account_id: str, expected: tuple[str, AccountSubtype, str]
) -> None:
    route = respx.post(f"{SANDBOX}/accounts/get").mock(
        return_value=httpx.Response(200, json=payload("accounts.get.json"))
    )
    name, subtype, mask = expected
    found = await plaid.account(KEYS, ACCESS, account_id)
    assert found == PlaidAccount(account_id=account_id, name=name, subtype=subtype, mask=mask)
    assert sent(route) == {"access_token": ACCESS, "options": {"account_ids": [account_id]}}
    # Only these four fields are kept: no balance, no official name.
    assert set(PlaidAccount.model_fields) == {"account_id", "name", "subtype", "mask"}


@respx.mock
@pytest.mark.parametrize("account_id", [CREDIT, "not-on-this-item"])
async def test_an_account_that_is_not_checking_or_savings_is_refused(account_id: str) -> None:
    respx.post(f"{SANDBOX}/accounts/get").mock(
        return_value=httpx.Response(200, json=payload("accounts.get.json"))
    )
    with pytest.raises(PlaidError, match="checking or savings"):
        await plaid.account(KEYS, ACCESS, account_id)


@respx.mock
async def test_an_account_with_no_name_says_so() -> None:
    found = payload("accounts.get.json")
    found["accounts"][0]["name"] = None
    respx.post(f"{SANDBOX}/accounts/get").mock(return_value=httpx.Response(200, json=found))
    with pytest.raises(PlaidError, match="no name"):
        await plaid.account(KEYS, ACCESS, CHECKING)


@respx.mock
async def test_remove_sends_the_access_token() -> None:
    route = respx.post(f"{SANDBOX}/item/remove").mock(
        return_value=httpx.Response(200, json=payload("item.remove.json"))
    )
    await plaid.remove_item(KEYS, ACCESS)
    assert sent(route) == {"access_token": ACCESS}


@respx.mock
async def test_removing_an_item_plaid_no_longer_has_is_no_error() -> None:
    respx.post(f"{SANDBOX}/item/remove").mock(
        return_value=httpx.Response(400, json=payload("error.item_not_found.json"))
    )
    await plaid.remove_item(KEYS, ACCESS)


@respx.mock
async def test_a_sync_page_from_the_start_and_from_a_cursor() -> None:
    route = respx.post(f"{SANDBOX}/transactions/sync").mock(
        return_value=httpx.Response(200, json=payload("transactions.sync.json"))
    )
    page = await plaid.transactions_sync(KEYS, ACCESS, None)
    body = sent(route)
    assert body == {
        "access_token": ACCESS,
        "count": plaid.SYNC_COUNT,
        "options": {"include_original_description": True},
    }
    assert [t["transaction_id"] for t in page.added] == [
        "lPNjeW1nR6CDn5okmGQ6hEpMo4lLNoSrzqDje",
        "yhnUVvtcGGcCKU0bcz8PDQr5ZUxUXebUvbKC0",
    ]
    assert page.modified == [] and page.removed == ["CmdQTNgems8BT1B7ibkoUXVPyAeehT3Tmzk0l"]
    assert page.next_cursor.startswith("tVUUL15l") and page.has_more is False
    await plaid.transactions_sync(KEYS, ACCESS, "cursor-1")
    assert sent(route)["cursor"] == "cursor-1"


@respx.mock
async def test_a_mutation_during_pagination_is_its_own_code() -> None:
    respx.post(f"{SANDBOX}/transactions/sync").mock(
        return_value=httpx.Response(400, json=payload("error.sync_mutation.json"))
    )
    with pytest.raises(PlaidError) as err:
        await plaid.transactions_sync(KEYS, ACCESS, "cursor-1")
    assert err.value.code == plaid.SYNC_MUTATION


@respx.mock
async def test_a_sync_answer_without_its_lists_is_a_plaid_error() -> None:
    respx.post(f"{SANDBOX}/transactions/sync").mock(
        return_value=httpx.Response(200, json={"next_cursor": "c", "has_more": False})
    )
    with pytest.raises(PlaidError, match="transactions"):
        await plaid.transactions_sync(KEYS, ACCESS, None)
