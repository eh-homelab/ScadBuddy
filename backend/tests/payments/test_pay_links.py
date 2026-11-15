"""The links the pay page (#5283 phase 4) rides on: the pay link keeps the token in the URL
fragment, which the browser never sends, and the provider's return URL for a storefront
order is the storefront's own order page. Neither needs a database."""

from __future__ import annotations

from typing import Any, cast

import pytest

from scadbuddy.payments.service import Payments
from scadbuddy.payments.venmo import pay_link

STOREFRONT = "https://shop.example.com"
BACK_OFFICE = "https://office.example.com"


def test_the_pay_link_holds_the_token_in_the_fragment_not_the_path() -> None:
    assert pay_link(STOREFRONT, "T" * 43) == f"{STOREFRONT}/pay#{'T' * 43}"
    assert pay_link(f"{STOREFRONT}/", "t") == f"{STOREFRONT}/pay#t"


def payments(public: str | None, storefront: str | None) -> Payments:
    return Payments(
        cast(Any, None),
        cast(Any, None),
        cast(Any, {}),
        lambda: public,
        lambda: storefront,
    )


def test_a_back_office_order_returns_to_the_back_office() -> None:
    service = payments(BACK_OFFICE, STOREFRONT)
    assert service._return_url("o1", "back_office") == f"{BACK_OFFICE}/orders/o1"


def test_a_storefront_order_returns_to_the_storefront_order_page() -> None:
    """The link's token cannot ride in a return URL: the page asks for the mail's link."""
    service = payments(BACK_OFFICE, STOREFRONT)
    assert service._return_url("o1", "storefront") == f"{STOREFRONT}/o"
    assert payments(None, f"{STOREFRONT}/")._return_url("o1", "storefront") == f"{STOREFRONT}/o"


@pytest.mark.parametrize(
    ("public", "storefront", "source"),
    [
        (None, STOREFRONT, "back_office"),
        (BACK_OFFICE, None, "storefront"),
        (None, None, "storefront"),
    ],
)
def test_no_url_to_return_to_gives_none(
    public: str | None, storefront: str | None, source: str
) -> None:
    assert payments(public, storefront)._return_url("o1", source) is None
