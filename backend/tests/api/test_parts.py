"""Parts over the routes (#5543, job cost spec §11): the `/parts` aliases of the inventory
(#7067 Task 9), a model's rule, an item's parts, what an order is short, and the take on
fulfil."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.test_orders_lifecycle import fulfil
from tests.api.test_orders_unfulfil import accepted_order, unfulfil
from tests.api.test_settings_runtime import _state
from tests.api.test_versions import SLUG
from tests.support.operations import press

pytestmark = pytest.mark.requires_git


def make_part(client: TestClient, **fields: Any) -> dict[str, Any]:
    body = {
        "name": "Silver hook",
        "supplier": "Bolt Depot",
        "pack_size": 100,
        "pack_price_minor": 599,
        **fields,
    }
    made = client.post("/api/v1/parts", json=body)
    assert made.status_code == 201, made.text
    part: dict[str, Any] = made.json()
    return part


def _moves(client: TestClient, part_id: str) -> list[dict[str, Any]]:
    got = client.get(f"/api/v1/parts/{part_id}/moves")
    assert got.status_code == 200, got.text
    moves: list[dict[str, Any]] = got.json()
    return moves


def test_parts_and_their_stock(client: TestClient) -> None:
    part = make_part(client, on_hand=3, low_stock=5)
    assert (part["unit_cost_minor"], part["currency"], part["low"]) == ("5.99", "USD", True)
    assert (part["supplier"], part["pack_size"], part["pack_price_minor"]) == (
        "Bolt Depot",
        100,
        599,
    )
    assert client.post("/api/v1/parts", json={**part, "name": "SILVER HOOK"}).status_code == 422
    clash = client.post(
        "/api/v1/parts", json={"name": "SILVER HOOK", "pack_size": 1, "pack_price_minor": 0}
    )
    assert clash.status_code == 409
    assert clash.json()["part_id"] == part["id"]

    added = client.post(f"/api/v1/parts/{part['id']}/stock", json={"packs": 1})
    assert added.status_code == 200, added.text
    assert (added.json()["on_hand"], added.json()["low"]) == (103, False)

    assert (
        client.post(f"/api/v1/parts/{part['id']}/adjust", json={"on_hand": 100}).status_code == 422
    )
    fixed = client.post(
        f"/api/v1/parts/{part['id']}/adjust", json={"on_hand": 100, "note": "counted"}
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["on_hand"] == 100
    moves = _moves(client, part["id"])
    assert [(m["kind"], m["delta"], m["on_hand_after"]) for m in moves] == [
        ("adjust", -3, 100),
        ("add", 100, 103),
        ("add", 3, 3),
    ]

    renamed = client.patch(f"/api/v1/parts/{part['id']}", json={"supplier": "AliExpress"})
    assert renamed.status_code == 200, renamed.text
    assert (renamed.json()["supplier"], renamed.json()["pack_price_minor"]) == ("AliExpress", 599)
    assert client.get("/api/v1/parts?low=true").json() == []
    assert client.post("/api/v1/parts/nope/stock", json={}).status_code == 404
    gone = "5f0e9c1a-1111-4a2b-8c3d-000000000001"
    assert client.post(f"/api/v1/parts/{gone}/stock", json={}).status_code == 404
    assert client.get(f"/api/v1/parts/{gone}/moves").status_code == 404


def test_a_part_starts_at_its_pack_price_over_its_size(client: TestClient) -> None:
    part = make_part(client)
    assert (part["unit_cost_minor"], part["on_hand"]) == ("5.99", 0)
    stocked = make_part(client, name="Ring", on_hand=40)
    assert (stocked["unit_cost_minor"], stocked["on_hand"]) == ("5.99", 40)
    [move] = _moves(client, stocked["id"])
    assert (move["kind"], move["delta"], move["on_hand_after"]) == ("add", 40, 40)


def test_add_stock_averages_and_records_the_pack(client: TestClient) -> None:
    part = make_part(client, pack_price_minor=500, on_hand=100)

    added = client.post(
        f"/api/v1/parts/{part['id']}/stock",
        json={"packs": 1, "pack_price_minor": 700, "note": "restock"},
    ).json()

    assert (added["on_hand"], added["unit_cost_minor"], added["pack_price_minor"]) == (
        200,
        "6",
        700,
    )
    move = _moves(client, part["id"])[0]
    assert (move["kind"], move["delta"], move["amount_minor"], move["note"]) == (
        "add",
        100,
        700,
        "restock",
    )


def test_low_when_at_or_below_its_level(client: TestClient) -> None:
    part = make_part(client, on_hand=5, low_stock=5)
    off = make_part(client, name="Ring")
    assert part["low"] and not off["low"]
    patched = client.patch(f"/api/v1/parts/{part['id']}", json={"low_stock": 4})
    assert patched.json()["low"] is False


def test_a_rename_clash_names_the_item_of_the_parts_kind(client: TestClient) -> None:
    """M3: the clash is in the part's own kind: a `hardware` part of the name is not it."""
    make_part(client, name="Feeder gear")
    ams = [
        client.post(
            "/api/v1/inventory/items",
            json={"kind_id": "ams_part", "name": name},
            headers=press(),
        ).json()["id"]
        for name in ("Feeder gear", "Cutter")
    ]
    clash = client.patch(f"/api/v1/parts/{ams[1]}", json={"name": "feeder GEAR"})
    assert clash.status_code == 409, clash.text
    assert clash.json()["part_id"] == ams[0]


def test_a_name_is_one_parts_in_any_case(client: TestClient) -> None:
    """The unique index answers, so two creates of one name at once give one part and
    `part_exists`, as a create after the other does."""
    part = make_part(client)
    other = make_part(client, name="Ring")
    for name in ("SILVER HOOK", "silver hook"):
        made = client.post(
            "/api/v1/parts", json={"name": name, "pack_size": 1, "pack_price_minor": 0}
        )
        assert made.status_code == 409, made.text
        assert made.json()["part_id"] == part["id"]
    renamed = client.patch(f"/api/v1/parts/{other['id']}", json={"name": "Silver Hook"})
    assert renamed.status_code == 409
    assert renamed.json()["part_id"] == part["id"]


def test_a_stock_add_past_the_most_one_change_moves_is_refused(client: TestClient) -> None:
    big = make_part(client, name="Magnet", pack_size=1_000_000)
    too_many = client.post(f"/api/v1/parts/{big['id']}/stock", json={"packs": 2})
    assert too_many.status_code == 422, too_many.text
    assert too_many.json()["errors"][0]["loc"] == ["body", "packs"]
    assert (
        client.post(f"/api/v1/parts/{big['id']}/stock", json={"packs": 1}).json()["on_hand"]
        == 1_000_000
    )
    assert [m["kind"] for m in _moves(client, big["id"])] == ["add"]


def _no_supplier(client: TestClient, **fields: Any) -> dict[str, Any]:
    return make_part(client, name="Ring", supplier="", pack_size=1, pack_price_minor=0, **fields)


def test_a_part_with_no_supplier_has_no_offer(client: TestClient) -> None:
    """R-P2-25: no supplier, no offer: a pack of 1 at 0, its link a `purchase` link; Add
    stock takes the body's price and writes no price point; a pack edit is refused."""
    part = _no_supplier(client, link="https://example.com/ring")
    assert (part["supplier"], part["pack_size"], part["pack_price_minor"]) == ("", 1, 0)
    assert (part["link"], part["cost_unknown"]) == ("https://example.com/ring", True)

    unpriced = client.post(f"/api/v1/parts/{part['id']}/stock", json={"packs": 2}).json()
    assert unpriced["on_hand"] == 2
    priced = client.post(
        f"/api/v1/parts/{part['id']}/stock", json={"packs": 3, "pack_price_minor": 40}
    ).json()
    assert (priced["on_hand"], priced["pack_price_minor"]) == (5, 0)
    lots = client.get(f"/api/v1/inventory/items/{part['id']}/lots").json()["lots"]
    assert [(lot["on_hand"], lot["cost_unknown"]) for lot in lots] == [("2", True), ("3", False)]
    assert client.get(f"/api/v1/inventory/items/{part['id']}/offers").json()["offers"] == []

    refused = client.patch(f"/api/v1/parts/{part['id']}", json={"pack_size": 10})
    assert refused.status_code == 422
    assert refused.json()["errors"][0]["loc"] == ["body", "supplier"]


@pytest.mark.parametrize("pack", [{"pack_size": 2}, {"pack_price_minor": 100}])
def test_a_create_with_a_pack_needs_a_supplier(client: TestClient, pack: dict[str, int]) -> None:
    """R-P2-34: with no supplier there is no offer to hold a pack, so "2 packs" would
    receive 2 units: the pack is refused, not dropped."""
    body = {"name": "Ring", "supplier": " ", "pack_size": 1, "pack_price_minor": 0, **pack}
    refused = client.post("/api/v1/parts", json=body)
    assert refused.status_code == 422, refused.text
    assert refused.json()["errors"][0]["loc"] == ["body", "supplier"]
    assert client.get("/api/v1/parts").json() == []


def test_a_counted_part_with_no_supplier_is_of_unknown_cost(client: TestClient) -> None:
    part = _no_supplier(client, on_hand=4)
    assert (part["on_hand"], part["unit_cost_minor"], part["cost_unknown"]) == (4, "0", True)
    [lot] = client.get(f"/api/v1/inventory/items/{part['id']}/lots").json()["lots"]
    assert lot["cost_unknown"] is True
    [move] = _moves(client, part["id"])
    assert (move["kind"], move["amount_minor"]) == ("add", None)


def test_the_unit_cost_leaves_out_lots_of_unknown_cost(client: TestClient) -> None:
    """R-P2-35: 2 units of unknown cost and 3 at 40 cost 40 a unit, not 24."""
    part = make_part(client, pack_size=1, pack_price_minor=40)
    received = client.post(
        f"/api/v1/inventory/items/{part['id']}/lots", json={"qty": "2"}, headers=press()
    )
    assert received.status_code == 201, received.text
    added = client.post(f"/api/v1/parts/{part['id']}/stock", json={"packs": 3}).json()
    assert (added["on_hand"], added["unit_cost_minor"], added["cost_unknown"]) == (5, "40", False)


def test_a_link_on_a_part_with_no_supplier(client: TestClient) -> None:
    """M1: the same link adds no row, a blank one removes the purchase links the aliases
    wrote, and blanking the supplier keeps the offer's link as a purchase link."""
    part = _no_supplier(client, link="https://example.com/a")
    path = f"/api/v1/parts/{part['id']}"

    def links() -> list[str]:
        got = client.get(f"/api/v1/entities/item:{part['id']}/links")
        assert got.status_code == 200, got.text
        return [link["url"] for link in got.json()["links"]]

    client.patch(path, json={"link": "https://example.com/a"})
    assert links() == ["https://example.com/a"]
    assert client.patch(path, json={"link": ""}).json()["link"] == ""
    assert links() == []

    hook = make_part(client, link="https://example.com/hook")
    blanked = client.patch(f"/api/v1/parts/{hook['id']}", json={"supplier": ""}).json()
    assert (blanked["supplier"], blanked["link"]) == ("", "https://example.com/hook")


def test_a_part_in_another_currency_round_trips(client: TestClient) -> None:
    """A part priced in EUR (shop USD) reads back in EUR; Add stock writes an EUR price
    point and a lot of unknown cost that keeps the cost entered (R-7067-11, R-P2-11)."""
    part = make_part(client, name="Euro hook", pack_size=1, pack_price_minor=400)
    with _state(client).refs.pool.connection() as conn:
        conn.execute(
            "UPDATE offer_price_points SET currency = 'EUR' WHERE offer_id IN"
            " (SELECT id FROM supplier_offers WHERE item_id = %s)",
            (part["id"],),
        )
    [listed] = [p for p in client.get("/api/v1/parts").json() if p["id"] == part["id"]]
    assert listed["currency"] == "EUR"

    added = client.post(
        f"/api/v1/parts/{part['id']}/stock", json={"packs": 1, "pack_price_minor": 450}
    )
    assert added.status_code == 200, added.text
    assert (added.json()["currency"], added.json()["pack_price_minor"]) == ("EUR", 450)
    [lot] = client.get(f"/api/v1/inventory/items/{part['id']}/lots").json()["lots"]
    assert (lot["cost_unknown"], lot["cost_currency"], lot["unit_cost_minor"]) == (
        True,
        "EUR",
        "0",
    )
    assert lot["foreign_unit_cost_minor"] == "450"
    assert client.get(f"/api/v1/inventory/items/{part['id']}").json()["cost_unknown"] is True


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/v1/parts", {"name": "A\u0000", "pack_size": 1, "pack_price_minor": 0}),
        ("post", "/api/v1/parts/{id}/stock", {"note": "\u0000"}),
        ("post", "/api/v1/parts/{id}/adjust", {"on_hand": 1, "note": "\u0000"}),
        ("patch", "/api/v1/parts/{id}", {"supplier": "\u0000"}),
        ("post", "/api/v1/parts", {"name": "Big", "pack_size": 1, "pack_price_minor": 10**12}),
        ("post", "/api/v1/parts/{id}/stock", {"pack_price_minor": 10**12}),
        ("post", "/api/v1/parts/{id}/adjust", {"on_hand": -1, "note": "counted"}),
        ("post", "/api/v1/parts/{id}/adjust", {"on_hand": 1, "note": "   "}),
    ],
)
def test_alias_bodies_never_reach_sql_unchecked(
    client: TestClient, method: str, path: str, body: dict[str, Any]
) -> None:
    """R-P2-16: a NUL byte, a negative count or a price past what a price point holds is
    a 422, never a 500."""
    part = make_part(client)
    got = getattr(client, method)(path.format(id=part["id"]), json=body)
    assert got.status_code == 422, got.text


def test_a_rule_fills_a_new_item_and_fulfil_takes_its_parts(client: TestClient) -> None:
    part = make_part(client, on_hand=1)
    rule = client.put(
        f"/api/v1/parts/rules/{SLUG}", json={"lines": [{"per_unit": 2, "part_id": part["id"]}]}
    )
    assert rule.status_code == 200, rule.text
    assert client.get(f"/api/v1/parts/rules/{SLUG}").json()["lines"][0]["item_id"] == part["id"]
    bad = client.put(f"/api/v1/parts/rules/{SLUG}", json={"lines": [{"part_id": "nope"}]})
    assert bad.status_code == 404

    order_id, [item_id] = accepted_order(client)
    parts = client.get(f"/api/v1/orders/{order_id}/parts").json()
    row = {"item_id": part["id"], "part_id": part["id"], "per_unit": 2, "used": 0}
    assert parts["items"] == [{"item_id": item_id, "parts": [row]}]
    assert [(s["name"], s["short"]) for s in parts["short"]] == [("Silver hook", "1")]

    fulfil(client, order_id, item_id, 1)

    costs = client.get(f"/api/v1/orders/{order_id}/costs").json()
    assert costs["items"][0]["cost_entries"] == []
    assert costs["items"][0]["parts_minor"] == 12
    assert client.get(f"/api/v1/parts/{part['id']}/moves").json()[0]["on_hand_after"] == -1
    refused = client.put(f"/api/v1/orders/{order_id}/items/{item_id}/parts", json={"parts": []})
    assert refused.status_code == 409
    assert refused.json()["type"].endswith("/item_parts_used")

    unfulfil(client, order_id, item_id)
    [listed] = [p for p in client.get("/api/v1/parts").json() if p["id"] == part["id"]]
    assert listed["on_hand"] == 1
    kinds = [e["kind"] for e in client.get(f"/api/v1/orders/{order_id}/events").json()]
    assert {"item.parts_set", "parts.used", "parts.returned"} <= set(kinds)


def test_an_items_parts_are_set_by_hand(client: TestClient) -> None:
    part = make_part(client)
    order_id, [item_id] = accepted_order(client)
    path = f"/api/v1/orders/{order_id}/items/{item_id}/parts"

    twice = {"parts": [{"item_id": part["id"], "per_unit": 1}] * 2}
    assert client.put(path, json=twice).status_code == 422
    unknown = client.put(path, json={"parts": [{"item_id": "nope", "per_unit": 1}]})
    assert unknown.status_code == 409
    done = client.put(path, json={"parts": [{"item_id": part["id"], "per_unit": 3}]})
    assert done.status_code == 200, done.text
    assert done.json() == [{"item_id": part["id"], "part_id": part["id"], "per_unit": 3, "used": 0}]

    history = client.get(
        "/api/v1/orders/history/unit-cost", params={"slug": SLUG, "item_id": item_id}
    ).json()
    # No history yet: nothing is figured, the parts included.
    assert (history["jobs"], history["parts_unit_minor"]) == (0, None)


def test_the_parts_page_reads_items_made_elsewhere(client: TestClient) -> None:
    """An item of kind `hardware` made on `/inventory` is a part on `/parts`."""
    made = client.post(
        "/api/v1/inventory/items",
        json={"kind_id": "hardware", "name": "M3 nut"},
        headers=press(),
    )
    assert made.status_code == 201, made.text
    nozzle = client.post(
        "/api/v1/inventory/items",
        json={"kind_id": "nozzle", "name": "0.4 nozzle"},
        headers=press(),
    )
    assert nozzle.status_code == 201, nozzle.text
    names = [p["name"] for p in client.get("/api/v1/parts").json()]
    # M2: only kinds that track lots are parts; a nozzle is counted in units.
    assert "M3 nut" in names
    assert "0.4 nozzle" not in names
    assert client.get(f"/api/v1/parts/{nozzle.json()['id']}/moves").status_code == 404
