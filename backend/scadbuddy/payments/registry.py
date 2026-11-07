"""The providers ScadBuddy can connect (#4957). #4958 adds Stripe here."""

from __future__ import annotations

from scadbuddy.payments.fake import FAKE
from scadbuddy.payments.provider import PaymentProvider


def providers(*, fake: bool) -> dict[str, PaymentProvider]:
    offered: dict[str, PaymentProvider] = {FAKE.id: FAKE} if fake else {}
    return offered
