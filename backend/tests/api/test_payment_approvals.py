"""An ``approved`` event (#4959): ScadBuddy, not the provider, decides whether a customer's
approval is captured. Under the order lock, an open or failed request moves to
``pending`` and the transaction commits. Only then is the provider's ``capture_request``
called, with no lock held and no transaction open. A cancelled or paid request is left
alone, so its approval is never captured. A capture that fails moves the request back to
``failed``. The capture's own events (paid, failed) move it on, as before."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.core.settings import Settings
from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.lifecycle import LifecycleError
from scadbuddy.orders.models import Actor
from scadbuddy.payments.fake import FAKE, signed_event
from scadbuddy.payments.models import CaptureOutcome
from scadbuddy.payments.provider import Credential, ProviderError, ProviderUnavailableError
from tests.api.test_order_payments import (
    connect_fake,
    payable_order,
    request_payment,
    with_payments,
)

pytestmark = pytest.mark.requires_postgres

HOOK = "/api/v1/payments/webhooks/fake"
BY = Actor(kind="browser")


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    return with_payments(settings, tmp_path)


@pytest.fixture
def captures(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Each (provider_ref, event_id) the fake was asked to capture; it answers captured."""
    calls: list[tuple[str, str]] = []

    async def capture(credential: Credential, provider_ref: str, event_id: str) -> CaptureOutcome:
        calls.append((provider_ref, event_id))
        return "captured"

    monkeypatch.setattr(FAKE, "capture_request", capture)
    return calls


@pytest.fixture
def cancels(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr(FAKE, "cancelled", [])
    return FAKE.cancelled


def answer_with(monkeypatch: pytest.MonkeyPatch, do: Callable[[str, str], Any]) -> list[str]:
    calls: list[str] = []

    async def capture(credential: Credential, provider_ref: str, event_id: str) -> CaptureOutcome:
        calls.append(event_id)
        result: CaptureOutcome = do(provider_ref, event_id)
        return result

    monkeypatch.setattr(FAKE, "capture_request", capture)
    return calls


def approved(req: dict[str, Any], event_id: str = "ap1") -> tuple[bytes, dict[str, str]]:
    return signed_event(
        "s3cret", {"id": event_id, "type": "approved", "provider_ref": req["provider_ref"]}
    )


def paid(req: dict[str, Any], event_id: str = "cap1") -> tuple[bytes, dict[str, str]]:
    return signed_event(
        "s3cret",
        {
            "id": event_id,
            "type": "paid",
            "provider_ref": req["provider_ref"],
            "amount_minor": req["amount_minor"],
            "currency": req["currency"],
        },
    )


def post(client: TestClient, signed: tuple[bytes, dict[str, str]]) -> Any:
    body, headers = signed
    return client.post(HOOK, content=body, headers=headers)


def status_of(client: TestClient, order_id: str, request_id: str) -> str:
    found = client.get(f"/api/v1/orders/{order_id}/payment-requests").json()
    return str(next(r["status"] for r in found if r["id"] == request_id))


def linked(client: TestClient) -> tuple[str, dict[str, Any]]:
    connect_fake(client)
    order = payable_order(client)
    return order["id"], request_payment(client, order["id"])


def lifecycle(client: TestClient) -> Any:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state.components.get(ORDERS).lifecycle


def test_an_open_request_moves_to_pending_and_is_captured(
    client: TestClient, captures: list[tuple[str, str]]
) -> None:
    order_id, req = linked(client)
    answer = post(client, approved(req))
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"outcome": "recorded"}
    assert captures == [(req["provider_ref"], "ap1")]
    assert status_of(client, order_id, req["id"]) == "pending"
    # The capture's own event pays it.
    assert post(client, paid(req)).json() == {"outcome": "recorded"}
    assert status_of(client, order_id, req["id"]) == "paid"


def test_the_capture_runs_after_the_commit_with_no_lock_held(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, pg_conninfo: str
) -> None:
    order_id, req = linked(client)
    seen: list[tuple[str, bool]] = []

    def check(provider_ref: str, event_id: str) -> CaptureOutcome:
        with psycopg.connect(pg_conninfo, autocommit=True) as conn:
            row = conn.execute(
                "SELECT status FROM payment_requests WHERE id = %s", (req["id"],)
            ).fetchone()
            assert row is not None
            # NOWAIT: a lock still held would raise here instead of waiting.
            conn.execute("SET lock_timeout = '1ms'")
            try:
                conn.execute("SELECT id FROM orders WHERE id = %s FOR UPDATE", (order_id,))
                free = True
            except psycopg.errors.LockNotAvailable:
                free = False
        seen.append((row[0], free))
        return "captured"

    answer_with(monkeypatch, check)
    assert post(client, approved(req)).json() == {"outcome": "recorded"}
    assert seen == [("pending", True)]


@pytest.mark.parametrize("closed", ["cancelled", "paid"])
def test_a_closed_request_s_approval_is_never_captured(
    client: TestClient, captures: list[tuple[str, str]], closed: str
) -> None:
    order_id, req = linked(client)
    if closed == "cancelled":
        done = client.post(f"/api/v1/orders/{order_id}/payment-requests/{req['id']}/cancel")
    else:
        done = client.post(
            f"/api/v1/orders/{order_id}/payments",
            json={"amount_minor": 1000, "method": "cash", "payment_request_id": req["id"]},
        )
    assert done.status_code in (200, 201), done.text
    assert post(client, approved(req)).json() == {"outcome": "ignored"}
    assert captures == []
    assert status_of(client, order_id, req["id"]) == closed


def test_a_failed_request_is_captured_again_on_a_new_approval(
    client: TestClient, captures: list[tuple[str, str]]
) -> None:
    order_id, req = linked(client)
    failed = signed_event(
        "s3cret", {"id": "f1", "type": "failed", "provider_ref": req["provider_ref"]}
    )
    assert post(client, failed).json() == {"outcome": "recorded"}
    assert status_of(client, order_id, req["id"]) == "failed"
    assert post(client, approved(req, "ap2")).json() == {"outcome": "recorded"}
    assert captures == [(req["provider_ref"], "ap2")]
    assert status_of(client, order_id, req["id"]) == "pending"


def test_a_declined_capture_leaves_the_request_failed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    order_id, req = linked(client)
    calls = answer_with(monkeypatch, lambda ref, event: "declined")
    assert post(client, approved(req)).json() == {"outcome": "recorded"}
    assert status_of(client, order_id, req["id"]) == "failed"
    # Kept: the same approval is not captured again.
    assert post(client, approved(req)).json() == {"outcome": "replayed"}
    assert calls == ["ap1"]


def test_a_capture_the_provider_refuses_leaves_the_request_failed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    order_id, req = linked(client)

    def refuse(ref: str, event: str) -> CaptureOutcome:
        raise ProviderError("PayPal refused: INVALID_REQUEST")

    answer_with(monkeypatch, refuse)
    answer = post(client, approved(req))
    assert answer.status_code == 200, answer.text
    assert status_of(client, order_id, req["id"]) == "failed"


def test_a_capture_the_provider_does_not_answer_is_503_and_tried_again(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    order_id, req = linked(client)
    tries = {"n": 0}

    def down_once(ref: str, event: str) -> CaptureOutcome:
        tries["n"] += 1
        if tries["n"] == 1:
            raise ProviderUnavailableError("PayPal did not capture the order")
        return "captured"

    calls = answer_with(monkeypatch, down_once)
    first = post(client, approved(req))
    assert first.status_code == 503
    assert status_of(client, order_id, req["id"]) == "failed"
    # The provider sends it again: the event was forgotten, so it is read again.
    assert post(client, approved(req)).json() == {"outcome": "recorded"}
    assert calls == ["ap1", "ap1"]
    assert status_of(client, order_id, req["id"]) == "pending"


def test_a_replay_of_an_approval_still_pending_captures_again(
    client: TestClient, captures: list[tuple[str, str]], pg_conninfo: str
) -> None:
    """The process stopped between the commit and the capture: the provider's retry reads
    ``replayed``, and the request still pending is captured (the provider's idempotency
    key is the event id, so a capture that did land is not made twice)."""
    _, req = linked(client)
    assert post(client, approved(req)).json() == {"outcome": "recorded"}
    assert post(client, approved(req)).json() == {"outcome": "replayed"}
    assert captures == [(req["provider_ref"], "ap1"), (req["provider_ref"], "ap1")]
    # Paid: a later replay captures nothing.
    post(client, paid(req))
    assert post(client, approved(req)).json() == {"outcome": "replayed"}
    assert len(captures) == 2


def test_a_cancel_during_the_capture_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    order_id, req = linked(client)
    refused: list[str] = []

    def cancel_meanwhile(ref: str, event: str) -> CaptureOutcome:
        try:
            lifecycle(client).cancel_payment_request(order_id, req["id"], BY)
        except LifecycleError as error:
            refused.append(error.slug)
        return "captured"

    answer_with(monkeypatch, cancel_meanwhile)
    assert post(client, approved(req)).json() == {"outcome": "recorded"}
    assert refused == ["payment_request_not_open"]
    assert status_of(client, order_id, req["id"]) == "pending"


def test_an_order_cancelled_during_the_capture_leaves_the_request_pending(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The order's cancel ends its open requests only; the money on its way is recorded
    when the capture's event arrives."""
    order_id, req = linked(client)

    def cancel_order(ref: str, event: str) -> CaptureOutcome:
        lifecycle(client).cancel_order(order_id, BY)
        return "captured"

    answer_with(monkeypatch, cancel_order)
    assert post(client, approved(req)).json() == {"outcome": "recorded"}
    assert status_of(client, order_id, req["id"]) == "pending"
    assert post(client, paid(req)).json() == {"outcome": "recorded"}
    found = client.get(f"/api/v1/orders/{order_id}").json()
    assert [p["amount_minor"] for p in found["payments"]] == [req["amount_minor"]]


def test_an_adapter_that_cannot_capture_reads_approved_as_other(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    order_id, req = linked(client)
    monkeypatch.delattr(type(FAKE), "capture_request", raising=False)
    assert post(client, approved(req)).json() == {"outcome": "ignored"}
    assert status_of(client, order_id, req["id"]) == "open"


def fail_it(client: TestClient, req: dict[str, Any], event_id: str = "f1") -> None:
    failed = signed_event(
        "s3cret", {"id": event_id, "type": "failed", "provider_ref": req["provider_ref"]}
    )
    assert post(client, failed).json() == {"outcome": "recorded"}


def test_an_approval_after_an_order_cancel_with_a_failed_request_is_not_captured(
    client: TestClient, captures: list[tuple[str, str]], cancels: list[str]
) -> None:
    """The order's cancel ends a failed request too, and closes its page."""
    order_id, req = linked(client)
    fail_it(client, req)
    assert client.post(f"/api/v1/orders/{order_id}/cancel").status_code == 200
    assert status_of(client, order_id, req["id"]) == "cancelled"
    assert cancels == [req["provider_ref"]]
    assert post(client, approved(req)).json() == {"outcome": "ignored"}
    assert captures == []


def test_a_cancel_ends_a_failed_request(client: TestClient, cancels: list[str]) -> None:
    order_id, req = linked(client)
    fail_it(client, req)
    done = client.post(f"/api/v1/orders/{order_id}/payment-requests/{req['id']}/cancel")
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "cancelled"
    assert cancels == [req["provider_ref"]]


@pytest.mark.parametrize("ended", ["closed", "cancelled"])
def test_an_approval_on_an_ended_order_is_never_captured(
    client: TestClient, captures: list[tuple[str, str]], pg_conninfo: str, ended: str
) -> None:
    """Whatever the request reads, an order no longer taking money is not captured."""
    order_id, req = linked(client)
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute("UPDATE orders SET status = %s WHERE id = %s", (ended, order_id))
    assert post(client, approved(req)).json() == {"outcome": "ignored"}
    assert captures == []
    assert status_of(client, order_id, req["id"]) == "open"
