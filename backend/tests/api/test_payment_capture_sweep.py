"""The pending captures' sweep (#4959, `Payments.settle_captures`): an approval moved a
request to ``pending``, the capture's outcome was never told, and PayPal stopped sending
the approval. The sweep reads the order with no lock held and settles the request, so it
never blocks its order's cancel and the disconnect for good."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.orders.component import ORDERS
from scadbuddy.payments import store as payments_store
from scadbuddy.payments.component import PAYMENTS
from scadbuddy.payments.fake import FAKE, signed_event
from scadbuddy.payments.models import CaptureOutcome, CaptureState, ProviderEvent
from scadbuddy.payments.provider import Credential, ProviderError, ProviderUnavailableError
from tests.api.test_order_payments import (
    connect_fake,
    payable_order,
    request_payment,
    with_payments,
)
from tests.api.test_settings_runtime import _state

pytestmark = pytest.mark.requires_postgres

HOOK = "/api/v1/payments/webhooks/fake"
BACKFILL = (
    Path(__file__).parents[2] / "scadbuddy/migrations/20261109T0026Z_payment_capture_backfill.sql"
)


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    return with_payments(settings, tmp_path)


@pytest.fixture
def captures(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Each capture asked for. The first, the webhook's, gets no answer, so the request
    stays ``pending``; any later one is captured."""
    calls: list[tuple[str, str]] = []

    async def capture(credential: Credential, provider_ref: str, event_id: str) -> CaptureOutcome:
        calls.append((provider_ref, event_id))
        if len(calls) == 1:
            raise ProviderUnavailableError("no answer")
        return "captured"

    monkeypatch.setattr(FAKE, "capture_request", capture)
    monkeypatch.setattr(FAKE, "capture_reads", {})
    monkeypatch.setattr(FAKE, "cancelled", [])
    return calls


def post(client: TestClient, event: dict[str, Any]) -> Any:
    body, headers = signed_event("s3cret", event)
    return client.post(HOOK, content=body, headers=headers)


def stuck(client: TestClient) -> tuple[str, dict[str, Any]]:
    """An order whose request an approval moved to ``pending``; the capture was not
    answered (503, so PayPal would send the approval again)."""
    connect_fake(client)
    order = payable_order(client)
    req = request_payment(client, order["id"])
    answer = post(client, {"id": "ap1", "type": "approved", "provider_ref": req["provider_ref"]})
    assert answer.status_code == 503, answer.text
    assert status_of(client, order["id"], req["id"]) == "pending"
    return order["id"], req


def status_of(client: TestClient, order_id: str, request_id: str) -> str:
    found = client.get(f"/api/v1/orders/{order_id}/payment-requests").json()
    return str(next(r["status"] for r in found if r["id"] == request_id))


def due(client: TestClient, request_id: str) -> None:
    """The request's wait, and any read's, is over."""
    with _state(client).components.get(ORDERS).store.transaction() as conn:
        conn.execute(
            "UPDATE payment_requests SET capture_pending_at = capture_pending_at - interval"
            " '2 days', capture_tried_at = capture_tried_at - interval '2 days' WHERE id = %s",
            (request_id,),
        )


def tried(client: TestClient, request_id: str, ago: str) -> None:
    """The sweep's last read of the request was ``ago``."""
    with _state(client).components.get(ORDERS).store.transaction() as conn:
        conn.execute(
            "UPDATE payment_requests SET capture_tried_at = clock_timestamp() - %s::interval"
            " WHERE id = %s",
            (ago, request_id),
        )


def marks(client: TestClient, request_id: str) -> tuple[Any, int]:
    with _state(client).components.get(ORDERS).store.transaction() as conn:
        row = conn.execute(
            "SELECT capture_pending_at, capture_attempts FROM payment_requests WHERE id = %s",
            (request_id,),
        ).fetchone()
    assert row is not None
    return row["capture_pending_at"], row["capture_attempts"]


def settle(client: TestClient) -> int:
    settled: int = asyncio.run(_state(client).components.get(PAYMENTS).service.settle_captures())
    return settled


def paid_read(req: dict[str, Any]) -> ProviderEvent:
    return ProviderEvent(
        event_id="capture:CAP1:paid",
        kind="paid",
        provider_ref=req["provider_ref"],
        amount_minor=req["amount_minor"],
        currency=req["currency"],
        fee_minor=30,
    )


def audit(client: TestClient, order_id: str, kind: str) -> list[dict[str, Any]]:
    events = client.get(f"/api/v1/orders/{order_id}/events").json()
    return [e["detail"] for e in events if e["kind"] == kind]


def test_a_capture_is_not_read_before_its_wait(
    client: TestClient, captures: list[tuple[str, str]]
) -> None:
    order_id, req = stuck(client)
    FAKE.capture_reads[req["provider_ref"]] = paid_read(req)
    assert settle(client) == 0
    assert status_of(client, order_id, req["id"]) == "pending"


def test_a_completed_capture_is_paid_through_the_webhooks_path(
    client: TestClient, captures: list[tuple[str, str]]
) -> None:
    """The read capture goes through `record_provider_event`'s ``paid``: the money and
    its fee are written once, the other balance request the payment cleared is paid and
    its page closed after the commit, and the capture's own webhook, coming later under
    its own event id, writes no second payment."""
    connect_fake(client)
    order = payable_order(client)
    other = request_payment(client, order["id"])
    order_id, req = order["id"], request_payment(client, order["id"])
    answer = post(client, {"id": "ap1", "type": "approved", "provider_ref": req["provider_ref"]})
    assert answer.status_code == 503
    due(client, req["id"])
    FAKE.capture_reads[req["provider_ref"]] = paid_read(req)
    assert settle(client) == 1
    assert status_of(client, order_id, req["id"]) == "paid"
    assert status_of(client, order_id, other["id"]) == "paid"
    assert FAKE.cancelled == [other["provider_ref"]]
    assert marks(client, req["id"]) == (None, 0)
    late = post(
        client,
        {
            "id": "WH-late",
            "type": "paid",
            "provider_ref": req["provider_ref"],
            "amount_minor": req["amount_minor"],
            "currency": req["currency"],
        },
    )
    assert late.status_code == 200
    payments = client.get(f"/api/v1/orders/{order_id}").json()["payments"]
    assert [p["amount_minor"] for p in payments] == [req["amount_minor"]]
    assert client.post(f"/api/v1/orders/{order_id}/cancel").status_code == 200


def test_a_declined_capture_is_failed(client: TestClient, captures: list[tuple[str, str]]) -> None:
    order_id, req = stuck(client)
    due(client, req["id"])
    FAKE.capture_reads[req["provider_ref"]] = ProviderEvent(
        event_id="capture:CAP1:failed", kind="failed", provider_ref=req["provider_ref"]
    )
    assert settle(client) == 1
    assert status_of(client, order_id, req["id"]) == "failed"
    assert marks(client, req["id"]) == (None, 0)


def pending_read(req: dict[str, Any]) -> ProviderEvent:
    return ProviderEvent(
        event_id="capture:CAP1:pending",
        kind="pending",
        provider_ref=req["provider_ref"],
        amount_minor=req["amount_minor"],
        currency=req["currency"],
    )


def test_a_pending_capture_is_read_once_a_day(
    client: TestClient, captures: list[tuple[str, str]]
) -> None:
    """The capture's own event settles a pending capture, as a bank debit's does; but
    that event can be lost too, so the sweep still reads it, at the daily cap."""
    order_id, req = stuck(client)
    due(client, req["id"])
    FAKE.capture_reads[req["provider_ref"]] = pending_read(req)
    assert settle(client) == 0
    assert status_of(client, order_id, req["id"]) == "pending"
    pending_at, attempts = marks(client, req["id"])
    assert pending_at is not None
    assert attempts == payments_store.CAPTURE_DAILY_ATTEMPTS
    # Not read again within the day.
    FAKE.capture_reads[req["provider_ref"]] = paid_read(req)
    tried(client, req["id"], "23 hours")
    assert settle(client) == 0
    tried(client, req["id"], "25 hours")
    assert settle(client) == 1
    assert status_of(client, order_id, req["id"]) == "paid"


def approved(client: TestClient, request_id: str, ago: str) -> None:
    """The approval that moved the request to ``pending`` was ``ago``."""
    with _state(client).components.get(ORDERS).store.transaction() as conn:
        conn.execute(
            "UPDATE payment_requests SET capture_pending_at = clock_timestamp() - %s::interval"
            " WHERE id = %s",
            (ago, request_id),
        )


def test_pending_reads_are_not_counted_toward_the_give_up(
    client: TestClient, captures: list[tuple[str, str]]
) -> None:
    """#5780 review: a capture PayPal reports as pending (an eCheck can take more than a
    week) is not failed by the read count, even after unknown reads: only by its own,
    longer bound."""
    order_id, req = stuck(client)
    with _state(client).components.get(ORDERS).store.transaction() as conn:
        conn.execute(
            "UPDATE payment_requests SET capture_attempts = %s WHERE id = %s",
            (payments_store.CAPTURE_GIVE_UP_ATTEMPTS - 1, req["id"]),
        )
    FAKE.capture_reads[req["provider_ref"]] = pending_read(req)
    for _ in range(payments_store.CAPTURE_GIVE_UP_ATTEMPTS + 2):
        due(client, req["id"])
        assert settle(client) == 0
        assert status_of(client, order_id, req["id"]) == "pending"
    assert marks(client, req["id"])[1] == payments_store.CAPTURE_GIVE_UP_ATTEMPTS - 1


def test_a_capture_pending_past_its_bound_is_failed(
    client: TestClient, captures: list[tuple[str, str]]
) -> None:
    """The request never blocks its order's cancel for good: once the approval is
    `CAPTURE_PENDING_GIVE_UP_DAYS` old, a pending read marks it failed."""
    order_id, req = stuck(client)
    FAKE.capture_reads[req["provider_ref"]] = pending_read(req)
    days = payments_store.CAPTURE_PENDING_GIVE_UP_DAYS
    approved(client, req["id"], f"{days - 1} days")
    assert settle(client) == 0
    assert status_of(client, order_id, req["id"]) == "pending"
    approved(client, req["id"], f"{days} days 1 hour")
    tried(client, req["id"], "25 hours")
    assert settle(client) == 1
    assert status_of(client, order_id, req["id"]) == "failed"
    failed = audit(client, order_id, "payment_request.failed")
    assert failed[-1]["reason"] == "capture_pending_unsettled"


def test_an_order_gone_with_no_capture_is_failed(
    client: TestClient, captures: list[tuple[str, str]]
) -> None:
    order_id, req = stuck(client)
    due(client, req["id"])
    FAKE.capture_reads[req["provider_ref"]] = "gone"
    assert settle(client) == 1
    assert status_of(client, order_id, req["id"]) == "failed"
    assert captures == [(req["provider_ref"], "ap1")]


def test_an_approved_order_with_no_capture_is_captured_again_under_the_approvals_key(
    client: TestClient, captures: list[tuple[str, str]]
) -> None:
    order_id, req = stuck(client)
    due(client, req["id"])
    FAKE.capture_reads[req["provider_ref"]] = "uncaptured"
    assert settle(client) == 0
    assert captures == [(req["provider_ref"], "ap1")] * 2
    # Captured: the capture's own event, or the next read, records the money.
    assert status_of(client, order_id, req["id"]) == "pending"
    assert marks(client, req["id"])[1] == 1
    due(client, req["id"])
    FAKE.capture_reads[req["provider_ref"]] = paid_read(req)
    assert settle(client) == 1
    assert status_of(client, order_id, req["id"]) == "paid"


def test_a_capture_refused_again_is_failed(
    client: TestClient, captures: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    order_id, req = stuck(client)

    async def refused(credential: Credential, provider_ref: str, event_id: str) -> CaptureOutcome:
        raise ProviderError("PayPal refused: ORDER_EXPIRED")

    monkeypatch.setattr(FAKE, "capture_request", refused)
    due(client, req["id"])
    FAKE.capture_reads[req["provider_ref"]] = "uncaptured"
    assert settle(client) == 1
    assert status_of(client, order_id, req["id"]) == "failed"


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE orders SET status = 'cancelled' WHERE id = %s",
        "UPDATE orders SET status = 'closed' WHERE id = %s",
        "UPDATE orders SET source = 'storefront', submitted_at = NULL WHERE id = %s",
    ],
    ids=["cancelled", "closed", "cart"],
)
def test_an_order_that_no_longer_takes_money_is_never_captured_again(
    client: TestClient, captures: list[tuple[str, str]], change: str
) -> None:
    """Checked under the order lock before the capture; the request is failed with the
    reason on its audit row, and never read again."""
    order_id, req = stuck(client)
    with _state(client).components.get(ORDERS).store.transaction() as conn:
        conn.execute(change, (order_id,))
    due(client, req["id"])
    FAKE.capture_reads[req["provider_ref"]] = "uncaptured"
    assert settle(client) == 1
    assert captures == [(req["provider_ref"], "ap1")]
    assert status_of(client, order_id, req["id"]) == "failed"
    failed = audit(client, order_id, "payment_request.failed")
    assert failed[-1]["reason"] == "order_not_taking_money"
    assert marks(client, req["id"]) == (None, 0)
    due(client, req["id"])
    assert settle(client) == 0


def test_a_capture_never_told_is_failed_after_the_last_read(
    client: TestClient, captures: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never a backoff forever: the request would block its order's cancel."""
    monkeypatch.setattr(payments_store, "CAPTURE_GIVE_UP_ATTEMPTS", 3)
    order_id, req = stuck(client)
    for _ in range(2):
        due(client, req["id"])
        assert settle(client) == 0
        assert status_of(client, order_id, req["id"]) == "pending"
    due(client, req["id"])
    assert settle(client) == 1
    assert status_of(client, order_id, req["id"]) == "failed"
    assert audit(client, order_id, "payment_request.failed")[-1]["reason"] == "capture_unsettled"


def test_a_bank_debits_pending_is_never_read(
    client: TestClient, captures: list[tuple[str, str]]
) -> None:
    connect_fake(client)
    order = payable_order(client)
    req = request_payment(client, order["id"])
    assert post(
        client, {"id": "p1", "type": "pending", "provider_ref": req["provider_ref"]}
    ).json() == {"outcome": "recorded"}
    assert marks(client, req["id"]) == (None, 0)
    FAKE.capture_reads[req["provider_ref"]] = paid_read(req)
    assert settle(client) == 0
    assert status_of(client, order["id"], req["id"]) == "pending"


def test_a_paid_read_the_lifecycle_does_not_take_counts_as_unsettled(
    client: TestClient, captures: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read whose event leaves the request ``pending`` (``currency_mismatch`` here,
    ``ignored`` or ``unmatched`` alike) is a read that could not tell: it is counted, so
    the give-up still bounds it."""
    monkeypatch.setattr(payments_store, "CAPTURE_GIVE_UP_ATTEMPTS", 2)
    order_id, req = stuck(client)
    wrong = paid_read(req).model_copy(
        update={"currency": "JPY" if req["currency"] != "JPY" else "USD"}
    )
    FAKE.capture_reads[req["provider_ref"]] = wrong
    due(client, req["id"])
    assert settle(client) == 0
    assert status_of(client, order_id, req["id"]) == "pending"
    assert marks(client, req["id"])[1] == 1
    due(client, req["id"])
    assert settle(client) == 1
    assert status_of(client, order_id, req["id"]) == "failed"
    assert audit(client, order_id, "payment_request.failed")[-1]["reason"] == "capture_unsettled"


def test_one_request_that_raises_does_not_stop_the_batch(
    client: TestClient, captures: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failing request's read is counted, as one that could not tell, and the rest of
    the batch is still read."""
    first_id, first = stuck(client)
    second = payable_order(client)
    other = request_payment(client, second["id"])
    # Captured this time; the capture's event is never told.
    answer = post(client, {"id": "ap2", "type": "approved", "provider_ref": other["provider_ref"]})
    assert answer.status_code == 200, answer.text
    assert status_of(client, second["id"], other["id"]) == "pending"
    FAKE.capture_reads[other["provider_ref"]] = paid_read(other)
    reads = FAKE.read_capture

    async def broken(credential: Credential, provider_ref: str) -> ProviderEvent | CaptureState:
        if provider_ref == first["provider_ref"]:
            raise RuntimeError("boom")
        return await reads(credential, provider_ref)

    monkeypatch.setattr(FAKE, "read_capture", broken)
    due(client, first["id"])
    due(client, other["id"])
    assert settle(client) == 1
    assert status_of(client, second["id"], other["id"]) == "paid"
    assert status_of(client, first_id, first["id"]) == "pending"
    assert marks(client, first["id"])[1] == 1


def test_the_backfill_marks_requests_an_approval_left_pending(
    client: TestClient, captures: list[tuple[str, str]]
) -> None:
    """Requests an approval moved to ``pending`` before ``capture_pending_at`` existed
    are marked, so the sweep reads them; a bank debit's ``pending`` is not."""
    order_id, req = stuck(client)
    connect_fake(client)
    debit_order = payable_order(client)
    debit = request_payment(client, debit_order["id"])
    assert post(
        client, {"id": "p1", "type": "pending", "provider_ref": debit["provider_ref"]}
    ).json() == {"outcome": "recorded"}
    with _state(client).components.get(ORDERS).store.transaction() as conn:
        conn.execute("UPDATE payment_requests SET capture_pending_at = NULL")
        conn.execute(BACKFILL.read_text())
    assert marks(client, req["id"])[0] is not None
    assert marks(client, debit["id"])[0] is None
    due(client, req["id"])
    FAKE.capture_reads[req["provider_ref"]] = paid_read(req)
    assert settle(client) == 1
    assert status_of(client, order_id, req["id"]) == "paid"
