"""Job cost (spec 2026-11-05 §4.3-4.5, §7, #5326): time and cost entries over the routes,
each on the audit line, and the costs read's charged, cost and margin."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.api.test_customers import create as create_customer
from tests.api.test_orders import create_order, pin_created
from tests.api.test_orders_lifecycle import fulfil, quote_and_accept
from tests.api.test_orders_reopen import item_of
from tests.api.test_orders_unfulfil import accepted_order
from tests.api.test_versions import SLUG

pytestmark = pytest.mark.requires_git


def test_time_entries_copy_the_labour_rate_and_count(client: TestClient) -> None:
    assert (
        client.put(
            "/api/v1/settings",
            json={"pricing_labour_rate_per_hour": "30", "orders_thin_margin_percent": "20"},
        ).status_code
        == 200
    )
    order_id, [item_id] = accepted_order(client)

    made = client.post(
        f"/api/v1/orders/{order_id}/time-entries",
        json={"order_item_id": item_id, "kind": "post_processing", "minutes": 30, "note": "sand"},
    )
    assert made.status_code == 201, made.text
    entry = made.json()
    assert (entry["rate_minor_per_hour"], entry["minutes"]) == (3000, 30)
    assert entry["created_by"]["kind"] == "browser"

    costs = client.get(f"/api/v1/orders/{order_id}/costs").json()
    (line,) = costs["items"]
    assert [e["id"] for e in line["time_entries"]] == [entry["id"]]
    # 30 minutes at 30.00 an hour; charged 1200 by the typed quote.
    assert (line["labour_minor"], line["revenue_minor"]) == (1500, 1200)
    assert (line["margin_minor"], line["margin_state"]) == (-300, "loss")
    assert costs["thin_margin_percent"] == "20"

    gone = client.delete(f"/api/v1/orders/{order_id}/time-entries/{entry['id']}")
    assert gone.status_code == 204
    assert client.delete(f"/api/v1/orders/{order_id}/time-entries/{entry['id']}").status_code == 404
    kinds = [e["kind"] for e in client.get(f"/api/v1/orders/{order_id}/events").json()]
    assert kinds[-2:] == ["time.added", "time.removed"]

    timer = {"kind": "setup", "minutes": 7, "source": "timer:2026-11-05T10:00:00.000Z"}
    first = client.post(f"/api/v1/orders/{order_id}/time-entries", json=timer)
    again = client.post(f"/api/v1/orders/{order_id}/time-entries", json=timer)
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["id"] == first.json()["id"]


def test_cost_entries_on_a_cancelled_order_and_once_per_source(client: TestClient) -> None:
    order_id, _ = accepted_order(client)
    assert client.post(f"/api/v1/orders/{order_id}/cancel").status_code == 200
    body = {"kind": "payment_fee", "amount_minor": 59, "source": "payment:p1"}

    first = client.post(f"/api/v1/orders/{order_id}/cost-entries", json=body)
    again = client.post(f"/api/v1/orders/{order_id}/cost-entries", json=body)

    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["id"] == first.json()["id"]
    costs = client.get(f"/api/v1/orders/{order_id}/costs").json()
    assert [e["amount_minor"] for e in costs["cost_entries"]] == [59]
    assert (costs["revenue_minor"], costs["margin_state"]) == (0, "loss")
    removed = client.delete(f"/api/v1/orders/{order_id}/cost-entries/{first.json()['id']}")
    assert removed.status_code == 204


def test_bad_entries_are_refused(client: TestClient) -> None:
    order_id, _ = accepted_order(client)
    path = f"/api/v1/orders/{order_id}"
    assert (
        client.post(f"{path}/time-entries", json={"kind": "setup", "minutes": 0}).status_code == 422
    )
    assert (
        client.post(
            f"{path}/cost-entries", json={"kind": "material", "amount_minor": -5}
        ).status_code
        == 422
    )
    other = client.post(
        f"{path}/cost-entries",
        json={"order_item_id": "nope", "kind": "material", "amount_minor": 5},
    )
    assert other.status_code == 404
    missing = client.post("/api/v1/orders/nope/time-entries", json={"kind": "setup", "minutes": 5})
    assert missing.status_code == 404


def accepted(client: TestClient, customer: str) -> tuple[str, str]:
    """An accepted order of one item charged 1200, its model already uploaded."""
    order = create_order(client, customer)["id"]
    item = item_of(client, order)["id"]
    quote_and_accept(client, order, item)
    assert client.post(f"/api/v1/orders/{order}/accept").status_code == 200
    return order, item


def test_the_list_shows_each_orders_margin_and_filters_by_it(
    client: TestClient, pg_conninfo: str
) -> None:
    """§6.2, §7 (#5327): each row carries its order's margin, the figures of its costs
    read, and ``margin_state`` narrows the list, a page at a time."""
    loss, [item] = accepted_order(client)  # charged 1200 by the typed quote
    cost = {"order_item_id": item, "kind": "material", "amount_minor": 1500}
    assert client.post(f"/api/v1/orders/{loss}/cost-entries", json=cost).status_code == 201
    customer = create_customer(client)["id"]
    ok, _ = accepted(client, customer)
    thin, item = accepted(client, customer)
    cost = {"order_item_id": item, "kind": "material", "amount_minor": 1100}
    assert client.post(f"/api/v1/orders/{thin}/cost-entries", json=cost).status_code == 201
    uncharged = create_order(client, customer)["id"]
    pin_created(pg_conninfo, [loss, ok, thin, uncharged])

    rows = client.get("/api/v1/orders").json()["items"]
    by_id = {row["id"]: row for row in rows}
    assert [by_id[o]["margin_state"] for o in (loss, ok, thin)] == ["loss", "ok", "thin"]
    blank = by_id[uncharged]
    assert [blank[k] for k in ("margin_minor", "margin_percent", "margin_state")] == [None] * 3
    for row in rows:
        costs = client.get(f"/api/v1/orders/{row['id']}/costs").json()
        for key in ("margin_minor", "margin_percent", "margin_state"):
            assert row[key] == costs[key]
        assert row["margin_estimated"] == bool(costs["estimated"])

    # "Loss or thin", one a page: newest first, and the cursor carries on past `ok`.
    query = "/api/v1/orders?margin_state=loss&margin_state=thin&limit=1"
    first = client.get(query).json()
    assert [r["id"] for r in first["items"]] == [thin]
    second = client.get(f"{query}&cursor={first['next_cursor']}").json()
    assert [r["id"] for r in second["items"]] == [loss]
    assert second["next_cursor"] is None
    assert client.get("/api/v1/orders?margin_state=nope").status_code == 422


def test_the_unit_cost_history_warns_below_the_models_real_cost(
    client: TestClient, pg_conninfo: str
) -> None:
    """§6.3, §7 (#5328): the model's fulfilled job, costed as its order's costs read, and a
    price below it warns with a suggested price at the shop's markup."""
    settings = {"pricing_markup_percent": "25", "pricing_labour_per_item": "0"}
    assert client.put("/api/v1/settings", json=settings).status_code == 200
    order, [item] = accepted_order(client)  # charged 1200 by the typed quote
    cost = {"order_item_id": item, "kind": "material", "amount_minor": 1500}
    assert client.post(f"/api/v1/orders/{order}/cost-entries", json=cost).status_code == 201
    fulfil(client, order, item, 1)
    # Pinned a day back: the window is judged against the wall clock, which jumps on WSL.
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            "UPDATE order_item_fulfilments SET accepted_at = %s WHERE order_item_id = %s"
            " AND accepted_at IS NOT NULL",
            (datetime.now(UTC) - timedelta(days=1), item),
        )
    real = client.get(f"/api/v1/orders/{order}/costs").json()["items"][0]["cost_minor"]

    answer = client.get(
        "/api/v1/orders/history/unit-cost", params={"slug": SLUG, "unit_price_minor": 1200}
    )

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert (body["jobs"], body["units"], body["unit_cost_minor"]) == (1, 1, real)
    assert real == 1500
    assert (body["main_reason"], body["main_reason_minor"]) == ("other", 1500)
    # 1500 marked up 25 %.
    assert (body["suggested_unit_price_minor"], body["markup_percent"]) == (1875, "25")
    assert (body["warning"], body["margin_percent"], body["minor_exponent"]) == (True, -25.0, 2)
    assert body["below_cost"]
    # 1600 is above the cost, but 100 over 1600 is 6.3 %: thin, not below the cost.
    thin = client.get(
        "/api/v1/orders/history/unit-cost", params={"slug": SLUG, "unit_price_minor": 1600}
    ).json()
    assert (thin["warning"], thin["below_cost"], thin["margin_percent"]) == (True, False, 6.3)

    none = client.get("/api/v1/orders/history/unit-cost", params={"slug": "nothing"}).json()
    assert (none["jobs"], none["unit_cost_minor"], none["warning"]) == (0, None, False)
    assert client.get("/api/v1/orders/history/unit-cost").status_code == 422
