"""`/api/v1/payments/bank` (#4961): the shop's checking or savings account, linked through
Plaid Link. Beside the payment providers, not one of them: Plaid opens no payment request.
Every write is a person's own (``auth/routes.py`` ``PERSON_ENDPOINTS``). No answer carries
a Plaid key or the access token, and no account number exists here to carry. Problem
types are ``https://scadbuddy.dev/problems/payments/<slug>``.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio

from fastapi import APIRouter, Response, status

from scadbuddy.core.problems import ApiError
from scadbuddy.payments.bank import (
    BankKeys,
    BankKeysChangedError,
    BankLinkCreate,
    BankLinkedError,
    BankNotConfiguredError,
    BankNotLinkedError,
    BankStatus,
    BankUnlinked,
    LinkToken,
)
from scadbuddy.payments.component import PaymentsDep
from scadbuddy.payments.credentials import CredentialUnreadableError, KekMissingError
from scadbuddy.payments.plaid import PlaidError

PROBLEMS = "https://scadbuddy.dev/problems/payments/"

router = APIRouter(prefix="/payments/bank", tags=["payments"])


def _problem(error: Exception) -> ApiError:
    if isinstance(error, PlaidError):
        return ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            str(error),
            errors=[{"loc": ["body"], "msg": str(error)}],
        )
    slug = {
        KekMissingError: "kek_missing",
        BankNotConfiguredError: "bank_not_configured",
        BankLinkedError: "bank_linked",
        BankKeysChangedError: "bank_keys_changed",
        BankNotLinkedError: "bank_not_linked",
        CredentialUnreadableError: "credential_unreadable",
    }[type(error)]
    return ApiError(status.HTTP_409_CONFLICT, str(error), type_=PROBLEMS + slug)


_REFUSALS = (
    PlaidError,
    KekMissingError,
    BankNotConfiguredError,
    BankLinkedError,
    BankKeysChangedError,
    BankNotLinkedError,
    CredentialUnreadableError,
)


@router.get("", response_model=BankStatus, summary="The shop's linked bank account")
async def get_bank_link(payments: PaymentsDep) -> BankStatus:
    return await asyncio.to_thread(payments.bank.status)


@router.put(
    "/keys",
    response_model=BankStatus,
    summary="Save the shop's Plaid keys",
    description=(
        "Plaid checks the keys first (a link token). 409 `kek_missing` without"
        " SCADBUDDY_SECRET_KEY_FILE; 409 `bank_linked` while an account is linked; 422 with"
        " Plaid's own text when it refuses."
    ),
)
async def put_bank_keys(body: BankKeys, payments: PaymentsDep) -> BankStatus:
    try:
        return await payments.bank.save_keys(body)
    except _REFUSALS as error:
        raise _problem(error) from None


@router.delete(
    "/keys",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Forget the shop's Plaid keys",
    description="409 `bank_linked` while an account is linked: unlink it first.",
)
async def delete_bank_keys(payments: PaymentsDep) -> Response:
    try:
        await asyncio.to_thread(payments.bank.forget_keys)
    except _REFUSALS as error:
        raise _problem(error) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/link-token",
    response_model=LinkToken,
    summary="A token that opens Plaid Link",
    description="409 `bank_not_configured` before the keys are saved.",
)
async def create_bank_link_token(payments: PaymentsDep) -> LinkToken:
    try:
        return LinkToken(link_token=await payments.bank.link_token())
    except _REFUSALS as error:
        raise _problem(error) from None


@router.post(
    "/link",
    response_model=BankStatus,
    summary="Link the account the shop chose in Plaid Link",
    description=(
        "Exchanges Link's public token and keeps the account's name, type and mask. 422"
        " for an account that is not checking or savings (its Item is removed at Plaid);"
        " 409 `bank_linked` when another link came first, `bank_keys_changed` when the keys"
        " were replaced or forgotten while Link was open (its Item is removed either way)."
    ),
)
async def create_bank_link(body: BankLinkCreate, payments: PaymentsDep) -> BankStatus:
    try:
        return await payments.bank.link(body.public_token, body.account_id)
    except _REFUSALS as error:
        raise _problem(error) from None


@router.delete(
    "/link",
    response_model=BankUnlinked,
    summary="Unlink the bank account",
    description=(
        "Removes the Item at Plaid, then drops the access token; the keys stay. When"
        " Plaid does not answer, 422 and the link stays. 409 `kek_missing` without"
        " SCADBUDDY_SECRET_KEY_FILE, and the link stays: the file may be mounted again."
        " When Plaid refuses the keys themselves, only the link is dropped here and the"
        " keys stay. When the keys are sealed under another key file, the row is deleted"
        " with its keys. In both cases `removed_at_plaid` is false and `note` tells the"
        " shop to remove the Item in the Plaid Dashboard (and, in the second, to save the"
        " keys again)."
    ),
)
async def delete_bank_link(payments: PaymentsDep) -> BankUnlinked:
    try:
        return await payments.bank.unlink()
    except _REFUSALS as error:
        raise _problem(error) from None
