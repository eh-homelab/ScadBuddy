"""A visitor's draft order on the CRUD side (#5282, spec 2026-11-02 §5.4, plan Task 8):
``POST …/storefront/draft/items`` adds a listing at the visitor's values as a model item
with revision 1, ``GET …/storefront/draft`` reads the draft, and ``DELETE
…/storefront/draft/items/{id}`` removes an item. Each answers only the visitor whose
draft it is."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.auth.service_credentials import ServiceCredentialStore
from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from scadbuddy.orders.component import ORDERS, OrdersComponent
from scadbuddy.orders.lifecycle import Lifecycle
from scadbuddy.orders.storefront.wire import Listing, ListingInput, ListingSnapshot
from scadbuddy.render.schema import CustomizerSchema, Parameter
from scadbuddy.storefront.wire import CLIENT_HEADER, STOREFRONT_PROBLEMS, VISITOR_HEADER

pytestmark = pytest.mark.requires_postgres

CRUD = "/api/v1/internal/crud/storefront"
TOKEN_A = "A" * 21 + "_" + "b" * 20 + "-"
TOKEN_B = "B" * 43
COMMIT = "c" * 40
SCHEMA = CustomizerSchema(
    parameters=[
        Parameter(name="width", type="number", initial=50, min=10, max=100, step=1),
        Parameter(name="height", type="number", initial=5),
    ]
)


def _snapshot(height: int = 5) -> ListingSnapshot:
    return ListingSnapshot(
        schema=SCHEMA,
        pipeline=False,
        template_name="Demo",
        libraries=[],
        snapshot_key=f"src-demo-{COMMIT}",
        fixed={"height": height},
    )


def _state(api: TestClient) -> AppState:
    state: AppState = getattr(api.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state


def _orders(api: TestClient) -> OrdersComponent:
    orders: OrdersComponent = _state(api).components.get(ORDERS)
    return orders


class CrudCaller:
    """The edge's calls: its credential, a visitor id and the browser's address."""

    def __init__(self, api: TestClient) -> None:
        self.api = api
        pool = _state(api).settings_store.pool
        issued = ServiceCredentialStore(pool).issue("storefront", "bootstrap", rate_limited=False)
        self.bearer = {"Authorization": f"Bearer {issued.token}"}

    def _headers(self, token: str, key: str | None) -> dict[str, str]:
        sent = {**self.bearer, VISITOR_HEADER: token, CLIENT_HEADER: "203.0.113.1"}
        if key is not None:
            sent["Idempotency-Key"] = key
        return sent

    def get(self, path: str, *, token: str = TOKEN_A) -> Any:
        return self.api.get(CRUD + path, headers=self._headers(token, None))

    def post(
        self,
        path: str,
        *,
        token: str = TOKEN_A,
        json: Any = None,
        content: str | None = None,
        key: str | None = None,
    ) -> Any:
        headers = self._headers(token, key)
        if content is not None:
            headers["Content-Type"] = "application/json"
            return self.api.post(CRUD + path, content=content, headers=headers)
        return self.api.post(CRUD + path, json=json, headers=headers)

    def delete(self, path: str, *, token: str = TOKEN_A, key: str | None = None) -> Any:
        return self.api.delete(CRUD + path, headers=self._headers(token, key))


@pytest.fixture
def api(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
def crud(api: TestClient) -> CrudCaller:
    return CrudCaller(api)


@pytest.fixture
def owner_lifecycle(api: TestClient) -> Lifecycle:
    return _orders(api).lifecycle


@pytest.fixture
def listing(api: TestClient) -> Listing:
    """A published listing of the template ``demo`` whose width a visitor picks."""
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


@pytest.fixture
def republish(api: TestClient, listing: Listing) -> Callable[[], Listing]:
    """Publish the listing again, at another locked height: its ``updated_at`` moves."""

    def again() -> Listing:
        store = _orders(api).listings
        current = store.get(listing.id)
        assert current is not None
        return store.mark_published(
            listing.id,
            snapshot=_snapshot(height=6),
            media=[],
            expected_updated_at=current.updated_at,
        )

    return again


def add_body(
    listing: Listing, values: dict[str, Any], *, quantity: int | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "listing_id": listing.id,
        "listing_updated_at": listing.updated_at.isoformat(),
        "values": values,
        "colours": {},
    }
    if quantity is not None:
        body["quantity"] = quantity
    return body


def visitor_rows(api: TestClient) -> int:
    with _state(api).projection.pool.connection() as conn:
        row = conn.execute("SELECT count(*) AS n FROM storefront_visitors").fetchone()
    assert row is not None
    n: int = row["n"]
    return n


def test_add_to_order_makes_revision_one(
    crud: CrudCaller, owner_lifecycle: Lifecycle, listing: Listing
) -> None:
    r = crud.post("/draft/items", json=add_body(listing, {"width": 40}, quantity=2), key="a1")
    assert r.status_code == 200, r.text
    draft = r.json()
    [item] = owner_lifecycle.store.items(draft["id"])
    assert item.reference is not None
    assert (item.kind, item.reference.slug, item.reference.commit) == ("model", "demo", COMMIT)
    assert (item.title, item.quantity, item.status) == ("A demo box", 2, "draft")
    assert [r.number for r in owner_lifecycle.store.revisions(item.id)] == [1]
    order = owner_lifecycle.store.order(draft["id"])
    assert order is not None
    assert (order.number, order.customer_id, order.unsubmitted) == (None, None, True)
    assert (draft["currency"], draft["minor_exponent"]) == ("USD", 2)
    assert draft["items"] == [
        {
            "id": item.id,
            "kind": "model",
            "title": "A demo box",
            "quantity": 2,
            "inputs": {"width": 40, "height": 5},
            "notes": None,
            "attachments": [],
        }
    ]


def test_two_first_adds_at_once_share_one_draft(crud: CrudCaller, listing: Listing) -> None:
    def add(width: int) -> Any:
        r = crud.post("/draft/items", json=add_body(listing, {"width": width}), key=f"k{width}")
        assert r.status_code == 200, r.text
        return r.json()

    with ThreadPoolExecutor(2) as pool:
        a, b = pool.map(add, (40, 41))
    assert a["id"] == b["id"]
    items = crud.get("/draft").json()["items"]
    assert sorted(i["inputs"]["width"] for i in items) == [40, 41]


def test_a_retried_add_makes_one_item(crud: CrudCaller, listing: Listing) -> None:
    answers = [
        crud.post("/draft/items", json=add_body(listing, {"width": 40}), key="same")
        for _ in range(2)
    ]
    assert [a.status_code for a in answers] == [200, 200]
    assert answers[0].json() == answers[1].json()
    assert len(crud.get("/draft").json()["items"]) == 1


def test_a_stale_listing_is_409_and_the_item_keeps_its_revision(
    crud: CrudCaller, listing: Listing, republish: Callable[[], Listing]
) -> None:
    crud.post("/draft/items", json=add_body(listing, {"width": 40}), key="b1")
    republish()
    r = crud.post("/draft/items", json=add_body(listing, {"width": 41}), key="b2")
    assert r.status_code == 409 and r.json()["type"] == f"{STOREFRONT_PROBLEMS}listing_changed"
    [item] = crud.get("/draft").json()["items"]
    assert item["inputs"] == {"width": 40, "height": 5}


def test_another_visitor_cannot_see_or_remove_my_item(
    crud: CrudCaller, owner_lifecycle: Lifecycle, listing: Listing
) -> None:
    draft = crud.post("/draft/items", json=add_body(listing, {"width": 40}), key="c1").json()
    item_id = draft["items"][0]["id"]
    assert crud.get("/draft", token=TOKEN_B).json() is None
    # B has a draft of its own: A's item is still not on it.
    crud.post("/draft/items", token=TOKEN_B, json=add_body(listing, {"width": 50}), key="c2")
    r = crud.delete(f"/draft/items/{item_id}", token=TOKEN_B, key="c3")
    assert r.status_code == 404
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}item_not_found"
    assert [i.id for i in owner_lifecycle.store.items(draft["id"])] == [item_id]


def test_the_visitor_removes_its_own_item(
    crud: CrudCaller, owner_lifecycle: Lifecycle, listing: Listing
) -> None:
    draft = crud.post("/draft/items", json=add_body(listing, {"width": 40}), key="d1").json()
    item_id = draft["items"][0]["id"]
    r = crud.delete(f"/draft/items/{item_id}", key="d2")
    assert r.status_code == 200, r.text
    assert (r.json()["id"], r.json()["items"]) == (draft["id"], [])
    assert owner_lifecycle.store.items(draft["id"]) == []
    # The same request again replays its answer; a new one finds no item.
    assert crud.delete(f"/draft/items/{item_id}", key="d2").status_code == 200
    assert crud.delete(f"/draft/items/{item_id}", key="d3").status_code == 404


@pytest.mark.parametrize("item_id", ["not-an-item", "A" * 36, "0" * 36])
def test_an_item_id_that_is_not_one_is_404(
    crud: CrudCaller, listing: Listing, item_id: str
) -> None:
    crud.post("/draft/items", json=add_body(listing, {"width": 40}), key="e1")
    r = crud.delete(f"/draft/items/{item_id}", key="e2")
    assert r.status_code == 404
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}item_not_found"


def test_a_visitor_with_no_draft_reads_null_and_makes_no_row(
    crud: CrudCaller, api: TestClient
) -> None:
    before = visitor_rows(api)
    r = crud.get("/draft")
    assert r.status_code == 200 and r.json() is None
    assert visitor_rows(api) == before
    r = crud.delete("/draft/items/00000000-0000-0000-0000-000000000000", key="f1")
    assert r.status_code == 404
    assert visitor_rows(api) == before


@pytest.mark.parametrize(
    ("values", "name", "reason"),
    [
        ({"width": 81}, "width", "range"),
        ({"width": 40.5}, "width", "step"),
        ({"height": 5}, "height", "locked"),
        ({"depth": 1}, "depth", "unknown"),
        ({"width": "40"}, "width", "range"),
    ],
)
def test_a_value_outside_the_listing_is_422(
    crud: CrudCaller, listing: Listing, values: dict[str, Any], name: str, reason: str
) -> None:
    r = crud.post("/draft/items", json=add_body(listing, values), key="g1")
    assert r.status_code == 422
    problem = r.json()
    assert problem["type"] == f"{STOREFRONT_PROBLEMS}input_outside_listing"
    assert (problem["name"], problem["reason"]) == (name, reason)
    assert crud.get("/draft").json() is None


def test_a_colour_outside_the_listing_is_422(crud: CrudCaller, listing: Listing) -> None:
    body = {**add_body(listing, {"width": 40}), "colours": {"0": "no-such-filament"}}
    r = crud.post("/draft/items", json=body, key="h1")
    assert r.status_code == 422
    assert (r.json()["name"], r.json()["reason"]) == ("colours.0", "colour")


@pytest.mark.parametrize(
    "change",
    [
        {"quantity": 0},
        {"quantity": 101},
        {"quantity": True},
        {"values": {f"p{i}": 1 for i in range(201)}},
        {"colours": {str(i): "x" for i in range(65)}},
        {"colours": {"-1": "x"}},
        {"listing_id": "x" * 65},
        {"extra": 1},
    ],
)
def test_a_body_outside_its_bounds_is_422(
    crud: CrudCaller, listing: Listing, change: dict[str, Any]
) -> None:
    r = crud.post("/draft/items", json={**add_body(listing, {"width": 40}), **change}, key="i1")
    assert r.status_code == 422
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}draft_body_invalid"


@pytest.mark.parametrize("content", ["", "not json", "[" * 5000, "[]"])
def test_a_body_that_is_not_an_item_is_422(crud: CrudCaller, content: str) -> None:
    r = crud.post("/draft/items", content=content, key="j1")
    assert r.status_code == 422
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}draft_body_invalid"


def test_an_unpublished_listing_is_404(crud: CrudCaller, listing: Listing) -> None:
    body = {
        **add_body(listing, {"width": 40}),
        "listing_id": "0" * 8 + "-0000-0000-0000-" + "0" * 12,
    }
    r = crud.post("/draft/items", json=body, key="k1")
    assert r.status_code == 404
    assert r.json()["type"] == f"{STOREFRONT_PROBLEMS}listing_not_found"


def test_a_write_needs_an_idempotency_key(crud: CrudCaller, listing: Listing) -> None:
    assert crud.post("/draft/items", json=add_body(listing, {"width": 40})).status_code == 428
    assert crud.get("/draft").json() is None
    item = "00000000-0000-0000-0000-000000000000"
    assert crud.delete(f"/draft/items/{item}").status_code == 428


def test_the_add_is_the_visitors_on_the_audit_line(
    crud: CrudCaller, owner_lifecycle: Lifecycle, listing: Listing
) -> None:
    draft = crud.post("/draft/items", json=add_body(listing, {"width": 40}), key="l1").json()
    events = owner_lifecycle.store.events(draft["id"])
    added = [e for e in events if e.kind == "item.added"]
    assert len(added) == 1
    assert added[0].by.kind == "visitor"
    assert (added[0].by.principal or "").startswith("storefront_visitor:")
