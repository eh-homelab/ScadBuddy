# ruff: noqa: F811  (pytest fixtures imported by name, then taken as arguments)
"""A storefront checkout charges the cached slice price (#6153, spec 2026-11-13 §5.4).

The slice price is firm (owner, 2026-11-14): a cart line whose variant has a ``done`` slice
price is charged it with no check by the shop. The server reads the price by the
variant's key when the visitor adds the line and again at checkout, never from the
browser. A cart may mix firm lines and lines the shop prices by hand: then the order
waits for the shop's check, and the customer pays once, after it. The render, the slicer
and the profile are `test_storefront_renders.py`'s fakes."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from scadbuddy.orders.component import ORDERS, OrdersComponent
from scadbuddy.orders.lifecycle import ManualQuoteInput
from scadbuddy.orders.models import Actor
from scadbuddy.orders.storefront.customers import customer_actor
from scadbuddy.storefront.wire import STOREFRONT_PROBLEMS
from tests.api.test_storefront_listings import (  # noqa: F401  (fixtures)
    _state,
    client,
    kek_file,
    template,
)
from tests.api.test_storefront_public import TOKEN_A
from tests.api.test_storefront_renders import (  # noqa: F401  (fixtures)
    CrudCaller,
    FakeSlicer,
    crud,
    dense_red,
    listing,
    priced,
    render_until_done,
    sliced,
)

pytestmark = pytest.mark.requires_postgres

SHOP = Actor(kind="browser")


def add(crud: CrudCaller, listing: dict[str, Any], width: int, **extra: Any) -> Any:
    body = {
        "listing_id": listing["id"],
        "listing_updated_at": listing["updated_at"],
        "values": {"width": width},
        "colours": {},
        **extra,
    }
    return crud.post("/draft/items", json=body, key=uuid.uuid4().hex)


def firm_line(crud: CrudCaller, listing: dict[str, Any], width: int) -> tuple[dict[str, Any], int]:
    """A line the visitor rendered and saw the slice price of, added: the draft and
    that price."""
    job = render_until_done(crud, TOKEN_A, listing, {"width": width})
    est = priced(crud, job["id"])
    assert est["source"] == "slice", est
    r = add(crud, listing, width)
    assert r.status_code == 200, r.text
    price: int = est["price_minor"]
    return r.json(), price


def submit(crud: CrudCaller, draft: dict[str, Any], **extra: Any) -> Any:
    body = {"draft_id": draft["id"], "name": "Ada", "email": "ada@example.com", **extra}
    return crud.post("/draft/submit", json=body, key=uuid.uuid4().hex)


def rows(crud: CrudCaller, sql: str, *args: Any) -> list[dict[str, Any]]:
    with _state(crud.api).projection.pool.connection() as conn:
        return list(conn.execute(sql, args).fetchall())


def quotes(crud: CrudCaller, order_id: str) -> list[tuple[str, str, int, bool]]:
    return [
        (r["order_item_id"], r["source"], r["total_minor"], r["accepted"])
        for r in rows(
            crud,
            "SELECT q.order_item_id, q.source, q.total_minor, q.accepted"
            " FROM order_item_quotes q JOIN order_items i ON i.id = q.order_item_id"
            " WHERE i.order_id = %s ORDER BY i.position",
            order_id,
        )
    ]


def requests(crud: CrudCaller, order_id: str) -> list[tuple[str, int, str]]:
    return [
        (r["kind"], r["amount_minor"], r["status"])
        for r in rows(
            crud,
            "SELECT kind, amount_minor, status FROM payment_requests WHERE order_id = %s"
            " ORDER BY created_at",
            order_id,
        )
    ]


def status_of(crud: CrudCaller, order_id: str) -> str:
    [row] = rows(crud, "SELECT status FROM orders WHERE id = %s", order_id)
    return str(row["status"])


def set_setup_fee(crud: CrudCaller, fee: str) -> None:
    r = crud.api.put("/api/v1/settings", json={"pricing_setup_fee": fee})
    assert r.status_code == 200, r.text


def test_a_firm_only_cart_pays_the_firm_total_with_no_shop_check(
    crud: CrudCaller,
    listing: dict[str, Any],
    sliced: type[FakeSlicer],
) -> None:
    """The customer pays the firm total at once: no deposit is asked, so no deposit
    request is left, open or cancelled, though the policy would ask one."""
    policy = [{"criterion": "first_order", "amount_minor": 400}]
    r = crud.api.put("/api/v1/settings", json={"orders_deposit_policy": policy})
    assert r.status_code == 200, r.text
    draft, price = firm_line(crud, listing, 40)
    [item] = draft["items"]
    assert (item["price"], item["unit_price_minor"], item["total_minor"]) == ("firm", price, price)
    assert draft["pay_now"] is True
    # The cart reads the price the server keeps, never a key or a cost.
    assert "price_key" not in str(draft) and "unit_cost" not in str(draft)
    r = submit(crud, draft)
    assert r.status_code == 200, r.text
    assert r.json()["pay_now"] is True
    order_id = r.json()["id"]
    assert status_of(crud, order_id) == "accepted"
    assert quotes(crud, order_id) == [(item["id"], "storefront", price, True)]
    assert requests(crud, order_id) == [("balance", price, "open")]
    [order] = rows(crud, "SELECT deposit_minor FROM orders WHERE id = %s", order_id)
    assert order["deposit_minor"] is None


def test_a_mixed_cart_goes_to_the_shops_check_and_charges_nothing_yet(
    crud: CrudCaller,
    listing: dict[str, Any],
    sliced: type[FakeSlicer],
) -> None:
    """A line added with no render has no firm price: the shop prices it. The firm line
    keeps its price, nothing is asked of the customer, and once the shop prices the other
    line and the customer accepts both, one balance request asks for the whole total."""
    _, price = firm_line(crud, listing, 41)
    r = add(crud, listing, 55)
    assert r.status_code == 200, r.text
    draft = r.json()
    firm, by_hand = draft["items"]
    assert (firm["price"], by_hand["price"]) == ("firm", "by_hand")
    assert by_hand["unit_price_minor"] is None and by_hand["total_minor"] is None
    assert draft["pay_now"] is False
    r = submit(crud, draft)
    assert r.status_code == 200, r.text
    assert r.json()["pay_now"] is False
    order_id = r.json()["id"]
    assert status_of(crud, order_id) == "draft"
    assert quotes(crud, order_id) == [(firm["id"], "storefront", price, False)]
    assert requests(crud, order_id) == []

    orders: OrdersComponent = _state(crud.api).components.get(ORDERS)
    lifecycle = orders.lifecycle
    hand = lifecycle.record_manual_quote(
        order_id, by_hand["id"], ManualQuoteInput(unit_price_minor=1234), SHOP
    )
    order = orders.store.order(order_id)
    assert order is not None and order.customer_id is not None
    customer = customer_actor(order.customer_id, "link:test")
    firm_quote = next(q for q in orders.store.quotes(firm["id"]) if q.source == "storefront")
    lifecycle.accept_quote(order_id, firm["id"], firm_quote.id, customer)
    assert requests(crud, order_id) == []
    lifecycle.accept_quote(order_id, by_hand["id"], hand.id, customer)
    assert status_of(crud, order_id) == "accepted"
    assert requests(crud, order_id) == [("balance", price + 1234, "open")]


def test_a_price_the_browser_sends_is_never_taken(
    crud: CrudCaller,
    listing: dict[str, Any],
    sliced: type[FakeSlicer],
) -> None:
    r = add(crud, listing, 42, unit_price_minor=1)
    assert r.status_code == 422, r.text
    assert r.json()["type"] == STOREFRONT_PROBLEMS + "draft_body_invalid"
    draft, price = firm_line(crud, listing, 42)
    assert draft["items"][0]["unit_price_minor"] == price
    r = submit(crud, draft, total_minor=1)
    assert r.status_code == 422, r.text
    assert r.json()["type"] == STOREFRONT_PROBLEMS + "submit_body_invalid"
    r = submit(crud, draft)
    assert r.status_code == 200, r.text
    assert requests(crud, r.json()["id"]) == [("balance", price, "open")]


def test_a_price_that_changed_is_told_before_the_customer_pays(
    crud: CrudCaller,
    listing: dict[str, Any],
    sliced: type[FakeSlicer],
) -> None:
    """The book changed after the add: the key moved. The new key's slice is done, so the
    checkout takes its price, but tells the customer first."""
    draft, before = firm_line(crud, listing, 43)
    set_setup_fee(crud, "4.50")
    job = render_until_done(crud, TOKEN_A, listing, {"width": 43})
    after = priced(crud, job["id"])["price_minor"]
    assert after != before
    r = submit(crud, draft)
    assert r.status_code == 409, r.text
    assert r.json()["type"] == STOREFRONT_PROBLEMS + "prices_changed"
    assert r.json()["items"] == [{"id": draft["items"][0]["id"], "price": "firm"}]
    shown = crud.get("/draft").json()
    assert shown["items"][0]["unit_price_minor"] == after
    r = submit(crud, shown)
    assert r.status_code == 200, r.text
    assert r.json()["pay_now"] is True
    assert requests(crud, r.json()["id"]) == [("balance", after, "open")]


def test_a_stale_key_with_no_new_price_goes_to_the_shops_check(
    crud: CrudCaller,
    listing: dict[str, Any],
    sliced: type[FakeSlicer],
) -> None:
    """The book changed after the add and the new key has no ``done`` row: the line has
    no firm price now. The customer saw one, so the first submit tells them; the next goes
    to the shop's check."""
    draft, _ = firm_line(crud, listing, 44)
    set_setup_fee(crud, "3.25")
    r = submit(crud, draft)
    assert r.status_code == 409, r.text
    assert r.json()["type"] == STOREFRONT_PROBLEMS + "prices_changed"
    assert r.json()["items"] == [{"id": draft["items"][0]["id"], "price": "by_hand"}]
    shown = crud.get("/draft").json()
    assert shown["items"][0]["price"] == "by_hand" and shown["pay_now"] is False
    r = submit(crud, shown)
    assert r.status_code == 200, r.text
    assert r.json()["pay_now"] is False
    order_id = r.json()["id"]
    assert status_of(crud, order_id) == "draft"
    assert quotes(crud, order_id) == []
    assert requests(crud, order_id) == []


def costs_of(crud: CrudCaller, order_id: str) -> dict[str, Any]:
    r = crud.api.get(f"/api/v1/orders/{order_id}/costs")
    assert r.status_code == 200, r.text
    [item] = r.json()["items"]
    return {k: item[k] for k in ("quoted_seconds", "quoted_grams", "quoted_filament_minor")}


def test_job_cost_reads_the_slice_facts_of_a_cached_price(
    crud: CrudCaller,
    listing: dict[str, Any],
    sliced: type[FakeSlicer],
) -> None:
    """scadsy's job cost (#6153 PR B): a `storefront` quote carries the slice's grams,
    seconds and filament cost, as the slice row has them."""
    draft, _ = firm_line(crud, listing, 46)
    [row] = rows(
        crud,
        "SELECT print_seconds, grams, lines FROM storefront_slice_prices WHERE status = 'done'",
    )
    r = submit(crud, draft)
    assert r.status_code == 200, r.text
    filament = sum(line["amount_minor"] for line in row["lines"] if line["kind"] == "filament")
    assert costs_of(crud, r.json()["id"]) == {
        "quoted_seconds": row["print_seconds"],
        "quoted_grams": float(sum(Decimal(g) for g in row["grams"].values())),
        "quoted_filament_minor": filament,
    }


def test_job_cost_reads_a_missing_slice_fact_as_unknown(
    crud: CrudCaller,
    listing: dict[str, Any],
    sliced: type[FakeSlicer],
) -> None:
    """A slice row with no plates and no seconds (one written before it kept them) gives
    a quote with no plates: job cost reads its figures as unknown, never as zero."""
    draft, _ = firm_line(crud, listing, 47)
    with _state(crud.api).projection.pool.connection() as conn:
        conn.execute("UPDATE storefront_slice_prices SET plates = NULL, print_seconds = NULL")
    r = submit(crud, draft)
    assert r.status_code == 200, r.text
    assert costs_of(crud, r.json()["id"]) == {
        "quoted_seconds": None,
        "quoted_grams": None,
        "quoted_filament_minor": None,
    }


def test_a_line_whose_design_moved_has_no_firm_price(
    crud: CrudCaller,
    listing: dict[str, Any],
    sliced: type[FakeSlicer],
) -> None:
    draft, _ = firm_line(crud, listing, 45)
    with _state(crud.api).projection.pool.connection() as conn:
        conn.execute(
            "UPDATE storefront_listings SET commit = %s WHERE id = %s", ("f" * 40, listing["id"])
        )
    r = submit(crud, draft)
    assert r.status_code == 409, r.text
    assert r.json()["items"] == [{"id": draft["items"][0]["id"], "price": "by_hand"}]
    r = submit(crud, draft)
    assert r.status_code == 200, r.text
    assert r.json()["pay_now"] is False
    assert quotes(crud, r.json()["id"]) == []
