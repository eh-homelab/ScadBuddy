"""The PayPal adapter (#4959). No test calls PayPal: respx answers every request, and the
payloads in ``paypal_payloads/`` keep the shapes of PayPal's API reference examples
(https://developer.paypal.com/docs/api/orders/v2/,
https://developer.paypal.com/docs/api/payments/v2/,
https://developer.paypal.com/docs/api/webhooks/v1/) and of the sample event in
https://developer.paypal.com/api/rest/webhooks/rest/, given ScadBuddy's ids."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from scadbuddy.orders.models import Actor
from scadbuddy.payments.models import PaymentRequestView, ProviderEvent
from scadbuddy.payments.paypal import API, CANCELLED, PAYPAL, PayPalProvider
from scadbuddy.payments.provider import (
    BadSignatureError,
    Credential,
    ProviderError,
    ProviderUnavailableError,
)
from scadbuddy.payments.registry import providers

PAYLOADS = Path(__file__).parent / "paypal_payloads"
SANDBOX = API["sandbox"]
CLIENT_ID = "AYSq3RDGsmBLJE-otTkBtM-jBRd1TCQwFf9RGfwddNXWz0uFU9ztymylOhRS"
SECRET = "EGnHDxD_qRPdaLdZz8iCr8N7_MzF-YHPTkjs6NKYQvQSBngp4PTTVWkPZRbL"
WEBHOOK_ID = "0NH55953DH663215D"
SETTINGS = {"environment": "sandbox", "client_id": CLIENT_ID, "webhook_id": WEBHOOK_ID}
CRED = Credential(settings=SETTINGS, secrets={"client_secret": SECRET})
ORDER = "5O190127TN364715T"
CAPTURE = "3C679366HH908993F"
#: The headers PayPal sends with a webhook
#: (https://developer.paypal.com/api/rest/webhooks/rest/); PayPal's API checks them.
HEADERS = {
    "paypal-auth-algo": "SHA256withRSA",
    "paypal-cert-url": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-360caa42-fca2a594-ab66f33d",
    "paypal-transmission-id": "db49fb10-1343-11ef-ac58-e32457403f67",
    "paypal-transmission-sig": "Hk3SEXPtqw0XAeOVjDiwv3fG2vAe8j4Kgk9H1U4lDG5CFJxw9J0bK8N2OZzkQ1Tq",
    "paypal-transmission-time": "2024-05-16T05:19:23Z",
}


def payload(name: str) -> Any:
    return json.loads((PAYLOADS / name).read_text())


TOKEN = payload("api.oauth2.token.json")["access_token"]


def view(**fields: Any) -> PaymentRequestView:
    base: dict[str, Any] = {
        "id": "req-1", "order_id": "order-1", "kind": "balance", "provider": "paypal",
        "amount_minor": 700, "currency": "USD", "hosted_url": None, "status": "open",
        "created_at": datetime.now(UTC), "paid_at": None, "provider_ref": None,
        "link_error": None, "payment_id": None, "created_by": Actor(kind="browser"),
    }  # fmt: skip
    return PaymentRequestView(**(base | fields))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def paypal(clock: Clock) -> PayPalProvider:
    """A provider with an empty token cache, so each test fetches its own token."""
    return PayPalProvider(clock=clock)


@pytest.fixture
def pp() -> Any:
    """PayPal's sandbox API, with the token and a SUCCESS verdict; each test adds the
    routes it reads."""
    with respx.mock(base_url=SANDBOX, assert_all_called=False) as router:
        router.post("/v1/oauth2/token", name="token").respond(json=payload("api.oauth2.token.json"))
        router.post("/v1/notifications/verify-webhook-signature", name="verify").respond(
            json=payload("api.verify.success.json")
        )
        # The order as it is just before a capture.
        router.get(f"/v2/checkout/orders/{ORDER}", name="order").respond(
            json=payload("api.order.approved.json")
        )
        yield router


async def parse(paypal: PayPalProvider, name: str) -> ProviderEvent:
    body = (PAYLOADS / name).read_bytes()
    return await paypal.parse_webhook(CRED, HEADERS, body)


# -- the provider ----------------------------------------------------------------------------


def test_the_registry_offers_paypal() -> None:
    assert providers(fake=False)["paypal"] is PAYPAL
    assert set(providers(fake=False)) == {"stripe", "paypal", "square"}
    assert set(providers(fake=True)) == {"stripe", "paypal", "square", "fake"}


def test_paypal_asks_for_the_app_the_webhook_and_the_environment() -> None:
    assert [(f.name, f.secret) for f in PAYPAL.fields] == [
        ("environment", False),
        ("client_id", False),
        ("webhook_id", False),
        ("client_secret", True),
    ]


async def test_verify_reads_the_webhook_and_names_the_app_and_environment(
    paypal: PayPalProvider, pp: Any
) -> None:
    hook = pp.get(f"/v1/notifications/webhooks/{WEBHOOK_ID}").respond(
        json=payload("api.webhook.json")
    )
    verified = await paypal.verify(CRED)
    assert verified.account_label == "PayPal app APP-80W284485P519543T (sandbox)"
    # An app takes many currencies: no account currency to compare (#5666).
    assert verified.currency is None
    token = pp["token"].calls.last.request
    basic = base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
    assert token.headers["authorization"] == f"Basic {basic}"
    assert token.content == b"grant_type=client_credentials"
    assert hook.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"


async def test_live_uses_the_live_api(paypal: PayPalProvider) -> None:
    live = Credential(settings=SETTINGS | {"environment": "live"}, secrets=CRED.secrets)
    with respx.mock(base_url=API["live"]) as router:
        router.post("/v1/oauth2/token").respond(json=payload("api.oauth2.token.json"))
        router.get(f"/v1/notifications/webhooks/{WEBHOOK_ID}").respond(
            json=payload("api.webhook.json")
        )
        verified = await paypal.verify(live)
        assert verified.account_label == "PayPal app APP-80W284485P519543T (live)"


@pytest.mark.parametrize(
    ("settings", "says"),
    [
        (SETTINGS | {"environment": "production"}, "sandbox or live"),
        (SETTINGS | {"webhook_id": "../v1/x"}, "webhook id"),
        (SETTINGS | {"client_id": ""}, "client id"),
    ],
)
async def test_verify_refuses_a_wrong_setting_before_any_call(
    paypal: PayPalProvider, settings: dict[str, str], says: str
) -> None:
    # Nothing is routed: any request would fail the test.
    with respx.mock, pytest.raises(ProviderError, match=says):
        await paypal.verify(Credential(settings=settings, secrets=CRED.secrets))


async def test_a_refused_secret_never_repeats_it(paypal: PayPalProvider, pp: Any) -> None:
    pp["token"].respond(401, json=payload("api.error.invalid_client.json"))
    with pytest.raises(ProviderError) as refused:
        await paypal.verify(CRED)
    assert str(refused.value) == "PayPal refused the client id and secret"
    assert SECRET not in str(refused.value)


async def test_an_unknown_webhook_id_is_refused(paypal: PayPalProvider, pp: Any) -> None:
    pp.get(f"/v1/notifications/webhooks/{WEBHOOK_ID}").respond(
        404, json=payload("api.error.webhook_not_found.json")
    )
    with pytest.raises(ProviderError, match=f"no webhook {WEBHOOK_ID}"):
        await paypal.verify(CRED)


async def test_a_webhook_missing_an_event_names_it(paypal: PayPalProvider, pp: Any) -> None:
    hook = payload("api.webhook.json")
    hook["event_types"] = [
        e for e in hook["event_types"] if e["name"] != "PAYMENT.CAPTURE.REFUNDED"
    ]
    pp.get(f"/v1/notifications/webhooks/{WEBHOOK_ID}").respond(json=hook)
    with pytest.raises(ProviderError, match=r"PAYMENT\.CAPTURE\.REFUNDED"):
        await paypal.verify(CRED)


@pytest.mark.parametrize(
    "names",
    [
        ["*"],
        [
            "CHECKOUT.ORDER.APPROVED",
            "PAYMENT.CAPTURE.COMPLETED",
            "PAYMENT.CAPTURE.PENDING",
            "PAYMENT.CAPTURE.DENIED",
            "PAYMENT.CAPTURE.REFUNDED",
        ],
    ],
)
async def test_every_event_or_the_older_denied_name_is_enough(
    paypal: PayPalProvider, pp: Any, names: list[str]
) -> None:
    hook = payload("api.webhook.json") | {"event_types": [{"name": n} for n in names]}
    pp.get(f"/v1/notifications/webhooks/{WEBHOOK_ID}").respond(json=hook)
    assert (await paypal.verify(CRED)).account_label.startswith("PayPal app")


async def test_paypal_not_answering_is_a_provider_error(paypal: PayPalProvider, pp: Any) -> None:
    pp["token"].mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(ProviderError, match="did not answer"):
        await paypal.verify(CRED)


# -- the token -------------------------------------------------------------------------------


async def test_the_token_is_kept_until_it_is_about_to_expire(
    paypal: PayPalProvider, pp: Any, clock: Clock
) -> None:
    pp.get(f"/v1/notifications/webhooks/{WEBHOOK_ID}").respond(json=payload("api.webhook.json"))
    await paypal.verify(CRED)
    await paypal.verify(CRED)
    assert pp["token"].call_count == 1
    # expires_in is 31668 s; a minute before that a new token is fetched.
    clock.now += 31668 - 59
    await paypal.verify(CRED)
    assert pp["token"].call_count == 2


async def test_an_expired_token_is_dropped_when_another_is_kept(
    paypal: PayPalProvider, pp: Any, clock: Clock
) -> None:
    """A rotated secret leaves its old token behind until it expires; then it goes."""
    pp.get(f"/v1/notifications/webhooks/{WEBHOOK_ID}").respond(json=payload("api.webhook.json"))
    await paypal.verify(CRED)
    clock.now += 31668
    await paypal.verify(Credential(settings=SETTINGS, secrets={"client_secret": "rotated"}))
    assert len(paypal._tokens) == 1


async def test_another_secret_never_reuses_a_token(paypal: PayPalProvider, pp: Any) -> None:
    pp.get(f"/v1/notifications/webhooks/{WEBHOOK_ID}").respond(json=payload("api.webhook.json"))
    await paypal.verify(CRED)
    await paypal.verify(Credential(settings=SETTINGS, secrets={"client_secret": "other"}))
    assert pp["token"].call_count == 2


async def test_a_401_drops_the_token(paypal: PayPalProvider, pp: Any) -> None:
    hook = pp.get(f"/v1/notifications/webhooks/{WEBHOOK_ID}")
    hook.respond(401, json={"error": "invalid_token", "error_description": "expired"})
    with pytest.raises(ProviderError):
        await paypal.verify(CRED)
    hook.respond(json=payload("api.webhook.json"))
    await paypal.verify(CRED)
    assert pp["token"].call_count == 2


def test_the_secrets_never_show_in_the_adapter() -> None:
    assert SECRET not in repr(PayPalProvider()) and SECRET not in repr(CRED)


# -- requests --------------------------------------------------------------------------------


async def test_a_request_is_an_order_to_capture_with_its_approval_link(
    paypal: PayPalProvider, pp: Any
) -> None:
    created = pp.post("/v2/checkout/orders").respond(201, json=payload("api.order.created.json"))
    hosted = await paypal.create_request(
        CRED,
        view(),
        return_url="https://shop.example/orders/order-1",
        idempotency_key="req-1",
        order_number="ORD-2026-7",
    )
    assert hosted.provider_ref == ORDER
    assert str(hosted.hosted_url) == f"https://www.sandbox.paypal.com/checkoutnow?token={ORDER}"
    request = created.calls.last.request
    assert request.headers["paypal-request-id"] == "req-1"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(request.content) == {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "custom_id": "req-1",
                "invoice_id": "ORD-2026-7-req-1",
                "description": "Order ORD-2026-7",
                "amount": {"currency_code": "USD", "value": "7.00"},
            }
        ],
        "payment_source": {
            "paypal": {
                "experience_context": {
                    "user_action": "PAY_NOW",
                    "shipping_preference": "NO_SHIPPING",
                    "return_url": "https://shop.example/orders/order-1",
                    "cancel_url": "https://shop.example/orders/order-1",
                }
            }
        },
    }


async def test_a_deposit_says_so_and_needs_no_return_url(paypal: PayPalProvider, pp: Any) -> None:
    created = pp.post("/v2/checkout/orders").respond(201, json=payload("api.order.created.json"))
    await paypal.create_request(
        CRED, view(kind="deposit"), return_url=None, idempotency_key="k", order_number="ORD-1"
    )
    sent = json.loads(created.calls.last.request.content)
    assert sent["purchase_units"][0]["description"] == "Deposit for order ORD-1"
    assert sent["payment_source"]["paypal"]["experience_context"] == {
        "user_action": "PAY_NOW",
        "shipping_preference": "NO_SHIPPING",
    }


@pytest.mark.parametrize(
    ("currency", "amount", "value"),
    [("JPY", 700, "700"), ("HUF", 70000, "700"), ("TWD", 12300, "123"), ("EUR", 5, "0.05")],
)
async def test_the_amount_is_in_paypal_s_decimals(
    paypal: PayPalProvider, pp: Any, currency: str, amount: int, value: str
) -> None:
    """HUF, JPY and TWD take no decimals at PayPal
    (https://developer.paypal.com/api/rest/reference/currency-codes/)."""
    created = pp.post("/v2/checkout/orders").respond(201, json=payload("api.order.created.json"))
    await paypal.create_request(
        CRED,
        view(currency=currency, amount_minor=amount),
        return_url=None,
        idempotency_key="k",
        order_number="O",
    )
    sent = json.loads(created.calls.last.request.content)
    assert sent["purchase_units"][0]["amount"] == {"currency_code": currency, "value": value}


async def test_a_fraction_of_a_forint_is_refused_before_any_call(paypal: PayPalProvider) -> None:
    with respx.mock, pytest.raises(ProviderError, match="whole HUF"):
        await paypal.create_request(
            CRED,
            view(currency="HUF", amount_minor=70050),
            return_url=None,
            idempotency_key="k",
            order_number="O",
        )


async def test_paypal_refusing_a_request_names_its_reason(paypal: PayPalProvider, pp: Any) -> None:
    pp.post("/v2/checkout/orders").respond(422, json=payload("api.error.decimal_precision.json"))
    with pytest.raises(ProviderError, match="DECIMAL_PRECISION"):
        await paypal.create_request(
            CRED, view(), return_url=None, idempotency_key="k", order_number="O"
        )


async def test_an_answer_with_no_approval_link_is_an_error(paypal: PayPalProvider, pp: Any) -> None:
    created = payload("api.order.created.json")
    created["links"] = created["links"][:1]
    pp.post("/v2/checkout/orders").respond(201, json=created)
    with pytest.raises(ProviderError, match="approval link"):
        await paypal.create_request(
            CRED, view(), return_url=None, idempotency_key="k", order_number="O"
        )


# -- cancel ----------------------------------------------------------------------------------


async def test_cancel_marks_the_order_so_its_approval_is_never_captured(
    paypal: PayPalProvider, pp: Any
) -> None:
    patched = pp.patch(f"/v2/checkout/orders/{ORDER}").respond(204)
    await paypal.cancel_request(CRED, ORDER)
    assert json.loads(patched.calls.last.request.content) == [
        {
            "op": "replace",
            "path": "/purchase_units/@reference_id=='default'/custom_id",
            "value": CANCELLED,
        }
    ]


@pytest.mark.parametrize(
    ("patch", "order"),
    [
        ((422, "api.error.order_not_patchable.json"), (200, "api.order.completed.json")),
        ((404, "api.error.order_not_found.json"), None),
    ],
)
async def test_cancel_of_an_order_already_closed_is_no_error(
    paypal: PayPalProvider, pp: Any, patch: tuple[int, str], order: tuple[int, str] | None
) -> None:
    pp.patch(f"/v2/checkout/orders/{ORDER}").respond(patch[0], json=payload(patch[1]))
    if order is not None:
        pp.get(f"/v2/checkout/orders/{ORDER}").respond(order[0], json=payload(order[1]))
    await paypal.cancel_request(CRED, ORDER)


async def test_cancel_of_an_order_awaiting_the_payer_is_no_error(
    paypal: PayPalProvider, pp: Any
) -> None:
    """PATCH "Updates an order with a CREATED or APPROVED status"
    (https://developer.paypal.com/docs/api/orders/v2/#orders_patch), and a new order is
    PAYER_ACTION_REQUIRED until the customer approves it. The mark is only defence in
    depth: ScadBuddy captures an approval only while its request is open or failed."""
    pp.patch(f"/v2/checkout/orders/{ORDER}").respond(
        422, json=payload("api.error.order_not_patchable.json")
    )
    pp.get(f"/v2/checkout/orders/{ORDER}").respond(
        json=payload("api.order.payer_action_required.json")
    )
    await paypal.cancel_request(CRED, ORDER)


async def test_cancel_that_leaves_an_approved_order_unmarked_is_an_error(
    paypal: PayPalProvider, pp: Any
) -> None:
    pp.patch(f"/v2/checkout/orders/{ORDER}").respond(
        422, json=payload("api.error.order_not_patchable.json")
    )
    pp.get(f"/v2/checkout/orders/{ORDER}").respond(json=payload("api.order.approved.json"))
    with pytest.raises(ProviderError):
        await paypal.cancel_request(CRED, ORDER)


async def test_an_id_that_is_not_a_paypal_id_is_never_put_in_a_path(
    paypal: PayPalProvider,
) -> None:
    with respx.mock, pytest.raises(ProviderError):
        await paypal.cancel_request(CRED, "../v1/oauth2/token")


# -- the signature ---------------------------------------------------------------------------


async def test_the_verify_call_carries_the_body_byte_for_byte(
    paypal: PayPalProvider, pp: Any
) -> None:
    """PayPal: re-serialising the event can fail verification
    (https://developer.paypal.com/api/rest/webhooks/rest/)."""
    pp.post(f"/v2/checkout/orders/{ORDER}/capture").respond(
        201, json=payload("api.order.captured.json")
    )
    body = (PAYLOADS / "checkout.order.approved.json").read_bytes()
    await paypal.parse_webhook(CRED, HEADERS, body)
    sent = pp["verify"].calls.last.request
    assert sent.headers["authorization"] == f"Bearer {TOKEN}"
    assert sent.headers["content-type"] == "application/json"
    assert body in sent.content
    assert json.loads(sent.content) == {
        "auth_algo": "SHA256withRSA",
        "cert_url": HEADERS["paypal-cert-url"],
        "transmission_id": HEADERS["paypal-transmission-id"],
        "transmission_sig": HEADERS["paypal-transmission-sig"],
        "transmission_time": HEADERS["paypal-transmission-time"],
        "webhook_id": WEBHOOK_ID,
        "webhook_event": json.loads(body),
    }


async def test_a_failure_verdict_is_a_bad_signature(paypal: PayPalProvider, pp: Any) -> None:
    pp["verify"].respond(json=payload("api.verify.failure.json"))
    with pytest.raises(BadSignatureError):
        await parse(paypal, "payment.capture.completed.json")


@pytest.mark.parametrize("missing", sorted(HEADERS))
async def test_a_missing_header_is_a_bad_signature_with_no_call(
    paypal: PayPalProvider, missing: str
) -> None:
    body = (PAYLOADS / "payment.capture.completed.json").read_bytes()
    headers = {k: v for k, v in HEADERS.items() if k != missing}
    with respx.mock, pytest.raises(BadSignatureError):
        await paypal.parse_webhook(CRED, headers, body)


async def test_an_overlong_header_is_a_bad_signature_with_no_call(paypal: PayPalProvider) -> None:
    body = (PAYLOADS / "payment.capture.completed.json").read_bytes()
    headers = HEADERS | {"paypal-transmission-sig": "A" * 501}
    with respx.mock, pytest.raises(BadSignatureError):
        await paypal.parse_webhook(CRED, headers, body)


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'"text"'])
async def test_a_body_that_is_no_json_object_is_a_bad_signature_with_no_call(
    paypal: PayPalProvider, body: bytes
) -> None:
    with respx.mock, pytest.raises(BadSignatureError):
        await paypal.parse_webhook(CRED, HEADERS, body)


async def test_no_webhook_id_is_a_bad_signature_with_no_call(paypal: PayPalProvider) -> None:
    cred = Credential(settings=SETTINGS | {"webhook_id": ""}, secrets=CRED.secrets)
    body = (PAYLOADS / "payment.capture.completed.json").read_bytes()
    with respx.mock, pytest.raises(BadSignatureError):
        await paypal.parse_webhook(cred, HEADERS, body)


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(500, json={"name": "INTERNAL_SERVICE_ERROR"}),
        httpx.Response(200, json={"verification_status": "MAYBE"}),
        httpx.ConnectError("down"),
    ],
)
async def test_the_verify_call_failing_is_unavailable(
    paypal: PayPalProvider, pp: Any, answer: httpx.Response | Exception
) -> None:
    if isinstance(answer, Exception):
        pp["verify"].mock(side_effect=answer)
    else:
        pp["verify"].mock(return_value=answer)
    with pytest.raises(ProviderUnavailableError):
        await parse(paypal, "payment.capture.completed.json")


async def test_the_token_failing_during_a_webhook_is_unavailable(
    paypal: PayPalProvider, pp: Any
) -> None:
    pp["token"].respond(500, json={})
    with pytest.raises(ProviderUnavailableError):
        await parse(paypal, "payment.capture.completed.json")


async def test_a_signed_body_that_is_not_an_event_is_a_provider_error(
    paypal: PayPalProvider, pp: Any
) -> None:
    body = b'{"id": "WH-1"}'
    with pytest.raises(ProviderError):
        await paypal.parse_webhook(CRED, HEADERS, body)


# -- events ----------------------------------------------------------------------------------


async def test_an_approved_order_is_approved_and_not_captured_here(
    paypal: PayPalProvider, pp: Any
) -> None:
    """ScadBuddy decides whether to capture (``capture_request``), after it has checked the
    request under the order lock; reading the event captures nothing."""
    # No capture route: a capture call would fail the test.
    event = await parse(paypal, "checkout.order.approved.json")
    assert event == ProviderEvent(
        event_id="WH-1AP12345AB678901C-2BC34567DE890123F", kind="approved", provider_ref=ORDER,
        created_at=datetime(2026, 11, 7, 10, 0, tzinfo=UTC),
    )  # fmt: skip
    assert pp["order"].called


async def test_a_cancelled_order_is_never_approved(paypal: PayPalProvider, pp: Any) -> None:
    pp["order"].respond(json=payload("api.order.approved.cancelled.json"))
    event = await parse(paypal, "checkout.order.approved.cancelled.json")
    assert (event.kind, event.provider_ref) == ("other", ORDER)


async def test_an_approval_sent_before_the_cancel_is_never_approved(
    paypal: PayPalProvider, pp: Any
) -> None:
    """The event is a snapshot: a retry of an approval PayPal sent before the cancel
    carries no mark. The order read again has it."""
    pp["order"].respond(json=payload("api.order.approved.cancelled.json"))
    event = await parse(paypal, "checkout.order.approved.json")
    assert (event.kind, event.provider_ref) == ("other", ORDER)
    assert pp["order"].called


async def test_an_order_scadbuddy_did_not_make_is_not_approved(
    paypal: PayPalProvider, pp: Any
) -> None:
    """Another integration on the same REST app: its order has no request id in
    ``custom_id`` with the matching ``invoice_id``, so ScadBuddy leaves it alone."""
    order = payload("api.order.approved.json")
    order["purchase_units"][0] |= {"custom_id": "other-shop-77", "invoice_id": "INV-9"}
    pp["order"].respond(json=order)
    event = await parse(paypal, "checkout.order.approved.json")
    assert (event.kind, event.provider_ref) == ("other", ORDER)


@pytest.mark.parametrize("status", ["COMPLETED", "VOIDED", "PAYER_ACTION_REQUIRED"])
async def test_an_order_no_longer_approved_is_not_approved(
    paypal: PayPalProvider, pp: Any, status: str
) -> None:
    pp["order"].respond(json=payload("api.order.approved.json") | {"status": status})
    event = await parse(paypal, "checkout.order.approved.json")
    assert (event.kind, event.provider_ref) == ("other", ORDER)


async def test_an_order_paypal_no_longer_has_is_other(paypal: PayPalProvider, pp: Any) -> None:
    """Nothing to capture: a 503 would only make PayPal send it 25 times more."""
    pp["order"].respond(404, json=payload("api.error.order_not_found.json"))
    event = await parse(paypal, "checkout.order.approved.json")
    assert (event.kind, event.provider_ref) == ("other", ORDER)


async def test_paypal_not_answering_the_order_read_is_unavailable(
    paypal: PayPalProvider, pp: Any
) -> None:
    pp["order"].respond(500, json={})
    with pytest.raises(ProviderUnavailableError):
        await parse(paypal, "checkout.order.approved.json")


# -- capture_request -------------------------------------------------------------------------

EVENT_ID = "WH-1AP12345AB678901C-2BC34567DE890123F"


async def test_capture_request_captures_keyed_on_the_approval(
    paypal: PayPalProvider, pp: Any
) -> None:
    captured = pp.post(f"/v2/checkout/orders/{ORDER}/capture").respond(
        201, json=payload("api.order.captured.json")
    )
    assert await paypal.capture_request(CRED, ORDER, EVENT_ID) == "captured"
    request = captured.calls.last.request
    # Keyed on the approval event: a retry of it captures once, and a new approval after a
    # decline is a new capture (ORDER_ALREADY_CAPTURED still stops a second one).
    assert request.headers["paypal-request-id"] == f"capture-{EVENT_ID}"
    assert request.headers["content-type"] == "application/json"


async def test_an_order_already_captured_counts_as_captured(
    paypal: PayPalProvider, pp: Any
) -> None:
    pp.post(f"/v2/checkout/orders/{ORDER}/capture").respond(
        422, json=payload("api.error.order_already_captured.json")
    )
    assert await paypal.capture_request(CRED, ORDER, EVENT_ID) == "captured"


@pytest.mark.parametrize(
    "error", ["api.error.instrument_declined.json", "api.error.transaction_refused.json"]
)
async def test_a_declined_capture_is_declined(paypal: PayPalProvider, pp: Any, error: str) -> None:
    """The request then reads ``failed``, which the shop sees. PayPal's capture reference
    lists these as the capture's own 422 answers
    (https://developer.paypal.com/api/rest/reference/orders/v2/errors/); no PayPal source
    says a ``PAYMENT.CAPTURE.DECLINED`` event follows one, so ScadBuddy does not wait for
    it. If one does come, it finds the request failed already and is ignored."""
    pp.post(f"/v2/checkout/orders/{ORDER}/capture").respond(422, json=payload(error))
    assert await paypal.capture_request(CRED, ORDER, EVENT_ID) == "declined"


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, "api.error.invalid_request.json"),
        (403, "api.error.not_authorized.json"),
        (404, "api.error.order_not_found.json"),
        (422, "api.error.order_not_patchable.json"),
    ],
)
async def test_a_capture_paypal_refuses_for_good_is_a_logged_provider_error(
    paypal: PayPalProvider,
    pp: Any,
    status: int,
    error: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A retry would get the same answer: no 503, so PayPal does not send it 25 times."""
    pp.post(f"/v2/checkout/orders/{ORDER}/capture").respond(status, json=payload(error))
    with caplog.at_level("WARNING"), pytest.raises(ProviderError):
        await paypal.capture_request(CRED, ORDER, EVENT_ID)
    assert any(str(status) in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("status", [401, 500, 503])
async def test_a_capture_paypal_does_not_answer_is_unavailable(
    paypal: PayPalProvider, pp: Any, status: int
) -> None:
    pp.post(f"/v2/checkout/orders/{ORDER}/capture").respond(status, json={})
    with pytest.raises(ProviderUnavailableError):
        await paypal.capture_request(CRED, ORDER, EVENT_ID)


async def test_a_capture_with_no_answer_is_unavailable(paypal: PayPalProvider, pp: Any) -> None:
    pp.post(f"/v2/checkout/orders/{ORDER}/capture").mock(side_effect=httpx.ConnectError("x"))
    with pytest.raises(ProviderUnavailableError):
        await paypal.capture_request(CRED, ORDER, EVENT_ID)


async def test_a_completed_capture_is_paid_with_its_fee(paypal: PayPalProvider, pp: Any) -> None:
    event = await parse(paypal, "payment.capture.completed.json")
    assert event == ProviderEvent(
        event_id="WH-3F562076HD293871E-75F399086E414290U", kind="paid", provider_ref=ORDER,
        amount_minor=700, currency="USD", fee_minor=69,
        created_at=datetime(2026, 11, 7, 10, 0, 6, 355000, tzinfo=UTC),
    )  # fmt: skip


async def test_a_fee_in_another_currency_is_not_recorded(paypal: PayPalProvider, pp: Any) -> None:
    found = payload("payment.capture.completed.json")
    found["resource"]["seller_receivable_breakdown"]["paypal_fee"]["currency_code"] = "EUR"
    body = json.dumps(found).encode()
    event = await paypal.parse_webhook(CRED, HEADERS, body)
    assert (event.kind, event.fee_minor) == ("paid", None)


async def test_a_pending_capture_is_pending(paypal: PayPalProvider, pp: Any) -> None:
    event = await parse(paypal, "payment.capture.pending.json")
    assert (event.kind, event.provider_ref, event.amount_minor, event.currency) == (
        "pending", ORDER, 700, "USD",
    )  # fmt: skip


@pytest.mark.parametrize("name", ["PAYMENT.CAPTURE.DECLINED", "PAYMENT.CAPTURE.DENIED"])
async def test_a_declined_capture_failed(paypal: PayPalProvider, pp: Any, name: str) -> None:
    found = payload("payment.capture.declined.json") | {"event_type": name}
    event = await paypal.parse_webhook(CRED, HEADERS, json.dumps(found).encode())
    assert (event.kind, event.provider_ref, event.amount_minor) == ("failed", ORDER, None)


async def test_each_refund_is_recorded_once_for_its_own_amount(
    paypal: PayPalProvider, pp: Any
) -> None:
    capture = pp.get(f"/v2/payments/captures/{CAPTURE}").respond(json=payload("api.capture.json"))
    first = await parse(paypal, "payment.capture.refunded.json")
    assert first == ProviderEvent(
        event_id="refund:1JU08902781691411", kind="refunded", provider_ref=ORDER,
        amount_minor=400, currency="USD", created_at=datetime(2026, 11, 7, 11, 0, 1, tzinfo=UTC),
    )  # fmt: skip
    assert capture.called
    second = await parse(paypal, "payment.capture.refunded.second.json")
    assert (second.event_id, second.amount_minor) == ("refund:2KV19013892702522", 300)


async def test_a_refund_still_pending_is_read_again_and_other_while_pending(
    paypal: PayPalProvider, pp: Any
) -> None:
    """PayPal documents ``PAYMENT.REFUND.PENDING`` and ``PAYMENT.REFUND.FAILED`` but no
    event for a pending refund that completes
    (https://developer.paypal.com/api/rest/webhooks/event-names/), so the refund is read
    again: one completed by then is recorded; one still pending is not (the README says
    to record it by hand)."""
    refund = pp.get("/v2/payments/refunds/3LW20124903813633").respond(
        json=payload("api.refund.pending.json")
    )
    event = await parse(paypal, "payment.capture.refunded.pending.json")
    assert (event.event_id, event.kind) == ("WH-8RE12345AB678901C-2BC34567DE890123F", "other")
    assert refund.called


async def test_a_refund_completed_by_the_time_it_is_read_is_recorded(
    paypal: PayPalProvider, pp: Any
) -> None:
    pp.get("/v2/payments/refunds/3LW20124903813633").respond(
        json=payload("api.refund.completed.json")
    )
    pp.get(f"/v2/payments/captures/{CAPTURE}").respond(json=payload("api.capture.json"))
    event = await parse(paypal, "payment.capture.refunded.pending.json")
    assert (event.event_id, event.kind, event.provider_ref, event.amount_minor) == (
        "refund:3LW20124903813633", "refunded", ORDER, 700,
    )  # fmt: skip


async def test_a_pending_refund_paypal_no_longer_has_is_other(
    paypal: PayPalProvider, pp: Any
) -> None:
    """A retry would find nothing either: no 503."""
    pp.get("/v2/payments/refunds/3LW20124903813633").respond(
        404, json=payload("api.error.order_not_found.json")
    )
    event = await parse(paypal, "payment.capture.refunded.pending.json")
    assert event.kind == "other"


async def test_a_pending_refund_with_no_id_is_other(paypal: PayPalProvider, pp: Any) -> None:
    """Nothing to read again and nothing to key it by: ``other``, never a 500."""
    found = payload("payment.capture.refunded.pending.json")
    del found["resource"]["id"]
    event = await paypal.parse_webhook(CRED, HEADERS, json.dumps(found).encode())
    assert event.kind == "other"


async def test_a_refund_of_a_sale_outside_scadbuddy_names_its_own_order(
    paypal: PayPalProvider, pp: Any
) -> None:
    pp.get("/v2/payments/captures/9XY12345AB678901C").respond(
        json=payload("api.capture.outside.json")
    )
    event = await parse(paypal, "payment.capture.refunded.outside.json")
    assert (event.kind, event.provider_ref) == ("refunded", "1AB23456CD789012E")


async def test_paypal_not_answering_for_a_refund_is_unavailable(
    paypal: PayPalProvider, pp: Any, caplog: pytest.LogCaptureFixture
) -> None:
    pp.get(f"/v2/payments/captures/{CAPTURE}").respond(404, json={})
    with caplog.at_level("WARNING"), pytest.raises(ProviderUnavailableError) as down:
        await parse(paypal, "payment.capture.refunded.json")
    assert "404" not in str(down.value)
    assert any("404" in r.getMessage() for r in caplog.records)


async def test_a_refund_whose_capture_link_is_not_a_paypal_id_is_malformed(
    paypal: PayPalProvider, pp: Any
) -> None:
    found = payload("payment.capture.refunded.json")
    found["resource"]["links"][1]["href"] = "https://evil.example/v2/payments/captures/../../x"
    with pytest.raises(ProviderError):
        await paypal.parse_webhook(CRED, HEADERS, json.dumps(found).encode())


async def test_an_event_scadbuddy_does_not_read_is_other(paypal: PayPalProvider, pp: Any) -> None:
    for name in ("PAYMENT.CAPTURE.REVERSED", "CUSTOMER.DISPUTE.CREATED"):
        found = payload("payment.capture.completed.json") | {"event_type": name}
        event = await paypal.parse_webhook(CRED, HEADERS, json.dumps(found).encode())
        assert event.kind == "other"


# -- the sweep's read of a capture never told (#4959) ----------------------------------------


def captured_order(status: str, *, fee: str | None = None) -> dict[str, Any]:
    order: dict[str, Any] = payload("api.order.captured.json")
    capture = order["purchase_units"][0]["payments"]["captures"][0]
    capture["status"] = status
    if fee is not None:
        capture["seller_receivable_breakdown"] = {
            "paypal_fee": {"currency_code": "USD", "value": fee}
        }
    return order


@pytest.mark.parametrize("status", ["COMPLETED", "PARTIALLY_REFUNDED", "REFUNDED"])
async def test_a_capture_made_reads_as_paid_keyed_by_the_capture(
    paypal: PayPalProvider, pp: Any, status: str
) -> None:
    pp.routes["order"].respond(json=captured_order(status, fee="0.50"))
    read = await paypal.read_capture(CRED, ORDER)
    assert read == ProviderEvent(
        event_id=f"capture:{CAPTURE}:paid",
        kind="paid",
        provider_ref=ORDER,
        amount_minor=700,
        currency="USD",
        fee_minor=50,
    )


@pytest.mark.parametrize("status", ["DECLINED", "FAILED"])
async def test_a_capture_that_failed_reads_as_failed(
    paypal: PayPalProvider, pp: Any, status: str
) -> None:
    pp.routes["order"].respond(json=captured_order(status))
    read = await paypal.read_capture(CRED, ORDER)
    assert isinstance(read, ProviderEvent)
    expected = ("failed", f"capture:{CAPTURE}:failed", ORDER)
    assert (read.kind, read.event_id, read.provider_ref) == expected


async def test_a_pending_capture_reads_as_pending(paypal: PayPalProvider, pp: Any) -> None:
    pp.routes["order"].respond(json=captured_order("PENDING"))
    read = await paypal.read_capture(CRED, ORDER)
    assert isinstance(read, ProviderEvent)
    assert read.kind == "pending"
    # The same capture read once it completed is a new event, never a replay of this one.
    pp.routes["order"].respond(json=captured_order("COMPLETED"))
    completed = await paypal.read_capture(CRED, ORDER)
    assert isinstance(completed, ProviderEvent)
    assert completed.event_id != read.event_id


async def test_an_approved_order_with_no_capture_reads_uncaptured(
    paypal: PayPalProvider, pp: Any
) -> None:
    assert await paypal.read_capture(CRED, ORDER) == "uncaptured"


async def test_a_voided_or_unknown_order_reads_gone(paypal: PayPalProvider, pp: Any) -> None:
    voided = payload("api.order.approved.json") | {"status": "VOIDED"}
    pp.routes["order"].respond(json=voided)
    assert await paypal.read_capture(CRED, ORDER) == "gone"
    pp.routes["order"].respond(404, json=payload("api.error.order_not_found.json"))
    assert await paypal.read_capture(CRED, ORDER) == "gone"


@pytest.mark.parametrize("answer", [500, 401, "no answer", "PAYER_ACTION_REQUIRED"])
async def test_anything_else_reads_unknown(
    paypal: PayPalProvider, pp: Any, answer: int | str
) -> None:
    if answer == "no answer":
        pp.routes["order"].side_effect = httpx.ConnectTimeout("no answer")
    elif isinstance(answer, int):
        pp.routes["order"].respond(answer, json={"name": "INTERNAL_SERVER_ERROR"})
    else:
        pp.routes["order"].respond(json=payload("api.order.approved.json") | {"status": answer})
    assert await paypal.read_capture(CRED, ORDER) == "unknown"
