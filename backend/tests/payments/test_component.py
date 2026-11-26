"""`payments/component.py`: where Plaid is told to send the bank webhook."""

from types import SimpleNamespace

import pytest

from scadbuddy.payments.component import PaymentsComponent


@pytest.mark.parametrize(
    ("public", "storefront", "expected"),
    [
        # The storefront's host is the public one: Plaid must reach the webhook, and the
        # back office's own URL may be LAN-only (prod: scadbuddy.internal..., #7976).
        (
            "https://scadbuddy.lan.example",
            "https://shop.example/",
            "https://shop.example/api/v1/payments/bank/webhook",
        ),
        (
            "https://scadbuddy.example",
            None,
            "https://scadbuddy.example/api/v1/payments/bank/webhook",
        ),
        (None, None, None),
    ],
)
def test_the_bank_webhook_url_is_on_the_public_host(
    public: str | None, storefront: str | None, expected: str | None
) -> None:
    settings = SimpleNamespace(public_url=public, storefront_public_url=storefront)
    component = PaymentsComponent(SimpleNamespace(settings=settings), None)  # type: ignore[arg-type]
    assert component._webhook_url() == expected
