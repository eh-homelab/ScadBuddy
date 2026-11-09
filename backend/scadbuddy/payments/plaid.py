"""Plaid (#4961): the calls that link the shop's checking or savings account. Plaid is not
a payment provider: it opens no payment request, so it is not in ``registry.py``. The
module holds no state: the shop's keys and the access token come in with each call, and
none of them is logged or put in an error. No call here reads an account or routing
number (that is Plaid's Auth product, which ScadBuddy does not ask for).

Plaid's documented behaviour this relies on:

- The API is at ``https://sandbox.plaid.com`` or ``https://production.plaid.com``; every
  request is a POST with a JSON body, and the client id and secret "may be sent either in
  the request body or in the headers ``PLAID-CLIENT-ID`` and ``PLAID-SECRET``"
  (https://plaid.com/docs/api/#api-host). They go in the headers here.
- An error answer carries ``error_code`` ("safe for programmatic use") and
  ``error_message`` ("a developer-friendly representation of the error code"), read "in
  preference to HTTP status codes" (https://plaid.com/docs/errors/,
  https://plaid.com/docs/api/#api-host).
- ``/link/token/create`` needs ``client_name`` (at most 30 characters), ``language``,
  ``country_codes``, ``user.client_user_id`` (no personal data) and ``products``;
  ``account_filters`` with ``depository.account_subtypes`` shows only those accounts in
  Link; ``webhook`` is where the Item's webhooks go
  (https://plaid.com/docs/api/link/#linktokencreate).
- ``/item/public_token/exchange`` turns Link's ``public_token`` into an ``access_token``,
  which does not expire, and the ``item_id`` to store with it
  (https://plaid.com/docs/api/items/#itempublic_tokenexchange).
- ``/accounts/get`` with ``options.account_ids`` gives each account's ``name``, ``type``,
  ``subtype`` and ``mask``, "the last 2-4 alphanumeric characters" of the account's
  displayed mask or number (https://plaid.com/docs/api/accounts/#accountsget).
- ``/transactions/sync`` gives the Item's ``added``, ``modified`` and ``removed``
  transactions since ``cursor`` (all of them with no cursor), ``count`` at most 500, with
  ``next_cursor`` and ``has_more``; when a page fails with
  ``TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION`` "the entire pagination request loop must
  be restarted beginning with the cursor for the first page of the update". A
  transaction's ``amount`` is positive "when money moves out of the account" and negative
  "when money moves in"; ``original_description`` is the bank's own text, sent only with
  ``options.include_original_description``
  (https://plaid.com/docs/api/products/transactions/#transactionssync,
  https://plaid.com/docs/errors/transactions/). A pending transaction that posts is
  removed, and the posted one is added with a new ``transaction_id``
  (https://plaid.com/docs/transactions/transactions-data/).
- ``/item/remove`` invalidates the access token (https://plaid.com/docs/api/items/#itemremove);
  ``ITEM_NOT_FOUND`` is an Item already removed, and ``INVALID_ACCESS_TOKEN`` a token
  Plaid no longer knows (https://plaid.com/docs/errors/invalid-input/).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import httpx
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

Environment = Literal["sandbox", "production"]
API: Final[dict[str, str]] = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}
TIMEOUT: Final = httpx.Timeout(20.0)
#: What Link shows the shop as the app asking for access (at most 30 characters).
CLIENT_NAME: Final = "ScadBuddy"
AccountSubtype = Literal["checking", "savings"]
ACCOUNT_SUBTYPES: Final[tuple[AccountSubtype, ...]] = ("checking", "savings")
#: Error codes that mean Plaid refuses the keys themselves: a rotated secret, or keys
#: of another environment (https://plaid.com/docs/errors/invalid-input/).
KEYS_REFUSED: Final = frozenset({"INVALID_API_KEYS", "UNAUTHORIZED_ENVIRONMENT"})
#: The most transactions one ``/transactions/sync`` page holds (Plaid's maximum).
SYNC_COUNT: Final = 500
#: A page whose data changed under it: the whole loop starts again from its first cursor.
SYNC_MUTATION: Final = "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"
#: Error codes that mean the Item is already gone at Plaid.
GONE: Final = frozenset({"ITEM_NOT_FOUND", "INVALID_ACCESS_TOKEN"})


@dataclass(frozen=True)
class PlaidKeys:
    """The shop's Plaid API keys. Kept out of ``repr``."""

    environment: Environment
    client_id: str = field(repr=False)
    secret: str = field(repr=False)


@dataclass(frozen=True)
class Exchanged:
    access_token: str = field(repr=False)
    item_id: str


@dataclass(frozen=True)
class SyncPage:
    """One ``/transactions/sync`` page. A transaction is Plaid's own object, as sent."""

    added: list[dict[str, Any]]
    modified: list[dict[str, Any]]
    removed: list[str]
    next_cursor: str
    has_more: bool


class PlaidAccount(BaseModel):
    """What ScadBuddy keeps of the linked account: no balance, no number."""

    account_id: str
    name: str
    subtype: AccountSubtype
    mask: str | None


class PlaidError(Exception):
    """A Plaid call failed. The message is safe to show; ``code`` is Plaid's
    ``error_code``, None when Plaid did not answer."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class PlaidUnavailableError(PlaidError):
    """Plaid did not answer (a timeout, a network failure, or no JSON): not the input's
    fault, so the API answers 503 rather than 422."""

    def __init__(self) -> None:
        super().__init__("Plaid did not answer")


async def _post(keys: PlaidKeys, path: str, body: dict[str, Any]) -> dict[str, Any]:
    headers = {"PLAID-CLIENT-ID": keys.client_id, "PLAID-SECRET": keys.secret}
    try:
        async with httpx.AsyncClient(base_url=API[keys.environment], timeout=TIMEOUT) as client:
            answer = await client.post(path, json=body, headers=headers)
        found = answer.json()
    except (httpx.HTTPError, ValueError):
        logger.warning("Plaid did not answer POST %s", path)
        raise PlaidUnavailableError() from None
    if not isinstance(found, dict):
        raise PlaidUnavailableError()
    if answer.is_success and "error_code" not in found:
        return found
    code = found.get("error_code")
    message = found.get("error_message")
    logger.warning("Plaid answered %s %s to POST %s", answer.status_code, code, path)
    raise PlaidError(
        f"Plaid refused: {message or f'HTTP {answer.status_code}'}",
        code if isinstance(code, str) else None,
    )


async def link_token(keys: PlaidKeys, *, user_id: str, webhook: str | None) -> str:
    """A token for Link that shows only checking and savings accounts."""
    body: dict[str, Any] = {
        "client_name": CLIENT_NAME,
        "language": "en",
        "country_codes": ["US"],
        "user": {"client_user_id": user_id},
        "products": ["transactions"],
        "account_filters": {"depository": {"account_subtypes": list(ACCOUNT_SUBTYPES)}},
    }
    if webhook is not None:
        body["webhook"] = webhook
    found = await _post(keys, "/link/token/create", body)
    token = found.get("link_token")
    if not isinstance(token, str):
        raise PlaidError("Plaid's answer held no link token")
    return token


async def exchange(keys: PlaidKeys, public_token: str) -> Exchanged:
    found = await _post(keys, "/item/public_token/exchange", {"public_token": public_token})
    access, item = found.get("access_token"), found.get("item_id")
    if not isinstance(access, str) or not isinstance(item, str):
        raise PlaidError("Plaid's answer held no access token")
    return Exchanged(access_token=access, item_id=item)


async def account(keys: PlaidKeys, access_token: str, account_id: str) -> PlaidAccount:
    """The chosen account; a `PlaidError` unless it is a checking or savings account."""
    found = await _post(
        keys,
        "/accounts/get",
        {"access_token": access_token, "options": {"account_ids": [account_id]}},
    )
    for row in found.get("accounts") or []:
        if not isinstance(row, dict) or row.get("account_id") != account_id:
            continue
        if row.get("type") != "depository" or row.get("subtype") not in ACCOUNT_SUBTYPES:
            break
        if not isinstance(row.get("name"), str) or not row["name"]:
            raise PlaidError("Plaid gave the chosen account no name")
        try:
            return PlaidAccount.model_validate(
                {k: row.get(k) for k in ("account_id", "name", "subtype", "mask")}
            )
        except ValidationError:
            break
    raise PlaidError("the chosen account is not a checking or savings account")


async def remove_item(keys: PlaidKeys, access_token: str) -> None:
    """Remove the Item at Plaid. One Plaid no longer has is no error."""
    try:
        await _post(keys, "/item/remove", {"access_token": access_token})
    except PlaidError as error:
        if error.code not in GONE:
            raise


async def transactions_sync(keys: PlaidKeys, access_token: str, cursor: str | None) -> SyncPage:
    """One page of the Item's transactions since ``cursor`` (from the start with None)."""
    body: dict[str, Any] = {
        "access_token": access_token,
        "count": SYNC_COUNT,
        "options": {"include_original_description": True},
    }
    if cursor is not None:
        body["cursor"] = cursor
    found = await _post(keys, "/transactions/sync", body)
    added, modified = found.get("added"), found.get("modified")
    removed, next_cursor = found.get("removed"), found.get("next_cursor")
    if (
        not isinstance(added, list)
        or not isinstance(modified, list)
        or not isinstance(removed, list)
        or not isinstance(next_cursor, str)
    ):
        raise PlaidError("Plaid's answer held no transactions")
    return SyncPage(
        added=[t for t in added if isinstance(t, dict)],
        modified=[t for t in modified if isinstance(t, dict)],
        removed=[
            str(t["transaction_id"])
            for t in removed
            if isinstance(t, dict) and isinstance(t.get("transaction_id"), str)
        ],
        next_cursor=next_cursor,
        has_more=found.get("has_more") is True,
    )
