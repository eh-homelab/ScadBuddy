"""The provider seam and the fake provider (#4957)."""

from __future__ import annotations

import pytest

from scadbuddy.payments.fake import FAKE, SIGNATURE_HEADER, sign, signed_event
from scadbuddy.payments.models import ProviderEvent
from scadbuddy.payments.provider import BadSignatureError, Credential
from scadbuddy.payments.registry import providers

CRED = Credential(settings={"account": "a"}, secrets={"signing_secret": "s3cret"})


def test_a_signed_event_parses() -> None:
    body, headers = signed_event(
        "s3cret",
        {
            "id": "e1", "type": "paid", "provider_ref": "fake_r1",
            "amount_minor": 500, "currency": "USD", "fee_minor": 30,
        },
    )  # fmt: skip
    event = FAKE.parse_webhook(CRED, headers, body)
    assert event == ProviderEvent(
        event_id="e1", kind="paid", provider_ref="fake_r1", amount_minor=500,
        currency="USD", fee_minor=30,
    )  # fmt: skip


def test_an_unknown_type_is_other() -> None:
    body, headers = signed_event("s3cret", {"id": "e2", "type": "dispute"})
    assert FAKE.parse_webhook(CRED, headers, body).kind == "other"


def test_a_bad_signature_is_refused_before_the_body_is_read() -> None:
    body, headers = signed_event("other", {"id": "e1", "type": "paid"})
    with pytest.raises(BadSignatureError):
        FAKE.parse_webhook(CRED, headers, body)
    with pytest.raises(BadSignatureError):
        FAKE.parse_webhook(CRED, {}, b"not json")
    with pytest.raises(BadSignatureError):
        FAKE.parse_webhook(CRED, {SIGNATURE_HEADER: sign("s3cret", b"x")}, b"not the signed body")


def test_the_registry_offers_the_fake_only_when_asked() -> None:
    assert providers(fake=False) == {}
    assert set(providers(fake=True)) == {"fake"}


def test_the_fake_has_one_secret_field() -> None:
    assert {f.name: f.secret for f in FAKE.fields} == {"account": False, "signing_secret": True}


async def test_verify_and_create_request() -> None:
    from datetime import UTC, datetime

    from scadbuddy.orders.models import Actor
    from scadbuddy.payments.models import PaymentRequestView

    assert await FAKE.verify(CRED) == "Fake account a"
    view = PaymentRequestView(
        id="r1", order_id="o1", kind="deposit", provider="fake", amount_minor=500,
        currency="USD", hosted_url=None, status="open", created_at=datetime.now(UTC),
        paid_at=None, provider_ref=None, link_error=None, payment_id=None,
        created_by=Actor(kind="browser"),
    )  # fmt: skip
    hosted = await FAKE.create_request(CRED, view, return_url=None, idempotency_key="k")
    assert hosted.provider_ref == "fake_r1"
    assert str(hosted.hosted_url) == "https://payments.fake.invalid/pay/fake_r1"


def test_repr_never_shows_a_secret() -> None:
    assert "s3cret" not in repr(CRED)
