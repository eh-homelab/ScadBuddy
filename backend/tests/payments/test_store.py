"""The payments store (#4957): request rows, the event ledger and the default provider."""

from __future__ import annotations

import pytest

from scadbuddy.orders.models import Actor, Order
from scadbuddy.orders.store import Conn, OrderStore
from scadbuddy.payments.models import (
    PaymentRequest,
    PaymentRequestStatus,
    PaymentRequestView,
    ProviderEvent,
)
from scadbuddy.payments.store import (
    CLOSE_GIVE_UP_ATTEMPTS,
    CLOSE_RETRY_FIRST_MINUTES,
    close_confirmed,
    close_failed,
    closes_due,
    default_provider,
    finish_event,
    has_open_requests,
    insert_event,
    insert_request,
    open_requests_for_order,
    request,
    request_by_ref,
    requests_for_order,
    resume_closes,
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


# -- a provider's page not yet confirmed closed (#5700) -----------------------------------


def _linked(conn: Conn, order: Order, ref: str = "fake_1") -> PaymentRequestView:
    req = insert_request(
        conn, order_id=order.id, kind="balance", provider="fake", amount_minor=500,
        currency="USD", created_by=BY,
    )  # fmt: skip
    set_request_link(conn, req.id, provider_ref=ref, hosted_url=LINK, link_error=None)
    return req


def _pay(rows: OrderStore, conn: Conn, order: Order, req_id: str, provider: str | None) -> str:
    return rows.insert_payment(
        conn, order.id, amount_minor=500, method="card", reference=None, received_at=None,
        notes="", provider=provider, payment_request_id=req_id,
    ).id  # fmt: skip


def test_a_cancel_of_a_linked_request_marks_its_page(rows: OrderStore, order: Order) -> None:
    with rows.transaction() as conn:
        req = _linked(conn, order)
        cancelled = set_request_status(conn, req.id, "cancelled")
    assert cancelled.close_pending_at is not None


def test_a_cancel_of_a_request_with_no_page_marks_nothing(rows: OrderStore, order: Order) -> None:
    with rows.transaction() as conn:
        req = insert_request(
            conn, order_id=order.id, kind="deposit", provider="fake", amount_minor=500,
            currency="USD", created_by=BY,
        )  # fmt: skip
        assert set_request_status(conn, req.id, "cancelled").close_pending_at is None


def test_a_manual_payment_marks_the_page_it_pays(rows: OrderStore, order: Order) -> None:
    with rows.transaction() as conn:
        req = _linked(conn, order)
        paid = set_request_status(
            conn, req.id, "paid", payment_id=_pay(rows, conn, order, req.id, None)
        )
    assert paid.close_pending_at is not None


def test_another_requests_provider_payment_marks_the_page(rows: OrderStore, order: Order) -> None:
    """A balance request a payment on another request settled: its own page is payable."""
    with rows.transaction() as conn:
        req = _linked(conn, order)
        other = _linked(conn, order, "fake_2")
        payment = _pay(rows, conn, order, other.id, "fake")
        assert set_request_status(conn, req.id, "paid", payment_id=payment).close_pending_at
        assert (
            set_request_status(conn, other.id, "paid", payment_id=payment).close_pending_at is None
        )


def test_the_providers_own_payment_clears_the_mark(rows: OrderStore, order: Order) -> None:
    """The page was paid after the cancel: there is nothing left to close."""
    with rows.transaction() as conn:
        req = _linked(conn, order)
        set_request_status(conn, req.id, "cancelled")
        payment = _pay(rows, conn, order, req.id, "fake")
        assert set_request_status(conn, req.id, "paid", payment_id=payment).close_pending_at is None


def test_closes_due_back_off_and_a_confirm_clears_the_mark(rows: OrderStore, order: Order) -> None:
    with rows.transaction() as conn:
        req = _linked(conn, order)
        set_request_status(conn, req.id, "cancelled")
        # Not before the first delay: the close after the commit is still under way.
        assert closes_due(conn) == []
        conn.execute(
            "UPDATE payment_requests SET close_pending_at = close_pending_at - %s * interval"
            " '1 minute' WHERE id = %s",
            (CLOSE_RETRY_FIRST_MINUTES, req.id),
        )
        assert [r.id for r in closes_due(conn)] == [req.id]
        close_failed(conn, req.id, "fake_1")
        assert closes_due(conn) == []
        conn.execute(
            "UPDATE payment_requests SET close_tried_at = close_tried_at - %s * interval"
            " '1 minute' WHERE id = %s",
            (2 * CLOSE_RETRY_FIRST_MINUTES, req.id),
        )
        assert [r.id for r in closes_due(conn)] == [req.id]
        close_confirmed(conn, req.id, "fake_1")
        assert closes_due(conn) == []
        found = request(conn, req.id)
    assert found is not None and found.close_pending_at is None


def test_a_due_page_is_handed_to_one_sweep_only(rows: OrderStore, order: Order) -> None:
    """#5718 review: the sweep runs from both Schedules. Taking a due page starts its
    wait again, so an overlapping run does not try it a second time."""
    with rows.transaction() as conn:
        req = _linked(conn, order)
        set_request_status(conn, req.id, "cancelled")
        conn.execute(
            "UPDATE payment_requests SET close_pending_at = close_pending_at - interval"
            " '1 day' WHERE id = %s",
            (req.id,),
        )
    with rows.transaction() as conn:
        assert [r.id for r in closes_due(conn)] == [req.id]
    with rows.transaction() as conn:
        assert closes_due(conn) == []
        row = conn.execute(
            "SELECT close_attempts FROM payment_requests WHERE id = %s", (req.id,)
        ).fetchone()
    # Taking it is not a failed try: the backoff grows only by `close_failed`.
    assert row == {"close_attempts": 0}


def test_a_page_whose_provider_is_gone_is_given_up_after_its_tries(
    rows: OrderStore, order: Order
) -> None:
    """#5718 review: the sweep stops at the first try, at or after the
    `CLOSE_GIVE_UP_ATTEMPTS`th, that finds the provider not connected, and the request
    says so; any other failure keeps trying."""
    with rows.transaction() as conn:
        req = _linked(conn, order)
        set_request_status(conn, req.id, "cancelled")
        for _ in range(CLOSE_GIVE_UP_ATTEMPTS - 1):
            close_failed(conn, req.id, "fake_1", provider_gone=True)
        close_failed(conn, req.id, "fake_1")
        assert request(conn, req.id).close_abandoned_at is None  # type: ignore[union-attr]
        close_failed(conn, req.id, "fake_1", provider_gone=True)
        found = request(conn, req.id)
        assert found is not None and found.close_abandoned_at is not None
        assert found.close_pending_at is not None
        conn.execute(
            "UPDATE payment_requests SET close_tried_at = close_tried_at - interval '2 days'"
            " WHERE id = %s",
            (req.id,),
        )
        assert closes_due(conn) == []


def _given_up(conn: Conn, order: Order) -> PaymentRequestView:
    req = _linked(conn, order)
    set_request_status(conn, req.id, "cancelled")
    for _ in range(CLOSE_GIVE_UP_ATTEMPTS):
        close_failed(conn, req.id, "fake_1", provider_gone=True)
    found = request(conn, req.id)
    assert found is not None and found.close_abandoned_at is not None
    return found


def test_the_providers_own_payment_clears_a_given_up_page(rows: OrderStore, order: Order) -> None:
    """#5728 review: the page was paid after it was given up (the shop reconnected).
    The paid status clears the mark, and the give-up with it."""
    with rows.transaction() as conn:
        req = _given_up(conn, order)
        payment = _pay(rows, conn, order, req.id, "fake")
        paid = set_request_status(conn, req.id, "paid", payment_id=payment)
    assert paid.close_pending_at is None and paid.close_abandoned_at is None


def test_a_reconnect_resumes_the_providers_given_up_pages(rows: OrderStore, order: Order) -> None:
    """#5728 review: a connect clears that provider's give-ups and their tries, so the
    sweep tries the pages again from the first wait; another provider's are kept."""
    with rows.transaction() as conn:
        req = _given_up(conn, order)
        other = _linked(conn, order, "other_1")
        conn.execute("UPDATE payment_requests SET provider = 'other' WHERE id = %s", (other.id,))
        set_request_status(conn, other.id, "cancelled")
        for _ in range(CLOSE_GIVE_UP_ATTEMPTS):
            close_failed(conn, other.id, "other_1", provider_gone=True)
        resume_closes(conn, "fake")
        found = request(conn, req.id)
        kept = request(conn, other.id)
        assert found is not None and found.close_abandoned_at is None
        assert found.close_pending_at is not None
        assert kept is not None and kept.close_abandoned_at is not None
        conn.execute(
            "UPDATE payment_requests SET close_pending_at = close_pending_at - %s * interval"
            " '1 minute' WHERE id = %s",
            (CLOSE_RETRY_FIRST_MINUTES, req.id),
        )
        assert [r.id for r in closes_due(conn)] == [req.id]


def test_a_close_of_another_page_leaves_the_mark(rows: OrderStore, order: Order) -> None:
    """The answer is about the page the try closed: a reference the request no longer
    holds neither clears its mark nor counts as its try."""
    with rows.transaction() as conn:
        req = _linked(conn, order)
        set_request_status(conn, req.id, "cancelled")
        close_failed(conn, req.id, "fake_other")
        close_confirmed(conn, req.id, "fake_other")
        row = conn.execute(
            "SELECT close_pending_at, close_attempts FROM payment_requests WHERE id = %s",
            (req.id,),
        ).fetchone()
    assert row is not None
    assert row["close_pending_at"] is not None and row["close_attempts"] == 0


def test_the_backoff_stops_at_a_day(rows: OrderStore, order: Order) -> None:
    with rows.transaction() as conn:
        req = _linked(conn, order)
        set_request_status(conn, req.id, "cancelled")
        conn.execute(
            "UPDATE payment_requests SET close_attempts = 1000,"
            " close_tried_at = clock_timestamp() - interval '1 day 1 minute' WHERE id = %s",
            (req.id,),
        )
        assert [r.id for r in closes_due(conn)] == [req.id]
