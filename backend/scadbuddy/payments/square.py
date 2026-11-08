"""Square (#5666): a payment request is a Square payment link, and Square's webhooks tell
ScadBuddy what became of its payment. The adapter holds no state: the shop's access token
and webhook signature key come in with each call, and neither is logged or put in an error.

Square's documented behaviour this relies on:

- The API is at ``https://connect.squareupsandbox.com`` (sandbox) or
  ``https://connect.squareup.com`` (production); a token of one environment does not work
  in the other (https://developer.squareup.com/docs/devtools/sandbox/overview). Each call
  names its API version in ``Square-Version``
  (https://developer.squareup.com/docs/build-basics/versioning-overview).
- ``POST /v2/online-checkout/payment-links`` with ``quick_pay`` (``name`` at most 255
  characters, ``price_money``, ``location_id``) makes a link whose ``payment_link`` has its
  ``id``, ``url`` and ``order_id``; ``payment_note`` (at most 500) is added to the
  resulting Payment, and ``checkout_options.redirect_url`` is where the buyer goes after
  paying. ``idempotency_key`` is at most 192 characters
  (https://developer.squareup.com/reference/square/checkout-api/create-payment-link,
  https://developer.squareup.com/docs/checkout-api/quick-pay-checkout); it needs
  ``PAYMENTS_WRITE``, ``ORDERS_READ`` and ``ORDERS_WRITE``, a location read
  ``MERCHANT_PROFILE_READ`` and a payment read ``PAYMENTS_READ`` (each operation's
  ``security`` in Square's OpenAPI specification,
  https://github.com/square/connect-api-specification). A request made
  again with the same key answers the first result and does not act twice
  (https://developer.squareup.com/docs/build-basics/common-api-patterns/idempotency).
- ``DELETE /v2/online-checkout/payment-links/{id}`` deletes a link and sets its order's
  state to ``CANCELED``
  (https://developer.squareup.com/reference/square/checkout-api/delete-payment-link,
  https://developer.squareup.com/docs/checkout-api/manage-checkout). There is no lookup
  of a link by its order: ``GET /v2/online-checkout/payment-links`` lists them all, a
  page at a time with ``cursor``, at most 1000 a page
  (https://developer.squareup.com/reference/square/checkout-api/list-payment-links).
- A Payment carries its ``order_id``, its ``status`` (``APPROVED``, ``PENDING``,
  ``COMPLETED``, ``CANCELED`` or ``FAILED``), ``total_money`` (``amount_money`` plus a tip)
  and ``processing_fee``: the fees and fee adjustments Square assessed, each typed
  ``INITIAL`` or ``ADJUSTMENT`` (https://developer.squareup.com/reference/square/objects/Payment,
  https://developer.squareup.com/reference/square/objects/ProcessingFee). Amounts are in
  the currency's smallest unit
  (https://developer.squareup.com/docs/build-basics/working-with-monetary-amounts).
- ``payment.updated`` is sent when a payment completes and again when Square adds its
  processing fees; an ACH bank payment is made ``PENDING`` (``payment.created``) and later
  ``COMPLETED`` or ``FAILED`` (https://developer.squareup.com/docs/payments-api/webhooks).
  So a completed payment is ``paid`` under Square's own event id each time it is told, and
  the lifecycle records its money once and its fee once, whichever update carries it.
- A refund (``refund.created``, ``refund.updated``) has its own ``id``, ``amount_money``,
  ``payment_id`` and ``status`` (``PENDING``, ``COMPLETED``, ``REJECTED``, ``FAILED``)
  (https://developer.squareup.com/reference/square/objects/PaymentRefund,
  https://developer.squareup.com/reference/square/refunds-api/webhooks/refund.updated).
  Its order is read from its payment (``GET /v2/payments/{id}``,
  https://developer.squareup.com/reference/square/payments-api/get-payment). A completed
  refund is ``refund:<id>``, so its updates record it once; a rejected or failed one is
  ``refund-failed:<id>``, which reverses it (#5681).
- Every notification carries ``x-square-hmacsha256-signature``: the base64 HMAC-SHA256,
  under the subscription's signature key, of the notification URL followed by the raw
  body (https://developer.squareup.com/docs/webhooks/step3validate; the SDK's
  ``verify_signature``,
  https://github.com/square/square-python-sdk/blob/master/src/square/utils/webhooks_helper.py).
  The signature has no timestamp. Square may send a notification more than once and in
  any order; its ``event_id`` is the idempotency value, and one not answered 2xx is sent
  again for up to 24 hours (https://developer.squareup.com/docs/webhooks/overview).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final

import httpx
from pydantic import ValidationError

from scadbuddy.payments.models import (
    HostedRequest,
    PaymentRequestView,
    ProviderEvent,
    ProviderEventKind,
)
from scadbuddy.payments.provider import (
    BadSignatureError,
    Credential,
    ProviderError,
    ProviderField,
    ProviderUnavailableError,
)

logger = logging.getLogger(__name__)

API: Final[Mapping[str, str]] = {
    "sandbox": "https://connect.squareupsandbox.com",
    "production": "https://connect.squareup.com",
}
#: The API version every call asks for, so an answer's shape does not follow the
#: application's default.
SQUARE_VERSION: Final = "2026-09-16"
SIGNATURE_HEADER: Final = "x-square-hmacsha256-signature"
TIMEOUT: Final = httpx.Timeout(20.0)
#: The largest page ``ListPaymentLinks`` gives.
LINKS_PAGE: Final = "1000"
_PAYMENT_KINDS: Final[Mapping[str, ProviderEventKind]] = {
    "COMPLETED": "paid",
    "PENDING": "pending",
    "FAILED": "failed",
    "CANCELED": "failed",
}
_PAYMENT_EVENTS: Final = frozenset({"payment.created", "payment.updated"})
_REFUND_EVENTS: Final = frozenset({"refund.created", "refund.updated"})
#: Order states in which a link can no longer take money: a ``COMPLETED`` order is fully
#: paid and a ``CANCELED`` one is not paid; both are terminal
#: (https://developer.squareup.com/reference/square/enums/OrderState).
_CLOSED_ORDER: Final = frozenset({"COMPLETED", "CANCELED"})
#: A Square object id (location, link, order, payment, refund); nothing else goes in a path.
_SQUARE_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,192}")


class _RefusedError(Exception):
    """Square answered with an error status. ``message`` is Square's own text."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _NoAnswerError(Exception):
    """Square did not answer, or answered with something that is not a JSON object."""


def _header(headers: Mapping[str, str], name: str) -> str:
    return next((v for k, v in headers.items() if k.lower() == name), "")


def sign(key: str, notification_url: str, body: bytes) -> str:
    """The signature Square sends for ``body`` posted to ``notification_url``."""
    digest = hmac.new(key.encode(), notification_url.encode() + body, hashlib.sha256)
    return base64.b64encode(digest.digest()).decode()


def verify_signature(key: str, notification_url: str, header: str, body: bytes) -> None:
    """Raises `BadSignatureError` unless ``header`` is Square's signature of ``body``."""
    if not key or not notification_url or not header:
        raise BadSignatureError("the signature does not match")
    expected = sign(key, notification_url, body)
    if not hmac.compare_digest(expected.encode(), header.encode()):
        raise BadSignatureError("the signature does not match")


def _refusal(status: int, found: Mapping[str, Any]) -> _RefusedError:
    errors = found.get("errors")
    first = errors[0] if isinstance(errors, list) and errors else {}
    first = first if isinstance(first, dict) else {}
    code, detail = first.get("code"), first.get("detail")
    parts = [str(p) for p in (code, detail) if p]
    return _RefusedError(status, ": ".join(parts) or f"HTTP {status}")


class SquareProvider:
    id = "square"
    label = "Square"
    fields: Sequence[ProviderField] = (
        ProviderField(
            name="environment", label="Environment (sandbox or production)", secret=False
        ),
        ProviderField(name="location_id", label="Location ID", secret=False),
        ProviderField(name="notification_url", label="Webhook notification URL", secret=False),
        ProviderField(name="access_token", label="Access token", secret=True),
        ProviderField(name="signature_key", label="Webhook signature key", secret=True),
    )

    def __repr__(self) -> str:
        return "SquareProvider()"

    # -- calls ------------------------------------------------------------------------

    @staticmethod
    def _base(credential: Credential) -> str:
        environment = credential.settings.get("environment", "")
        if environment not in API:
            raise ProviderError("the Square environment is sandbox or production")
        return API[environment]

    async def _call(
        self,
        credential: Credential,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Any = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {credential.secrets.get('access_token', '')}",
            "Square-Version": SQUARE_VERSION,
            "Content-Type": "application/json",
        }
        content = None if body is None else json.dumps(body).encode()
        try:
            async with httpx.AsyncClient(base_url=self._base(credential), timeout=TIMEOUT) as c:
                answer = await c.request(
                    method, path, params=params, headers=headers, content=content
                )
            found = answer.json() if answer.content else {}
        except (httpx.HTTPError, ValueError):
            raise _NoAnswerError() from None
        if not isinstance(found, dict):
            raise _NoAnswerError()
        if answer.is_success:
            return found
        raise _refusal(answer.status_code, found)

    async def _act(self, credential: Credential, method: str, path: str, **kwargs: Any) -> Any:
        """A call a person waits on: every failure is a `ProviderError` they can read."""
        try:
            return await self._call(credential, method, path, **kwargs)
        except _NoAnswerError:
            raise ProviderError("Square did not answer") from None
        except _RefusedError as refused:
            raise _shown(refused) from None

    async def _read(self, credential: Credential, path: str) -> Any:
        """A read a webhook needs after its signature held: any failure is
        `ProviderUnavailableError`, so nothing is recorded and Square sends it again."""
        try:
            return await self._call(credential, "GET", path)
        except _NoAnswerError:
            logger.warning("Square did not answer GET %s", path)
        except _RefusedError as refused:
            # The status says why Square keeps failing it; it goes to the log only.
            logger.warning("Square answered %s to GET %s", refused.status, path)
        raise ProviderUnavailableError("Square did not answer a read the event needs")

    # -- the seam ---------------------------------------------------------------------

    async def verify(self, credential: Credential) -> str:
        environment = credential.settings.get("environment", "")
        self._base(credential)
        location = credential.settings.get("location_id", "")
        if not _SQUARE_ID.fullmatch(location):
            raise ProviderError("a Square location id is letters and digits")
        url = httpx.URL(credential.settings.get("notification_url", ""))
        if url.scheme != "https" or not url.host:
            raise ProviderError("the Square notification URL is an https URL")
        if not credential.secrets.get("access_token"):
            raise ProviderError("a Square access token is needed")
        if not credential.secrets.get("signature_key"):
            raise ProviderError("a Square webhook signature key is needed")
        found = await self._act(credential, "GET", f"/v2/locations/{location}")
        place = found.get("location")
        if not isinstance(place, dict) or place.get("status") != "ACTIVE":
            raise ProviderError(f"the Square location {location} is not active")
        return f"Square location {place.get('name') or location} ({environment})"

    async def create_request(
        self,
        credential: Credential,
        request: PaymentRequestView,
        *,
        return_url: str | None,
        idempotency_key: str,
        order_number: str,
    ) -> HostedRequest:
        name = (
            f"Deposit for order {order_number}"
            if request.kind == "deposit"
            else f"Order {order_number}"
        )
        body: dict[str, Any] = {
            "idempotency_key": idempotency_key,
            "quick_pay": {
                "name": name[:255],
                "price_money": {"amount": request.amount_minor, "currency": request.currency},
                "location_id": credential.settings.get("location_id", ""),
            },
            "payment_note": f"{name} (ScadBuddy request {request.id})"[:500],
        }
        if return_url is not None:
            body["checkout_options"] = {"redirect_url": return_url}
        found = await self._act(credential, "POST", "/v2/online-checkout/payment-links", body=body)
        link = found.get("payment_link")
        link = link if isinstance(link, dict) else {}
        try:
            return HostedRequest.model_validate(
                {"provider_ref": _path_id(link.get("order_id")), "hosted_url": link.get("url")}
            )
        except (ProviderError, ValidationError):
            raise ProviderError("Square's answer named no payment link") from None

    async def cancel_request(self, credential: Credential, provider_ref: str) -> None:
        """Delete the payment link of the order ``provider_ref``. A link already gone, or
        one whose order is completed or cancelled, is no error; any other failure is a
        `ProviderError`, which the housekeeping sweep tries again (#5700)."""
        order_id = _path_id(provider_ref)
        try:
            link = await self._link_of(credential, order_id)
            if link is None:
                return
            await self._call(credential, "DELETE", f"/v2/online-checkout/payment-links/{link}")
            return
        except _NoAnswerError:
            raise ProviderError("Square did not answer") from None
        except _RefusedError as refused:
            if refused.status == 404:
                return
            if refused.status not in (400, 409):
                raise _shown(refused) from None
        found = await self._act(credential, "GET", f"/v2/orders/{order_id}")
        order = found.get("order")
        if not isinstance(order, dict) or order.get("state") not in _CLOSED_ORDER:
            raise ProviderError("Square did not delete the payment link")

    async def _link_of(self, credential: Credential, order_id: str) -> str | None:
        """The id of the payment link that made ``order_id``, or None when there is none."""
        cursor: str | None = None
        while True:
            params = {"limit": LINKS_PAGE} | ({} if cursor is None else {"cursor": cursor})
            page = await self._call(
                credential, "GET", "/v2/online-checkout/payment-links", params=params
            )
            links = page.get("payment_links")
            for link in links if isinstance(links, list) else []:
                if isinstance(link, dict) and link.get("order_id") == order_id:
                    return _path_id(link.get("id"))
            following = page.get("cursor")
            if not isinstance(following, str) or not following or following == cursor:
                return None
            cursor = following

    async def parse_webhook(
        self, credential: Credential, headers: Mapping[str, str], body: bytes
    ) -> ProviderEvent:
        verify_signature(
            credential.secrets.get("signature_key", ""),
            credential.settings.get("notification_url", ""),
            _header(headers, SIGNATURE_HEADER),
            body,
        )
        try:
            event = json.loads(body)
            event_id = event["event_id"]
            kind = event["type"]
            obj = event["data"]["object"]
            if not isinstance(event_id, str) or not isinstance(obj, dict):
                raise TypeError
            found = await self._event(credential, event_id, kind, obj)
            created = event.get("created_at")
            if isinstance(created, str):
                found = found.model_copy(update={"created_at": datetime.fromisoformat(created)})
            return found
        except (ValueError, KeyError, TypeError, AttributeError, ValidationError):
            raise ProviderError("the event body is malformed") from None

    # -- events -----------------------------------------------------------------------

    async def _event(
        self, credential: Credential, event_id: str, kind: object, obj: dict[str, Any]
    ) -> ProviderEvent:
        if kind in _PAYMENT_EVENTS:
            return _payment_event(event_id, obj["payment"])
        if kind in _REFUND_EVENTS:
            return await self._refund(credential, event_id, obj["refund"])
        return ProviderEvent(event_id=event_id, kind="other")

    async def _refund(
        self, credential: Credential, event_id: str, refund: dict[str, Any]
    ) -> ProviderEvent:
        status = refund.get("status")
        refund_key = f"refund:{_path_id(refund['id'])}"
        if status not in ("COMPLETED", "REJECTED", "FAILED"):
            return ProviderEvent(event_id=event_id, kind="other")
        found = await self._read(credential, f"/v2/payments/{_path_id(refund['payment_id'])}")
        order_id = _path_id(found["payment"]["order_id"])
        amount, currency = _money(refund["amount_money"])
        if status != "COMPLETED":
            return ProviderEvent(
                event_id=f"refund-failed:{_path_id(refund['id'])}",
                kind="refund_failed",
                provider_ref=order_id,
                currency=currency,
                reverses=refund_key,
            )
        return ProviderEvent(
            event_id=refund_key,
            kind="refunded",
            provider_ref=order_id,
            amount_minor=amount,
            currency=currency,
        )


def _shown(refused: _RefusedError) -> ProviderError:
    if refused.status == 401:
        return ProviderError("Square refused the access token")
    if refused.status == 403:
        return ProviderError("the Square access token lacks a permission ScadBuddy needs")
    return ProviderError(f"Square refused: {refused.message}")


def _path_id(value: object) -> str:
    if not isinstance(value, str) or not _SQUARE_ID.fullmatch(value):
        raise ProviderError("that is not a Square id")
    return value


def _money(money: Mapping[str, Any]) -> tuple[int, str]:
    amount = money["amount"]
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise TypeError
    return amount, str(money["currency"]).upper()


def _payment_event(event_id: str, payment: dict[str, Any]) -> ProviderEvent:
    kind = _PAYMENT_KINDS.get(str(payment.get("status")))
    if kind is None:
        return ProviderEvent(event_id=event_id, kind="other")
    order_id = _path_id(payment["order_id"])
    if kind == "failed":
        return ProviderEvent(event_id=event_id, kind=kind, provider_ref=order_id)
    amount, currency = _money(payment["total_money"])
    return ProviderEvent(
        event_id=event_id,
        kind=kind,
        provider_ref=order_id,
        amount_minor=amount,
        currency=currency,
        fee_minor=_fee(payment, currency) if kind == "paid" else None,
    )


def _fee(payment: Mapping[str, Any], currency: str) -> int | None:
    """Square's fee for the payment: its ``INITIAL`` processing fees in the payment's
    currency; None until Square adds them. An ``ADJUSTMENT`` (a fee given back, as on a
    refund) is not the payment's cost."""
    fees = payment.get("processing_fee")
    total, seen = 0, False
    for fee in fees if isinstance(fees, list) else []:
        if not isinstance(fee, dict) or fee.get("type") != "INITIAL":
            continue
        money = fee.get("amount_money")
        if not isinstance(money, dict) or str(money.get("currency", "")).upper() != currency:
            continue
        amount = money.get("amount")
        if isinstance(amount, int) and not isinstance(amount, bool):
            total, seen = total + amount, True
    return total if seen and total >= 0 else None


SQUARE = SquareProvider()
