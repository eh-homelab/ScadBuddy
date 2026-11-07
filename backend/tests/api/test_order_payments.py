"""`/api/v1/orders/{id}/payment-requests` and a manual payment that names one (#4957)."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.payments.fake import FAKE
from scadbuddy.payments.provider import ProviderError
from tests.api.test_customers import create as create_customer
from tests.api.test_orders import create_order

pytestmark = pytest.mark.requires_postgres

FAKE_CONNECT = {"settings": {"account": "a"}, "secrets": {"signing_secret": "s3cret"}}
PROBLEMS = "https://scadbuddy.dev/problems/orders/"


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
