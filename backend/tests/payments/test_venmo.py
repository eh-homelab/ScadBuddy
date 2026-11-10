"""Venmo through PayPal (#4960): once the pay page is switched on (`PAY_PAGE_READY`), a
USD PayPal request from a shop with Venmo on gets a pay token, its link is that page, and
``venmo_checkout`` reads what the page needs by the token only while the request can still
be paid. Until it is on, no link is rewritten and Venmo cannot be turned on; the tests of the
rewrite turn the flag on (`page_ready`)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scadbuddy.orders.models import Order
from scadbuddy.orders.store import OrderStore
from scadbuddy.payments import store as payments_store
from scadbuddy.payments import venmo
from scadbuddy.payments.models import HostedRequest, PaymentRequestView
from scadbuddy.payments.paypal import PAYPAL
from scadbuddy.payments.provider import Credential, Verified
from scadbuddy.payments.service import FieldError, Payments, ProviderConnect
from scadbuddy.payments.venmo import (
    VenmoCheckout,
    VenmoPageMissingError,
    pay_token,
    token_hash,
    venmo_checkout,
    venmo_eligible,
    venmo_setting_error,
)
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY
from tests.orders.test_payment_requests import PayShop

pytestmark = pytest.mark.requires_postgres

STOREFRONT = "https://shop.example.com"
PAYPAL_ORDER = "5O190127TN364715T"
PAYER_ACTION = f"https://www.sandbox.paypal.com/checkoutnow?token={PAYPAL_ORDER}"
SETTINGS = {
    "environment": "sandbox",
    "client_id": "AYSq3RDGsmBLJE-otTkBtM",
    "webhook_id": "0NH55953DH663215D",
    "venmo": "on",
}


def connect_paypal(rows: OrderStore, settings: Mapping[str, str] = SETTINGS) -> None:
    with rows.transaction() as conn:
        conn.execute(
            "INSERT INTO payment_credentials (provider, account_label, settings, secret_sealed,"
            " dek_sealed, kek_id) VALUES ('paypal', 'PayPal', %s::jsonb, '\\x00', '\\x00', 'k')",
            (json.dumps(dict(settings)),),
        )


@pytest.fixture
def page_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pay page is switched on (`PAY_PAGE_READY`)."""
    monkeypatch.setattr(venmo, "PAY_PAGE_READY", True)


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> PayShop:
    made = PayShop(rows, BlobRefs(pool))
    made.lifecycle.offered_providers = frozenset({"paypal"})
    connect_paypal(rows)
    return made


def tokened(shop: PayShop) -> tuple[Order, PaymentRequestView, str]:
    """An accepted order with a PayPal balance request for 1000, linked with a pay token."""
    order, _ = shop.accepted()
    req = shop.lifecycle.open_payment_request(order.id, BY, provider="paypal")
    token, digest = pay_token()
    shop.lifecycle.set_request_link(
        req.id,
        hosted=HostedRequest.model_validate(
            {"provider_ref": PAYPAL_ORDER, "hosted_url": f"{STOREFRONT}/pay#{token}"}
        ),
        error=None,
        pay_token_hash=digest,
    )
    return order, req, token


def checkout(shop: PayShop, token: str) -> VenmoCheckout | None:
    with shop.rows.transaction() as conn:
        return venmo_checkout(conn, token)


def stored_hash(shop: PayShop, request_id: str) -> str | None:
    with shop.rows.transaction() as conn:
        row = conn.execute(
            "SELECT pay_token_hash FROM payment_requests WHERE id = %s", (request_id,)
        ).fetchone()
    assert row is not None
    return cast(str | None, row["pay_token_hash"])


# -- the token -------------------------------------------------------------------------------


def test_a_pay_token_is_32_random_bytes_and_only_its_hash_is_kept() -> None:
    token, digest = pay_token()
    assert len(token) == 43 and "=" not in token
    assert digest == hashlib.sha256(token.encode()).hexdigest() == token_hash(token)
    assert pay_token()[0] != token


def test_the_request_stores_the_hash_never_the_token(shop: PayShop) -> None:
    _, req, token = tokened(shop)
    assert stored_hash(shop, req.id) == token_hash(token)
    with shop.rows.transaction() as conn:
        row = conn.execute(
            "SELECT row_to_json(r)::text AS whole FROM payment_requests r WHERE id = %s",
            (req.id,),
        ).fetchone()
    assert row is not None and token not in row["whole"].replace(f"/pay#{token}", "")


# -- venmo_checkout --------------------------------------------------------------------------


def test_an_open_request_reads_what_the_pay_page_needs(shop: PayShop) -> None:
    order, _, token = tokened(shop)
    assert order.number is not None
    assert checkout(shop, token) == VenmoCheckout(
        amount_minor=1000,
        currency="USD",
        order_number=order.number,
        client_id=SETTINGS["client_id"],
        environment="sandbox",
        paypal_order_id=PAYPAL_ORDER,
        minor_exponent=2,
    )


@pytest.mark.parametrize("token", ["", "x" * 43, "not a token", "a" * 5000])
def test_an_unknown_token_reads_nothing(shop: PayShop, token: str) -> None:
    tokened(shop)
    assert checkout(shop, token) is None


@pytest.mark.parametrize("status", ["paid", "cancelled", "expired"])
def test_a_request_that_ends_reads_nothing_and_drops_its_token(shop: PayShop, status: Any) -> None:
    """A token is spent once its request ends: the storefront's ``/pay#<token>`` page gets
    a 404 for it, whatever the request does next."""
    _, req, token = tokened(shop)
    with shop.rows.transaction() as conn:
        payments_store.set_request_status(conn, req.id, status)
    assert checkout(shop, token) is None
    assert stored_hash(shop, req.id) is None


def test_a_pending_request_reads_nothing_and_a_failed_one_reads_again(shop: PayShop) -> None:
    """#5769 review: an approval moves the request to ``pending`` (the capture is in
    flight, so the page offers nothing), and a capture PayPal refused puts it back to
    ``failed``, which the customer can approve again (#4959): its pay link works again."""
    _, req, token = tokened(shop)
    with shop.rows.transaction() as conn:
        payments_store.set_request_status(conn, req.id, "pending")
    assert checkout(shop, token) is None
    assert stored_hash(shop, req.id) == token_hash(token)
    with shop.rows.transaction() as conn:
        payments_store.set_request_status(conn, req.id, "failed")
    assert checkout(shop, token) is not None
    # #5791 review: the token that outlived a refused capture dies once the request ends.
    with shop.rows.transaction() as conn:
        payments_store.set_request_status(conn, req.id, "paid")
    assert checkout(shop, token) is None
    assert stored_hash(shop, req.id) is None


def test_a_cancel_through_the_lifecycle_drops_the_token(shop: PayShop) -> None:
    order, req, token = tokened(shop)
    shop.lifecycle.cancel_payment_request(order.id, req.id, BY)
    assert stored_hash(shop, req.id) is None
    assert checkout(shop, token) is None


def test_a_relink_without_a_token_drops_the_old_one(shop: PayShop) -> None:
    """Venmo turned off, then the request linked again: PayPal's own link, and the old
    pay page is dead."""
    _, req, token = tokened(shop)
    shop.lifecycle.set_request_link(
        req.id,
        hosted=HostedRequest.model_validate(
            {"provider_ref": PAYPAL_ORDER, "hosted_url": PAYER_ACTION}
        ),
        error=None,
    )
    assert stored_hash(shop, req.id) is None
    assert checkout(shop, token) is None


def test_a_failed_relink_keeps_the_token(shop: PayShop) -> None:
    _, req, token = tokened(shop)
    shop.lifecycle.set_request_link(req.id, hosted=None, error="PayPal did not answer")
    assert stored_hash(shop, req.id) == token_hash(token)
    assert checkout(shop, token) is not None


def test_the_toggle_off_kills_every_pay_link_at_once(shop: PayShop) -> None:
    _, _, token = tokened(shop)
    with shop.rows.transaction() as conn:
        conn.execute(
            'UPDATE payment_credentials SET settings = settings || \'{"venmo": "off"}\''
            " WHERE provider = 'paypal'"
        )
    assert checkout(shop, token) is None


def test_a_request_of_another_provider_reads_nothing(shop: PayShop) -> None:
    _, req, token = tokened(shop)
    with shop.rows.transaction() as conn:
        conn.execute("UPDATE payment_requests SET provider = 'fake' WHERE id = %s", (req.id,))
    assert checkout(shop, token) is None


# -- eligibility -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "settings", "currency", "storefront", "eligible"),
    [
        ("paypal", SETTINGS, "USD", STOREFRONT, True),
        ("paypal", SETTINGS, "EUR", STOREFRONT, False),
        ("paypal", SETTINGS | {"venmo": "off"}, "USD", STOREFRONT, False),
        ("paypal", {k: v for k, v in SETTINGS.items() if k != "venmo"}, "USD", STOREFRONT, False),
        ("paypal", SETTINGS, "USD", None, False),
        ("stripe", SETTINGS, "USD", STOREFRONT, False),
    ],
)
def test_venmo_needs_paypal_usd_the_toggle_and_the_storefront(
    page_ready: None,
    provider: str,
    settings: Mapping[str, str],
    currency: str,
    storefront: str | None,
    eligible: bool,
) -> None:
    assert venmo_eligible(provider, settings, currency, storefront) is eligible


def test_nothing_is_eligible_until_the_pay_page_is_switched_on() -> None:
    assert not venmo_eligible("paypal", SETTINGS, "USD", STOREFRONT)


def test_the_toggle_is_refused_until_the_pay_page_is_switched_on() -> None:
    with pytest.raises(VenmoPageMissingError):
        venmo_setting_error("paypal", SETTINGS, STOREFRONT)
    assert venmo_setting_error("paypal", SETTINGS | {"venmo": "off"}, STOREFRONT) is None


def test_the_toggle_needs_the_storefront_url(page_ready: None) -> None:
    assert venmo_setting_error("paypal", SETTINGS, None) is not None
    assert venmo_setting_error("paypal", SETTINGS, STOREFRONT) is None
    assert venmo_setting_error("paypal", SETTINGS | {"venmo": "off"}, None) is None
    assert venmo_setting_error("paypal", SETTINGS | {"venmo": "maybe"}, STOREFRONT) is not None
    assert venmo_setting_error("stripe", {}, None) is None


# -- the link --------------------------------------------------------------------------------


class _PayPal:
    """A PayPal stand-in that answers PayPal's own approval link."""

    id = "paypal"
    label = "PayPal"
    fields = PAYPAL.fields

    reports_currency = False

    async def verify(self, credential: Credential) -> Verified:
        return Verified(account_label="PayPal (sandbox)")

    async def create_request(self, credential: Credential, request: Any, **_: Any) -> HostedRequest:
        return HostedRequest.model_validate(
            {"provider_ref": PAYPAL_ORDER, "hosted_url": PAYER_ACTION}
        )


class _Credentials:
    can_seal = True

    def __init__(self, settings: Mapping[str, str]) -> None:
        self.settings = settings

    def credential(self, provider: str) -> Credential:
        return Credential(settings=dict(self.settings), secrets={"client_secret": "s"})


def service(
    shop: PayShop, *, settings: Mapping[str, str] = SETTINGS, storefront: str | None = STOREFRONT
) -> Payments:
    orders = SimpleNamespace(lifecycle=shop.lifecycle, store=shop.rows)
    return Payments(
        cast(Any, orders),
        cast(Any, _Credentials(settings)),
        cast(Any, {"paypal": _PayPal()}),
        lambda: None,
        lambda: storefront,
    )


async def test_until_the_pay_page_is_switched_on_paypal_s_link_stays(shop: PayShop) -> None:
    """#5769 review: no request links to a page nothing serves."""
    order, _ = shop.accepted()
    req = shop.lifecycle.open_payment_request(order.id, BY, provider="paypal")
    linked = await service(shop).link(req.id)
    assert linked.hosted_url == PAYER_ACTION
    assert stored_hash(shop, req.id) is None


async def test_an_eligible_request_links_to_the_storefront_pay_page(
    shop: PayShop, page_ready: None
) -> None:
    order, _ = shop.accepted()
    req = shop.lifecycle.open_payment_request(order.id, BY, provider="paypal")
    linked = await service(shop).link(req.id)
    assert linked.provider_ref == PAYPAL_ORDER
    assert linked.hosted_url is not None
    assert linked.hosted_url.startswith(f"{STOREFRONT}/pay#")
    token = linked.hosted_url.removeprefix(f"{STOREFRONT}/pay#")
    assert stored_hash(shop, req.id) == token_hash(token)
    found = checkout(shop, token)
    assert found is not None and found.paypal_order_id == PAYPAL_ORDER


@pytest.mark.parametrize(
    ("settings", "storefront"),
    [(SETTINGS | {"venmo": "off"}, STOREFRONT), (SETTINGS, None)],
)
async def test_any_other_request_keeps_paypal_s_link(
    shop: PayShop, page_ready: None, settings: Mapping[str, str], storefront: str | None
) -> None:
    order, _ = shop.accepted()
    req = shop.lifecycle.open_payment_request(order.id, BY, provider="paypal")
    linked = await service(shop, settings=settings, storefront=storefront).link(req.id)
    assert linked.hosted_url == PAYER_ACTION
    assert stored_hash(shop, req.id) is None


async def test_connect_refuses_venmo_on_until_the_pay_page_is_switched_on(shop: PayShop) -> None:
    payments = service(shop)
    body = ProviderConnect(settings=dict(SETTINGS), secrets={"client_secret": "s"})
    with pytest.raises(VenmoPageMissingError):
        await payments.connect("paypal", body, shop_currency="USD")


async def test_connect_takes_venmo_off_when_it_is_left_out(shop: PayShop) -> None:
    """#5769 review: a reconnect from before the field existed still saves."""
    payments = service(shop)
    saved: list[Mapping[str, Any]] = []
    credentials = cast(Any, payments)._credentials
    credentials.connect = lambda provider, **kw: saved.append(kw["settings"])
    cast(Any, payments).statuses = lambda: [SimpleNamespace(id="paypal")]
    left_out = {k: v for k, v in SETTINGS.items() if k != "venmo"}
    body = ProviderConnect(settings=left_out, secrets={"client_secret": "s"})
    await payments.connect("paypal", body, shop_currency="USD")
    assert [s["venmo"] for s in saved] == ["off"]


async def test_connect_refuses_venmo_on_without_the_storefront_url(
    shop: PayShop, page_ready: None
) -> None:
    payments = service(shop, storefront=None)
    body = ProviderConnect(settings=dict(SETTINGS), secrets={"client_secret": "s"})
    with pytest.raises(FieldError) as refused:
        await payments.connect("paypal", body, shop_currency="USD")
    assert refused.value.loc == ("settings", "venmo")
