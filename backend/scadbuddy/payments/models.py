"""Row shapes of the payments seam (#4957). ``PaymentRequest`` is the storefront's shared
shape and never carries ``provider_ref``; the back office reads ``PaymentRequestView``."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, UrlConstraints

from scadbuddy.orders.models import Actor, Money

PaymentRequestKind = Literal["deposit", "balance"]
PaymentRequestStatus = Literal["open", "paid", "expired", "cancelled"]
ProviderEventKind = Literal["paid", "refunded", "expired", "other"]
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


class ProviderEvent(BaseModel):
    """One event a provider sent, reduced to what ScadBuddy reads."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=200)
    kind: ProviderEventKind
    provider_ref: str | None = None
    amount_minor: Money | None = Field(default=None, ge=1)
    currency: str | None = None
    fee_minor: Money | None = Field(default=None, ge=0)


class HostedRequest(BaseModel):
    """What a provider answers when it creates a hosted payment page."""

    provider_ref: str
    hosted_url: HttpsUrl
