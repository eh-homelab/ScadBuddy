"""The provider seam (#4957): what a payment provider must offer ScadBuddy. A provider
adapter (the fake one, Stripe's in ``stripe.py``, PayPal's in ``paypal.py``, Square's in
``square.py``) holds no state but PayPal's in-memory access token (#4959); the shop's
credential comes in with each call."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel

from scadbuddy.payments.models import (
    CaptureOutcome,
    CaptureState,
    HostedRequest,
    PaymentRequestView,
    ProviderEvent,
)


@dataclass(frozen=True)
class Credential:
    """The shop's account at a provider: plain settings and opened secrets. The secrets
    stay out of ``repr``."""

    settings: Mapping[str, str]
    secrets: Mapping[str, str] = field(repr=False)


class ProviderField(BaseModel):
    """One input the connect form asks for."""

    name: str
    label: str
    secret: bool


class Verified(BaseModel):
    """What a provider's check of the credential says: the account label for "Connected
    as ...", and the one currency the account takes payments in when it has one (#5666: a
    Square location's). ``Payments.connect`` refuses an account whose currency is not the
    shop's ``orders_currency``; None is an account that takes any."""

    account_label: str
    currency: str | None = None


class BadSignatureError(Exception):
    """A webhook whose signature does not match. Nothing of it was read."""


class ProviderError(Exception):
    """A provider call failed. The message is safe to show: it carries no secret."""


class EventTooEarlyError(Exception):
    """A provider event that needs a record not written yet: a fee that arrived before
    its payment's event committed (#4958). The lifecycle writes nothing, not even the
    event's row, so the provider's retry is read again."""


class ProviderUnavailableError(Exception):
    """A signed webhook whose reading needs the provider's API, and that call failed
    (#4958). Nothing is recorded and the webhook answers 503, so the provider sends the
    event again. The message is safe to show."""


class PaymentProvider(Protocol):
    id: str
    label: str
    fields: Sequence[ProviderField]
    #: Whether an account takes one currency, which `verify` gives as
    #: `Verified.currency` (#5666: a Square location). Settings asks to reconnect such
    #: an account whose currency was never kept.
    reports_currency: bool

    async def verify(self, credential: Credential) -> Verified:
        """Check the credential; the account label for "Connected as ..." and the
        account's currency, when it has one."""
        ...

    async def create_request(
        self,
        credential: Credential,
        request: PaymentRequestView,
        *,
        return_url: str | None,
        idempotency_key: str,
        order_number: str,
    ) -> HostedRequest: ...

    async def cancel_request(self, credential: Credential, provider_ref: str) -> None:
        """Close the hosted page of a request ScadBuddy no longer collects, so no one
        pays it. A page already closed is no error."""
        ...

    async def parse_webhook(
        self, credential: Credential, headers: Mapping[str, str], body: bytes
    ) -> ProviderEvent:
        """Raises `BadSignatureError` before it parses anything. Once the signature holds
        it may call the provider (#4958: Stripe's fee and a refund's session); a failed
        call is `ProviderUnavailableError`."""
        ...


class CapturingProvider(PaymentProvider, Protocol):
    """A provider whose customer approves a payment that ScadBuddy then captures
    (PayPal, #4959). Only such a provider's ``parse_webhook`` returns ``approved``. The
    service moves an open or failed request to ``pending`` under the order lock, commits,
    and only then calls ``capture_request``; a cancelled or paid request is never
    captured."""

    async def capture_request(
        self, credential: Credential, provider_ref: str, event_id: str
    ) -> CaptureOutcome:
        """Capture the approved payment, keyed on ``event_id`` so a retry captures once.
        ``ProviderUnavailableError`` when the provider did not answer (the request stays
        ``pending`` and the event's row is kept, so the provider's retry of the event reads
        ``replayed`` and captures again under the same key);
        ``ProviderError`` for a refusal a retry would meet again."""
        ...

    async def read_capture(
        self, credential: Credential, provider_ref: str
    ) -> ProviderEvent | CaptureState:
        """#4959, the housekeeping sweep: what became of the capture of an approved
        payment whose capture outcome was never told. A capture made: its ``paid`` event
        (keyed by the capture, so a later webhook writes no second payment) or its
        ``failed`` one. Otherwise a `CaptureState`. Never raises for a failed call: one
        that cannot be told is ``unknown``."""
        ...
