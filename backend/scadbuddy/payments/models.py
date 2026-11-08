"""Row shapes of the payments seam (#4957). ``PaymentRequest`` is the storefront's shared
shape and never carries ``provider_ref``; the back office reads ``PaymentRequestView``."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, UrlConstraints

from scadbuddy.orders.models import Actor, Money

PaymentRequestKind = Literal["deposit", "balance"]
#: ``pending``: the customer paid by a method that settles later (a US bank debit, #4958);
#: ``failed``: that payment did not settle. Neither records money.
PaymentRequestStatus = Literal["open", "pending", "paid", "expired", "cancelled", "failed"]
#: ``fee``: the provider's fee for a payment it already told, once it is known (#4958).
#: ``approved``: the customer approved a payment that ScadBuddy must capture (#4959). Only a
#: provider with ``capture_request`` sends it, and the service captures only while the
#: request is open or failed.
ProviderEventKind = Literal[
    "paid",
    "pending",
    "failed",
    "refunded",
    "refund_failed",
    "expired",
    "fee",
    "approved",
    "other",
]
#: ``capture_request``'s answer: the money is captured (or was already), or the provider
#: declined the customer's method, so the request reads ``failed``.
CaptureOutcome = Literal["captured", "declined"]
#: A method a provider can name for its payment, other than itself (#4960: a PayPal
#: capture funded by Venmo). A payment's ``method`` is this, or the provider's id.
ProviderEventMethod = Literal["venmo"]
EventOutcome = Literal["recorded", "replayed", "unmatched", "ignored", "currency_mismatch"]

HttpsUrl = Annotated[HttpUrl, UrlConstraints(allowed_schemes=["https"])]


class PaymentRequest(BaseModel):
    id: str
    order_id: str
    kind: PaymentRequestKind
    provider: str | None
    amount_minor: Money
    currency: str
    hosted_url: str | None
    status: PaymentRequestStatus
    created_at: datetime
    paid_at: datetime | None


class PaymentRequestView(PaymentRequest):
    """What the back office reads: the request plus the provider's own reference."""

    provider_ref: str | None
    link_error: str | None
    payment_id: str | None
    created_by: Actor
    #: #5700: the request was closed here (a cancel, or a payment that is not the page's
    #: own) and the provider has not yet confirmed that its page is closed. The
    #: housekeeping sweep tries again until it does.
    close_pending_at: datetime | None = None
    #: The sweep stopped trying: the first try, at or after the
    #: `payments.store.CLOSE_GIVE_UP_ATTEMPTS`th, that found the provider not connected.
    #: Close the page at the provider.
    close_abandoned_at: datetime | None = None


class ProviderEvent(BaseModel):
    """One event a provider sent, reduced to what ScadBuddy reads."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=200)
    kind: ProviderEventKind
    provider_ref: str | None = None
    amount_minor: Money | None = Field(default=None, ge=1)
    currency: str | None = None
    fee_minor: Money | None = Field(default=None, ge=0)
    #: How the customer paid, when the provider names a method of its own (#4960).
    method: ProviderEventMethod | None = None
    #: When the provider made the event (#4958): how long a ``fee`` that arrived
    #: before its payment is sent back for a retry (``Lifecycle._late_fee``).
    created_at: datetime | None = None
    #: A ``refund_failed`` event's: the event id of the refund it reverses (#5681).
    reverses: str | None = Field(default=None, min_length=1, max_length=200)


class HostedRequest(BaseModel):
    """What a provider answers when it creates a hosted payment page."""

    provider_ref: str
    hosted_url: HttpsUrl
