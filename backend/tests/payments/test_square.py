"""The Square adapter (#5666). No test calls Square: respx answers every request, and the
payloads in ``square_payloads/`` are the examples in Square's OpenAPI specification
(https://github.com/square/connect-api-specification, ``api.json``: ``CreatePaymentLinkResponse``,
``ListPaymentLinksResponse``, ``RetrieveLocationResponse``, ``GetPaymentResponse``,
``PaymentUpdatedEvent``, ``RefundCreatedEvent`` and ``RefundUpdatedEvent``), given
ScadBuddy's ids and amounts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from scadbuddy.orders.models import Actor
from scadbuddy.payments.models import PaymentRequestView, ProviderEvent
from scadbuddy.payments.paypal import PAYPAL
from scadbuddy.payments.provider import (
    BadSignatureError,
    Credential,
    ProviderError,
    ProviderUnavailableError,
)
from scadbuddy.payments.registry import providers
from scadbuddy.payments.square import API, SQUARE, SQUARE_VERSION, SquareProvider, sign
from scadbuddy.payments.stripe import STRIPE

PAYLOADS = Path(__file__).parent / "square_payloads"
SANDBOX = API["sandbox"]
TOKEN = "test-square-access-token-not-a-real-one"
SIGNATURE_KEY = "Qm9vbXNoYWthbGFrYS1zaWduYXR1cmUta2V5LTEyMw"
LOCATION = "L88917AVBK2S5"
NOTIFY = "https://shop.example/api/v1/payments/webhooks/square"
SETTINGS = {"environment": "sandbox", "location_id": LOCATION, "notification_url": NOTIFY}
CRED = Credential(
    settings=SETTINGS, secrets={"access_token": TOKEN, "signature_key": SIGNATURE_KEY}
)
LINK = "PKVT6XGJZXYUP3NZ"
ORDER = "o4b7saqp4HzhNttf5AJxC0Srjd4F"
PAYMENT = "hYy9pRFVxpDsO1FB05SunFWUe9JZY"
REFUND = "KkAkhdMsgzn59SM8A89WgKwekxLZY_ptNBVqHYxt5gAdfcobBe4u1AZsXhoz06KTtuq9Ls24P"


def payload(name: str) -> Any:
    return json.loads((PAYLOADS / name).read_text())


def signed(
    name: str, *, url: str = NOTIFY, key: str = SIGNATURE_KEY
) -> tuple[bytes, dict[str, str]]:
    """The body and headers Square posts: the HMAC-SHA256 of the notification URL and the
    body under the signature key, base64
    (https://developer.squareup.com/docs/webhooks/step3validate)."""
    body = (PAYLOADS / name).read_bytes()
    digest = hmac.new(key.encode(), url.encode() + body, hashlib.sha256).digest()
    return body, {"x-square-hmacsha256-signature": base64.b64encode(digest).decode()}


def view(**fields: Any) -> PaymentRequestView:
    base: dict[str, Any] = {
        "id": "req-1", "order_id": "order-1", "kind": "balance", "provider": "square",
        "amount_minor": 700, "currency": "USD", "hosted_url": None, "status": "open",
        "created_at": datetime.now(UTC), "paid_at": None, "provider_ref": None,
        "link_error": None, "payment_id": None, "created_by": Actor(kind="browser"),
    }  # fmt: skip
    return PaymentRequestView(**(base | fields))


@pytest.fixture
def sq() -> Any:
    """Square's sandbox API; each test adds the routes it reads."""
    with respx.mock(base_url=SANDBOX, assert_all_called=False) as router:
        router.get(f"/v2/payments/{PAYMENT}", name="payment").respond(
            json=payload("api.payment.json")
        )
        yield router


async def parse(name: str) -> ProviderEvent:
    body, headers = signed(name)
    return await SQUARE.parse_webhook(CRED, headers, body)


# -- the provider ----------------------------------------------------------------------------


def test_the_registry_offers_square() -> None:
    assert providers(fake=False) == {"stripe": STRIPE, "paypal": PAYPAL, "square": SQUARE}
    assert set(providers(fake=True)) == {"stripe", "paypal", "square", "fake"}


def test_square_asks_for_the_location_the_webhook_and_two_secrets() -> None:
    assert [(f.name, f.secret) for f in SQUARE.fields] == [
        ("environment", False),
        ("location_id", False),
        ("notification_url", False),
        ("access_token", True),
        ("signature_key", True),
    ]


def test_the_secrets_never_show_in_the_adapter() -> None:
    shown = repr(SquareProvider()) + repr(CRED)
    assert TOKEN not in shown and SIGNATURE_KEY not in shown


async def test_verify_reads_the_location_and_names_it_and_the_environment(sq: Any) -> None:
    location = sq.get(f"/v2/locations/{LOCATION}").respond(json=payload("api.location.json"))
    assert await SQUARE.verify(CRED) == "Square location Print shop (sandbox)"
    sent = location.calls.last.request
    assert sent.headers["authorization"] == f"Bearer {TOKEN}"
    assert sent.headers["square-version"] == SQUARE_VERSION


async def test_production_uses_the_production_api() -> None:
    live = Credential(settings=SETTINGS | {"environment": "production"}, secrets=CRED.secrets)
    with respx.mock(base_url=API["production"]) as router:
        router.get(f"/v2/locations/{LOCATION}").respond(json=payload("api.location.json"))
        assert await SQUARE.verify(live) == "Square location Print shop (production)"


@pytest.mark.parametrize(
    ("settings", "secrets", "says"),
    [
        (SETTINGS | {"environment": "live"}, CRED.secrets, "sandbox or production"),
        (SETTINGS | {"location_id": "../v2/x"}, CRED.secrets, "location id"),
        (SETTINGS | {"notification_url": "http://shop.example/hook"}, CRED.secrets, "https"),
        (SETTINGS | {"notification_url": ""}, CRED.secrets, "https"),
        (SETTINGS, {"access_token": TOKEN, "signature_key": ""}, "signature key"),
        (SETTINGS, {"access_token": "", "signature_key": SIGNATURE_KEY}, "access token"),
    ],
)
async def test_verify_refuses_a_wrong_setting_before_any_call(
    settings: dict[str, str], secrets: dict[str, str], says: str
) -> None:
    # Nothing is routed: any request would fail the test.
    with respx.mock, pytest.raises(ProviderError, match=says):
        await SQUARE.verify(Credential(settings=settings, secrets=secrets))


async def test_an_inactive_location_is_refused(sq: Any) -> None:
    sq.get(f"/v2/locations/{LOCATION}").respond(json=payload("api.location.inactive.json"))
    with pytest.raises(ProviderError, match="not active"):
        await SQUARE.verify(CRED)


async def test_a_refused_token_never_repeats_it(sq: Any) -> None:
    sq.get(f"/v2/locations/{LOCATION}").respond(401, json=payload("api.error.unauthorized.json"))
    with pytest.raises(ProviderError) as refused:
        await SQUARE.verify(CRED)
    assert str(refused.value) == "Square refused the access token"
    assert TOKEN not in str(refused.value)


async def test_square_not_answering_is_a_provider_error(sq: Any) -> None:
    sq.get(f"/v2/locations/{LOCATION}").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(ProviderError, match="did not answer"):
        await SQUARE.verify(CRED)


# -- requests --------------------------------------------------------------------------------


async def test_a_request_is_a_quick_pay_link_named_for_the_order(sq: Any) -> None:
    created = sq.post("/v2/online-checkout/payment-links").respond(
        json=payload("api.payment_link.created.json")
    )
    hosted = await SQUARE.create_request(
        CRED,
        view(),
        return_url="https://shop.example/orders/order-1",
        idempotency_key="req-1",
        order_number="ORD-2026-7",
    )
    # The payment carries the link's order id, so the order is the request's reference.
    assert hosted.provider_ref == ORDER
    assert str(hosted.hosted_url) == "https://sandbox.square.link/u/kpTBBMfZ"
    sent = created.calls.last.request
    assert sent.headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(sent.content) == {
        "idempotency_key": "req-1",
        "quick_pay": {
            "name": "Order ORD-2026-7",
            "price_money": {"amount": 700, "currency": "USD"},
            "location_id": LOCATION,
        },
        "payment_note": "Order ORD-2026-7 (ScadBuddy request req-1)",
        "checkout_options": {"redirect_url": "https://shop.example/orders/order-1"},
    }


async def test_a_deposit_says_so_and_needs_no_return_url(sq: Any) -> None:
    created = sq.post("/v2/online-checkout/payment-links").respond(
        json=payload("api.payment_link.created.json")
    )
    await SQUARE.create_request(
        CRED, view(kind="deposit"), return_url=None, idempotency_key="k", order_number="ORD-1"
    )
    sent = json.loads(created.calls.last.request.content)
    assert sent["quick_pay"]["name"] == "Deposit for order ORD-1"
    assert "checkout_options" not in sent


async def test_square_refusing_a_request_names_its_reason(sq: Any) -> None:
    sq.post("/v2/online-checkout/payment-links").respond(
        400, json=payload("api.error.currency.json")
    )
    with pytest.raises(ProviderError, match="INVALID_VALUE: Currency 'EUR'"):
        await SQUARE.create_request(
            CRED, view(currency="EUR"), return_url=None, idempotency_key="k", order_number="O"
        )


async def test_an_answer_with_no_link_is_an_error(sq: Any) -> None:
    answer = payload("api.payment_link.created.json")
    del answer["payment_link"]["url"]
    sq.post("/v2/online-checkout/payment-links").respond(json=answer)
    with pytest.raises(ProviderError, match="no payment link"):
        await SQUARE.create_request(
            CRED, view(), return_url=None, idempotency_key="k", order_number="O"
        )


# -- cancel ----------------------------------------------------------------------------------


def _pages(sq: Any) -> None:
    sq.get("/v2/online-checkout/payment-links", params={"cursor": "MTY1NQ=="}).respond(
        json=payload("api.payment_links.page2.json")
    )
    sq.get("/v2/online-checkout/payment-links").respond(
        json=payload("api.payment_links.page1.json")
    )


async def test_cancel_finds_the_order_s_link_and_deletes_it(sq: Any) -> None:
    _pages(sq)
    deleted = sq.delete(f"/v2/online-checkout/payment-links/{LINK}").respond(
        json=payload("api.payment_link.deleted.json")
    )
    await SQUARE.cancel_request(CRED, ORDER)
    assert deleted.called


async def test_cancel_of_a_link_already_gone_is_no_error(sq: Any) -> None:
    sq.get("/v2/online-checkout/payment-links").respond(
        json={"payment_links": payload("api.payment_links.page1.json")["payment_links"]}
    )
    await SQUARE.cancel_request(CRED, ORDER)
    _pages(sq)
    sq.delete(f"/v2/online-checkout/payment-links/{LINK}").respond(
        404, json=payload("api.error.not_found.json")
    )
    await SQUARE.cancel_request(CRED, ORDER)


async def test_cancel_refused_for_a_paid_order_is_no_error(sq: Any) -> None:
    _pages(sq)
    sq.delete(f"/v2/online-checkout/payment-links/{LINK}").respond(
        400, json=payload("api.error.bad_request.json")
    )
    sq.get(f"/v2/orders/{ORDER}").respond(json=payload("api.order.completed.json"))
    await SQUARE.cancel_request(CRED, ORDER)


async def test_cancel_that_leaves_the_link_payable_is_an_error(sq: Any) -> None:
    """The housekeeping sweep tries it again (#5700)."""
    _pages(sq)
    sq.delete(f"/v2/online-checkout/payment-links/{LINK}").respond(
        400, json=payload("api.error.bad_request.json")
    )
    sq.get(f"/v2/orders/{ORDER}").respond(json=payload("api.order.open.json"))
    with pytest.raises(ProviderError):
        await SQUARE.cancel_request(CRED, ORDER)


async def test_cancel_with_square_down_is_an_error(sq: Any) -> None:
    sq.get("/v2/online-checkout/payment-links").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(ProviderError, match="did not answer"):
        await SQUARE.cancel_request(CRED, ORDER)


async def test_an_id_that_is_not_a_square_id_is_never_put_in_a_path() -> None:
    with respx.mock, pytest.raises(ProviderError, match="not a Square id"):
        await SQUARE.cancel_request(CRED, "../v2/locations")


# -- the signature ---------------------------------------------------------------------------


def test_sign_is_the_documented_hmac() -> None:
    body = b'{"x":1}'
    digest = hmac.new(b"k", NOTIFY.encode() + body, hashlib.sha256).digest()
    assert sign("k", NOTIFY, body) == base64.b64encode(digest).decode()


@pytest.mark.parametrize(
    "forged",
    [
        lambda: ((PAYLOADS / "payment.updated.completed.json").read_bytes(), {}),
        lambda: signed("payment.updated.completed.json", key="another key"),
        lambda: signed("payment.updated.completed.json", url="https://elsewhere.example/hook"),
        lambda: (b"{}", signed("payment.updated.completed.json")[1]),
        lambda: (b"not json", {"x-square-hmacsha256-signature": "bm90IGEgc2lnbmF0dXJl"}),
    ],
)
async def test_a_bad_signature_is_refused_before_anything_is_read(forged: Any) -> None:
    body, headers = forged()
    with respx.mock, pytest.raises(BadSignatureError):
        await SQUARE.parse_webhook(CRED, headers, body)


async def test_no_signature_key_refuses_every_webhook() -> None:
    body, headers = signed("payment.updated.completed.json", key="")
    empty = Credential(settings=SETTINGS, secrets={"access_token": TOKEN, "signature_key": ""})
    with pytest.raises(BadSignatureError):
        await SQUARE.parse_webhook(empty, headers, body)


async def test_a_signed_body_that_is_not_an_event_is_malformed() -> None:
    body = b'{"type": "payment.updated"}'
    headers = {"X-Square-HmacSha256-Signature": sign(SIGNATURE_KEY, NOTIFY, body)}
    with pytest.raises(ProviderError, match="malformed"):
        await SQUARE.parse_webhook(CRED, headers, body)


# -- events ----------------------------------------------------------------------------------


async def test_a_completed_payment_is_paid_under_square_s_event_id() -> None:
    event = await parse("payment.updated.completed.json")
    assert event == ProviderEvent(
        event_id="6a8f5f28-54a1-4eb0-a98a-3111513fd4fc",
        kind="paid",
        provider_ref=ORDER,
        amount_minor=700,
        currency="USD",
        fee_minor=None,
        created_at=datetime(2026, 11, 8, 17, 10, 0, 512000, tzinfo=UTC),
    )


async def test_the_fee_is_the_payment_s_initial_processing_fee() -> None:
    event = await parse("payment.updated.completed.fee.json")
    assert (event.kind, event.event_id, event.fee_minor) == (
        "paid",
        "1f6b5c0e-2d8a-4b0c-9a3e-6b1e0d3c4f21",
        50,
    )


@pytest.mark.parametrize(
    ("fees", "expected"),
    [
        ([("INITIAL", 30, "USD"), ("INITIAL", 20, "USD")], 50),
        ([("INITIAL", 50, "USD"), ("ADJUSTMENT", -10, "USD")], 50),
        ([("ADJUSTMENT", 10, "USD")], None),
        ([("INITIAL", 50, "CAD")], None),
        ([("INITIAL", -5, "USD")], None),
        ([], None),
    ],
)
async def test_only_initial_fees_in_the_payment_s_currency_count(
    fees: list[tuple[str, int, str]], expected: int | None
) -> None:
    event = payload("payment.updated.completed.fee.json")
    event["data"]["object"]["payment"]["processing_fee"] = [
        {"type": t, "amount_money": {"amount": a, "currency": c}} for t, a, c in fees
    ]
    body = json.dumps(event).encode()
    headers = {"x-square-hmacsha256-signature": sign(SIGNATURE_KEY, NOTIFY, body)}
    assert (await SQUARE.parse_webhook(CRED, headers, body)).fee_minor == expected


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("payment.created.pending.json", "pending"),
        ("payment.updated.failed.json", "failed"),
        ("payment.updated.settled.json", "paid"),
    ],
)
async def test_a_bank_payment_is_pending_until_it_settles_or_fails(name: str, kind: str) -> None:
    event = await parse(name)
    assert (event.kind, event.provider_ref) == (kind, ORDER)


@pytest.mark.parametrize(
    ("name", "status", "kind"),
    [
        ("payment.updated.completed.json", "APPROVED", "other"),
        # A declined or voided card leaves the link payable: the request stays open.
        ("payment.updated.completed.json", "FAILED", "other"),
        ("payment.updated.completed.json", "CANCELED", "other"),
        # Only a bank (ACH) payment that did not settle fails the request.
        ("payment.created.pending.json", "FAILED", "failed"),
        ("payment.created.pending.json", "CANCELED", "other"),
    ],
)
async def test_only_a_bank_payment_that_did_not_settle_is_failed(
    name: str, status: str, kind: str
) -> None:
    event = payload(name)
    event["data"]["object"]["payment"]["status"] = status
    body = json.dumps(event).encode()
    headers = {"x-square-hmacsha256-signature": sign(SIGNATURE_KEY, NOTIFY, body)}
    found = await SQUARE.parse_webhook(CRED, headers, body)
    assert found.kind == kind


async def test_a_completed_refund_is_keyed_by_its_own_id(sq: Any) -> None:
    event = await parse("refund.updated.completed.json")
    assert event == ProviderEvent(
        event_id=f"refund:{REFUND}",
        kind="refunded",
        provider_ref=ORDER,
        amount_minor=400,
        currency="USD",
        created_at=datetime(2026, 11, 9, 10, 5, 16, 421000, tzinfo=UTC),
    )
    again = await parse("refund.updated.completed.again.json")
    assert again.event_id == event.event_id
    assert sq["payment"].call_count == 2


async def test_a_pending_refund_is_other() -> None:
    event = await parse("refund.created.pending.json")
    assert event.kind == "other"


async def test_a_failed_refund_reverses_the_refund(sq: Any) -> None:
    event = await parse("refund.updated.failed.json")
    assert (event.event_id, event.kind, event.reverses, event.provider_ref) == (
        f"refund-failed:{REFUND}",
        "refund_failed",
        f"refund:{REFUND}",
        ORDER,
    )


async def test_square_down_while_reading_a_refund_s_payment_is_unavailable(sq: Any) -> None:
    sq["payment"].respond(
        500, json={"errors": [{"category": "API_ERROR", "code": "INTERNAL_SERVER_ERROR"}]}
    )
    with pytest.raises(ProviderUnavailableError):
        await parse("refund.updated.completed.json")


async def test_another_event_type_is_other() -> None:
    event = payload("payment.updated.completed.json") | {"type": "order.updated"}
    body = json.dumps(event).encode()
    headers = {"x-square-hmacsha256-signature": sign(SIGNATURE_KEY, NOTIFY, body)}
    found = await SQUARE.parse_webhook(CRED, headers, body)
    assert (found.kind, found.event_id) == ("other", event["event_id"])
