"""The providers ScadBuddy can connect (#4957): Stripe (#4958) always, the fake one only
when asked."""

from __future__ import annotations

from scadbuddy.payments.fake import FAKE
from scadbuddy.payments.provider import PaymentProvider
from scadbuddy.payments.stripe import STRIPE


def providers(*, fake: bool) -> dict[str, PaymentProvider]:
    offered: dict[str, PaymentProvider] = {STRIPE.id: STRIPE}
    if fake:
        offered[FAKE.id] = FAKE
    return offered
