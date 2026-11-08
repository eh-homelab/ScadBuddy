"""`POST /api/v1/payments/webhooks/{provider}` (#4957): a provider tells ScadBuddy that a
request was paid, refunded or expired, that a payment is pending or failed, or what its fee
was (#4958). It is the one back-office route the public
reaches (``auth/routes.py`` ``PUBLIC_ENDPOINTS``): the provider's signature is its only
credential. The route reads the raw body itself, as the signature covers those exact
bytes; a body above ``MAX_WEBHOOK_BYTES`` is 413 before anything parses it. A bad
signature is 400 and records no event or money (only its category on the credential row,
#5684); a provider call the reading needed that failed is 503 and records no event or
money either. Neither the body nor the headers are logged.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import logging

from fastapi import APIRouter, Request, status

from scadbuddy.api.payments import PROBLEMS, not_found
from scadbuddy.core.problems import ApiError
from scadbuddy.payments.component import PaymentsDep
from scadbuddy.payments.credentials import CredentialUnreadableError, KekMissingError
from scadbuddy.payments.provider import (
    BadSignatureError,
    ProviderError,
    ProviderUnavailableError,
)
from scadbuddy.payments.service import ProviderNotFoundError, WebhookAnswer

logger = logging.getLogger(__name__)

#: A provider's event is a few KiB; this is far above any of them.
MAX_WEBHOOK_BYTES = 256 * 1024

router = APIRouter(prefix="/payments", tags=["payments"])


def _too_large() -> ApiError:
    return ApiError(
        status.HTTP_413_CONTENT_TOO_LARGE,
        f"a webhook body is at most {MAX_WEBHOOK_BYTES} bytes",
    )


async def _body(request: Request) -> bytes:
    """The raw body, read no further than the limit."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_WEBHOOK_BYTES:
        raise _too_large()
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_WEBHOOK_BYTES:
            raise _too_large()
    return bytes(body)


@router.post(
    "/webhooks/{provider}",
    response_model=WebhookAnswer,
    summary="A payment provider's signed event",
    description=(
        "Public: the provider's signature is the credential. 400 for a bad signature"
        " (no event or money is recorded), 404 for a provider not connected, 413 above 256 KiB,"
        " 503 when the stored credential cannot be opened, and 503 `provider_unavailable`"
        " when reading the event needed the provider's API and that call failed (no event"
        " or money is recorded, so the provider sends it again). A replayed event answers"
        " `replayed` and records nothing more."
    ),
)
async def receive_webhook(provider: str, request: Request, payments: PaymentsDep) -> WebhookAnswer:
    body = await _body(request)
    headers = {name.lower(): value for name, value in request.headers.items()}
    try:
        outcome = await payments.service.handle_webhook(provider, headers, body)
    except ProviderNotFoundError as error:
        raise not_found(error) from None
    except BadSignatureError:
        raise ApiError(
            status.HTTP_400_BAD_REQUEST,
            "the webhook signature does not match",
            type_=PROBLEMS + "bad_signature",
        ) from None
    except ProviderError as error:
        raise ApiError(
            status.HTTP_400_BAD_REQUEST, str(error), type_=PROBLEMS + "malformed_event"
        ) from None
    except ProviderUnavailableError as error:
        # #4958: nothing was recorded; the provider sends the event again.
        logger.warning("a webhook from %s was not read: %s", provider, error)
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            str(error),
            type_=PROBLEMS + "provider_unavailable",
        ) from None
    except (CredentialUnreadableError, KekMissingError) as error:
        # The caller is anyone: the real reason goes to the log, a fixed text to them.
        logger.warning("a webhook from %s was not read: %s", provider, error)
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "the provider's credential is unavailable",
            type_=PROBLEMS + "credential_unreadable",
        ) from None
    return WebhookAnswer(outcome=outcome)
