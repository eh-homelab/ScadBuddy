"""`/api/v1/orders/{id}/payment-requests` and a manual payment that names one (#4957)."""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.core.settings import Settings
from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.models import Actor
from scadbuddy.payments.component import PAYMENTS
from scadbuddy.payments.fake import FAKE
from scadbuddy.payments.models import HostedRequest
from scadbuddy.payments.provider import ProviderError
from scadbuddy.payments.service import Payments
from scadbuddy.payments.store import CLOSE_GIVE_UP_ATTEMPTS
from tests.api.test_customers import create as create_customer
from tests.api.test_orders import create_order

pytestmark = pytest.mark.requires_postgres

FAKE_CONNECT = {"settings": {"account": "a"}, "secrets": {"signing_secret": "s3cret"}}
PROBLEMS = "https://scadbuddy.dev/problems/orders/"
BY = Actor(kind="browser")


def with_payments(settings: Settings, tmp_path: Path) -> Settings:
    """The app with a KEK and the fake provider offered."""
    key = tmp_path / "kek"
    key.write_text(base64.b64encode(os.urandom(32)).decode())
    return settings.model_copy(update={"secret_key_file": key, "payments_fake_provider": True})


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    return with_payments(settings, tmp_path)


def payable_order(client: TestClient, amount_minor: int = 1000) -> dict[str, Any]:
    """A draft order with one charge, so ``amount_minor`` is due."""
    order = create_order(client, create_customer(client)["id"])
    patched = client.patch(
        f"/api/v1/orders/{order['id']}",
        json={"charges": [{"kind": "fee", "label": "Print", "amount_minor": amount_minor}]},
    )
    assert patched.status_code == 200, patched.text
    return order


def connect_fake(client: TestClient) -> None:
    connected = client.put("/api/v1/payments/providers/fake", json=FAKE_CONNECT)
    assert connected.status_code == 200, connected.text


def request_payment(client: TestClient, order_id: str, **body: Any) -> dict[str, Any]:
    created = client.post(f"/api/v1/orders/{order_id}/payment-requests", json=body)
    assert created.status_code == 201, created.text
    answer: dict[str, Any] = created.json()
    return answer


def test_with_no_provider_the_request_has_no_link(client: TestClient) -> None:
    order = payable_order(client)
    req = request_payment(client, order["id"])
    assert (req["provider"], req["hosted_url"], req["link_error"]) == (None, None, None)
    assert (req["kind"], req["amount_minor"], req["status"]) == ("balance", 1000, "open")
    listed = client.get(f"/api/v1/orders/{order['id']}/payment-requests").json()
    assert [r["id"] for r in listed] == [req["id"]]
    detail = client.get(f"/api/v1/orders/{order['id']}").json()
    assert [r["id"] for r in detail["payment_requests"]] == [req["id"]]


def test_a_request_through_the_default_provider_is_linked(client: TestClient) -> None:
    connect_fake(client)
    order = payable_order(client)
    req = request_payment(client, order["id"], kind="deposit", amount_minor=400)
    assert (req["provider"], req["kind"], req["amount_minor"]) == ("fake", "deposit", 400)
    assert req["hosted_url"] == f"https://payments.fake.invalid/pay/fake_{req['id']}"
    assert req["provider_ref"] == f"fake_{req['id']}"
    manual = request_payment(client, order["id"], provider=None, amount_minor=100)
    assert (manual["provider"], manual["hosted_url"]) == (None, None)


def test_an_unknown_or_unconnected_provider_is_422_and_writes_nothing(client: TestClient) -> None:
    order = payable_order(client)
    for provider in ("stripe", "fake"):
        refused = client.post(
            f"/api/v1/orders/{order['id']}/payment-requests", json={"provider": provider}
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["errors"][0]["loc"] == ["body", "provider"]
    assert client.get(f"/api/v1/orders/{order['id']}/payment-requests").json() == []


def test_a_request_on_an_unknown_order_is_404(client: TestClient) -> None:
    assert client.post("/api/v1/orders/nope/payment-requests", json={}).status_code == 404
    assert client.get("/api/v1/orders/nope/payment-requests").status_code == 404


def test_nothing_due_is_409(client: TestClient) -> None:
    order = create_order(client, create_customer(client)["id"])
    refused = client.post(f"/api/v1/orders/{order['id']}/payment-requests", json={})
    assert refused.status_code == 409
    assert refused.json()["type"] == PROBLEMS + "nothing_due"


def test_a_request_is_cancelled_once(client: TestClient) -> None:
    order = payable_order(client)
    req = request_payment(client, order["id"])
    path = f"/api/v1/orders/{order['id']}/payment-requests/{req['id']}/cancel"
    cancelled = client.post(path)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    again = client.post(path)
    assert again.status_code == 409
    assert again.json()["type"] == PROBLEMS + "payment_request_not_open"


def test_another_orders_request_is_404(client: TestClient) -> None:
    order, other = payable_order(client), payable_order(client)
    req = request_payment(client, order["id"])
    for action in ("cancel", "link"):
        wrong = client.post(f"/api/v1/orders/{other['id']}/payment-requests/{req['id']}/{action}")
        assert wrong.status_code == 404, action
        assert wrong.json()["type"] == PROBLEMS + "payment_request_not_found"


def test_a_manual_payment_names_an_open_request(client: TestClient) -> None:
    order = payable_order(client)
    req = request_payment(client, order["id"], amount_minor=1000)
    paid = client.post(
        f"/api/v1/orders/{order['id']}/payments",
        json={"amount_minor": 1000, "method": "cash", "payment_request_id": req["id"]},
    )
    assert paid.status_code == 201, paid.text
    assert paid.json()["payment_request_id"] == req["id"]
    [closed] = client.get(f"/api/v1/orders/{order['id']}").json()["payment_requests"]
    assert (closed["status"], closed["payment_id"]) == ("paid", paid.json()["id"])


def test_naming_a_closed_request_is_409(client: TestClient) -> None:
    order = payable_order(client)
    req = request_payment(client, order["id"], amount_minor=100)
    client.post(f"/api/v1/orders/{order['id']}/payment-requests/{req['id']}/cancel")
    refused = client.post(
        f"/api/v1/orders/{order['id']}/payments",
        json={"amount_minor": 100, "payment_request_id": req["id"]},
    )
    assert refused.status_code == 409
    assert refused.json()["type"] == PROBLEMS + "payment_request_not_open"


def test_a_failed_link_is_kept_and_retried(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect_fake(client)
    order = payable_order(client)
    real = FAKE.create_request
    calls: list[str] = []

    async def down_once(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs["idempotency_key"])
        if len(calls) == 1:
            raise ProviderError("down")
        return await real(*args, **kwargs)

    monkeypatch.setattr(FAKE, "create_request", down_once)
    req = request_payment(client, order["id"])
    assert (req["status"], req["hosted_url"], req["link_error"]) == ("open", None, "down")
    linked = client.post(f"/api/v1/orders/{order['id']}/payment-requests/{req['id']}/link")
    assert linked.status_code == 200, linked.text
    assert linked.json()["hosted_url"].startswith("https://payments.fake.invalid/pay/")
    assert linked.json()["link_error"] is None
    # The request id is the provider's idempotency key, on every try.
    assert calls == [req["id"], req["id"]]
    # Linked once, a request is not linked again.
    client.post(f"/api/v1/orders/{order['id']}/payment-requests/{req['id']}/link")
    assert len(calls) == 2


def test_a_cancelled_request_is_not_linked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect_fake(client)
    order = payable_order(client)

    real = FAKE.create_request
    calls: list[str] = []

    async def down_once(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs["idempotency_key"])
        if len(calls) == 1:
            raise ProviderError("down")
        return await real(*args, **kwargs)

    monkeypatch.setattr(FAKE, "create_request", down_once)
    req = request_payment(client, order["id"])
    client.post(f"/api/v1/orders/{order['id']}/payment-requests/{req['id']}/cancel")
    late = client.post(f"/api/v1/orders/{order['id']}/payment-requests/{req['id']}/link")
    assert late.status_code == 200
    assert (late.json()["status"], late.json()["hosted_url"]) == ("cancelled", None)
    # A request no longer open is not sent to the provider again.
    assert calls == [req["id"]]


def test_a_disconnect_between_the_check_and_the_open_is_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan gap 11: the service's check passes, then the disconnect commits; the open
    finds no credential row and writes nothing."""
    connect_fake(client)
    order = payable_order(client)

    def checked_then_gone(self: Payments, provider: str) -> bool:
        assert client.delete(f"/api/v1/payments/providers/{provider}").status_code == 204
        return True

    monkeypatch.setattr(Payments, "_is_connected", checked_then_gone)
    refused = client.post(
        f"/api/v1/orders/{order['id']}/payment-requests", json={"provider": "fake"}
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["errors"][0]["loc"] == ["body", "provider"]
    assert client.get(f"/api/v1/orders/{order['id']}/payment-requests").json() == []


def _cancels(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The fake's own record, empty for this test."""
    monkeypatch.setattr(FAKE, "cancelled", [])
    return FAKE.cancelled


def test_cancelling_a_request_closes_its_hosted_page(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancels = _cancels(monkeypatch)
    connect_fake(client)
    order = payable_order(client)
    req = request_payment(client, order["id"])
    path = f"/api/v1/orders/{order['id']}/payment-requests/{req['id']}/cancel"
    assert client.post(path).status_code == 200
    assert cancels == [req["provider_ref"]]


def test_cancelling_the_order_closes_each_open_hosted_page(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancels = _cancels(monkeypatch)
    connect_fake(client)
    order = payable_order(client)
    first = request_payment(client, order["id"], kind="deposit", amount_minor=400)
    second = request_payment(client, order["id"], amount_minor=600)
    manual = request_payment(client, order["id"], provider=None, amount_minor=100)
    done = client.post(f"/api/v1/orders/{order['id']}/payment-requests/{first['id']}/cancel")
    assert done.status_code == 200
    cancels.clear()
    assert client.post(f"/api/v1/orders/{order['id']}/cancel").status_code == 200
    # The request cancelled before is not sent again; one with no provider has no page.
    assert cancels == [second["provider_ref"]]
    statuses = {
        r["id"]: r["status"]
        for r in client.get(f"/api/v1/orders/{order['id']}/payment-requests").json()
    }
    assert statuses == {
        first["id"]: "cancelled",
        second["id"]: "cancelled",
        manual["id"]: "cancelled",
    }


def test_a_manual_payment_closes_the_hosted_pages_it_pays(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancels = _cancels(monkeypatch)
    connect_fake(client)
    order = payable_order(client)
    named = request_payment(client, order["id"], kind="deposit", amount_minor=400)
    balance = request_payment(client, order["id"], amount_minor=600)
    paid = client.post(
        f"/api/v1/orders/{order['id']}/payments",
        json={"amount_minor": 400, "method": "cash", "payment_request_id": named["id"]},
    )
    assert paid.status_code == 201, paid.text
    assert cancels == [named["provider_ref"]]
    # Cash that brings the balance to zero pays the open balance request too.
    rest = client.post(f"/api/v1/orders/{order['id']}/payments", json={"amount_minor": 600})
    assert rest.status_code == 201, rest.text
    assert cancels == [named["provider_ref"], balance["provider_ref"]]


def test_a_failed_page_cancel_leaves_the_request_cancelled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect_fake(client)
    order = payable_order(client)
    req = request_payment(client, order["id"])

    async def down(*args: Any, **kwargs: Any) -> None:
        raise ProviderError("down")

    monkeypatch.setattr(FAKE, "cancel_request", down)
    path = f"/api/v1/orders/{order['id']}/payment-requests/{req['id']}/cancel"
    cancelled = client.post(path)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    [kept] = client.get(f"/api/v1/orders/{order['id']}/payment-requests").json()
    assert kept["status"] == "cancelled"
    # #5700: the back office sees that the page is not yet confirmed closed.
    assert kept["close_pending_at"] is not None


def test_a_closed_page_clears_its_mark(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    cancels = _cancels(monkeypatch)
    connect_fake(client)
    order = payable_order(client)
    req = request_payment(client, order["id"])
    path = f"/api/v1/orders/{order['id']}/payment-requests/{req['id']}/cancel"
    assert client.post(path).json()["close_pending_at"] is None
    [closed] = client.get(f"/api/v1/orders/{order['id']}/payment-requests").json()
    assert closed["close_pending_at"] is None
    assert cancels == [req["provider_ref"]]


def _state(client: TestClient) -> Any:
    app: Any = client.app
    return getattr(app.state, STATE_ATTR)


def _due(client: TestClient, request_id: str) -> None:
    """The mark's wait, and any try's, is over."""
    with _state(client).components.get(ORDERS).store.transaction() as conn:
        conn.execute(
            "UPDATE payment_requests SET close_pending_at = close_pending_at - interval"
            " '2 days', close_tried_at = close_tried_at - interval '2 days' WHERE id = %s",
            (request_id,),
        )


def test_the_sweep_closes_a_page_whose_close_failed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5700: a failed close is tried again by the sweep until the provider confirms;
    each failed try is counted, and a success clears the mark."""
    connect_fake(client)
    order = payable_order(client)
    req = request_payment(client, order["id"])
    tries: list[str] = []

    async def down(credential: Any, provider_ref: str) -> None:
        tries.append(provider_ref)
        raise ProviderError("down")

    monkeypatch.setattr(FAKE, "cancel_request", down)
    client.post(f"/api/v1/orders/{order['id']}/payment-requests/{req['id']}/cancel")
    service = _state(client).components.get(PAYMENTS).service
    # Not due yet: the failed close after the commit started the wait again.
    assert asyncio.run(service.retry_closes()) == 0
    assert tries == [req["provider_ref"]]
    _due(client, req["id"])
    assert asyncio.run(service.retry_closes()) == 0
    assert tries == [req["provider_ref"]] * 2
    with _state(client).components.get(ORDERS).store.transaction() as conn:
        row = conn.execute(
            "SELECT close_attempts FROM payment_requests WHERE id = %s", (req["id"],)
        ).fetchone()
    assert row == {"close_attempts": 2}
    cancels = _cancels(monkeypatch)
    monkeypatch.setattr(FAKE, "cancel_request", type(FAKE).cancel_request.__get__(FAKE))
    _due(client, req["id"])
    assert asyncio.run(service.retry_closes()) == 1
    assert cancels == [req["provider_ref"]]
    [closed] = client.get(f"/api/v1/orders/{order['id']}/payment-requests").json()
    assert closed["close_pending_at"] is None
    assert asyncio.run(service.retry_closes()) == 0


def test_a_reference_another_request_holds_is_a_link_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect_fake(client)
    order = payable_order(client)

    async def same_ref(*args: Any, **kwargs: Any) -> HostedRequest:
        return HostedRequest.model_validate(
            {"provider_ref": "fake_same", "hosted_url": "https://payments.fake.invalid/pay/same"}
        )

    monkeypatch.setattr(FAKE, "create_request", same_ref)
    first = request_payment(client, order["id"], amount_minor=400)
    assert first["provider_ref"] == "fake_same"
    second = request_payment(client, order["id"], amount_minor=600)
    assert (second["status"], second["provider_ref"], second["hosted_url"]) == ("open", None, None)
    assert second["link_error"] == "the provider's reference is held by another payment request"


def test_the_sweep_gives_up_on_a_page_whose_provider_is_disconnected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5718 review: a provider no longer connected fails every try. The sweep stops
    after `CLOSE_GIVE_UP_ATTEMPTS`, and the request says to close the page there."""
    connect_fake(client)
    order = payable_order(client)
    req = request_payment(client, order["id"])
    with _state(client).components.get(ORDERS).store.transaction() as conn:
        conn.execute("DELETE FROM payment_credentials WHERE provider = 'fake'")
    client.post(f"/api/v1/orders/{order['id']}/payment-requests/{req['id']}/cancel")
    service = _state(client).components.get(PAYMENTS).service
    for _ in range(CLOSE_GIVE_UP_ATTEMPTS - 1):
        _due(client, req["id"])
        assert asyncio.run(service.retry_closes()) == 0
    [kept] = client.get(f"/api/v1/orders/{order['id']}/payment-requests").json()
    assert kept["close_abandoned_at"] is not None
    _due(client, req["id"])
    with _state(client).components.get(ORDERS).store.transaction() as conn:
        row = conn.execute(
            "SELECT close_attempts FROM payment_requests WHERE id = %s", (req["id"],)
        ).fetchone()
    asyncio.run(service.retry_closes())
    with _state(client).components.get(ORDERS).store.transaction() as conn:
        after = conn.execute(
            "SELECT close_attempts FROM payment_requests WHERE id = %s", (req["id"],)
        ).fetchone()
    # Given up: the sweep tries it no more.
    assert row == after == {"close_attempts": CLOSE_GIVE_UP_ATTEMPTS}


def test_a_reconnect_resumes_the_closes_the_sweep_gave_up(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5728 review: the shop reconnects the provider; the given-up page is tried again
    and closed, and the request no longer asks the shop to close it."""
    cancels = _cancels(monkeypatch)
    connect_fake(client)
    order = payable_order(client)
    req = request_payment(client, order["id"])
    with _state(client).components.get(ORDERS).store.transaction() as conn:
        conn.execute("DELETE FROM payment_credentials WHERE provider = 'fake'")
    client.post(f"/api/v1/orders/{order['id']}/payment-requests/{req['id']}/cancel")
    service = _state(client).components.get(PAYMENTS).service
    for _ in range(CLOSE_GIVE_UP_ATTEMPTS - 1):
        _due(client, req["id"])
        asyncio.run(service.retry_closes())
    [given_up] = client.get(f"/api/v1/orders/{order['id']}/payment-requests").json()
    assert given_up["close_abandoned_at"] is not None
    connect_fake(client)
    [resumed] = client.get(f"/api/v1/orders/{order['id']}/payment-requests").json()
    assert resumed["close_abandoned_at"] is None
    _due(client, req["id"])
    assert asyncio.run(service.retry_closes()) == 1
    assert cancels == [req["provider_ref"]]
    [closed] = client.get(f"/api/v1/orders/{order['id']}/payment-requests").json()
    assert closed["close_pending_at"] is None and closed["close_abandoned_at"] is None


def test_a_late_page_whose_reference_another_request_holds_is_not_closed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5718 review: the reference belongs to another request's live page, so closing
    it would close that page. Nothing is closed and nothing is marked."""
    cancels = _cancels(monkeypatch)
    connect_fake(client)
    order = payable_order(client)
    orders = _state(client).components.get(ORDERS)
    calls: list[str] = []

    async def same_ref(credential: Any, request: Any, **kwargs: Any) -> HostedRequest:
        calls.append(request.id)
        if len(calls) == 2:
            orders.lifecycle.cancel_payment_request(request.order_id, request.id, BY)
        return HostedRequest.model_validate(
            {"provider_ref": "fake_same", "hosted_url": "https://payments.fake.invalid/pay/same"}
        )

    monkeypatch.setattr(FAKE, "create_request", same_ref)
    first = request_payment(client, order["id"], amount_minor=400)
    second = request_payment(client, order["id"], amount_minor=600)
    assert (second["status"], second["provider_ref"]) == ("cancelled", None)
    assert second["close_pending_at"] is None
    assert cancels == []
    [kept] = [
        r
        for r in client.get(f"/api/v1/orders/{order['id']}/payment-requests").json()
        if r["id"] == first["id"]
    ]
    assert (kept["status"], kept["provider_ref"]) == ("open", "fake_same")


def test_a_page_made_after_the_cancel_is_retried_until_closed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5700: the request is cancelled while the provider makes its page. The page's
    reference is kept on the cancelled request with the mark, so a close that fails
    there is tried again by the sweep like any other."""
    connect_fake(client)
    order = payable_order(client)
    orders = _state(client).components.get(ORDERS)
    real = FAKE.create_request
    made: list[str] = []

    async def cancelled_meanwhile(credential: Any, request: Any, **kwargs: Any) -> HostedRequest:
        orders.lifecycle.cancel_payment_request(request.order_id, request.id, BY)
        hosted = await real(credential, request, **kwargs)
        made.append(hosted.provider_ref)
        return hosted

    async def down(credential: Any, provider_ref: str) -> None:
        raise ProviderError("down")

    monkeypatch.setattr(FAKE, "create_request", cancelled_meanwhile)
    monkeypatch.setattr(FAKE, "cancel_request", down)
    req = request_payment(client, order["id"])
    assert (req["status"], req["hosted_url"]) == ("cancelled", None)
    assert req["close_pending_at"] is not None
    cancels = _cancels(monkeypatch)
    monkeypatch.setattr(FAKE, "cancel_request", type(FAKE).cancel_request.__get__(FAKE))
    _due(client, req["id"])
    service = _state(client).components.get(PAYMENTS).service
    assert asyncio.run(service.retry_closes()) == 1
    assert cancels == made
    [closed] = client.get(f"/api/v1/orders/{order['id']}/payment-requests").json()
    assert (closed["status"], closed["close_pending_at"]) == ("cancelled", None)
