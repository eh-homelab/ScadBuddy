"""PayPal (#4959): a payment request is an Orders v2 order to capture, and PayPal's webhook
tells ScadBuddy what became of it. The shop's client secret comes in with each call, and
it is never logged or put in an error. The adapter keeps one thing in memory: each app's
OAuth access token, until it is about to expire. The token is never stored.

PayPal's documented behaviour this relies on:

- The API is at ``https://api-m.sandbox.paypal.com`` (sandbox) or ``https://api-m.paypal.com``
  (live) (https://developer.paypal.com/api/rest/requests/). An access token comes from
  ``POST /v1/oauth2/token`` with ``grant_type=client_credentials`` and the client id and
  secret as Basic auth. The answer carries ``access_token``, ``app_id`` and ``expires_in``
  in seconds, and a new token is asked for when it expires
  (https://developer.paypal.com/api/rest/authentication/).
- ``PayPal-Request-Id`` makes a POST idempotent: PayPal keeps the id for a period of time,
  and a call made again with it does not act twice
  (https://developer.paypal.com/api/rest/requests/).
- An order is ``POST /v2/checkout/orders`` with ``intent: CAPTURE``. With
  ``payment_source.paypal`` the answer is ``PAYER_ACTION_REQUIRED`` with a ``payer-action``
  link, where the customer approves it. ``invoice_id`` must be unique in the merchant
  account by default. ``custom_id`` is not shown to the payer
  (https://developer.paypal.com/docs/api/orders/v2/#orders_create).
- An approved order is captured with ``POST /v2/checkout/orders/{id}/capture``
  (https://developer.paypal.com/docs/api/orders/v2/#orders_capture). Its 422 answers include
  ``ORDER_ALREADY_CAPTURED`` (one capture per order), ``INSTRUMENT_DECLINED`` and
  ``TRANSACTION_REFUSED`` (https://developer.paypal.com/api/rest/reference/orders/v2/errors/).
  No PayPal source says a ``PAYMENT.CAPTURE.DECLINED`` event follows a capture refused this
  way, so ``capture_request`` answers ``declined`` and the request reads ``failed``.
- Orders v2 has no call that voids an order. ``PATCH /v2/checkout/orders/{id}`` "Updates an
  order with a ``CREATED`` or ``APPROVED`` status"
  (https://developer.paypal.com/docs/api/orders/v2/#orders_patch), so an order still
  ``PAYER_ACTION_REQUIRED`` (not yet approved) cannot be marked. ScadBuddy does not rely on
  the mark: it captures an approval only while its own request is open or failed.
- HUF, JPY and TWD take no decimals; a decimal amount is an error
  (https://developer.paypal.com/api/rest/reference/currency-codes/).
- The events: ``CHECKOUT.ORDER.APPROVED`` (a buyer approved an order),
  ``PAYMENT.CAPTURE.COMPLETED``, ``PAYMENT.CAPTURE.PENDING``, ``PAYMENT.CAPTURE.DECLINED``,
  ``PAYMENT.CAPTURE.REFUNDED`` (a merchant refunds a capture) and
  ``PAYMENT.CAPTURE.REVERSED`` (https://developer.paypal.com/api/rest/webhooks/event-names/).
  Older PayPal material calls the declined event ``PAYMENT.CAPTURE.DENIED``; both are read.
  ``PAYMENT.CAPTURE.REVERSED`` (a chargeback or reversal) is not read: it is ``other``, and
  the request stays ``paid``.
- A capture event's resource is the capture: ``amount``,
  ``seller_receivable_breakdown.paypal_fee`` and ``supplementary_data.related_ids.order_id``
  (the sample event in https://developer.paypal.com/api/rest/webhooks/rest/,
  https://developer.paypal.com/docs/api/payments/v2/#captures_get). The breakdown is not
  there while a capture is pending.
- A refund's resource has its own ``id``, ``amount`` and ``status`` (``CANCELLED``,
  ``FAILED``, ``PENDING``, ``COMPLETED``), and an ``up`` link to its capture, but no order id
  (https://developer.paypal.com/docs/api/payments/v2/#refunds_get). PayPal sends
  ``PAYMENT.REFUND.PENDING`` and ``PAYMENT.REFUND.FAILED``, and names no event for a pending
  refund that later completes (https://developer.paypal.com/api/rest/webhooks/event-names/).
  So a ``PAYMENT.CAPTURE.REFUNDED`` whose refund is pending reads the refund again; one still
  pending is not recorded, and the shop records it by hand (README).
- A webhook carries ``PAYPAL-AUTH-ALGO``, ``PAYPAL-CERT-URL``, ``PAYPAL-TRANSMISSION-ID``,
  ``PAYPAL-TRANSMISSION-SIG`` and ``PAYPAL-TRANSMISSION-TIME``.
  ``POST /v1/notifications/verify-webhook-signature`` takes them, the webhook id and the
  event, and answers ``verification_status`` ``SUCCESS`` or ``FAILURE``
  (https://developer.paypal.com/docs/api/webhooks/v1/#verify-webhook-signature_post). The
  event must be posted back exactly as it was received: re-serialising it can fail
  verification. PayPal retries a delivery that gets no 2xx up to 25 times over 3 days
  (https://developer.paypal.com/api/rest/webhooks/rest/).
- ``GET /v2/checkout/orders/{id}`` shows the order (https://developer.paypal.com/docs/api/orders/v2/#orders_get).
  Its ``status`` is one of ``CREATED``, ``SAVED``, ``APPROVED``, ``VOIDED`` ("All purchase
  units in the order are voided"), ``COMPLETED`` ("Check the payment status in
  ``purchase_units[].payments.captures[].status``": a completed order can also mean a
  declined payment) and ``PAYER_ACTION_REQUIRED``. A capture's ``status`` is one of
  ``COMPLETED``, ``DECLINED`` ("The funds could not be captured"), ``PARTIALLY_REFUNDED``,
  ``PENDING``, ``REFUNDED`` and ``FAILED`` ("There was an error while capturing payment")
  (the ``order_status`` and ``capture_status`` schemas in
  https://developer.paypal.com/api/orders/v2/schema.json, read 2026-11-08).
- PayPal documents neither how long it keeps a ``PayPal-Request-Id`` for Orders v2 (the
  idempotency guide says "for a period of time" and defers to each API's reference,
  https://developer.paypal.com/api/rest/reference/idempotency/, and the Orders v2 schema
  says only "A unique ID identifying the request header for idempotency purposes") nor
  how long an ``APPROVED`` order can still be captured. So the housekeeping sweep never
  relies on either: it reads the order first and captures again only an ``APPROVED``
  order that holds no capture (``read_capture``), and a capture PayPal refuses then
  (an expired or voided order among them) marks the request ``failed``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import httpx
from pydantic import ValidationError

from scadbuddy.orders.currency import minor_exponent
from scadbuddy.payments.models import (
    CaptureOutcome,
    CaptureState,
    HostedRequest,
    PaymentRequestView,
    ProviderEvent,
    ProviderEventKind,
    ProviderEventMethod,
)
from scadbuddy.payments.provider import (
    BadSignatureError,
    Credential,
    ProviderError,
    ProviderField,
    ProviderUnavailableError,
    Verified,
)
from scadbuddy.payments.venmo import PAY_PAGE_MISSING, PAY_PAGE_READY

logger = logging.getLogger(__name__)

API: Final[Mapping[str, str]] = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}
TIMEOUT: Final = httpx.Timeout(20.0)
#: A token is fetched again this long before PayPal says it expires.
TOKEN_MARGIN_SECONDS: Final = 60
#: The ``custom_id`` a cancelled request's order carries, as defence in depth: ScadBuddy
#: captures an approval only while its request is open or failed (``capture_request``), so
#: an unmarked order of a cancelled request is not captured either. The mark is set after
#: the cancel commits. A failed PATCH is logged and tried again by the housekeeping sweep,
#: with a backoff (#5700). A capture completed or under way before the cancel is recorded
#: as a payment on the cancelled request.
CANCELLED: Final = "scadbuddy-cancelled"
#: Currencies PayPal takes in whole units only.
NO_DECIMALS: Final = frozenset({"HUF", "JPY", "TWD"})
#: The webhook headers, with the longest value the verify call takes for each.
SIGNATURE_HEADERS: Final[Mapping[str, tuple[str, int]]] = {
    "auth_algo": ("paypal-auth-algo", 100),
    "cert_url": ("paypal-cert-url", 500),
    "transmission_id": ("paypal-transmission-id", 50),
    "transmission_sig": ("paypal-transmission-sig", 500),
    "transmission_time": ("paypal-transmission-time", 100),
}
#: The events the shop's webhook must send. ``DECLINED`` or its older name will do.
NEEDED_EVENTS: Final = (
    ("CHECKOUT.ORDER.APPROVED",),
    ("PAYMENT.CAPTURE.COMPLETED",),
    ("PAYMENT.CAPTURE.PENDING",),
    ("PAYMENT.CAPTURE.DECLINED", "PAYMENT.CAPTURE.DENIED"),
    ("PAYMENT.CAPTURE.REFUNDED",),
)
#: A capture's 422 issues that mean the customer's method was declined.
_DECLINED: Final = frozenset({"INSTRUMENT_DECLINED", "TRANSACTION_REFUSED"})
_CAPTURE_KINDS: Final[Mapping[str, ProviderEventKind]] = {
    "PAYMENT.CAPTURE.COMPLETED": "paid",
    "PAYMENT.CAPTURE.PENDING": "pending",
    "PAYMENT.CAPTURE.DECLINED": "failed",
    "PAYMENT.CAPTURE.DENIED": "failed",
}
#: The funding sources under an order's ``payment_source`` that a payment names as its
#: method; any other (``paypal``, ``card``) is recorded as ``paypal``.
_METHODS: Final[Mapping[str, ProviderEventMethod]] = {"venmo": "venmo"}

#: A capture's status, read from its order, as the event it settles (``read_capture``).
#: A refunded capture was captured first; the refund is its own event.
_READ_KINDS: Final[Mapping[object, ProviderEventKind]] = {
    "COMPLETED": "paid",
    "PARTIALLY_REFUNDED": "paid",
    "REFUNDED": "paid",
    "PENDING": "pending",
    "DECLINED": "failed",
    "FAILED": "failed",
}
#: A PayPal object id (order, capture, refund); nothing else goes in a path.
_PAYPAL_ID: Final = re.compile(r"[A-Z0-9]{1,36}")
#: The verify call's own pattern for a webhook id.
_WEBHOOK_ID: Final = re.compile(r"[A-Za-z0-9]{1,50}")
_CAPTURE_PATH: Final = re.compile(r"/v2/payments/captures/([^/?#]+)")


class _RefusedError(Exception):
    """PayPal answered with an error status. ``message`` is PayPal's own text."""

    def __init__(self, status: int, issue: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.issue = issue
        self.message = message


class _NoAnswerError(Exception):
    """PayPal did not answer, or answered with something that is not a JSON object."""


@dataclass(frozen=True)
class _Token:
    value: str
    app_id: str
    expires_at: float


def _header(headers: Mapping[str, str], name: str) -> str:
    return next((v for k, v in headers.items() if k.lower() == name), "")


def _refusal(status: int, found: Mapping[str, Any]) -> _RefusedError:
    """PayPal's error shapes: ``name``/``message``/``details[].issue`` for the REST APIs,
    ``error``/``error_description`` for the token."""
    details = found.get("details")
    first = details[0] if isinstance(details, list) and details else {}
    first = first if isinstance(first, dict) else {}
    issue = str(first.get("issue") or found.get("name") or found.get("error") or "")
    message = str(
        first.get("description")
        or found.get("message")
        or found.get("error_description")
        or f"HTTP {status}"
    )
    return _RefusedError(status, issue, message)


class PayPalProvider:
    id = "paypal"
    label = "PayPal"
    reports_currency = False
    #: The wallet under ``payment_source``; Venmo (#4960) is the same order with ``venmo``.
    funding_source = "paypal"
    fields: Sequence[ProviderField] = (
        ProviderField(
            name="environment", label="Environment", secret=False, choices=["sandbox", "live"]
        ),
        ProviderField(name="client_id", label="Client ID", secret=False),
        ProviderField(name="webhook_id", label="Webhook ID", secret=False),
        # #4960: Venmo on the storefront's pay page, for USD requests (`venmo.py`); off when
        # left out, and refused on until that page exists.
        ProviderField(
            name="venmo",
            label="Venmo (USD)",
            secret=False,
            choices=["off", "on"],
            default="off",
            unavailable=None if PAY_PAGE_READY else {"on": PAY_PAGE_MISSING},
        ),
        ProviderField(name="client_secret", label="Secret", secret=True),
    )

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        #: Each app's token, keyed by a hash of its environment, client id and secret.
        self._tokens: dict[str, _Token] = {}

    def __repr__(self) -> str:
        return "PayPalProvider()"

    # -- calls ------------------------------------------------------------------------

    @staticmethod
    def _base(credential: Credential) -> str:
        environment = credential.settings.get("environment", "")
        if environment not in API:
            raise ProviderError("the PayPal environment is sandbox or live")
        return API[environment]

    @staticmethod
    def _token_key(credential: Credential) -> str:
        parts = (
            credential.settings.get("environment", ""),
            credential.settings.get("client_id", ""),
            credential.secrets.get("client_secret", ""),
        )
        return hashlib.sha256("\0".join(parts).encode()).hexdigest()

    async def _send(
        self,
        base: str,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        auth: tuple[str, str] | None = None,
        content: bytes | None = None,
        data: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(base_url=base, timeout=TIMEOUT) as client:
                answer = await client.request(
                    method, path, headers=headers, auth=auth, content=content, data=data
                )
            found = answer.json() if answer.content else {}
        except (httpx.HTTPError, ValueError):
            raise _NoAnswerError() from None
        if not isinstance(found, dict):
            raise _NoAnswerError()
        if answer.is_success:
            return found
        raise _refusal(answer.status_code, found)

    async def _token(self, credential: Credential) -> _Token:
        key = self._token_key(credential)
        cached = self._tokens.get(key)
        if cached is not None and self._clock() < cached.expires_at - TOKEN_MARGIN_SECONDS:
            return cached
        found = await self._send(
            self._base(credential),
            "POST",
            "/v1/oauth2/token",
            headers={"Accept": "application/json"},
            auth=(
                credential.settings.get("client_id", ""),
                credential.secrets.get("client_secret", ""),
            ),
            data={"grant_type": "client_credentials"},
        )
        value, expires_in = found.get("access_token"), found.get("expires_in")
        if not isinstance(value, str) or not isinstance(expires_in, int):
            raise _NoAnswerError()
        now = self._clock()
        token = _Token(value, str(found.get("app_id", "")), now + expires_in)
        # A rotated secret's token is dead weight once it expires.
        self._tokens = {k: t for k, t in self._tokens.items() if t.expires_at > now}
        self._tokens[key] = token
        return token

    async def _call(
        self,
        credential: Credential,
        method: str,
        path: str,
        *,
        body: Any = None,
        raw: bytes | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        token = await self._token(credential)
        headers = {"Authorization": f"Bearer {token.value}", "Content-Type": "application/json"}
        if request_id is not None:
            headers["PayPal-Request-Id"] = request_id
        content = raw if raw is not None else None if body is None else json.dumps(body).encode()
        try:
            return await self._send(
                self._base(credential), method, path, headers=headers, content=content
            )
        except _RefusedError as refused:
            if refused.status == 401:
                # Revoked or expired early: the next call fetches a new one.
                self._tokens.pop(self._token_key(credential), None)
            raise

    async def _act(self, credential: Credential, method: str, path: str, **kwargs: Any) -> Any:
        """A call a person waits on: every failure is a `ProviderError` they can read."""
        try:
            return await self._call(credential, method, path, **kwargs)
        except _NoAnswerError:
            raise ProviderError("PayPal did not answer") from None
        except _RefusedError as refused:
            raise _shown(refused) from None

    async def _read(
        self, credential: Credential, method: str, path: str, *, gone: bool = False, **kwargs: Any
    ) -> Any:
        """A call a webhook needs after its signature held: any failure is
        `ProviderUnavailableError`, so nothing is recorded and PayPal sends it again. With
        ``gone``, a 404 is None: a retry would find nothing either."""
        try:
            return await self._call(credential, method, path, **kwargs)
        except _NoAnswerError:
            logger.warning("PayPal did not answer %s %s", method, path)
        except _RefusedError as refused:
            if gone and refused.status == 404:
                logger.info("PayPal has no %s", path)
                return None
            # The status says why PayPal keeps failing it; it goes to the log only.
            logger.warning("PayPal answered %s to %s %s", refused.status, method, path)
        raise ProviderUnavailableError("PayPal did not answer a call the event needs")

    # -- the seam ---------------------------------------------------------------------

    async def verify(self, credential: Credential) -> Verified:
        environment = credential.settings.get("environment", "")
        self._base(credential)
        if not credential.settings.get("client_id"):
            raise ProviderError("a PayPal client id is needed")
        webhook_id = credential.settings.get("webhook_id", "")
        if not _WEBHOOK_ID.fullmatch(webhook_id):
            raise ProviderError("a PayPal webhook id is letters and digits")
        try:
            token = await self._token(credential)
            hook = await self._call(credential, "GET", f"/v1/notifications/webhooks/{webhook_id}")
        except _NoAnswerError:
            raise ProviderError("PayPal did not answer") from None
        except _RefusedError as refused:
            if refused.status == 404:
                raise ProviderError(f"PayPal has no webhook {webhook_id} for this app") from None
            raise _shown(refused) from None
        types = hook.get("event_types")
        names = (
            {e.get("name") for e in types if isinstance(e, dict)}
            if isinstance(types, list)
            else set()
        )
        if "*" not in names:
            missing = [group[0] for group in NEEDED_EVENTS if not names.intersection(group)]
            if missing:
                raise ProviderError(
                    f"the PayPal webhook {webhook_id} does not send {', '.join(missing)}"
                )
        # An app takes many currencies: none to compare.
        return Verified(account_label=f"PayPal app {token.app_id} ({environment})")

    async def create_request(
        self,
        credential: Credential,
        request: PaymentRequestView,
        *,
        return_url: str | None,
        idempotency_key: str,
        order_number: str,
    ) -> HostedRequest:
        description = (
            f"Deposit for order {order_number}"
            if request.kind == "deposit"
            else f"Order {order_number}"
        )
        experience: dict[str, str] = {
            "user_action": "PAY_NOW",
            "shipping_preference": "NO_SHIPPING",
        }
        if return_url is not None:
            experience |= {"return_url": return_url, "cancel_url": return_url}
        body = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "custom_id": request.id,
                    "invoice_id": f"{order_number}-{request.id}"[:127],
                    "description": description,
                    "amount": {
                        "currency_code": request.currency,
                        "value": _value(request.amount_minor, request.currency),
                    },
                }
            ],
            "payment_source": {self.funding_source: {"experience_context": experience}},
        }
        order = await self._act(
            credential, "POST", "/v2/checkout/orders", body=body, request_id=idempotency_key
        )
        links = order.get("links")
        approve = next(
            (
                link.get("href")
                for link in (links if isinstance(links, list) else [])
                if isinstance(link, dict) and link.get("rel") == "payer-action"
            ),
            None,
        )
        try:
            return HostedRequest.model_validate(
                {"provider_ref": _path_id(str(order.get("id", ""))), "hosted_url": approve}
            )
        except ValidationError:
            raise ProviderError("PayPal's answer named no approval link") from None

    async def cancel_request(self, credential: Credential, provider_ref: str) -> None:
        """Mark the order as cancelled, as defence in depth: ScadBuddy captures an
        approval only while its request is open or failed. An order already completed,
        voided or gone is no error, and neither is one still awaiting the payer, which
        PATCH cannot change (it updates CREATED or APPROVED orders only). A failure here
        is tried again by the housekeeping sweep (#5700)."""
        path = f"/v2/checkout/orders/{_path_id(provider_ref)}"
        patch = [
            {
                "op": "replace",
                "path": "/purchase_units/@reference_id=='default'/custom_id",
                "value": CANCELLED,
            }
        ]
        try:
            await self._call(credential, "PATCH", path, body=patch)
            return
        except _NoAnswerError:
            raise ProviderError("PayPal did not answer") from None
        except _RefusedError as refused:
            if refused.status == 404:
                return
            if refused.status != 422:
                raise _shown(refused) from None
        try:
            order = await self._call(credential, "GET", path)
        except _NoAnswerError:
            raise ProviderError("PayPal did not answer") from None
        except _RefusedError as refused:
            if refused.status == 404:
                return
            raise _shown(refused) from None
        if order.get("status") not in ("COMPLETED", "VOIDED", "PAYER_ACTION_REQUIRED"):
            raise ProviderError("PayPal did not mark the order cancelled")

    async def parse_webhook(
        self, credential: Credential, headers: Mapping[str, str], body: bytes
    ) -> ProviderEvent:
        await self._verify_signature(credential, headers, body)
        try:
            event = json.loads(body)
            event_id = event["id"]
            kind = event["event_type"]
            resource = event["resource"]
            if not isinstance(resource, dict) or not isinstance(kind, str):
                raise TypeError
            found = await self._event(credential, event_id, kind, resource)
            created = event.get("create_time")
            if isinstance(created, str):
                found = found.model_copy(update={"created_at": datetime.fromisoformat(created)})
            return found
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            IndexError,
            ValidationError,
            InvalidOperation,
        ):
            raise ProviderError("the event body is malformed") from None

    # -- the signature ----------------------------------------------------------------

    async def _verify_signature(
        self, credential: Credential, headers: Mapping[str, str], body: bytes
    ) -> None:
        """Raises `BadSignatureError` unless PayPal says the signature is its own. Nothing
        is read from the body before that; a body that is no JSON object, a missing
        header and a missing webhook id are refused with no call."""
        given: dict[str, str] = {}
        for name, (header, longest) in SIGNATURE_HEADERS.items():
            value = _header(headers, header)
            if not value or len(value) > longest:
                raise BadSignatureError("the signature does not match")
            given[name] = value
        webhook_id = credential.settings.get("webhook_id", "")
        if not _WEBHOOK_ID.fullmatch(webhook_id):
            raise BadSignatureError("the signature does not match")
        try:
            if not isinstance(json.loads(body), dict):
                raise ValueError
        except ValueError:
            raise BadSignatureError("the signature does not match") from None
        # The event goes back byte for byte; only the fields around it are encoded.
        around = json.dumps(given | {"webhook_id": webhook_id})
        raw = around[:-1].encode() + b', "webhook_event": ' + body + b"}"
        verdict = await self._read(
            credential, "POST", "/v1/notifications/verify-webhook-signature", raw=raw
        )
        status = verdict.get("verification_status")
        if status == "FAILURE":
            raise BadSignatureError("the signature does not match")
        if status != "SUCCESS":
            logger.warning("PayPal's signature verdict was %r", status)
            raise ProviderUnavailableError("PayPal did not say whether the signature holds")

    # -- events -----------------------------------------------------------------------

    async def _event(
        self, credential: Credential, event_id: str, kind: str, resource: dict[str, Any]
    ) -> ProviderEvent:
        if kind == "CHECKOUT.ORDER.APPROVED":
            return await self._approved(credential, event_id, resource)
        if kind in _CAPTURE_KINDS:
            found = _capture_event(event_id, _CAPTURE_KINDS[kind], resource)
            if found.kind == "paid" and found.provider_ref is not None:
                found = found.model_copy(
                    update={"method": await self._funding(credential, found.provider_ref)}
                )
            return found
        if kind == "PAYMENT.CAPTURE.REFUNDED":
            return await self._refund(credential, event_id, resource)
        return ProviderEvent(event_id=event_id, kind="other")

    async def _approved(
        self, credential: Credential, event_id: str, order: dict[str, Any]
    ) -> ProviderEvent:
        """``approved`` for an order ScadBuddy made that is ``APPROVED`` with no unit marked
        cancelled; the service then decides whether to capture it (``capture_request``).
        The event is a snapshot: a retry of an approval sent before a cancel carries no
        mark, so the order is read again. One PayPal no longer has is ``other``."""
        order_id = _path_id(order["id"])
        other = ProviderEvent(event_id=event_id, kind="other", provider_ref=order_id)
        now = await self._read(credential, "GET", f"/v2/checkout/orders/{order_id}", gone=True)
        if now is None:
            return other
        units = now.get("purchase_units")
        if now.get("status") != "APPROVED" or not isinstance(units, list):
            return other
        if not all(isinstance(u, dict) and _ours(u) for u in units):
            # Cancelled here, or another integration's order on the same REST app.
            return other
        return ProviderEvent(event_id=event_id, kind="approved", provider_ref=order_id)

    async def capture_request(
        self, credential: Credential, provider_ref: str, event_id: str
    ) -> CaptureOutcome:
        """Capture the approved order. Keyed on the approval: its retry captures once, and
        a new approval after a decline is a new capture; ``ORDER_ALREADY_CAPTURED`` counts
        as captured. A declined method is ``declined``. 401, 5xx or no answer is
        ``ProviderUnavailableError`` (PayPal sends the approval again); any other refusal
        would meet the same answer again, so it is a logged ``ProviderError``."""
        path = f"/v2/checkout/orders/{_path_id(provider_ref)}/capture"
        try:
            await self._call(credential, "POST", path, request_id=f"capture-{event_id}")
            return "captured"
        except _NoAnswerError:
            logger.warning("PayPal did not answer POST %s", path)
            raise ProviderUnavailableError("PayPal did not capture the order") from None
        except _RefusedError as refused:
            logger.warning("PayPal answered %s %s to POST %s", refused.status, refused.issue, path)
            if refused.status == 401 or refused.status >= 500:
                raise ProviderUnavailableError("PayPal did not capture the order") from None
            if refused.status == 422 and refused.issue == "ORDER_ALREADY_CAPTURED":
                return "captured"
            if refused.status == 422 and refused.issue in _DECLINED:
                return "declined"
            raise _shown(refused) from None

    async def _funding(self, credential: Credential, order_id: str) -> ProviderEventMethod | None:
        """The method the order was paid with when it is one ScadBuddy names (#4960): the
        capture does not say, the order's ``payment_source`` does. None when the order
        cannot be read: the capture is still recorded, without its method."""
        try:
            order = await self._read(credential, "GET", f"/v2/checkout/orders/{order_id}")
        except ProviderUnavailableError:
            # The capture is recorded without its method rather than held back.
            return None
        return _method_of(order)

    async def read_capture(
        self, credential: Credential, provider_ref: str
    ) -> ProviderEvent | CaptureState:
        """#4959: read the order of an approval whose capture outcome was never told. A
        capture made is its ``paid``, ``pending`` or ``failed`` event, keyed by the
        capture (a later webhook for it writes no second payment, the payment being
        matched by the order). An ``APPROVED`` order with no capture is ``uncaptured``,
        a voided or unknown order ``gone``; anything else, or a failed call, ``unknown``."""
        order_id = _path_id(provider_ref)
        try:
            order = await self._call(credential, "GET", f"/v2/checkout/orders/{order_id}")
        except _NoAnswerError:
            logger.warning("PayPal did not answer the read of order %s", order_id)
            return "unknown"
        except _RefusedError as refused:
            if refused.status == 404:
                return "gone"
            logger.warning("PayPal answered %s to the read of order %s", refused.status, order_id)
            return "unknown"
        try:
            captures = [
                capture
                for unit in order.get("purchase_units") or []
                for capture in ((unit.get("payments") or {}).get("captures") or [])
                if isinstance(capture, dict)
            ]
            status = order.get("status")
            if status == "VOIDED" and not captures:
                return "gone"
            if status == "APPROVED" and not captures:
                return "uncaptured"
            for capture in captures:
                kind = _READ_KINDS.get(capture.get("status"))
                if kind is not None:
                    capture_id = _path_id(capture.get("id"))
                    found = _capture_event(f"capture:{capture_id}", kind, capture, order_id)
                    if kind == "paid":
                        # The order read names the funding source (#4960), as `_funding`.
                        found = found.model_copy(update={"method": _method_of(order)})
                    return found
        except (KeyError, TypeError, AttributeError, ValueError, ValidationError, ProviderError):
            logger.warning("PayPal's order %s did not read as expected", order_id)
        return "unknown"

    async def _refund(
        self, credential: Credential, event_id: str, refund: dict[str, Any]
    ) -> ProviderEvent:
        """A completed refund, keyed by its own id, for its own amount. Its order comes
        from its capture. One not completed yet is read again: PayPal names no event for a
        pending refund that completes later."""
        if refund.get("status") != "COMPLETED":
            refund_id = refund.get("id")
            if not isinstance(refund_id, str) or not _PAYPAL_ID.fullmatch(refund_id):
                return ProviderEvent(event_id=event_id, kind="other")
            again = await self._read(
                credential, "GET", f"/v2/payments/refunds/{refund_id}", gone=True
            )
            if again is None or again.get("status") != "COMPLETED":
                return ProviderEvent(event_id=event_id, kind="other")
            refund = again
        up = next(
            (
                link.get("href")
                for link in refund["links"]
                if isinstance(link, dict) and link.get("rel") == "up"
            ),
            None,
        )
        found = _CAPTURE_PATH.search(httpx.URL(up).path) if isinstance(up, str) else None
        if found is None:
            raise ProviderError("the refund names no capture")
        capture = await self._read(
            credential, "GET", f"/v2/payments/captures/{_path_id(found.group(1))}"
        )
        amount, currency = _money(refund["amount"])
        return ProviderEvent(
            event_id=f"refund:{_path_id(refund['id'])}",
            kind="refunded",
            provider_ref=_order_of(capture),
            amount_minor=amount,
            currency=currency,
        )


def _shown(refused: _RefusedError) -> ProviderError:
    if refused.status == 401:
        return ProviderError("PayPal refused the client id and secret")
    if refused.status == 403:
        return ProviderError("the PayPal app lacks a permission ScadBuddy needs")
    return ProviderError(f"PayPal refused: {refused.issue}: {refused.message}")


def _ours(unit: Mapping[str, Any]) -> bool:
    """A purchase unit ScadBuddy made and has not cancelled: ``custom_id`` is the request id
    and ``invoice_id`` is ``<order number>-<request id>`` (``create_request``)."""
    custom_id = unit.get("custom_id")
    invoice_id = unit.get("invoice_id")
    return (
        isinstance(custom_id, str)
        and bool(custom_id)
        and custom_id != CANCELLED
        and isinstance(invoice_id, str)
        and invoice_id.endswith(f"-{custom_id}")
    )


def _path_id(value: object) -> str:
    if not isinstance(value, str) or not _PAYPAL_ID.fullmatch(value):
        raise ProviderError("that is not a PayPal id")
    return value


def _value(amount_minor: int, currency: str) -> str:
    """``amount_minor`` as PayPal's decimal string."""
    exponent = minor_exponent(currency)
    major = Decimal(amount_minor).scaleb(-exponent)
    if currency.upper() in NO_DECIMALS:
        if major != major.to_integral_value():
            raise ProviderError(f"PayPal takes whole {currency.upper()} amounts only")
        return str(int(major))
    return f"{major:.{exponent}f}"


def _money(money: Mapping[str, Any]) -> tuple[int, str]:
    """A PayPal ``money`` object as minor units and its currency."""
    currency = str(money["currency_code"]).upper()
    minor = Decimal(str(money["value"])).scaleb(minor_exponent(currency))
    if minor != minor.to_integral_value():
        raise ValueError("not a whole number of minor units")
    return int(minor), currency


def _order_of(capture: Mapping[str, Any]) -> str:
    return _path_id(capture["supplementary_data"]["related_ids"]["order_id"])


def _method_of(order: Mapping[str, Any]) -> ProviderEventMethod | None:
    """The method an order's ``payment_source`` names, when ScadBuddy names it (#4960)."""
    source = order.get("payment_source")
    if not isinstance(source, dict):
        return None
    return next((_METHODS[key] for key in source if key in _METHODS), None)


def _capture_event(
    event_id: str, kind: ProviderEventKind, capture: dict[str, Any], order_id: str | None = None
) -> ProviderEvent:
    """A capture as its event. A webhook's capture names its order; one read from its
    order (``read_capture``) is given the order's id."""
    order_id = _order_of(capture) if order_id is None else order_id
    if kind == "failed":
        return ProviderEvent(event_id=event_id, kind=kind, provider_ref=order_id)
    amount, currency = _money(capture["amount"])
    fee: int | None = None
    breakdown = capture.get("seller_receivable_breakdown")
    paypal_fee = breakdown.get("paypal_fee") if isinstance(breakdown, dict) else None
    if kind == "paid" and isinstance(paypal_fee, dict):
        fee_minor, fee_currency = _money(paypal_fee)
        fee = fee_minor if fee_currency == currency else None
    return ProviderEvent(
        event_id=event_id,
        kind=kind,
        provider_ref=order_id,
        amount_minor=amount,
        currency=currency,
        fee_minor=fee,
    )


PAYPAL = PayPalProvider()
