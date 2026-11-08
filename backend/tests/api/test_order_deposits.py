"""Deposits, the balance and the shipping option through the API (#5281, storefront spec
§8-§9): `PUT /orders/{id}/deposit`, `POST …/deposit/policy`, the two waivers,
`PUT …/shipping-option`, the totals that show what is due, and the provider pages that a
cancelled request leaves behind, closed after the commit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.storefront.visitors import VisitorStore, visitor_actor
from scadbuddy.payments import store as payments_store
from scadbuddy.payments.fake import FAKE
from scadbuddy.payments.models import PaymentRequestStatus
from tests.api.test_customers import create as create_customer
from tests.api.test_order_payments import connect_fake, request_payment, with_payments
from tests.api.test_orders import create_order
from tests.api.test_orders_lifecycle import add_item, fulfil, problem
from tests.api.test_settings_runtime import _state
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

CUSTOM = {"kind": "custom", "title": "Stand", "quantity": 1, "unit_price_minor": 1000}


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    return with_payments(settings, tmp_path)


def quoted(client: TestClient) -> tuple[dict[str, Any], dict[str, Any]]:
    """A quoted order of one custom item at 1000."""
    order = create_order(client, create_customer(client)["id"])
    return order, add_item(client, order["id"], **CUSTOM)


def detail(client: TestClient, order_id: str) -> dict[str, Any]:
    found: dict[str, Any] = client.get(f"/api/v1/orders/{order_id}").json()
    return found


def put_deposit(client: TestClient, order_id: str, amount: int | None) -> dict[str, Any]:
    answer = client.put(f"/api/v1/orders/{order_id}/deposit", json={"amount_minor": amount})
    assert answer.status_code == 200, answer.text
    order: dict[str, Any] = answer.json()
    return order


def pay(client: TestClient, order_id: str, amount: int) -> None:
    paid = client.post(
        f"/api/v1/orders/{order_id}/payments", json={"amount_minor": amount, "method": "cash"}
    )
    assert paid.status_code == 201, paid.text


def set_status(client: TestClient, request_id: str, status: PaymentRequestStatus) -> None:
    """A request moved as a provider's event moves it."""
    orders = _state(client).components.get(ORDERS)
    with orders.store.transaction() as conn:
        payments_store.set_request_status(conn, request_id, status)


def cancels(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr(FAKE, "cancelled", [])
    return FAKE.cancelled


def statuses(client: TestClient, order_id: str) -> dict[str, str]:
    return {r["id"]: r["status"] for r in detail(client, order_id)["payment_requests"]}


# -- deposits -----------------------------------------------------------------------------


@pytest.mark.requires_git
def test_deposit_unpaid_is_a_409_problem(client: TestClient) -> None:
    from tests.api.test_order_revisions import a_reference, a_render, a_request

    order, item = a_request(client)
    base = f"/api/v1/orders/{order['id']}/items/{item['id']}/revisions"
    rev = client.post(base, json={"reference": a_reference(client)}).json()
    a_render(client, base, rev["id"])
    put_deposit(client, order["id"], 2000)
    problem(client.post(f"{base}/{rev['id']}/send"), "deposit_unpaid")
    assert detail(client, order["id"])["totals"]["deposit_due_minor"] == 2000


def test_a_deposit_opens_a_request_and_shows_what_is_due(client: TestClient) -> None:
    order, _ = quoted(client)
    updated = put_deposit(client, order["id"], 300)
    assert updated["deposit_minor"] == 300
    found = detail(client, order["id"])
    assert found["totals"]["deposit_due_minor"] == 300
    assert found["totals"]["shipping_held"] is False
    [req] = found["payment_requests"]
    assert (req["kind"], req["amount_minor"], req["status"]) == ("deposit", 300, "open")
    pay(client, order["id"], 100)
    assert detail(client, order["id"])["totals"]["deposit_due_minor"] == 200
    cleared = put_deposit(client, order["id"], None)
    assert cleared["deposit_minor"] is None
    assert detail(client, order["id"])["totals"]["deposit_due_minor"] == 0


def test_a_deposit_below_one_is_422(client: TestClient) -> None:
    order, _ = quoted(client)
    refused = client.put(f"/api/v1/orders/{order['id']}/deposit", json={"amount_minor": 0})
    assert refused.status_code == 422
    assert refused.json()["errors"][0]["loc"] == ["body", "amount_minor"]


def test_the_policy_sets_the_deposit(client: TestClient) -> None:
    saved = client.put(
        "/api/v1/settings",
        json={"orders_deposit_policy": [{"criterion": "first_order", "amount_minor": 400}]},
    )
    assert saved.status_code == 200, saved.text
    order, _ = quoted(client)
    applied = client.post(f"/api/v1/orders/{order['id']}/deposit/policy")
    assert applied.status_code == 200, applied.text
    assert applied.json()["deposit_minor"] == 400


def test_a_waiver_needs_a_reason(client: TestClient) -> None:
    order, _ = quoted(client)
    put_deposit(client, order["id"], 300)
    for path in ("deposit/waive", "balance/waive"):
        for reason in ("", "x" * 501):
            refused = client.post(f"/api/v1/orders/{order['id']}/{path}", json={"reason": reason})
            assert refused.status_code == 422, (path, refused.text)
            assert refused.json()["errors"][0]["loc"] == ["body", "reason"]
        blank = client.post(f"/api/v1/orders/{order['id']}/{path}", json={"reason": "   "})
        assert blank.status_code == 422, (path, blank.text)
        assert blank.json()["errors"][0]["loc"] == ["body", "reason"]


def test_proceeding_without_the_deposit_lifts_it(client: TestClient) -> None:
    order, _ = quoted(client)
    put_deposit(client, order["id"], 300)
    waived = client.post(f"/api/v1/orders/{order['id']}/deposit/waive", json={"reason": "trusted"})
    assert waived.status_code == 200, waived.text
    assert waived.json()["deposit_waived_reason"] == "trusted"
    assert detail(client, order["id"])["totals"]["deposit_due_minor"] == 0


# -- the balance --------------------------------------------------------------------------


def test_shipping_held_until_the_balance_is_paid(client: TestClient) -> None:
    order, item = quoted(client)
    put_deposit(client, order["id"], 300)
    pay(client, order["id"], 300)
    assert client.post(f"/api/v1/orders/{order['id']}/accept").status_code == 200
    found = detail(client, order["id"])
    assert found["totals"]["shipping_held"] is True
    assert found["totals"]["deposit_due_minor"] == 0
    fulfil(client, order["id"], item["id"], 1)
    ship = {"items": [{"order_item_id": item["id"], "quantity": 1}]}
    problem(client.post(f"/api/v1/orders/{order['id']}/shipments", json=ship), "balance_unpaid")
    waived = client.post(
        f"/api/v1/orders/{order['id']}/balance/waive", json={"reason": "pays on pickup"}
    )
    assert waived.status_code == 200, waived.text
    assert waived.json()["balance_waived_reason"] == "pays on pickup"
    assert detail(client, order["id"])["totals"]["shipping_held"] is False
    shipped = client.post(f"/api/v1/orders/{order['id']}/shipments", json=ship)
    assert shipped.status_code == 201, shipped.text


def test_the_balance_is_not_waived_before_acceptance(client: TestClient) -> None:
    order, _ = quoted(client)
    refused = client.post(f"/api/v1/orders/{order['id']}/balance/waive", json={"reason": "why"})
    problem(refused, "order_not_accepted")


def test_accept_links_the_balance_request_on_the_default_provider(client: TestClient) -> None:
    connect_fake(client)
    order, _ = quoted(client)
    put_deposit(client, order["id"], 300)
    assert detail(client, order["id"])["payment_requests"][-1]["hosted_url"]
    pay(client, order["id"], 300)
    assert client.post(f"/api/v1/orders/{order['id']}/accept").status_code == 200
    last = detail(client, order["id"])["payment_requests"][-1]
    assert (last["kind"], last["amount_minor"], last["provider"]) == ("balance", 700, "fake")
    assert last["hosted_url"]


# -- provider pages close after the commit ------------------------------------------------


def test_a_new_deposit_closes_the_page_it_cancels(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed = cancels(monkeypatch)
    connect_fake(client)
    order, _ = quoted(client)
    put_deposit(client, order["id"], 300)
    [first] = detail(client, order["id"])["payment_requests"]
    kept = request_payment(client, order["id"], amount_minor=200)
    put_deposit(client, order["id"], 400)
    assert closed == [first["provider_ref"]]
    found = detail(client, order["id"])["payment_requests"]
    assert statuses(client, order["id"])[first["id"]] == "cancelled"
    assert statuses(client, order["id"])[kept["id"]] == "open"
    assert (found[-1]["kind"], found[-1]["amount_minor"]) == ("deposit", 400)
    assert found[-1]["hosted_url"]
    # The same deposit again keeps its request: no page is closed.
    put_deposit(client, order["id"], 400)
    assert closed == [first["provider_ref"]]


def test_the_policy_closes_the_page_it_cancels(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed = cancels(monkeypatch)
    connect_fake(client)
    client.put(
        "/api/v1/settings",
        json={"orders_deposit_policy": [{"criterion": "first_order", "amount_minor": 500}]},
    )
    order, _ = quoted(client)
    put_deposit(client, order["id"], 300)
    [first] = detail(client, order["id"])["payment_requests"]
    kept = request_payment(client, order["id"], amount_minor=200)
    applied = client.post(f"/api/v1/orders/{order['id']}/deposit/policy")
    assert applied.status_code == 200, applied.text
    assert closed == [first["provider_ref"]]
    assert statuses(client, order["id"])[kept["id"]] == "open"
    last = detail(client, order["id"])["payment_requests"][-1]
    assert (last["kind"], last["amount_minor"]) == ("deposit", 500)
    assert last["hosted_url"]
    # The same policy deposit again keeps its request.
    assert client.post(f"/api/v1/orders/{order['id']}/deposit/policy").status_code == 200
    assert closed == [first["provider_ref"]]


def test_accept_closes_each_page_it_cancels(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed = cancels(monkeypatch)
    connect_fake(client)
    order, _ = quoted(client)
    put_deposit(client, order["id"], 300)
    [deposit] = detail(client, order["id"])["payment_requests"]
    other = request_payment(client, order["id"], amount_minor=200)
    by_hand = request_payment(client, order["id"], provider=None, amount_minor=100)
    assert client.post(f"/api/v1/orders/{order['id']}/accept").status_code == 200
    assert sorted(closed) == sorted([deposit["provider_ref"], other["provider_ref"]])
    found = statuses(client, order["id"])
    assert {found[r["id"]] for r in (deposit, other, by_hand)} == {"cancelled"}
    last = detail(client, order["id"])["payment_requests"][-1]
    assert (last["kind"], last["amount_minor"], last["status"]) == ("balance", 1000, "open")
    assert last["hosted_url"]


def test_accept_keeps_a_balance_request_and_its_page(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed = cancels(monkeypatch)
    connect_fake(client)
    order, _ = quoted(client)
    kept = request_payment(client, order["id"])
    assert client.post(f"/api/v1/orders/{order['id']}/accept").status_code == 200
    assert closed == []
    assert statuses(client, order["id"]) == {kept["id"]: "open"}


# -- refusals -----------------------------------------------------------------------------


@pytest.fixture
def cart(client: TestClient, pg_pool: PgPool) -> str:
    orders = _state(client).components.get(ORDERS)
    visitor = VisitorStore(pg_pool).ensure("v" * 43)
    return orders.lifecycle.ensure_draft(visitor.id, visitor_actor(visitor), currency="USD").id


CART_CALLS: dict[str, tuple[str, str, dict[str, Any] | None]] = {
    "set_deposit": ("PUT", "deposit", {"amount_minor": 2000}),
    "apply_policy": ("POST", "deposit/policy", None),
    "waive_deposit": ("POST", "deposit/waive", {"reason": "why"}),
    "waive_balance": ("POST", "balance/waive", {"reason": "why"}),
    "accept": ("POST", "accept", None),
}


@pytest.mark.parametrize("call", list(CART_CALLS))
def test_a_cart_is_not_submitted(client: TestClient, cart: str, call: str) -> None:
    method, path, body = CART_CALLS[call]
    answer = client.request(method, f"/api/v1/orders/{cart}/{path}", json=body)
    problem(answer, "order_not_submitted")


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", "shipping-option", {"option_id": "pickup"}),
        ("PATCH", "", {"charges": [{"kind": "fee", "label": "Rush", "amount_minor": 100}]}),
    ],
)
def test_the_back_office_sets_no_option_or_charges_on_a_cart(
    client: TestClient, cart: str, method: str, path: str, body: dict[str, Any]
) -> None:
    client.put("/api/v1/settings", json={"storefront_shipping_options": OPTIONS})
    url = f"/api/v1/orders/{cart}/{path}".rstrip("/")
    problem(client.request(method, url, json=body), "order_not_submitted")
    assert detail(client, cart)["charges"] == []


PENDING_CALLS: dict[str, tuple[str, str, dict[str, Any] | None]] = {
    "set_deposit": ("PUT", "deposit", {"amount_minor": 500}),
    "apply_policy": ("POST", "deposit/policy", None),
    "accept": ("POST", "accept", None),
}


@pytest.mark.parametrize("call", list(PENDING_CALLS))
def test_a_pending_request_is_a_payment_in_flight(client: TestClient, call: str) -> None:
    order, _ = quoted(client)
    put_deposit(client, order["id"], 300)
    [req] = detail(client, order["id"])["payment_requests"]
    set_status(client, req["id"], "pending")
    method, path, body = PENDING_CALLS[call]
    answer = client.request(method, f"/api/v1/orders/{order['id']}/{path}", json=body)
    problem(answer, "payment_in_flight")
    assert statuses(client, order["id"]) == {req["id"]: "pending"}


def test_an_unknown_order_is_404(client: TestClient) -> None:
    for method, path, body in CART_CALLS.values():
        answer = client.request(method, f"/api/v1/orders/nope/{path}", json=body)
        assert answer.status_code == 404, (path, answer.text)


# -- the shipping option ------------------------------------------------------------------

OPTIONS = [
    {"id": "pickup", "label": "Pick up", "amount_minor": 0, "ships": False},
    {"id": "post", "label": "Post", "amount_minor": 500, "ships": True},
]


def test_a_pickup_option_needs_no_address(client: TestClient) -> None:
    saved = client.put("/api/v1/settings", json={"storefront_shipping_options": OPTIONS})
    assert saved.status_code == 200, saved.text
    order, _ = quoted(client)
    unset = client.patch(f"/api/v1/orders/{order['id']}", json={"shipping_address_id": None})
    assert unset.status_code == 200, unset.text
    url = f"/api/v1/orders/{order['id']}/shipping-option"
    problem(client.put(url, json={"option_id": "post"}), "shipping_address_required")
    picked = client.put(url, json={"option_id": "pickup"})
    assert picked.status_code == 200, picked.text
    charges = detail(client, order["id"])["charges"]
    assert [(c["kind"], c["label"], c["shipping_option_id"]) for c in charges] == [
        ("shipping", "Pick up", "pickup")
    ]
    unknown = client.put(url, json={"option_id": "drone"})
    assert unknown.status_code == 422
    assert unknown.json()["errors"][0]["loc"] == ["body", "option_id"]
    removed = client.put(url, json={"option_id": None})
    assert removed.status_code == 200, removed.text
    assert detail(client, order["id"])["charges"] == []
