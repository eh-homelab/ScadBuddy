"""Venmo through PayPal (#4960). Venmo is a PayPal funding source that only PayPal's
JavaScript SDK offers, with ``enable-funding=venmo``, for US buyers paying in USD
(https://developer.paypal.com/docs/checkout/pay-with-venmo/,
https://developer.paypal.com/v5/venmo/integrate.md). Orders v2's
``payment_source.venmo.experience_context`` takes no ``return_url``
(https://developer.paypal.com/docs/api/orders/v2/), so PayPal's redirect link never shows
Venmo. The plan is a pay page on the storefront, ``<SCADBUDDY_STOREFRONT_PUBLIC_URL>/pay#<token>``,
that renders PayPal's buttons for the request's PayPal order; its approval goes to the
existing capture path. The token is in the URL fragment, which the browser never sends, so
no access log holds it. The page is the storefront's (#5283 phase 4,
``frontend/src/storefront/pages/PayPage.tsx``) and is built. Until the sandbox measurement
below is made (`PAY_PAGE_READY`), no request's link is rewritten, every request
keeps PayPal's own approval link, and a connect with Venmo on is refused
(`VenmoPageMissingError`, 409 ``venmo_page_missing``). The token, its hash, the checkout
read and its CRUD route (`api/crud_storefront.py`) are in place for that page.

Not yet measured (no PayPal sandbox credentials were available when the page was built):
PayPal's docs do not say whether the JS SDK's buttons can fund, with
Venmo, an order created with ``payment_source.paypal`` (as ``create_request`` creates
it); the Venmo guides show only the SDK's script parameter and the buttons' render.
The sandbox measurement is still pending. `PAY_PAGE_READY` flips in its own PR once it is
measured, and that PR creates the order without ``payment_source`` for a pay-page request
if the SDK cannot fund it.

The token is 32 random bytes, URL-safe base64. Only its sha256 is stored
(``payment_requests.pay_token_hash``), and it is dropped when the request is paid,
cancelled or expired (``store.set_request_status``). It reads while the request is
``open`` or ``failed`` (a failed page can be approved again, #4959), not while a
capture is ``pending``."""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Mapping
from typing import Final

from pydantic import BaseModel

from scadbuddy.orders.store import Conn

#: Whether Venmo's pay page (``/pay#<token>``, #5283 phase 4) is switched on. The page is
#: built; the flag stays False until the sandbox measurement in the module docstring is
#: made, and flips in its own PR. While False, no request is Venmo-eligible and the toggle
#: cannot be turned on.
PAY_PAGE_READY: Final = False
#: Why the toggle's ``on`` is refused while the page does not exist.
PAY_PAGE_MISSING: Final = "Venmo needs the storefront's pay page, which is not built yet"
#: The provider Venmo rides on, and its settings field that turns Venmo on.
PROVIDER: Final = "paypal"
SETTING: Final = "venmo"
CURRENCY: Final = "USD"
#: What ``secrets.token_urlsafe(32)`` gives: 43 characters of URL-safe base64.
_TOKEN: Final = re.compile(r"[A-Za-z0-9_-]{43}")


class VenmoPageMissingError(Exception):
    """Venmo turned on before the storefront's pay page exists (#4960); 409
    ``venmo_page_missing``."""


class VenmoCheckout(BaseModel):
    """What the storefront's pay page needs to render PayPal's buttons for one request."""

    amount_minor: int
    currency: str
    order_number: str
    client_id: str
    environment: str
    paypal_order_id: str


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def pay_token() -> tuple[str, str]:
    """A new token and its hash. The token goes in the link only."""
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def venmo_eligible(
    provider: str, settings: Mapping[str, str], currency: str, storefront_url: str | None
) -> bool:
    """A PayPal request in USD, from a shop with Venmo on and a storefront pay page to pay
    on."""
    return (
        PAY_PAGE_READY
        and provider == PROVIDER
        and currency.upper() == CURRENCY
        and settings.get(SETTING) == "on"
        and bool(storefront_url)
    )


def venmo_setting_error(
    provider: str, settings: Mapping[str, str], storefront_url: str | None
) -> str | None:
    """Why the connect form's Venmo toggle cannot be saved, or None; raises
    `VenmoPageMissingError` for ``on`` while the pay page does not exist."""
    if provider != PROVIDER or settings.get(SETTING, "off") == "off":
        return None
    if settings.get(SETTING) != "on":
        return "Venmo is on or off"
    if not PAY_PAGE_READY:
        raise VenmoPageMissingError(PAY_PAGE_MISSING)
    if not storefront_url:
        return "Venmo needs the storefront: set SCADBUDDY_STOREFRONT_PUBLIC_URL for the API"
    return None


def pay_link(storefront_url: str, token: str) -> str:
    return f"{storefront_url.rstrip('/')}/pay#{token}"


def venmo_checkout(conn: Conn, token: str) -> VenmoCheckout | None:
    """The pay page's request by its token: None for an unknown token (a request that
    ended dropped its token), a request that is not ``open`` or ``failed`` (a capture
    ``pending`` reads nothing), one whose provider is not PayPal, or a shop whose Venmo
    toggle is off (turning it off ends every pay link at once)."""
    if not _TOKEN.fullmatch(token):
        return None
    row = conn.execute(
        "SELECT r.amount_minor, r.currency, o.number AS order_number,"
        " c.settings->>'client_id' AS client_id, c.settings->>'environment' AS environment,"
        " r.provider_ref AS paypal_order_id"
        " FROM payment_requests r"
        " JOIN orders o ON o.id = r.order_id"
        " JOIN payment_credentials c ON c.provider = r.provider"
        " WHERE r.pay_token_hash = %s AND r.provider = %s AND r.status IN ('open', 'failed')"
        " AND r.provider_ref IS NOT NULL AND c.settings->>%s = 'on'",
        (token_hash(token), PROVIDER, SETTING),
    ).fetchone()
    if row is None or row["client_id"] is None or row["environment"] is None:
        return None
    return VenmoCheckout.model_validate(dict(row))
