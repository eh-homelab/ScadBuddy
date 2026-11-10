"""The pay page's CRUD route (#4960, the interface on #5283): ``POST
/api/v1/internal/crud/storefront/pay/checkout``, for the ``storefront`` service credential
only, answers `venmo_checkout` for the token in the body, or a bare 404."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api import crud_storefront
from scadbuddy.auth.component import AUTH
from scadbuddy.auth.service_credentials import InProcessProvider, ServiceCaller
from scadbuddy.payments.venmo import VenmoCheckout

URL = "/api/v1/internal/crud/storefront/pay/checkout"
TOKEN = "t" * 43
FOUND = VenmoCheckout(
    amount_minor=700,
    currency="USD",
    order_number="ORD-2026-1",
    client_id="client",
    paypal_order_id="5O190127TN364715T",
    minor_exponent=2,
)


def token(client: TestClient, caller: ServiceCaller = "storefront") -> dict[str, str]:
    app: Any = client.app
    credentials = app.state.scadbuddy.components.get(AUTH).credentials
    minted = InProcessProvider(caller, lambda: credentials).token()
    assert minted is not None
    return {"authorization": f"Bearer {minted}"}


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`venmo_checkout` (its own tests are in tests/payments/test_venmo.py) answering
    `FOUND` for `TOKEN` only."""
    seen: list[str] = []

    def checkout(conn: Any, given: str) -> VenmoCheckout | None:
        seen.append(given)
        return FOUND if given == TOKEN else None

    monkeypatch.setattr(crud_storefront, "venmo_checkout", checkout)
    return seen


def test_a_token_reads_what_the_pay_page_needs(client: TestClient, reads: list[str]) -> None:
    answer = client.post(URL, json={"token": TOKEN}, headers=token(client))
    assert answer.status_code == 200, answer.text
    assert answer.headers["x-scadbuddy-crud"] == "1"
    assert answer.json() == FOUND.model_dump()
    assert reads == [TOKEN]


def test_a_token_that_reads_nothing_is_a_bare_404(client: TestClient, reads: list[str]) -> None:
    answer = client.post(URL, json={"token": "u" * 43}, headers=token(client))
    assert answer.status_code == 404
    assert answer.content == b""


def test_only_the_storefront_credential_reaches_it(client: TestClient, reads: list[str]) -> None:
    assert client.post(URL, json={"token": TOKEN}).status_code == 401
    assert (
        client.post(URL, json={"token": TOKEN}, headers=token(client, "agent")).status_code == 403
    )
    assert reads == []


def test_the_token_is_bounded_and_nothing_else_is_taken(
    client: TestClient, reads: list[str]
) -> None:
    long = client.post(URL, json={"token": "t" * 101}, headers=token(client))
    extra = client.post(URL, json={"token": TOKEN, "order_id": "x"}, headers=token(client))
    assert (long.status_code, extra.status_code) == (422, 422)
    assert reads == []
