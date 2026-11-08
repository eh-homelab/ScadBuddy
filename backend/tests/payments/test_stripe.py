"""The Stripe adapter (#4958). No test calls Stripe: respx answers every request, and the
events in ``stripe_payloads/`` keep the shapes of Stripe's API reference examples
(https://docs.stripe.com/api/checkout/sessions/object,
https://docs.stripe.com/api/charges/object, https://docs.stripe.com/api/events/object),
cut to the fields the adapter reads and given ScadBuddy's metadata."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from scadbuddy.orders.models import Actor
from scadbuddy.payments.models import PaymentRequestView, ProviderEvent
from scadbuddy.payments.provider import (
    BadSignatureError,
    Credential,
    ProviderError,
    ProviderUnavailableError,
)
from scadbuddy.payments.registry import providers
from scadbuddy.payments.stripe import API, SIGNATURE_TOLERANCE_SECONDS, STRIPE, StripeProvider

PAYLOADS = Path(__file__).parent / "stripe_payloads"
WHSEC = "whsec_test_secret"
KEY = "sk_test_51Example"
CRED = Credential(settings={}, secrets={"secret_key": KEY, "webhook_secret": WHSEC})
CS = "cs_test_a11YYufWQzNY63zpQ6QSNRQhkUpVph4WRmzW0zWJO2znZKdVujZ0N0S22u"
PI = "pi_3MtwBwLkdIwHu7ix28a3tqPa"
TXN = "txn_3MtwBwLkdIwHu7ix2rw9Uyaf"
#: When the recorded session and charge events were made (their ``created``).
MADE = datetime.fromtimestamp(1762560000, UTC)


def payload(name: str) -> Any:
    return json.loads((PAYLOADS / name).read_text())


def signed(
    body: bytes, *, secret: str = WHSEC, at: int | None = None, extra: str = ""
) -> dict[str, str]:
    """The header Stripe sends: ``t=<timestamp>,v1=<HMAC-SHA256 of "t.body">``
    (https://docs.stripe.com/webhooks#verify-manually)."""
    t = int(time.time()) if at is None else at
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return {"stripe-signature": f"t={t},v1={mac}{extra}"}


async def parse(name: str) -> ProviderEvent:
    """The recorded event ``name``, signed now, through the adapter."""
    body = (PAYLOADS / name).read_bytes()
    return await STRIPE.parse_webhook(CRED, signed(body), body)


def view(**fields: Any) -> PaymentRequestView:
    base: dict[str, Any] = {
        "id": "req-1", "order_id": "order-1", "kind": "balance", "provider": "stripe",
        "amount_minor": 700, "currency": "USD", "hosted_url": None, "status": "open",
        "created_at": datetime.now(UTC), "paid_at": None, "provider_ref": None,
        "link_error": None, "payment_id": None, "created_by": Actor(kind="browser"),
    }  # fmt: skip
    return PaymentRequestView(**(base | fields))


def form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode())


# -- the provider ----------------------------------------------------------------------------


def test_the_registry_offers_stripe() -> None:
    assert providers(fake=False)["stripe"] is STRIPE
    assert set(providers(fake=False)) == {"stripe", "paypal", "square"}
    assert set(providers(fake=True)) == {"stripe", "paypal", "square", "fake"}


def test_stripe_asks_for_two_secrets() -> None:
    assert {f.name: f.secret for f in STRIPE.fields} == {
        "secret_key": True,
        "webhook_secret": True,
    }


@respx.mock
async def test_verify_lists_one_session_and_names_the_mode() -> None:
    listed = respx.get(f"{API}/v1/checkout/sessions").mock(
        return_value=httpx.Response(200, json=payload("api.checkout_sessions.list.empty.json"))
    )
    assert await STRIPE.verify(CRED) == "Stripe account (test mode)"
    request = listed.calls.last.request
    assert request.url.params["limit"] == "1"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    live = Credential(settings={}, secrets={"secret_key": "rk_live_1", "webhook_secret": WHSEC})
    assert await STRIPE.verify(live) == "Stripe account (live)"


@pytest.mark.parametrize(
    ("secrets", "says"),
    [
        ({"secret_key": "pk_test_1", "webhook_secret": WHSEC}, "secret key"),
        ({"secret_key": KEY, "webhook_secret": "abc"}, "whsec_"),
    ],
)
async def test_verify_refuses_a_wrong_kind_of_key_before_any_call(
    secrets: dict[str, str], says: str
) -> None:
    # Any request would fail the test: none is routed.
    with respx.mock, pytest.raises(ProviderError, match=says):
        await STRIPE.verify(Credential(settings={}, secrets=secrets))


@respx.mock
async def test_a_refused_key_never_repeats_the_key() -> None:
    respx.get(f"{API}/v1/checkout/sessions").mock(
        return_value=httpx.Response(401, json=payload("api.error.invalid_key.json"))
    )
    with pytest.raises(ProviderError) as refused:
        await STRIPE.verify(CRED)
    assert str(refused.value) == "Stripe refused the secret key"


@respx.mock
async def test_stripe_not_answering_is_a_provider_error() -> None:
    respx.get(f"{API}/v1/checkout/sessions").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(ProviderError, match="did not answer"):
        await STRIPE.verify(CRED)


# -- requests --------------------------------------------------------------------------------


@respx.mock
async def test_a_request_is_a_checkout_session_for_the_amount() -> None:
    created = respx.post(f"{API}/v1/checkout/sessions").mock(
        return_value=httpx.Response(200, json=payload("api.checkout_session.created.json"))
    )
    hosted = await STRIPE.create_request(
        CRED,
        view(),
        return_url="https://shop.example/orders/order-1",
        idempotency_key="req-1",
        order_number="ORD-2026-7",
    )
    assert hosted.provider_ref == CS
    assert str(hosted.hosted_url).startswith("https://checkout.stripe.com/c/pay/")
    request = created.calls.last.request
    assert request.headers["idempotency-key"] == "req-1"
    assert request.headers["stripe-version"] == "2026-10-28.endive"
    sent = form(request)
    assert sent["mode"] == ["payment"]
    assert sent["line_items[0][quantity]"] == ["1"]
    assert sent["line_items[0][price_data][currency]"] == ["usd"]
    assert sent["line_items[0][price_data][unit_amount]"] == ["700"]
    assert sent["line_items[0][price_data][product_data][name]"] == ["Order ORD-2026-7"]
    assert sent["client_reference_id"] == ["req-1"]
    assert sent["success_url"] == ["https://shop.example/orders/order-1"]
    for prefix in ("metadata", "payment_intent_data[metadata]"):
        assert sent[f"{prefix}[scadbuddy_order_number]"] == ["ORD-2026-7"]
        assert sent[f"{prefix}[scadbuddy_order_id]"] == ["order-1"]
        assert sent[f"{prefix}[scadbuddy_payment_request_id]"] == ["req-1"]
    # The account's dynamic payment methods: card, and US bank account when it is on.
    assert not any(k.startswith("payment_method_types") for k in sent)


@respx.mock
async def test_a_deposit_request_says_so_and_needs_no_return_url() -> None:
    created = respx.post(f"{API}/v1/checkout/sessions").mock(
        return_value=httpx.Response(200, json=payload("api.checkout_session.created.json"))
    )
    await STRIPE.create_request(
        CRED, view(kind="deposit"), return_url=None, idempotency_key="k", order_number="ORD-1"
    )
    sent = form(created.calls.last.request)
    assert sent["line_items[0][price_data][product_data][name]"] == ["Deposit for order ORD-1"]
    assert "success_url" not in sent


@respx.mock
async def test_stripe_refusing_a_request_names_its_reason() -> None:
    respx.post(f"{API}/v1/checkout/sessions").mock(
        return_value=httpx.Response(400, json=payload("api.error.amount.json"))
    )
    with pytest.raises(ProviderError, match=r"Amount must be at least \$0.50 usd"):
        await STRIPE.create_request(
            CRED, view(amount_minor=10), return_url=None, idempotency_key="k", order_number="O"
        )


@respx.mock
async def test_cancel_expires_the_session() -> None:
    expired = respx.post(f"{API}/v1/checkout/sessions/{CS}/expire").mock(
        return_value=httpx.Response(200, json=payload("api.checkout_session.expired.json"))
    )
    await STRIPE.cancel_request(CRED, CS)
    assert expired.called


@pytest.mark.parametrize("state", ["expired", "complete"])
@respx.mock
async def test_cancel_of_a_session_already_closed_is_no_error(state: str) -> None:
    respx.post(f"{API}/v1/checkout/sessions/{CS}/expire").mock(
        return_value=httpx.Response(400, json=payload("api.error.not_expirable.json"))
    )
    respx.get(f"{API}/v1/checkout/sessions/{CS}").mock(
        return_value=httpx.Response(200, json=payload(f"api.checkout_session.{state}.json"))
    )
    await STRIPE.cancel_request(CRED, CS)


@respx.mock
async def test_cancel_that_leaves_the_session_open_is_an_error() -> None:
    respx.post(f"{API}/v1/checkout/sessions/{CS}/expire").mock(
        return_value=httpx.Response(400, json=payload("api.error.not_expirable.json"))
    )
    respx.get(f"{API}/v1/checkout/sessions/{CS}").mock(
        return_value=httpx.Response(200, json=payload("api.checkout_session.open.json"))
    )
    with pytest.raises(ProviderError):
        await STRIPE.cancel_request(CRED, CS)


# -- the signature ---------------------------------------------------------------------------


async def test_a_bad_signature_is_refused_before_anything_is_read() -> None:
    body = (PAYLOADS / "checkout.session.completed.card.json").read_bytes()
    with respx.mock:
        for headers in (
            signed(body, secret="whsec_other"),
            {},
            {"stripe-signature": "garbage"},
            {"stripe-signature": f"t={int(time.time())}"},
            signed(b"another body"),
        ):
            with pytest.raises(BadSignatureError):
                await STRIPE.parse_webhook(CRED, headers, body)
        with pytest.raises(BadSignatureError):
            await STRIPE.parse_webhook(CRED, signed(b"not json"), b"not json!")


@pytest.mark.parametrize("stamp", ["\u00b2", "9" * 5000, "-5", " 1"])
async def test_a_malformed_timestamp_is_a_bad_signature(stamp: str) -> None:
    """``str.isdigit`` takes ``\u00b2`` and 5000 digits, which ``int`` refuses."""
    body = (PAYLOADS / "checkout.session.expired.json").read_bytes()
    good = signed(body)["stripe-signature"].split(",", 1)[1]
    with pytest.raises(BadSignatureError):
        await STRIPE.parse_webhook(CRED, {"stripe-signature": f"t={stamp},{good}"}, body)


async def test_an_old_timestamp_is_refused() -> None:
    body = (PAYLOADS / "checkout.session.expired.json").read_bytes()
    old = int(time.time()) - SIGNATURE_TOLERANCE_SECONDS - 5
    with pytest.raises(BadSignatureError, match="too old"):
        await STRIPE.parse_webhook(CRED, signed(body, at=old), body)


async def test_a_timestamp_far_in_the_future_is_refused() -> None:
    body = (PAYLOADS / "checkout.session.expired.json").read_bytes()
    ahead = int(time.time()) + SIGNATURE_TOLERANCE_SECONDS + 5
    with pytest.raises(BadSignatureError):
        await STRIPE.parse_webhook(CRED, signed(body, at=ahead), body)


async def test_one_matching_v1_among_several_is_enough_and_v0_is_ignored() -> None:
    """While a rolled secret is still active Stripe signs with each
    (https://docs.stripe.com/webhooks#verify-manually)."""
    body = (PAYLOADS / "checkout.session.expired.json").read_bytes()
    t = int(time.time())
    good = signed(body, at=t)["stripe-signature"].split(",v1=")[1]
    header = {"Stripe-Signature": f"t={t},v1={'0' * 64},v1={good},v0={'1' * 64}"}
    assert (await STRIPE.parse_webhook(CRED, header, body)).kind == "expired"
    only_v0 = {"stripe-signature": f"t={t},v0={good}"}
    with pytest.raises(BadSignatureError):
        await STRIPE.parse_webhook(CRED, only_v0, body)


async def test_a_signed_body_that_is_not_an_event_is_a_provider_error() -> None:
    body = b'{"id": "evt_x"}'
    with pytest.raises(ProviderError):
        await STRIPE.parse_webhook(CRED, signed(body), body)


# -- events ----------------------------------------------------------------------------------


@respx.mock
async def test_a_card_payment_is_paid_with_its_fee() -> None:
    fetched = respx.get(f"{API}/v1/payment_intents/{PI}").mock(
        return_value=httpx.Response(200, json=payload("api.payment_intent.expanded.json"))
    )
    event = await parse("checkout.session.completed.card.json")
    assert event == ProviderEvent(
        event_id="evt_1card", kind="paid", provider_ref=CS, amount_minor=700,
        currency="USD", fee_minor=50, created_at=MADE,
    )  # fmt: skip
    assert fetched.calls.last.request.url.params["expand[]"] == "latest_charge.balance_transaction"


@respx.mock
async def test_a_payment_whose_fee_is_not_known_yet_is_paid_without_one() -> None:
    respx.get(f"{API}/v1/payment_intents/{PI}").mock(
        return_value=httpx.Response(
            200, json=payload("api.payment_intent.no_balance_transaction.json")
        )
    )
    event = await parse("checkout.session.completed.card.json")
    assert (event.kind, event.fee_minor) == ("paid", None)


@respx.mock
async def test_a_fee_in_another_currency_is_not_recorded() -> None:
    intent = payload("api.payment_intent.expanded.json")
    intent["latest_charge"]["balance_transaction"]["currency"] = "eur"
    respx.get(f"{API}/v1/payment_intents/{PI}").mock(return_value=httpx.Response(200, json=intent))
    event = await parse("checkout.session.completed.card.json")
    assert (event.kind, event.fee_minor) == ("paid", None)


@respx.mock
async def test_stripe_not_answering_for_the_fee_is_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The log says why (Stripe's status); the reply to the caller does not."""
    respx.get(f"{API}/v1/payment_intents/{PI}").mock(
        return_value=httpx.Response(404, json={"error": {"message": "No such payment_intent"}})
    )
    with caplog.at_level("WARNING"), pytest.raises(ProviderUnavailableError) as down:
        await parse("checkout.session.completed.card.json")
    assert "404" not in str(down.value)
    assert any("404" in r.getMessage() for r in caplog.records)


async def test_a_bank_debit_is_pending_then_paid_or_failed() -> None:
    with respx.mock:
        pending = await parse("checkout.session.completed.ach.json")
    assert pending == ProviderEvent(
        event_id="evt_1ach", kind="pending", provider_ref=CS, amount_minor=700, currency="USD",
        created_at=MADE,
    )  # fmt: skip
    with respx.mock:
        respx.get(f"{API}/v1/payment_intents/{PI}").mock(
            return_value=httpx.Response(200, json=payload("api.payment_intent.expanded.json"))
        )
        settled = await parse("checkout.session.async_payment_succeeded.json")
    assert (settled.kind, settled.provider_ref, settled.fee_minor) == ("paid", CS, 50)
    with respx.mock:
        failed = await parse("checkout.session.async_payment_failed.json")
    assert (failed.kind, failed.provider_ref, failed.amount_minor) == ("failed", CS, None)


async def test_an_expired_session_expires_the_request() -> None:
    with respx.mock:
        event = await parse("checkout.session.expired.json")
    assert (event.event_id, event.kind, event.provider_ref) == ("evt_1exp", "expired", CS)


@respx.mock
async def test_each_refund_is_recorded_once_for_its_own_amount() -> None:
    """A Refund carries its own ``amount`` (https://docs.stripe.com/api/refunds/object),
    so two part refunds are 400 and 300; the event id is the Refund's, so its
    ``refund.created`` and ``refund.updated`` record it once."""
    listed = respx.get(f"{API}/v1/checkout/sessions").mock(
        return_value=httpx.Response(200, json=payload("api.checkout_sessions.list.json"))
    )
    first = await parse("refund.created.json")
    assert first == ProviderEvent(
        event_id="refund:re_1first", kind="refunded", provider_ref=CS, amount_minor=400,
        currency="USD", created_at=datetime.fromtimestamp(1762560100, UTC),
    )  # fmt: skip
    assert listed.calls.last.request.url.params["payment_intent"] == PI
    second = await parse("refund.created.second.json")
    assert (second.event_id, second.amount_minor, second.provider_ref) == (
        "refund:re_2second", 300, CS,
    )  # fmt: skip
    again = await parse("refund.updated.metadata.json")
    assert (again.event_id, again.amount_minor) == ("refund:re_1first", 400)


@respx.mock
async def test_a_pending_refund_is_recorded_when_it_succeeds() -> None:
    respx.get(f"{API}/v1/checkout/sessions").mock(
        return_value=httpx.Response(200, json=payload("api.checkout_sessions.list.json"))
    )
    pending = await parse("refund.created.pending.json")
    assert (pending.event_id, pending.kind) == ("evt_3ref", "other")
    settled = await parse("refund.updated.succeeded.json")
    assert (settled.event_id, settled.kind, settled.amount_minor) == (
        "refund:re_3bank", "refunded", 700,
    )  # fmt: skip


@respx.mock
@pytest.mark.parametrize(
    ("kind", "status", "failure_txn"),
    [
        ("refund.failed", "failed", "txn_3refundfail"),
        ("refund.updated", "failed", "txn_3refundfail"),
        ("refund.updated", "failed", None),
        ("refund.updated", "canceled", "txn_3refundfail"),
        ("refund.updated", "canceled", None),
    ],
)
async def test_every_failed_or_cancelled_refund_is_one_refund_failed_event(
    kind: str, status: str, failure_txn: str | None
) -> None:
    """#5681: a Refund that ends ``failed`` or ``canceled`` (a cancel is a kind of
    failure, https://docs.stripe.com/refunds#cancel-refund) gives one ``refund_failed``
    event, whatever its ``failure_balance_transaction``: Stripe sets that on a failure that
    never succeeded too (https://docs.stripe.com/refunds#failed-refunds). ``refund.failed``
    and ``refund.updated`` share its event id, and it names the refund's own event id; the
    lifecycle decides whether there is a refund to reverse."""
    respx.get(f"{API}/v1/checkout/sessions").mock(
        return_value=httpx.Response(200, json=payload("api.checkout_sessions.list.json"))
    )
    want = ProviderEvent(
        event_id="refund-failed:re_3bank", kind="refund_failed", provider_ref=CS,
        currency="USD", reverses="refund:re_3bank",
        created_at=MADE + timedelta(seconds=200),
    )  # fmt: skip
    failed = payload("refund.failed.json")
    failed["type"] = kind
    failed["data"]["object"] |= {"status": status, "failure_balance_transaction": failure_txn}
    body = json.dumps(failed).encode()
    assert await STRIPE.parse_webhook(CRED, signed(body), body) == want


async def test_a_refund_that_needs_the_customers_bank_details_is_other() -> None:
    """``requires_action`` (https://docs.stripe.com/refunds#refunds-that-require-action) is
    neither money out nor a failure: nothing is told until it succeeds, fails or is
    cancelled."""
    event = payload("refund.failed.json")
    event["type"] = "refund.updated"
    event["data"]["object"] |= {"status": "requires_action", "failure_balance_transaction": None}
    body = json.dumps(event).encode()
    with respx.mock:
        parsed = await STRIPE.parse_webhook(CRED, signed(body), body)
    assert (parsed.event_id, parsed.kind) == ("evt_5reffail", "other")


@respx.mock
async def test_a_refund_of_a_sale_outside_scadbuddy_is_unmatched() -> None:
    respx.get(f"{API}/v1/checkout/sessions").mock(
        return_value=httpx.Response(200, json=payload("api.checkout_sessions.list.empty.json"))
    )
    event = await parse("refund.created.outside.json")
    assert (event.kind, event.provider_ref) == ("refunded", None)


async def test_charge_refunded_is_not_read() -> None:
    """Its running ``amount_refunded`` cannot tell this refund's amount; the Refund's own
    events are read instead."""
    body = json.dumps(
        {"id": "evt_c", "type": "charge.refunded", "data": {"object": {"id": "ch_1"}}}
    ).encode()
    with respx.mock:
        assert (await STRIPE.parse_webhook(CRED, signed(body), body)).kind == "other"


@respx.mock
async def test_stripe_not_answering_for_a_refund_is_unavailable() -> None:
    respx.get(f"{API}/v1/checkout/sessions").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderUnavailableError):
        await parse("refund.created.json")


@respx.mock
async def test_the_balance_transaction_arriving_is_a_fee_event() -> None:
    respx.get(f"{API}/v1/balance_transactions/{TXN}").mock(
        return_value=httpx.Response(200, json=payload("api.balance_transaction.json"))
    )
    respx.get(f"{API}/v1/checkout/sessions").mock(
        return_value=httpx.Response(200, json=payload("api.checkout_sessions.list.json"))
    )
    event = await parse("charge.updated.balance_transaction.json")
    assert event == ProviderEvent(
        event_id="evt_1fee", kind="fee", provider_ref=CS, currency="USD", fee_minor=50,
        created_at=MADE,
    )  # fmt: skip


async def test_another_charge_update_is_other_with_no_call() -> None:
    with respx.mock:
        event = await parse("charge.updated.metadata.json")
    assert event.kind == "other"


async def test_an_event_stripe_sends_that_scadbuddy_does_not_read_is_other() -> None:
    body = json.dumps(
        {"id": "evt_other", "type": "customer.created", "data": {"object": {"id": "cus_1"}}}
    ).encode()
    assert (await STRIPE.parse_webhook(CRED, signed(body), body)).kind == "other"


def test_the_secrets_never_show_in_the_adapter() -> None:
    assert KEY not in repr(StripeProvider()) and WHSEC not in repr(CRED)


@respx.mock
async def test_a_key_without_a_permission_never_repeats_the_key() -> None:
    """Stripe's permission error quotes the key's prefix and last digits."""
    message = "The provided key 'rk_test_****1234' does not have the required permissions"
    respx.get(f"{API}/v1/checkout/sessions").mock(
        return_value=httpx.Response(403, json={"error": {"message": message}})
    )
    with pytest.raises(ProviderError) as refused:
        await STRIPE.verify(CRED)
    assert str(refused.value) == "the Stripe key lacks a permission ScadBuddy needs"


async def test_an_id_that_is_not_a_stripe_id_is_never_put_in_a_path() -> None:
    # Nothing is routed: a request would fail the test.
    with respx.mock, pytest.raises(ProviderError):
        await STRIPE.cancel_request(CRED, "../v1/charges")
    session = {
        "id": CS, "currency": "usd", "amount_total": 700, "payment_status": "paid",
        "payment_intent": "../v1/x",
    }  # fmt: skip
    body = json.dumps(
        {"id": "evt_x", "type": "checkout.session.completed", "data": {"object": session}}
    ).encode()
    with respx.mock, pytest.raises(ProviderError):
        await STRIPE.parse_webhook(CRED, signed(body), body)
