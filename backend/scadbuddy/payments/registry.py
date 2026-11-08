"""The providers ScadBuddy can connect (#4957): Stripe (#4958), PayPal (#4959) and Square
(#5666) always, the fake one only when asked."""

from __future__ import annotations

from scadbuddy.payments.fake import FAKE
from scadbuddy.payments.paypal import PAYPAL
from scadbuddy.payments.provider import PaymentProvider
from scadbuddy.payments.square import SQUARE
from scadbuddy.payments.stripe import STRIPE


def providers(*, fake: bool) -> dict[str, PaymentProvider]:
    offered: dict[str, PaymentProvider] = {
        STRIPE.id: STRIPE,
        PAYPAL.id: PAYPAL,
        SQUARE.id: SQUARE,
    }
    if fake:
        offered[FAKE.id] = FAKE
    return offered
