"""Stripe (#4958): a payment request is a Checkout Session, and Stripe's webhook tells
ScadBuddy what became of it. The adapter holds no state: the shop's secret key and webhook
signing secret come in with each call, and neither is logged or put in an error.

Stripe's documented behaviour this relies on:

- A Checkout Session in ``mode=payment`` with inline ``price_data``; ``payment_intent_data
  .metadata`` lands on the PaymentIntent, and Stripe copies a PaymentIntent's metadata to
  its Charge (https://docs.stripe.com/api/checkout/sessions/create,
  https://docs.stripe.com/metadata#copy-metadata-to-another-object). With no
  ``payment_method_types`` the account's dynamic payment methods apply, so card and US
  bank account (ACH Direct Debit) appear when the account has them on
  (https://docs.stripe.com/payments/ach-direct-debit).
- Only an ``open`` session can be expired
  (https://docs.stripe.com/api/checkout/sessions/expire).
- ``Stripe-Signature`` is ``t=<timestamp>`` and one ``v1=`` per active secret: HMAC-SHA256
  of ``<timestamp>.<body>`` under the endpoint secret; other schemes are ignored, and
  Stripe's libraries allow 5 minutes (https://docs.stripe.com/webhooks#verify-manually).
- A delayed method (ACH) completes the session with ``payment_status`` ``unpaid`` and
  settles later with ``checkout.session.async_payment_succeeded`` or ``_failed``
  (https://docs.stripe.com/checkout/fulfillment).
- The fee is on the Charge's balance transaction, which Stripe may create after the
  payment succeeds; ``charge.updated`` tells when it is there
  (https://docs.stripe.com/expand/use-cases#see-the-stripe-fee-for-a-given-payment).
- A refund is read from the Refund's own events, ``refund.created`` and
  ``refund.updated``: a Refund carries its own ``amount``, ``payment_intent`` and
  ``status`` (``pending``, ``requires_action``, ``succeeded``, ``failed``, ``canceled``)
  (https://docs.stripe.com/api/refunds/object, https://docs.stripe.com/refunds#refund-events).
  ``charge.refunded`` is not read: its Charge holds only the running ``amount_refunded``,
  and ``data.previous_attributes`` is documented only for ``*.updated`` events
  (https://docs.stripe.com/api/events/object#event_object-data-previous_attributes).
  A Refund is recorded once it is ``succeeded``, under the event id ``refund:<id>``, so
  its ``refund.created`` and a later ``refund.updated`` record it once.
- A refund can fail: the bank returns the money, Stripe adds it back to the balance, the
  Refund's status moves to ``failed``, and Stripe sends ``refund.failed``
  (https://docs.stripe.com/refunds#failed-refunds). It can fail without ever succeeding
  (``insufficient_funds`` past the pending window), and a refund that succeeded can come
  back (``requires_action``) and then be ``canceled``, which is a kind of failure too
  (https://docs.stripe.com/refunds#refunds-that-require-action,
  https://docs.stripe.com/refunds#cancel-refund). So ``failure_balance_transaction`` does
  not tell whether ScadBuddy recorded the refund: every ``failed`` or ``canceled`` Refund,
  from ``refund.failed`` or ``refund.updated``, is one ``refund_failed`` event,
  ``refund-failed:<id>``, naming the refund's own event id (``reverses``). The lifecycle
  reverses the refund when it was recorded, and otherwise keeps the failure so the refund
  told later writes nothing (#5681).
- Events can arrive in any order (https://docs.stripe.com/webhooks#event-ordering), and a
  non-2xx answer makes Stripe send the event again
  (https://docs.stripe.com/webhooks#automatic-retries).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
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
    Verified,
)

logger = logging.getLogger(__name__)

API: Final = "https://api.stripe.com"
#: The API version every call asks for, so a response's shape does not follow the
#: account's default (https://docs.stripe.com/api/versioning). Events still follow the
#: account's version; the fields read from them are the same in every version since
#: ``latest_charge`` (2022-11-15).
STRIPE_VERSION: Final = "2026-10-28.endive"
SIGNATURE_HEADER: Final = "stripe-signature"
SIGNATURE_TOLERANCE_SECONDS: Final = 300
TIMEOUT: Final = httpx.Timeout(20.0)

#: Metadata keys on the session and, through ``payment_intent_data``, the Charge.
ORDER_NUMBER: Final = "scadbuddy_order_number"
ORDER_ID: Final = "scadbuddy_order_id"
REQUEST_ID: Final = "scadbuddy_payment_request_id"
#: A Stripe object id (``cs_…``, ``pi_…``, ``txn_…``); nothing else goes in a path.
_STRIPE_ID: Final = re.compile(r"[A-Za-z0-9_]{1,255}")
_TIMESTAMP: Final = re.compile(r"[0-9]{1,12}")


class _RefusedError(Exception):
    """Stripe answered with an error status. ``message`` is Stripe's own text."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _NoAnswerError(Exception):
    """Stripe did not answer, or answered with something that is not JSON."""


def _header(headers: Mapping[str, str], name: str) -> str:
    return next((v for k, v in headers.items() if k.lower() == name), "")


def verify_signature(secret: str, header: str, body: bytes, *, now: float) -> None:
    """Raises `BadSignatureError` unless one ``v1`` signature in ``header`` is ours and
    its timestamp is within the tolerance."""
    timestamp: int | None = None
    signatures: list[str] = []
    for part in header.split(","):
        prefix, _, value = part.strip().partition("=")
        # ASCII digits of a bounded length only: ``str.isdigit`` takes "\u00b2" and
        # 5000 digits, which ``int`` refuses.
        if prefix == "t" and _TIMESTAMP.fullmatch(value):
            timestamp = int(value)
        elif prefix == "v1":
            signatures.append(value)
    if not secret or timestamp is None or not signatures:
        raise BadSignatureError("the signature does not match")
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    digest = expected.hexdigest().encode()
    if not any(hmac.compare_digest(digest, s.encode()) for s in signatures):
        raise BadSignatureError("the signature does not match")
    if abs(now - timestamp) > SIGNATURE_TOLERANCE_SECONDS:
        raise BadSignatureError("the signature is too old or too far ahead")


class StripeProvider:
    id = "stripe"
    label = "Stripe"
    reports_currency = False
    fields: Sequence[ProviderField] = (
        ProviderField(name="secret_key", label="Secret or restricted key", secret=True),
        ProviderField(name="webhook_secret", label="Webhook signing secret", secret=True),
    )

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock

    def __repr__(self) -> str:
        return "StripeProvider()"

    # -- calls ------------------------------------------------------------------------

    async def _call(
        self,
        credential: Credential,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {credential.secrets.get('secret_key', '')}",
            "Stripe-Version": STRIPE_VERSION,
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            async with httpx.AsyncClient(base_url=API, timeout=TIMEOUT) as client:
                answer = await client.request(
                    method, path, params=params, data=data, headers=headers
                )
            found = answer.json()
        except (httpx.HTTPError, ValueError):
            raise _NoAnswerError() from None
        if not isinstance(found, dict):
            raise _NoAnswerError()
        if answer.is_success:
            return found
        error = found.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        raise _RefusedError(answer.status_code, str(message or f"HTTP {answer.status_code}"))

    async def _act(self, credential: Credential, method: str, path: str, **kwargs: Any) -> Any:
        """A call a person waits on: every failure is a `ProviderError` they can read."""
        try:
            return await self._call(credential, method, path, **kwargs)
        except _NoAnswerError:
            raise ProviderError("Stripe did not answer") from None
        except _RefusedError as refused:
            # Stripe's text for these two quotes part of the key.
            if refused.status == 401:
                raise ProviderError("Stripe refused the secret key") from None
            if refused.status == 403:
                raise ProviderError("the Stripe key lacks a permission ScadBuddy needs") from None
            raise ProviderError(f"Stripe refused: {refused.message}") from None

    async def _read(self, credential: Credential, path: str, params: Mapping[str, str]) -> Any:
        """A read a webhook needs after its signature held: any failure is
        `ProviderUnavailableError`, so nothing is recorded and Stripe sends it again."""
        try:
            return await self._call(credential, "GET", path, params=params)
        except _NoAnswerError:
            logger.warning("Stripe did not answer GET %s", path)
        except _RefusedError as refused:
            # The status says why Stripe keeps failing it (a 404, a missing permission);
            # it goes to the log only, never to the caller.
            logger.warning("Stripe answered %s to GET %s", refused.status, path)
        raise ProviderUnavailableError("Stripe did not answer a read the event needs")

    # -- the seam ---------------------------------------------------------------------

    async def verify(self, credential: Credential) -> Verified:
        key = credential.secrets.get("secret_key", "")
        if not key.startswith(("sk_", "rk_")):
            raise ProviderError("a Stripe secret key starts sk_ or rk_ (a restricted key)")
        if not credential.secrets.get("webhook_secret", "").startswith("whsec_"):
            raise ProviderError("a Stripe webhook signing secret starts whsec_")
        # The permission every request needs; a restricted key may lack the account read.
        await self._act(credential, "GET", "/v1/checkout/sessions", params={"limit": "1"})
        # An account takes many currencies: none to compare.
        live = "_live_" in key
        return Verified(
            account_label="Stripe account (live)" if live else "Stripe account (test mode)"
        )

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
        meta = {ORDER_NUMBER: order_number, ORDER_ID: request.order_id, REQUEST_ID: request.id}
        data = {
            "mode": "payment",
            "client_reference_id": request.id,
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": request.currency.lower(),
            "line_items[0][price_data][unit_amount]": str(request.amount_minor),
            "line_items[0][price_data][product_data][name]": name,
            **{f"metadata[{k}]": v for k, v in meta.items()},
            **{f"payment_intent_data[metadata][{k}]": v for k, v in meta.items()},
        }
        if return_url is not None:
            data["success_url"] = return_url
        session = await self._act(
            credential,
            "POST",
            "/v1/checkout/sessions",
            data=data,
            idempotency_key=idempotency_key,
        )
        try:
            return HostedRequest.model_validate(
                {"provider_ref": session["id"], "hosted_url": session["url"]}
            )
        except (KeyError, ValidationError):
            raise ProviderError("Stripe's answer named no Checkout Session page") from None

    async def cancel_request(self, credential: Credential, provider_ref: str) -> None:
        path = f"/v1/checkout/sessions/{_path_id(provider_ref)}"
        try:
            await self._call(credential, "POST", f"{path}/expire")
            return
        except _NoAnswerError:
            raise ProviderError("Stripe did not answer") from None
        except _RefusedError as refused:
            if refused.status != 400:
                raise ProviderError(f"Stripe refused: {refused.message}") from None
        # Stripe refuses a session that is not open; one already closed is no error.
        session = await self._act(credential, "GET", path)
        if session.get("status") == "open":
            raise ProviderError("Stripe did not expire the Checkout Session")

    async def parse_webhook(
        self, credential: Credential, headers: Mapping[str, str], body: bytes
    ) -> ProviderEvent:
        verify_signature(
            credential.secrets.get("webhook_secret", ""),
            _header(headers, SIGNATURE_HEADER),
            body,
            now=self._clock(),
        )
        try:
            event = json.loads(body)
            event_id = event["id"]
            kind = event["type"]
            obj = event["data"]["object"]
            previous = event["data"].get("previous_attributes") or {}
            if not isinstance(obj, dict) or not isinstance(previous, dict):
                raise TypeError
            found = await self._event(credential, event_id, kind, obj, previous)
            created = event.get("created")
            if isinstance(created, int):
                found = found.model_copy(
                    update={"created_at": datetime.fromtimestamp(created, UTC)}
                )
            return found
        except (ValueError, KeyError, TypeError, AttributeError, ValidationError):
            raise ProviderError("the event body is malformed") from None

    # -- events -----------------------------------------------------------------------

    async def _event(
        self,
        credential: Credential,
        event_id: str,
        kind: str,
        obj: dict[str, Any],
        previous: dict[str, Any],
    ) -> ProviderEvent:
        other = ProviderEvent(event_id=event_id, kind="other")
        if kind.startswith("checkout.session."):
            return await self._session_event(credential, event_id, kind, obj) or other
        if kind in ("refund.created", "refund.updated", "refund.failed"):
            return await self._refund(credential, obj) or other
        if kind == "charge.updated":
            return await self._late_fee(credential, event_id, obj, previous) or other
        return other

    async def _session_event(
        self, credential: Credential, event_id: str, kind: str, session: dict[str, Any]
    ) -> ProviderEvent | None:
        ref = session["id"]
        currency = str(session["currency"]).upper()
        amount = session.get("amount_total")
        settled: ProviderEventKind | None = None
        if kind == "checkout.session.completed":
            status = session.get("payment_status")
            if status == "unpaid":
                return ProviderEvent(
                    event_id=event_id,
                    kind="pending",
                    provider_ref=ref,
                    amount_minor=amount,
                    currency=currency,
                )
            settled = "paid" if status == "paid" else None
        elif kind == "checkout.session.async_payment_succeeded":
            settled = "paid"
        elif kind == "checkout.session.async_payment_failed":
            return ProviderEvent(event_id=event_id, kind="failed", provider_ref=ref)
        elif kind == "checkout.session.expired":
            return ProviderEvent(event_id=event_id, kind="expired", provider_ref=ref)
        if settled is None:
            return None
        fee = await self._payment_fee(credential, session.get("payment_intent"), currency)
        return ProviderEvent(
            event_id=event_id,
            kind=settled,
            provider_ref=ref,
            amount_minor=amount,
            currency=currency,
            fee_minor=fee,
        )

    async def _payment_fee(
        self, credential: Credential, payment_intent: object, currency: str
    ) -> int | None:
        """The fee when Stripe already made the balance transaction; None until then (a
        later ``charge.updated`` brings it) or when it is in another currency."""
        if not isinstance(payment_intent, str):
            return None
        intent = await self._read(
            credential,
            f"/v1/payment_intents/{_path_id(payment_intent)}",
            {"expand[]": "latest_charge.balance_transaction"},
        )
        charge = intent.get("latest_charge")
        txn = charge.get("balance_transaction") if isinstance(charge, dict) else None
        return _fee(txn, currency)

    async def _session_of(self, credential: Credential, payment_intent: object) -> str | None:
        """The Checkout Session that made ``payment_intent``: the request's reference."""
        if not isinstance(payment_intent, str):
            return None
        found = await self._read(
            credential,
            "/v1/checkout/sessions",
            {"payment_intent": payment_intent, "limit": "1"},
        )
        sessions = found.get("data") or []
        return str(sessions[0]["id"]) if sessions else None

    async def _refund(
        self,
        credential: Credential,
        refund: dict[str, Any],
    ) -> ProviderEvent | None:
        """A succeeded Refund, keyed by its own id, for its own amount; or a failed or
        cancelled one, which reverses it."""
        status = refund.get("status")
        refund_key = f"refund:{_path_id(refund['id'])}"
        if status in ("failed", "canceled"):
            return ProviderEvent(
                event_id=f"refund-failed:{_path_id(refund['id'])}",
                kind="refund_failed",
                provider_ref=await self._session_of(credential, refund.get("payment_intent")),
                currency=str(refund["currency"]).upper(),
                reverses=refund_key,
            )
        if status != "succeeded":
            return None
        return ProviderEvent(
            event_id=refund_key,
            kind="refunded",
            provider_ref=await self._session_of(credential, refund.get("payment_intent")),
            amount_minor=int(refund["amount"]),
            currency=str(refund["currency"]).upper(),
        )

    async def _late_fee(
        self,
        credential: Credential,
        event_id: str,
        charge: dict[str, Any],
        previous: dict[str, Any],
    ) -> ProviderEvent | None:
        txn_id = charge.get("balance_transaction")
        if (
            not _ours(charge)
            or "balance_transaction" not in previous
            or not isinstance(txn_id, str)
        ):
            return None
        currency = str(charge["currency"]).upper()
        txn = await self._read(credential, f"/v1/balance_transactions/{_path_id(txn_id)}", {})
        fee = _fee(txn, currency)
        if fee is None:
            return None
        return ProviderEvent(
            event_id=event_id,
            kind="fee",
            provider_ref=await self._session_of(credential, charge.get("payment_intent")),
            currency=currency,
            fee_minor=fee,
        )


def _path_id(value: str) -> str:
    if not _STRIPE_ID.fullmatch(value):
        raise ProviderError("that is not a Stripe id")
    return value


def _ours(charge: Mapping[str, Any]) -> bool:
    """A Charge from a ScadBuddy Checkout Session carries its request id; another sale on
    the same account does not, and costs no API call."""
    meta = charge.get("metadata")
    return isinstance(meta, dict) and bool(meta.get(REQUEST_ID))


def _fee(txn: object, currency: str) -> int | None:
    if not isinstance(txn, dict) or str(txn.get("currency", "")).upper() != currency:
        return None
    fee = txn.get("fee")
    return fee if isinstance(fee, int) and fee >= 0 else None


STRIPE = StripeProvider()
