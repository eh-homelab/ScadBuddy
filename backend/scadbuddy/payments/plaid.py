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


async def _post(keys: PlaidKeys, path: str, body: dict[str, Any]) -> dict[str, Any]:
    headers = {"PLAID-CLIENT-ID": keys.client_id, "PLAID-SECRET": keys.secret}
    try:
        async with httpx.AsyncClient(base_url=API[keys.environment], timeout=TIMEOUT) as client:
            answer = await client.post(path, json=body, headers=headers)
        found = answer.json()
    except (httpx.HTTPError, ValueError):
        logger.warning("Plaid did not answer POST %s", path)
        raise PlaidError("Plaid did not answer") from None
    if not isinstance(found, dict):
        raise PlaidError("Plaid did not answer")
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
