"""`/api/v1/payments/bank` (#4961): the shop's checking or savings account, linked through
Plaid Link. Beside the payment providers, not one of them: Plaid opens no payment request.
Every write is a person's own (``auth/routes.py`` ``PERSON_ENDPOINTS``), but Plaid's
webhook, which is public (``PUBLIC_ENDPOINTS``): its ``Plaid-Verification`` JWT is its only
credential. No answer carries a Plaid key or the access token, and no account number
exists here to carry. Problem types are ``https://scadbuddy.dev/problems/payments/<slug>``.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio
import logging
import math

from fastapi import APIRouter, BackgroundTasks, Request, Response, status

from scadbuddy.api.deps import StateDep
from scadbuddy.api.payment_webhooks import read_body
from scadbuddy.core.events import EventBus
from scadbuddy.core.problems import ApiError
from scadbuddy.payments.bank import (
    AccessTokenMissingError,
    BankChoiceError,
    BankKeys,
    BankKeysChangedError,
    BankLinkCreate,
    BankLinkedError,
    BankNotConfiguredError,
    BankNotLinkedError,
    BankStatus,
    BankUnlinked,
    BankWebhookAnswer,
    LinkToken,
    WebhookKeyLookupLimitedError,
)
from scadbuddy.payments.component import PaymentsDep
from scadbuddy.payments.credentials import CredentialUnreadableError, KekMissingError
from scadbuddy.payments.deposits import sync_deposits
from scadbuddy.payments.plaid import PlaidError, PlaidUnavailableError
from scadbuddy.payments.provider import BadSignatureError
from scadbuddy.telemetry.admission import bucket_key, relay_client

logger = logging.getLogger(__name__)

PROBLEMS = "https://scadbuddy.dev/problems/payments/"

router = APIRouter(prefix="/payments/bank", tags=["payments"])


def _problem(error: Exception) -> ApiError:
    if isinstance(error, PlaidUnavailableError):
        return ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, str(error), type_=PROBLEMS + "plaid_unavailable"
        )
    if isinstance(error, BankChoiceError):
        return ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            str(error),
            errors=[{"loc": ["body", "account_ids"], "msg": str(error)}],
        )
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
        AccessTokenMissingError: "access_token_missing",
    }[type(error)]
    return ApiError(status.HTTP_409_CONFLICT, str(error), type_=PROBLEMS + slug)


_REFUSALS = (
    PlaidError,
    BankChoiceError,
    AccessTokenMissingError,
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
        " Plaid's own text when it refuses; 503 `plaid_unavailable` when Plaid does not"
        " answer."
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
    description=(
        'Once an account is linked, the token is for Link\'s update mode ("Reconnect").'
        " 409 `bank_not_configured` before the keys are saved; 409 `access_token_missing`"
        " when the linked account's stored access token is gone (update mode needs it)."
    ),
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
        " for no account or several chosen, or an account that is not checking or savings"
        " (the token is exchanged first, so its Item is removed at Plaid);"
        " 409 `bank_linked` when another link came first, `bank_keys_changed` when the keys"
        " were replaced or forgotten while Link was open (its Item is removed either way)."
    ),
)
async def create_bank_link(body: BankLinkCreate, payments: PaymentsDep) -> BankStatus:
    try:
        return await payments.bank.link(body.public_token, body.account_ids)
    except _REFUSALS as error:
        raise _problem(error) from None


@router.delete(
    "/link",
    response_model=BankUnlinked,
    summary="Unlink the bank account",
    description=(
        "Removes the Item at Plaid, then drops the access token; the keys stay. When"
        " Plaid does not answer, 503 `plaid_unavailable` and the link stays. 409"
        " `kek_missing` without SCADBUDDY_SECRET_KEY_FILE, and the link stays: the file may"
        " be mounted again."
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


@router.post(
    "/reconnected",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Link's update mode finished: the bank is signed in again",
    description=(
        'Clears "Reconnect", and the sync failure that was the sign-in, then syncs the'
        " deposits once the answer is sent. 409 `bank_not_linked` with nothing linked."
    ),
)
async def reconnect_bank_link(
    payments: PaymentsDep, state: StateDep, later: BackgroundTasks
) -> Response:
    try:
        await asyncio.to_thread(payments.bank.reconnected)
    except _REFUSALS as error:
        raise _problem(error) from None
    # The deposits the bank held back while signed out show up at once.
    later.add_task(_sync_logged, payments, state.events)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _sync_logged(payments: PaymentsDep, bus: EventBus) -> None:
    try:
        await sync_deposits(payments.bank, bus)
    except Exception:
        logger.exception("the deposit sync a Plaid webhook or a reconnect asked for failed")


@router.post(
    "/webhook",
    response_model=BankWebhookAnswer,
    summary="Plaid's signed webhook for the linked Item",
    description=(
        "Public: the `Plaid-Verification` JWT is the credential. 400 when it does not"
        " verify or Plaid has no key with its `kid` (nothing changes), 413 above 256 KiB,"
        " 503 `provider_unavailable` when Plaid could not give its key, 429 with"
        " `Retry-After` when the sender or the process asked Plaid for keys too often (Plaid"
        " sends it again in both cases). `SYNC_UPDATES_AVAILABLE`"
        " syncs the deposits once the answer is sent; an `ITEM` `ERROR` with"
        ' `ITEM_LOGIN_REQUIRED` shows "Reconnect"; `LOGIN_REPAIRED` clears it.'
    ),
)
async def receive_bank_webhook(
    request: Request, payments: PaymentsDep, state: StateDep, later: BackgroundTasks
) -> BankWebhookAnswer:
    body = await read_body(request)
    # The sender as the app trusts it: a trusted proxy's X-Forwarded-For, else the peer.
    peer = request.client.host if request.client else None
    trusted = state.settings.trusted_proxy_networks
    headers = {name.lower(): value for name, value in request.headers.items()}
    try:
        outcome = await payments.bank.handle_webhook(
            headers, body, source=bucket_key(relay_client(request.headers, peer, trusted))
        )
    except BadSignatureError:
        raise ApiError(
            status.HTTP_400_BAD_REQUEST,
            "the webhook signature does not match",
            type_=PROBLEMS + "bad_signature",
        ) from None
    except WebhookKeyLookupLimitedError as limited:
        logger.warning("a Plaid webhook was not read: its key lookup is over the limit")
        # Plaid retries a 429 after its Retry-After
        # (https://plaid.com/docs/api/webhooks/#webhook-retries).
        raise ApiError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Plaid was asked for webhook keys too often",
            title="Too Many Requests",
            headers={"Retry-After": str(max(1, math.ceil(limited.retry_after)))},
        ) from None
    except PlaidError as error:
        logger.warning("a Plaid webhook was not read: %s", error.code)
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Plaid did not give the webhook's key",
            type_=PROBLEMS + "provider_unavailable",
        ) from None
    except (CredentialUnreadableError, KekMissingError, AccessTokenMissingError) as error:
        logger.warning("a Plaid webhook was not read: %s", error)
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "the Plaid keys are unavailable",
            type_=PROBLEMS + "credential_unreadable",
        ) from None
    if outcome == "sync":
        # Plaid wants an answer within 10 seconds; the sync runs after it.
        later.add_task(_sync_logged, payments, state.events)
    return BankWebhookAnswer(outcome=outcome)
