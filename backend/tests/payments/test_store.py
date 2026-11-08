"""The payments store (#4957): request rows, the event ledger and the default provider."""

from __future__ import annotations

import pytest

from scadbuddy.orders.models import Actor, Order
from scadbuddy.orders.store import OrderStore
from scadbuddy.payments.models import (
    PaymentRequest,
    PaymentRequestStatus,
    ProviderEvent,
)
from scadbuddy.payments.store import (
    default_provider,
    finish_event,
    has_open_requests,
    insert_event,
    insert_request,
    open_requests_for_order,
    request,
    request_by_ref,
    requests_for_order,
    set_request_link,
    set_request_status,
)

pytestmark = pytest.mark.requires_postgres

BY = Actor(kind="browser")
EVENT = ProviderEvent(event_id="evt_1", kind="paid", provider_ref="fake_1", amount_minor=500)
LINK = "https://payments.fake.invalid/pay/fake_1"


@pytest.fixture
def order(rows: OrderStore) -> Order:
    customer = rows.insert_customer(
        display_name="Ada", company=None, notes="", tags=[], contacts=[], addresses=[]
    )
    with rows.transaction() as conn:
        return rows.insert_order(conn, customer_id=customer.id, currency="USD", created_by=BY)


def test_open_requests_for_order_leaves_out_provider_ref_and_closed_ones(
    rows: OrderStore, order: Order
) -> None:
    with rows.transaction() as conn:
        linked = insert_request(
            conn, order_id=order.id, kind="deposit", provider="fake", amount_minor=500,
            currency="USD", created_by=BY,
        )  # fmt: skip
        set_request_link(conn, linked.id, provider_ref="fake_1", hosted_url=LINK, link_error=None)
        done = insert_request(
            conn, order_id=order.id, kind="balance", provider="fake", amount_minor=900,
            currency="USD", created_by=BY,
        )  # fmt: skip
        set_request_status(conn, done.id, "paid")
        found = open_requests_for_order(conn, order.id)
    assert [r.id for r in found] == [linked.id]
    assert type(found[0]) is PaymentRequest
    assert "provider_ref" not in found[0].model_dump()
    assert found[0].hosted_url == LINK


def test_a_request_with_no_provider_has_no_link(rows: OrderStore, order: Order) -> None:
    with rows.transaction() as conn:
        req = insert_request(
            conn, order_id=order.id, kind="deposit", provider=None, amount_minor=500,
            currency="USD", created_by=BY,
        )  # fmt: skip
    assert (req.provider, req.hosted_url, req.status, req.paid_at) == (None, None, "open", None)
    assert req.created_by == BY


def test_paid_stamps_paid_at_and_names_the_payment(rows: OrderStore, order: Order) -> None:
    with rows.transaction() as conn:
        req = insert_request(
            conn, order_id=order.id, kind="balance", provider=None, amount_minor=500,
            currency="USD", created_by=BY,
        )  # fmt: skip
        payment = rows.insert_payment(
            conn, order.id, amount_minor=500, method="card", reference=None,
            received_at=None, notes="", provider="fake", payment_request_id=req.id,
        )  # fmt: skip
        paid = set_request_status(conn, req.id, "paid", payment_id=payment.id)
    assert paid.status == "paid" and paid.paid_at is not None and paid.payment_id == payment.id
    assert (payment.provider, payment.payment_request_id) == ("fake", req.id)


def test_an_event_is_inserted_once(rows: OrderStore, order: Order) -> None:
    with rows.transaction() as conn:
        first = insert_event(conn, "fake", EVENT)
        assert isinstance(first, int) and insert_event(conn, "fake", EVENT) is None
        finish_event(conn, first, outcome="unmatched", order_id=order.id)
        row = conn.execute("SELECT outcome, order_id FROM payment_events").fetchone()
    assert row == {"outcome": "unmatched", "order_id": order.id}


def test_request_by_ref_is_per_provider(rows: OrderStore, order: Order) -> None:
    with rows.transaction() as conn:
        linked = insert_request(
            conn, order_id=order.id, kind="deposit", provider="fake", amount_minor=500,
            currency="USD", created_by=BY,
        )  # fmt: skip
        set_request_link(conn, linked.id, provider_ref="fake_1", hosted_url=LINK, link_error=None)
        found = request_by_ref(conn, "fake", "fake_1")
        assert found is not None and found.id == linked.id
        assert found.provider_ref == "fake_1"
        assert request_by_ref(conn, "other", "fake_1") is None
        assert request(conn, linked.id) == found
        assert [r.id for r in requests_for_order(conn, order.id)] == [linked.id]


def test_has_open_requests_and_the_default_provider(rows: OrderStore, order: Order) -> None:
    with rows.transaction() as conn:
        assert default_provider(conn, {"fake"}) is None and not has_open_requests(conn, "fake")
        req = insert_request(
            conn, order_id=order.id, kind="deposit", provider="fake", amount_minor=500,
            currency="USD", created_by=BY,
        )  # fmt: skip
        assert has_open_requests(conn, "fake") and not has_open_requests(conn, "other")
        # A failed request's page can still be approved (#4959): it holds the provider.
        set_request_status(conn, req.id, "failed")
        assert has_open_requests(conn, "fake")
        set_request_status(conn, req.id, "cancelled")
        assert not has_open_requests(conn, "fake")
        conn.execute(
            "INSERT INTO payment_credentials"
            " (provider, account_label, secret_sealed, dek_sealed, kek_id, is_default)"
            " VALUES ('fake', 'Fake', '\\x00', '\\x00', 'k', true)"
        )
        assert default_provider(conn, {"fake"}) == "fake"
        # A default no longer offered (the fake flag went off) is no default.
        assert default_provider(conn, set()) is None


@pytest.mark.parametrize("status", ["cancelled", "expired"])
def test_closing_without_payment_leaves_paid_at_empty(
    rows: OrderStore, order: Order, status: PaymentRequestStatus
) -> None:
    with rows.transaction() as conn:
        req = insert_request(
            conn, order_id=order.id, kind="deposit", provider=None, amount_minor=500,
            currency="USD", created_by=BY,
        )  # fmt: skip
        closed = set_request_status(conn, req.id, status)
    assert closed.status == status and closed.paid_at is None
