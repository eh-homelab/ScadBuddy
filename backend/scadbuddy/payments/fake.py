"""A provider that needs no network (#4957): dev runs and tests pay through it. It signs
a webhook body with HMAC-SHA256 under the shop's ``signing_secret``. The registry offers
it only when ``SCADBUDDY_PAYMENTS_FAKE_PROVIDER`` is true."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from typing import Final

from pydantic import ValidationError

from scadbuddy.payments.models import (
    CaptureOutcome,
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
)

SIGNATURE_HEADER: Final = "x-fake-signature"
_KINDS: Final[frozenset[str]] = frozenset(
    {"paid", "pending", "failed", "refunded", "refund_failed", "expired", "fee", "approved"}
)


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def signed_event(secret: str, event: Mapping[str, object]) -> tuple[bytes, dict[str, str]]:
    """The body and headers a test or a dev script posts to the webhook."""
    body = json.dumps(dict(event), sort_keys=True).encode()
    return body, {SIGNATURE_HEADER: sign(secret, body)}


class FakeProvider:
    id = "fake"
    label = "Fake provider (development)"
    fields: Sequence[ProviderField] = (
        ProviderField(name="account", label="Account", secret=False),
        ProviderField(name="signing_secret", label="Signing secret", secret=True),
    )

    def __init__(self) -> None:
        #: Each ``provider_ref`` whose page was closed, in order, for tests to read.
        self.cancelled: list[str] = []
        #: Each ``(provider_ref, event_id)`` captured after an ``approved`` event (#4959).
        self.captured: list[tuple[str, str]] = []

    async def verify(self, credential: Credential) -> str:
        return f"Fake account {credential.settings.get('account', '')}"

    async def create_request(
        self,
        credential: Credential,
        request: PaymentRequestView,
        *,
        return_url: str | None,
        idempotency_key: str,
        order_number: str,
    ) -> HostedRequest:
        ref = f"fake_{request.id}"
        return HostedRequest.model_validate(
            {"provider_ref": ref, "hosted_url": f"https://payments.fake.invalid/pay/{ref}"}
        )

    async def cancel_request(self, credential: Credential, provider_ref: str) -> None:
        self.cancelled.append(provider_ref)

    async def capture_request(
        self, credential: Credential, provider_ref: str, event_id: str
    ) -> CaptureOutcome:
        self.captured.append((provider_ref, event_id))
        return "captured"

    async def parse_webhook(
        self, credential: Credential, headers: Mapping[str, str], body: bytes
    ) -> ProviderEvent:
        secret = credential.secrets.get("signing_secret", "")
        given = next((v for k, v in headers.items() if k.lower() == SIGNATURE_HEADER), "")
        if not secret or not hmac.compare_digest(given.encode(), sign(secret, body).encode()):
            raise BadSignatureError("the signature does not match")
        try:
            data = json.loads(body)
            kind: ProviderEventKind = data["type"] if data.get("type") in _KINDS else "other"
            return ProviderEvent(
                event_id=data["id"],
                kind=kind,
                provider_ref=data.get("provider_ref"),
                amount_minor=data.get("amount_minor"),
                currency=data.get("currency"),
                fee_minor=data.get("fee_minor"),
                created_at=data.get("created_at"),
                reverses=data.get("reverses"),
            )
        except (ValueError, KeyError, TypeError, AttributeError, ValidationError):
            raise ProviderError("the event body is malformed") from None


FAKE = FakeProvider()
