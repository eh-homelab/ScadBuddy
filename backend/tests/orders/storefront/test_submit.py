"""A visitor submits its cart as an order (#5283, spec 2026-11-02 §5.5, plan Task 3):
``POST …/storefront/draft/submit`` makes a new customer, takes the number, sets
the shipping option, the tax and the deposit, stamps ``submitted_at``, makes the order
link and queues its mail, in one transaction."""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from scadbuddy.orders.lifecycle import Lifecycle, LifecycleError
from scadbuddy.orders.models import AddressInput, ContactInput, Customer
from scadbuddy.orders.money import DepositRule, OrderRules, ShippingOption
from scadbuddy.orders.storefront.submit import _refusal
from scadbuddy.orders.storefront.wire import Listing, ListingInput
from scadbuddy.payments import store as payments
from scadbuddy.storefront.wire import STOREFRONT_PROBLEMS
from tests.orders.storefront.test_drafts import (
    COMMIT,
    TOKEN_B,
    CrudCaller,
    _orders,
    _snapshot,
    _state,
    add_body,
)

pytestmark = pytest.mark.requires_postgres

POST = ShippingOption(id="post", label="Post", amount_minor=700, ships=True)
PICKUP = ShippingOption(id="pickup", label="Pick up", amount_minor=0, ships=False)
ADDRESS = {
    "recipient": "Ada Lovelace",
    "lines": ["1 Analytical Way"],
    "locality": "London",
    "postal_code": "N1 1AA",
    "country": "GB",
}


@pytest.fixture
def api(settings: Settings, tmp_path: Path) -> Iterator[TestClient]:
    """The app with a KEK: the order link's token is sealed under it."""
    kek = tmp_path / "kek"
    kek.write_text(base64.b64encode(os.urandom(32)).decode())
    with TestClient(create_app(settings.model_copy(update={"secret_key_file": kek}))) as client:
        yield client


@pytest.fixture
def crud(api: TestClient) -> CrudCaller:
    return CrudCaller(api)


@pytest.fixture
def owner_lifecycle(api: TestClient) -> Lifecycle:
    return _orders(api).lifecycle


@pytest.fixture
def listing(api: TestClient) -> Listing:
    """A published listing whose width a visitor picks (as `test_drafts.py`'s)."""
    store = _orders(api).listings
    made = store.create(
        ListingInput(
            slug="demo",
            commit=COMMIT,
            title="A demo box",
            params={"width": {"kind": "number", "min": 20, "max": 80, "step": 1}},  # type: ignore[dict-item]
        )
    )
    return store.mark_published(
        made.id, snapshot=_snapshot(), media=[], expected_updated_at=made.updated_at
    )


def rules(
    *, options: tuple[ShippingOption, ...] = (), deposit: tuple[DepositRule, ...] = ()
) -> Any:
    return lambda: OrderRules("USD", Decimal(0), False, deposit, options)


def draft_of(
    crud: CrudCaller, listing: Listing, *, token: str | None = None, key: str = "a1"
) -> Any:
    kwargs: dict[str, Any] = {} if token is None else {"token": token}
    r = crud.post("/draft/items", json=add_body(listing, {"width": 40}), key=key, **kwargs)
    assert r.status_code == 200, r.text
    return r.json()


def submit_body(draft: Any, **extra: Any) -> dict[str, Any]:
    return {"draft_id": draft["id"], "name": "Ada", "email": "ada@example.com", **extra}


def rows(api: TestClient, sql: str, *args: Any) -> list[dict[str, Any]]:
    with _state(api).projection.pool.connection() as conn:
        return list(conn.execute(sql, args).fetchall())


def customer_with(api: TestClient, email: str, name: str = "Ada") -> Customer:
    return _orders(api).store.insert_customer(
        display_name=name,
        company=None,
        notes="",
        tags=[],
        contacts=[ContactInput(kind="email", value=email)],
        addresses=[],
    )


def test_a_replayed_submit_makes_one_order_one_number_and_one_mail(
    api: TestClient, crud: CrudCaller, listing: Listing
) -> None:
    draft = draft_of(crud, listing)
    answers = [crud.post("/draft/submit", json=submit_body(draft), key="s1") for _ in range(3)]
    assert [a.status_code for a in answers] == [200, 200, 200], answers[0].text
    assert answers[0].json() == answers[1].json() == answers[2].json()
    order = _orders(api).store.order(draft["id"])
    assert order is not None and order.submitted_at is not None
    assert answers[0].json() == {
        "id": draft["id"],
        "number": order.number,
        "email": "ada@example.com",
    }
    assert order.number is not None and order.number.endswith("-1")
    assert rows(api, "SELECT last FROM order_numbers") == [{"last": 1}]
    mails = rows(api, "SELECT template, to_address, context FROM storefront_mail")
    [link] = rows(api, "SELECT id, sent_to, token_sealed FROM storefront_order_links")
    assert mails == [
        {
            "template": "order_link",
            "to_address": "ada@example.com",
            "context": {"order": order.number, "link_id": str(link["id"])},
        }
    ]
    assert link["sent_to"] == "ada@example.com" and link["token_sealed"] is not None
    # No token reaches the ledger's stored reply.
    assert "token" not in str(rows(api, "SELECT body FROM crud_requests"))


def test_a_new_customer_is_made_with_the_name_and_the_email(
    api: TestClient, crud: CrudCaller, listing: Listing
) -> None:
    draft = draft_of(crud, listing)
    r = crud.post("/draft/submit", json=submit_body(draft, name="  Ada L  "), key="s1")
    assert r.status_code == 200, r.text
    order = _orders(api).store.order(draft["id"])
    assert order is not None and order.customer_id is not None
    customer = _orders(api).store.customer(order.customer_id)
    assert customer is not None and customer.display_name == "Ada L"
    assert [(c.kind, c.value, c.primary) for c in customer.contacts] == [
        ("email", "ada@example.com", True)
    ]


def _unchanged(api: TestClient, customer: Customer) -> None:
    """``customer`` exactly as it was made: no order, no address, no other change."""
    now = _orders(api).store.customer(customer.id)
    assert now == customer
    assert rows(api, "SELECT id FROM orders WHERE customer_id = %s", customer.id) == []


@pytest.mark.parametrize("matching", [1, 2])
def test_a_customer_with_the_same_email_is_never_linked_or_changed(
    api: TestClient, crud: CrudCaller, owner_lifecycle: Lifecycle, listing: Listing, matching: int
) -> None:
    """Spec §5.2: a typed email proves nothing; only a verified account links a customer."""
    owner_lifecycle.rules = rules(options=(POST,))
    known = [
        _orders(api).store.insert_customer(
            display_name=f"Known {n}",
            company=None,
            notes="",
            tags=[],
            contacts=[ContactInput(kind="email", value="Ada@Example.com")],
            addresses=[AddressInput(recipient="Old", default_shipping=True)],
        )
        for n in range(matching)
    ]
    draft = draft_of(crud, listing)
    r = crud.post(
        "/draft/submit",
        json=submit_body(draft, shipping_option_id="post", address=ADDRESS),
        key="s1",
    )
    assert r.status_code == 200, r.text
    order = _orders(api).store.order(draft["id"])
    assert order is not None and order.customer_id not in [None, *(k.id for k in known)]
    for customer in known:
        _unchanged(api, customer)
    # The new customer holds the address, as its default shipping address.
    assert order.customer_id is not None
    made = _orders(api).store.customer(order.customer_id)
    assert made is not None
    assert [(a.recipient, a.default_shipping) for a in made.addresses] == [("Ada Lovelace", True)]
    assert order.shipping_address_id == made.addresses[0].id


def test_an_option_that_ships_needs_an_address(
    api: TestClient, crud: CrudCaller, owner_lifecycle: Lifecycle, listing: Listing
) -> None:
    owner_lifecycle.rules = rules(options=(POST, PICKUP))
    draft = draft_of(crud, listing)
    r = crud.post("/draft/submit", json=submit_body(draft, shipping_option_id="post"), key="s1")
    assert r.status_code == 409, r.text
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}shipping_address_required"
    # Nothing of it was kept: no customer, no number, no mail.
    order = _orders(api).store.order(draft["id"])
    assert order is not None and order.unsubmitted and order.customer_id is None
    assert rows(api, "SELECT * FROM storefront_mail") == []
    assert rows(api, "SELECT * FROM customers") == []

    r = crud.post(
        "/draft/submit",
        json=submit_body(draft, shipping_option_id="post", address=ADDRESS),
        key="s2",
    )
    assert r.status_code == 200, r.text
    order = _orders(api).store.order(draft["id"])
    assert order is not None and order.shipping_address_id is not None
    found = _orders(api).store.address(order.shipping_address_id)
    assert found is not None and found[0] == order.customer_id
    assert (found[1].recipient, found[1].country) == ("Ada Lovelace", "GB")
    charges = _orders(api).store.charges(order.id)
    assert [(c.kind, c.amount_minor, c.shipping_option_id) for c in charges] == [
        ("shipping", 700, "post")
    ]


def test_a_pickup_needs_no_address(
    api: TestClient, crud: CrudCaller, owner_lifecycle: Lifecycle, listing: Listing
) -> None:
    owner_lifecycle.rules = rules(options=(POST, PICKUP))
    draft = draft_of(crud, listing)
    r = crud.post("/draft/submit", json=submit_body(draft, shipping_option_id="pickup"), key="s1")
    assert r.status_code == 200, r.text
    order = _orders(api).store.order(draft["id"])
    assert order is not None and order.shipping_address_id is None


def test_a_shop_with_options_needs_one_chosen(
    crud: CrudCaller, owner_lifecycle: Lifecycle, listing: Listing
) -> None:
    owner_lifecycle.rules = rules(options=(POST, PICKUP))
    draft = draft_of(crud, listing)
    r = crud.post("/draft/submit", json=submit_body(draft), key="s1")
    assert r.status_code == 422, r.text
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}shipping_option_id"


def test_the_draft_view_shows_the_shipping_options(
    crud: CrudCaller, owner_lifecycle: Lifecycle, listing: Listing
) -> None:
    owner_lifecycle.rules = rules(options=(POST, PICKUP))
    draft = draft_of(crud, listing)
    assert draft["shipping_options"] == [
        {"id": "post", "label": "Post", "amount_minor": 700, "ships": True},
        {"id": "pickup", "label": "Pick up", "amount_minor": 0, "ships": False},
    ]


def test_the_deposit_policy_applies(
    api: TestClient, crud: CrudCaller, owner_lifecycle: Lifecycle, listing: Listing
) -> None:
    owner_lifecycle.rules = rules(deposit=(DepositRule(criterion="first_order", amount_minor=500),))
    draft = draft_of(crud, listing)
    assert crud.post("/draft/submit", json=submit_body(draft), key="s1").status_code == 200
    order = _orders(api).store.order(draft["id"])
    assert order is not None and order.deposit_minor == 500
    with _state(api).projection.pool.connection() as conn:
        requests = payments.requests_for_order(conn, order.id)
    assert [(r.kind, r.status, r.amount_minor) for r in requests] == [("deposit", "open", 500)]
    # The order link's mail leads to the page that shows the request: no second mail.
    assert [r["template"] for r in rows(api, "SELECT template FROM storefront_mail")] == [
        "order_link"
    ]


def test_a_first_order_deposit_applies_even_when_the_email_has_ordered_before(
    api: TestClient, crud: CrudCaller, owner_lifecycle: Lifecycle, listing: Listing
) -> None:
    """An anonymous submit makes a new customer, so ``first_order`` is true for it, even
    when a customer with that email has an accepted order: the shop merges them by hand."""
    owner_lifecycle.rules = rules(deposit=(DepositRule(criterion="first_order", amount_minor=500),))
    known = customer_with(api, "ada@example.com")
    with _state(api).projection.pool.connection() as conn:
        conn.execute(
            "INSERT INTO orders (number, customer_id, currency, created_by, accepted_at)"
            " VALUES ('ORD-1999-1', %s, 'USD', '{\"kind\": \"browser\"}', now())",
            (known.id,),
        )
    draft = draft_of(crud, listing)
    assert crud.post("/draft/submit", json=submit_body(draft), key="s1").status_code == 200
    order = _orders(api).store.order(draft["id"])
    assert order is not None and order.customer_id != known.id
    assert order.deposit_minor == 500


def test_another_visitors_submit_sees_no_draft(
    api: TestClient, crud: CrudCaller, listing: Listing
) -> None:
    draft = draft_of(crud, listing)
    r = crud.post("/draft/submit", token=TOKEN_B, json=submit_body(draft), key="s1")
    assert r.status_code == 404, r.text
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}draft_not_found"
    # Even once B has a draft of its own.
    draft_of(crud, listing, token=TOKEN_B, key="b1")
    r = crud.post("/draft/submit", token=TOKEN_B, json=submit_body(draft), key="s2")
    assert r.status_code == 404
    order = _orders(api).store.order(draft["id"])
    assert order is not None and order.unsubmitted


def test_an_empty_cart_is_409(crud: CrudCaller, listing: Listing) -> None:
    draft = draft_of(crud, listing)
    crud.delete(f"/draft/items/{draft['items'][0]['id']}", key="d1")
    r = crud.post("/draft/submit", json=submit_body(draft), key="s1")
    assert r.status_code == 409, r.text
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}draft_empty"


def test_a_second_submit_with_a_new_key_is_409(
    api: TestClient, crud: CrudCaller, listing: Listing
) -> None:
    draft = draft_of(crud, listing)
    assert crud.post("/draft/submit", json=submit_body(draft), key="s1").status_code == 200
    r = crud.post("/draft/submit", json=submit_body(draft), key="s2")
    assert r.status_code == 409, r.text
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}order_submitted"
    assert len(rows(api, "SELECT id FROM storefront_mail")) == 1
    # The submitted order is no longer the visitor's draft.
    assert crud.get("/draft").json() is None


@pytest.mark.parametrize(
    "email", ["ada", "Ada <ada@example.com>", "a@example.com, b@example.com", "a@b.c\r\nBcc: x@y.z"]
)
def test_an_email_that_is_not_one_address_is_422(
    api: TestClient, crud: CrudCaller, listing: Listing, email: str
) -> None:
    draft = draft_of(crud, listing)
    r = crud.post("/draft/submit", json=submit_body(draft, email=email), key="s1")
    assert r.status_code == 422, r.text
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}email_invalid"
    order = _orders(api).store.order(draft["id"])
    assert order is not None and order.unsubmitted


def test_a_body_that_names_an_account_is_422(
    api: TestClient, crud: CrudCaller, listing: Listing
) -> None:
    """Spec §6.4: the account comes from the CRUD side's session, never from a body."""
    draft = draft_of(crud, listing)
    r = crud.post("/draft/submit", json=submit_body(draft, account_id="acct-1"), key="s1")
    assert r.status_code == 422, r.text
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}submit_body_invalid"
    order = _orders(api).store.order(draft["id"])
    assert order is not None and order.unsubmitted


@pytest.mark.parametrize(
    "extra",
    [
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 201},
        {"email": "a" * 250 + "@example.com"},
        {"address": {"recipient": ""}},
    ],
)
def test_a_body_outside_its_bounds_is_422(
    crud: CrudCaller, listing: Listing, extra: dict[str, Any]
) -> None:
    draft = draft_of(crud, listing)
    r = crud.post("/draft/submit", json=submit_body(draft, **extra), key="s1")
    assert r.status_code == 422, r.text
    assert r.json()["type"].startswith(STOREFRONT_PROBLEMS)


def test_a_submit_needs_an_idempotency_key(crud: CrudCaller, listing: Listing) -> None:
    draft = draft_of(crud, listing)
    assert crud.post("/draft/submit", json=submit_body(draft)).status_code == 428


def test_the_submit_is_the_customers_on_the_audit_line(
    api: TestClient, crud: CrudCaller, listing: Listing
) -> None:
    draft = draft_of(crud, listing)
    crud.post("/draft/submit", json=submit_body(draft), key="s1")
    order = _orders(api).store.order(draft["id"])
    assert order is not None
    [submitted] = [e for e in _orders(api).store.events(order.id) if e.kind == "order.submitted"]
    assert submitted.by.kind == "customer"
    assert submitted.by.principal == f"customer:{order.customer_id}"


@pytest.mark.parametrize("slug", ["shipping_option_id", "amount_minor"])
def test_a_422_refusal_keeps_its_own_slug(slug: str) -> None:
    error = _refusal(LifecycleError(slug, f"{slug}: no", unprocessable=True))
    assert (error.status, error.type) == (422, f"{STOREFRONT_PROBLEMS}{slug}")
