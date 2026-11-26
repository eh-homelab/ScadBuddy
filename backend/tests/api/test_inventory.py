"""Inventory kinds, items and offers over HTTP (#7067, spec 6.4 and 6.5; #6949 K1). Kinds
and items are Go's since K1 (R-K1-4, R-K1-24): a read goes to Go's CRUD route and a write
starts its activity on the queue `inventory`. Go and Temporal are fakes here (R-K1-21);
the Go tests hold the kinds' and items' own rules. Offers stay Python's until K3."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from temporalio.common import ActivityIDConflictPolicy, ActivityIDReusePolicy

from scadbuddy.core.authorship import AUTHOR_HEADER
from scadbuddy.inventory.component import INVENTORY
from tests.api.test_settings_runtime import _state
from tests.conftest import HistoryRows
from tests.support.inventory_core import HANG, FakeTemporal, Go, install_go
from tests.support.inventory_rows import insert_item, insert_kind
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres

API = "/api/v1/inventory"
PROBLEMS = "https://scadbuddy.dev/problems/inventory/"
ENTITY_PROBLEMS = "https://scadbuddy.dev/problems/entities/"
ANSWERS = (
    Path(__file__).resolve().parents[3]
    / "tools/go/core/internal/inventory/activities/testdata/answers"
)


def answer(name: str) -> Any:
    """Go's recorded answer `name` (`activities/testdata/answers`)."""
    return json.loads((ANSWERS / f"{name}.json").read_text(encoding="utf-8"))


def _code(response: Any, status: int, code: str, prefix: str = PROBLEMS) -> None:
    assert response.status_code == status, response.text
    assert response.json()["type"] == prefix + code


def _item(client: TestClient, kind_id: str = "hardware", **columns: Any) -> dict[str, Any]:
    """An item row, made as Go's save leaves it: Python makes no item since K1."""
    return {"id": str(insert_item(_state(client).refs.pool, kind_id, **columns))}


def _supplier(client: TestClient) -> str:
    with _state(client).refs.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO filament_suppliers (name) VALUES (%s) RETURNING id",
            (f"Bolt Depot {uuid.uuid4()}",),
        ).fetchone()
    assert row is not None
    return str(row["id"])


def done(status: int, body: Any) -> dict[str, Any]:
    """An activity's answer."""
    return {"status": status, "body": body}


@pytest.fixture
def go(client: TestClient) -> Go:
    return install_go(client)


# -- kinds -------------------------------------------------------------------------------


def test_kind_reads_go_to_go(client: TestClient, go: Go) -> None:
    kind = answer("ItemKind")
    go.reads["/kinds"] = (200, answer("KindList"))
    go.reads[f"/kinds/{kind['id']}/schema"] = (200, answer("KindSchema"))

    listed = client.get(f"{API}/kinds", params={"archived": "true"})
    schema = client.get(f"{API}/kinds/{kind['id']}/schema")

    assert listed.status_code == 200, listed.text
    assert listed.json() == answer("KindList")
    assert schema.json() == answer("KindSchema")
    assert dict(go.seen[0].url.params) == {"archived": "true"}


def test_a_kind_create_starts_kind_create(client: TestClient, go: Go) -> None:
    go.temporal.answers.append(done(201, answer("KindCreate")))
    body = {"id": "bolt", "name": "Bolt", "parent_id": "fastener", "nature": "physical"}

    made = client.post(f"{API}/kinds", json=body, headers=press())

    assert made.status_code == 201, made.text
    assert made.json() == answer("KindCreate")
    start = go.start
    assert start.name == "inventory.KindCreate"
    assert start.options["task_queue"] == "inventory"
    assert start.options["id_conflict_policy"] == ActivityIDConflictPolicy.USE_EXISTING
    assert start.options["id_reuse_policy"] == ActivityIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY
    assert start.arg["params"] == {}
    assert start.arg["body"] == body
    assert start.arg["actor"]["kind"] == "browser"
    assert start.options["summary"] == "KindCreate item_kind:bolt"


def test_a_kind_patch_sends_its_nulls(client: TestClient, go: Go) -> None:
    """A patch's null opens a class key or makes the kind a root; an absent field is
    left out (Task 10). `steps` replaces the kind's own."""
    go.temporal.answers.append(done(200, answer("KindUpdate")))
    body = {"parent_id": None, "role": None, "steps": {"life": {"handler": "life.none"}}}

    patched = client.patch(f"{API}/kinds/bolt", json=body, headers=press())

    assert patched.status_code == 200, patched.text
    assert (go.start.name, go.start.arg["params"], go.start.arg["body"]) == (
        "inventory.KindUpdate",
        {"kind_id": "bolt"},
        body,
    )


@pytest.mark.parametrize("action", ["archive", "restore"])
def test_a_kind_archive_and_restore_start_their_activities(
    client: TestClient, go: Go, action: str
) -> None:
    go.temporal.answers.append(done(200, answer(f"Kind{action.capitalize()}")))

    made = client.post(f"{API}/kinds/bolt/{action}", headers=press())

    assert made.status_code == 200, made.text
    assert go.start.name == f"inventory.Kind{action.capitalize()}"
    assert (go.start.arg["params"], go.start.arg["body"]) == ({"kind_id": "bolt"}, None)


def test_kind_schema_history_route(client: TestClient, history_rows: HistoryRows) -> None:
    old = {"type": "object", "properties": {}}
    new = {"type": "object", "properties": {"mpn": {"type": "string"}}}
    with _state(client).refs.pool.connection() as conn:
        history_rows(
            conn,
            {"event_seq": 9001, "owner": "item_kind:bolt", "old": None, "new": old},
            {"event_seq": 9002, "owner": "item_kind:bolt", "old": old, "new": new},
            {"event_seq": 9003, "owner": "item_kind:nut", "old": None, "new": old},
        )

    got = client.get(f"{API}/kinds/bolt/schema/history")

    assert got.status_code == 200, got.text
    assert [c["new"] for c in got.json()["changes"]] == [new, old]
    assert client.get(f"{API}/kinds/bolt/schema/history", params={"limit": 0}).status_code == 422


# -- items -------------------------------------------------------------------------------


def test_item_reads_go_to_go(client: TestClient, go: Go) -> None:
    item = answer("Item")
    go.reads["/items"] = (200, answer("ItemList"))
    go.reads[f"/items/{item['id']}"] = (200, item)

    listed = client.get(
        f"{API}/items",
        params={"kind": "hardware", "descendants": "false", "q": "M3", "low": "true", "limit": 5},
    )
    one = client.get(f"{API}/items/{item['id']}")

    assert listed.json() == answer("ItemList")
    assert one.json() == item
    assert dict(go.seen[0].url.params) == {
        "kind": "hardware",
        "descendants": "false",
        "q": "M3",
        "low": "true",
        "archived": "false",
        "limit": "5",
    }


def test_an_item_create_starts_item_create(client: TestClient, go: Go) -> None:
    go.temporal.answers.append(done(201, answer("ItemCreate")))
    body = {"kind_id": "hardware", "name": "M3 bolt", "low_stock": "4.5"}

    made = client.post(f"{API}/items", json=body, headers=press())

    assert made.status_code == 201, made.text
    assert made.json() == answer("ItemCreate")
    assert (go.start.name, go.start.arg["body"]) == ("inventory.ItemCreate", body)
    assert go.start.options["summary"] == "ItemCreate item_kind:hardware"


def test_an_item_patch_and_its_archive_start_their_activities(client: TestClient, go: Go) -> None:
    item_id = answer("Item")["id"]
    go.temporal.answers += [
        done(200, answer("ItemUpdate")),
        done(200, answer("ItemArchive")),
        done(200, answer("ItemRestore")),
    ]
    body: dict[str, Any] = {"life_hours": None, "steps": {}}

    patched = client.patch(f"{API}/items/{item_id}", json=body, headers=press())
    archived = client.post(f"{API}/items/{item_id}/archive", headers=press())
    restored = client.post(f"{API}/items/{item_id}/restore", headers=press())

    assert [r.status_code for r in (patched, archived, restored)] == [200, 200, 200]
    assert [(s.name, s.arg["params"], s.arg["body"]) for s in go.temporal.starts] == [
        ("inventory.ItemUpdate", {"item_id": item_id}, body),
        ("inventory.ItemArchive", {"item_id": item_id}, None),
        ("inventory.ItemRestore", {"item_id": item_id}, None),
    ]


def test_an_item_input_has_no_properties_yet(client: TestClient, go: Go) -> None:
    """#7950's `properties` is a later task's: the input still refuses the key."""
    made = client.post(
        f"{API}/items",
        json={"kind_id": "hardware", "name": "N", "properties": {"mpn": "x"}},
        headers=press(),
    )
    assert made.status_code == 422, made.text
    assert go.temporal.starts == []


# -- the forwarders' own answers ---------------------------------------------------------


def test_a_go_problem_is_passed_through(client: TestClient, go: Go) -> None:
    problem = {
        "type": PROBLEMS + "step_not_applicable",
        "title": "Conflict",
        "status": 409,
        "detail": "the item's class has no consume step: x",
        "code": "step_not_applicable",
        "class": {"nature": "service"},
        "step": "consume",
    }
    go.temporal.answers.append(done(409, problem))

    used = client.post(f"{API}/items/{uuid.uuid4()}/use", json={"qty": "1"}, headers=press())

    _code(used, 409, "step_not_applicable")
    body = used.json()
    assert (body["code"], body["step"], body["class"]) == (
        "step_not_applicable",
        "consume",
        {"nature": "service"},
    )
    assert body["instance"] == used.request.url.path


def test_a_slow_write_answers_202_and_a_repeat_202_again(client: TestClient, go: Go) -> None:
    key = {"Idempotency-Key": "k-slow"}
    go.temporal.answers += [HANG, HANG]

    first = client.post(f"{API}/kinds/bolt/archive", headers=key)
    again = client.post(f"{API}/kinds/bolt/archive", headers=key)

    for accepted in (first, again):
        assert accepted.status_code == 202, accepted.text
        body = accepted.json()
        assert (body["kind"], body["subject"], body["status"]) == (
            "inventory.KindArchive",
            "item_kind:bolt",
            "running",
        )
        assert body["id"].endswith(":k-slow")
    assert len(go.temporal.starts) == 2
    assert {s.options["id"] for s in go.temporal.starts} == {first.json()["id"]}


def test_a_done_write_answers_the_stored_result(client: TestClient, go: Go) -> None:
    go.temporal.answers.append(HANG)
    sent = {"kind_id": "hardware", "name": "N"}
    first = client.post(f"{API}/items", json=sent, headers={"Idempotency-Key": "k-done"})
    key = first.json()["id"]
    go.temporal.done[key] = done(201, answer("ItemCreate"))

    again = client.post(f"{API}/items", json=sent, headers={"Idempotency-Key": "k-done"})

    assert again.status_code == 201, again.text
    assert again.json() == answer("ItemCreate")
    assert go.temporal.handles == [key]


def test_a_failed_activity_runs_again(client: TestClient, go: Go) -> None:
    """A key whose run failed starts again (Allow Duplicate Failed Only): the route
    answers the new run."""
    go.temporal.answers.append(done(200, answer("KindRestore")))

    made = client.post(f"{API}/kinds/bolt/restore", headers={"Idempotency-Key": "k-failed"})

    assert made.status_code == 200, made.text
    assert go.temporal.handles == []


def test_temporal_out_of_reach_is_a_503(client: TestClient) -> None:
    go = install_go(client, FakeTemporal(refuse=True))

    _code(client.post(f"{API}/kinds/bolt/archive", headers=press()), 503, "inventory_unavailable")
    assert len(go.temporal.starts) == 1


def test_go_out_of_reach_is_a_503(client: TestClient, go: Go) -> None:
    go.down = True

    _code(client.get(f"{API}/kinds"), 503, "inventory_unavailable")
    assert _state(client).components.get(INVENTORY).core is not None


def test_no_core_url_is_a_503(client: TestClient) -> None:
    install_go(client, url=None)

    _code(client.get(f"{API}/items"), 503, "inventory_unavailable")
    _code(client.get(f"{API}/operations/x"), 503, "inventory_unavailable")


def test_a_persons_key_is_scoped_to_the_actor(client: TestClient, go: Go) -> None:
    go.temporal.answers += [done(200, answer("KindArchive"))] * 2
    key = {"Idempotency-Key": "same"}

    client.post(f"{API}/kinds/bolt/archive", headers=key)
    client.post(f"{API}/kinds/bolt/archive", headers={**key, AUTHOR_HEADER: "token:abc123"})

    ids = [s.options["id"] for s in go.temporal.starts]
    assert len(set(ids)) == 2, ids
    assert [s.arg["actor"]["kind"] for s in go.temporal.starts] == ["browser", "agent"]


def test_a_write_with_no_key_gets_its_own(client: TestClient, go: Go) -> None:
    go.temporal.answers += [done(200, answer("KindArchive"))] * 2

    client.post(f"{API}/kinds/bolt/archive")
    client.post(f"{API}/kinds/bolt/archive")

    ids = [s.options["id"] for s in go.temporal.starts]
    assert all(i.startswith("req:") for i in ids) and ids[0] != ids[1]


def test_the_operation_route_reads_go(client: TestClient, go: Go) -> None:
    operation = {
        "id": "browser::k",
        "kind": "inventory.Take",
        "subject": "item:x",
        "status": "failed",
        "result": None,
        "error": {
            "type": PROBLEMS + "lot_empty",
            "title": "Conflict",
            "status": 409,
            "detail": "the named lot has no stock on hand",
            "extensions": {"code": "lot_empty"},
        },
        "created_at": "2026-11-26T12:00:00Z",
        "finished_at": "2026-11-26T12:00:01Z",
    }
    go.reads["/operations/one"] = (200, operation)

    got = client.get(f"{API}/operations/browser%3A%3Ak")

    assert got.status_code == 200, got.text
    assert got.json() == operation
    assert dict(go.seen[0].url.params) == {"id": "browser::k"}


def test_an_unknown_operation_is_404(client: TestClient, go: Go) -> None:
    go.reads["/operations/one"] = (
        404,
        {
            "type": PROBLEMS + "operation_not_found",
            "title": "Not Found",
            "status": 404,
            "detail": "no inventory operation 'x'",
            "code": "operation_not_found",
        },
    )

    _code(client.get(f"{API}/operations/x"), 404, "operation_not_found")


def test_bodies_pydantic_refuses_never_start(client: TestClient, go: Go) -> None:
    """A NUL, a bound or an unknown field is the request's own 422: nothing starts."""
    item = uuid.uuid4()
    for got in (
        client.get(f"{API}/kinds/a%00b/schema"),
        client.patch(f"{API}/kinds/a%00b", json={}, headers=press()),
        client.post(f"{API}/kinds/a%00b/archive", headers=press()),
        client.get(f"{API}/items", params={"kind": "a\x00b"}),
        client.get(f"{API}/items", params={"q": "a\x00b"}),
        client.get(f"{API}/items", params={"compatible": "a\x00b"}),
        client.post(f"{API}/kinds", json={"id": "k1", "name": "K\x00"}, headers=press()),
        client.post(f"{API}/kinds", json={"id": "k1", "name": "K", "role": "x"}, headers=press()),
        client.patch(f"{API}/items/{item}", json={"note": "\x00"}, headers=press()),
        client.patch(f"{API}/items/{item}", json={"life_months": 10**10}, headers=press()),
        client.post(f"{API}/kinds/bolt/archive", headers={"Idempotency-Key": "k" * 201}),
    ):
        assert got.status_code == 422, got.text
    for field, value in (
        ("name", "M3\x00"),
        ("manufacturer", "Ruthex\x00"),
        ("note", "\x00"),
        ("compatible_with", ["X1\x00"]),
        ("source", "csv\x00"),
        ("source_ref", "r\x00"),
        ("life_months", 1201),
        ("manufacturer", "m" * 201),
        ("compatible_with", ["c" * 101]),
        ("compatible_with", [f"c{n}" for n in range(101)]),
        ("source", "s" * 101),
        ("source_ref", "r" * 201),
    ):
        made = client.post(
            f"{API}/items", json={"kind_id": "hardware", "name": "N", field: value}, headers=press()
        )
        assert made.status_code == 422, (field, made.text)
    assert go.temporal.starts == []


# -- offers ------------------------------------------------------------------------------


def test_an_offer_round_trips_with_its_prices(client: TestClient, go: Go) -> None:
    item = _item(client)
    go.reads[f"/items/{item['id']}"] = (200, {**answer("Item"), "id": item["id"]})
    supplier = _supplier(client)
    made = client.post(
        f"{API}/offers",
        json={"supplier_id": supplier, "item_id": item["id"], "sku": "M3-100", "pack_size": "100"},
        headers=press(),
    )
    assert made.status_code == 201, made.text
    offer = made.json()
    assert offer["latest_price"] is None
    # R-P2-40: decimals are normalized, as an item's are.
    assert offer["pack_size"] == "100"

    assert [o["id"] for o in client.get(f"{API}/items/{item['id']}/offers").json()["offers"]] == [
        offer["id"]
    ]
    by_supplier = client.get(f"{API}/suppliers/{supplier}/offers").json()["offers"]
    assert [o["id"] for o in by_supplier] == [offer["id"]]

    patched = client.patch(
        f"{API}/offers/{offer['id']}", json={"url": "https://example.com/m3"}, headers=press()
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["url"] == "https://example.com/m3"

    price = client.post(
        f"{API}/offers/{offer['id']}/prices",
        json={"price": "4.99", "currency": "EUR", "on_date": "2026-11-20"},
        headers=press(),
    )
    assert price.status_code == 201, price.text
    assert (price.json()["currency"], price.json()["source"]) == ("EUR", "manual")
    assert price.json()["price"] == "4.99"
    listed = client.get(f"{API}/items/{item['id']}/offers").json()["offers"][0]
    assert (listed["pack_size"], listed["latest_price"]["price"]) == ("100", "4.99")
    prices = client.get(f"{API}/offers/{offer['id']}/prices").json()["prices"]
    assert [p["id"] for p in prices] == [price.json()["id"]]

    archived = client.post(f"{API}/offers/{offer['id']}/archive", headers=press())
    assert archived.status_code == 200, archived.text
    assert client.get(f"{API}/items/{item['id']}/offers").json()["offers"] == []


def test_offer_refusals(client: TestClient, go: Go) -> None:
    item = _item(client)
    supplier = _supplier(client)
    body = {"supplier_id": supplier, "item_id": item["id"], "sku": "NUT-3"}
    assert client.post(f"{API}/offers", json=body, headers=press()).status_code == 201
    _code(
        client.post(f"{API}/offers", json={**body, "sku": "nut-3"}, headers=press()),
        409,
        "offer_exists",
    )
    nowhere = {**body, "supplier_id": str(uuid.uuid4())}
    _code(client.post(f"{API}/offers", json=nowhere, headers=press()), 404, "supplier_not_found")
    unknown = uuid.uuid4()
    listed = client.get(f"{API}/suppliers/{unknown}/offers")
    _code(listed, 404, "supplier_not_found")
    assert listed.json()["detail"] == f"no supplier {str(unknown)!r}"
    no_item = {**body, "item_id": str(uuid.uuid4())}
    _code(client.post(f"{API}/offers", json=no_item, headers=press()), 404, "item_not_found")
    missing_item = uuid.uuid4()
    go.reads[f"/items/{missing_item}"] = (
        404,
        {
            "type": PROBLEMS + "item_not_found",
            "title": "Not Found",
            "status": 404,
            "detail": f"no item {str(missing_item)!r}",
            "code": "item_not_found",
        },
    )
    _code(client.get(f"{API}/items/{missing_item}/offers"), 404, "item_not_found")
    missing = uuid.uuid4()
    _code(client.patch(f"{API}/offers/{missing}", json={}, headers=press()), 404, "offer_not_found")
    _code(client.get(f"{API}/offers/{missing}/prices"), 404, "offer_not_found")
    price = client.post(f"{API}/offers/{missing}/prices", json={"price": "1"}, headers=press())
    _code(price, 404, "offer_not_found")

    other = client.post(f"{API}/offers", json={**body, "sku": "NUT-4"}, headers=press())
    clash = client.patch(
        f"{API}/offers/{other.json()['id']}", json={"sku": "nut-3"}, headers=press()
    )
    _code(clash, 409, "offer_exists")

    filament_item = _item(client, "filament", name=f"PLA {uuid.uuid4().hex[:6]}")
    filament = client.post(
        f"{API}/offers", json={**body, "item_id": filament_item["id"]}, headers=press()
    )
    _code(filament, 409, "filament_item_refused")
    for field, value in (
        ("sku", "S\x00"),
        ("pack_uom", "box\x00"),
        ("note", "\x00"),
        ("pack_uom", "u" * 101),
        ("note", "n" * 4001),
    ):
        offer = client.post(f"{API}/offers", json={**body, field: value}, headers=press())
        assert offer.status_code == 422, (field, offer.text)


# -- an item's properties ----------------------------------------------------------------


def test_an_items_property_schema_is_its_kinds_merged(client: TestClient, go: Go) -> None:
    """The item's kind is read through Go (C16); the schema and the keys are the API's."""
    pool = _state(client).refs.pool
    parent = insert_kind(
        pool, attribute_schema={"type": "object", "properties": {"mpn": {"type": "string"}}}
    )
    child = insert_kind(
        pool,
        parent_id=parent,
        attribute_schema={"type": "object", "properties": {"length_mm": {"type": "number"}}},
    )
    item = _item(client, child)
    go.reads[f"/items/{item['id']}"] = (
        200,
        {**answer("Item"), "id": item["id"], "kind_id": child},
    )
    got = client.get(f"/api/v1/entities/item:{item['id']}/property-schema")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["kind"] == "item"
    assert set(body["schema"]["properties"]) == {"mpn", "length_mm"}

    wrong = client.patch(
        f"/api/v1/entities/item:{item['id']}/properties",
        json={"set": {"mpn": 12}},
        headers=press(),
    )
    _code(wrong, 422, "bad_property_value", ENTITY_PROBLEMS)
    unknown_key = client.patch(
        f"/api/v1/entities/item:{item['id']}/properties",
        json={"set": {"finish": "zinc"}},
        headers=press(),
    )
    assert unknown_key.status_code == 200, unknown_key.text
    again = client.get(f"/api/v1/entities/item:{item['id']}/property-schema")
    assert again.json()["keys_in_use"] == [{"key": "finish", "count": 1}]

    put = client.put(
        "/api/v1/entity-kinds/item/property-schema",
        json={"schema": {"type": "object", "properties": {}}},
        headers=press(),
    )
    _code(put, 409, "schema_owned_by_item_kind", ENTITY_PROBLEMS)


def test_the_entity_property_schema_serves_other_kinds_and_refuses_a_missing_entity(
    client: TestClient, go: Go
) -> None:
    supplier = _supplier(client)
    got = client.get(f"/api/v1/entities/supplier:{supplier}/property-schema")
    assert got.status_code == 200, got.text
    assert got.json()["kind"] == "supplier"
    assert got.json()["schema"]["type"] == "object"
    missing = client.get(f"/api/v1/entities/item:{uuid.uuid4()}/property-schema")
    _code(missing, 404, "entity_not_found", ENTITY_PROBLEMS)
    _code(
        client.get("/api/v1/entities/nope/property-schema"),
        422,
        "bad_entity_ref",
        ENTITY_PROBLEMS,
    )
