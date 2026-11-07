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
_KINDS: Final[frozenset[str]] = frozenset({"paid", "refunded", "expired"})


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

    async def verify(self, credential: Credential) -> str:
        return f"Fake account {credential.settings.get('account', '')}"

    async def create_request(
        self,
        credential: Credential,
        request: PaymentRequestView,
        *,
        return_url: str | None,
        idempotency_key: str,
    ) -> HostedRequest:
        ref = f"fake_{request.id}"
        return HostedRequest.model_validate(
            {"provider_ref": ref, "hosted_url": f"https://payments.fake.invalid/pay/{ref}"}
        )

    def parse_webhook(
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
            )
        except (ValueError, KeyError, TypeError, AttributeError, ValidationError):
            raise ProviderError("the event body is malformed") from None


FAKE = FakeProvider()
