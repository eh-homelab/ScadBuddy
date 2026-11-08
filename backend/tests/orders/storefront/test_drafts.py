"""A visitor's draft order on the CRUD side (#5282, spec 2026-11-02 §5.4, plan Task 8):
``POST …/storefront/draft/items`` adds a listing at the visitor's values as a model item
with revision 1, ``GET …/storefront/draft`` reads the draft, and ``DELETE
…/storefront/draft/items/{id}`` removes an item. Each answers only the visitor whose
draft it is."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.auth.service_credentials import ServiceCredentialStore
from scadbuddy.core.events import Event, EventBus, OrderChanged, OrderItemChanged, emit
from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from scadbuddy.orders import lifecycle as lifecycle_module
from scadbuddy.orders.component import ORDERS, OrdersComponent
from scadbuddy.orders.lifecycle import Lifecycle, NewModelItem
from scadbuddy.orders.models import Order
from scadbuddy.orders.storefront import drafts as drafts_module
from scadbuddy.orders.storefront.inputs import listing_reference
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
            # Only what the listing offered: never its locks or the template's defaults.
            "inputs": {"width": 40},
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
    assert item["inputs"] == {"width": 40}


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


# -- fix round 1 ---------------------------------------------------------------------------


def the_draft(api: TestClient) -> Order:
    with _state(api).projection.pool.connection() as conn:
        row = conn.execute(
            "SELECT id FROM orders WHERE source = 'storefront' AND submitted_at IS NULL"
        ).fetchone()
    assert row is not None
    order = _orders(api).store.order(row["id"])
    assert order is not None
    return order


def test_an_item_not_from_a_listing_shows_no_inputs(
    crud: CrudCaller, api: TestClient, owner_lifecycle: Lifecycle, listing: Listing
) -> None:
    crud.post("/draft/items", json=add_body(listing, {"width": 40}), key="m1")
    draft = the_draft(api)
    resolved = listing_reference(listing, {"width": 30, "height": 5}, {}, filaments={})
    owner_lifecycle.add_items(
        draft.id, draft.created_by, [NewModelItem(resolved=resolved, quantity=1)]
    )
    items = crud.get("/draft").json()["items"]
    assert [i["inputs"] for i in items] == [{"width": 40}, None]


def test_a_retried_add_replays_whatever_changed_since(
    crud: CrudCaller, listing: Listing, republish: Callable[[], Listing]
) -> None:
    first = crud.post("/draft/items", json=add_body(listing, {"width": 40}), key="n1")
    republish()
    again = crud.post("/draft/items", json=add_body(listing, {"width": 40}), key="n1")
    assert (again.status_code, again.json()) == (200, first.json())


def test_a_retried_add_makes_no_draft(crud: CrudCaller, api: TestClient, listing: Listing) -> None:
    crud.post("/draft/items", json=add_body(listing, {"width": 40}), key="o1")
    draft = the_draft(api)
    # The sweep takes the draft.
    with _state(api).projection.pool.connection() as conn:
        conn.execute("DELETE FROM orders WHERE id = %s", (draft.id,))
    r = crud.post("/draft/items", json=add_body(listing, {"width": 40}), key="o1")
    assert r.status_code == 409 and r.json()["type"] == f"{STOREFRONT_PROBLEMS}draft_changed"
    assert crud.get("/draft").json() is None


def test_a_retried_removal_replays_whatever_changed_since(
    crud: CrudCaller, api: TestClient, listing: Listing
) -> None:
    draft = crud.post("/draft/items", json=add_body(listing, {"width": 40}), key="p1").json()
    item_id = draft["items"][0]["id"]
    first = crud.delete(f"/draft/items/{item_id}", key="p2")
    assert first.status_code == 200
    # The draft is submitted meanwhile (as Task 12's submit leaves it): the retry still
    # answers the order its removal named.
    customer = _orders(api).store.insert_customer(
        display_name="Visitor", company=None, notes="", tags=[], contacts=[], addresses=[]
    )
    with _state(api).projection.pool.connection() as conn:
        conn.execute(
            "UPDATE orders SET submitted_at = now(), number = 9001, customer_id = %s WHERE id = %s",
            (customer.id, draft["id"]),
        )
    again = crud.delete(f"/draft/items/{item_id}", key="p2")
    assert (again.status_code, again.json()["id"]) == (200, draft["id"])


@pytest.fixture
def heard(monkeypatch: pytest.MonkeyPatch) -> list[OrderChanged | OrderItemChanged]:
    """Every order event the lifecycle publishes, as it publishes it: the bus itself
    delivers later, on its own thread."""
    events: list[OrderChanged | OrderItemChanged] = []

    def publish(bus: EventBus | None, event: Event) -> None:
        if isinstance(event, OrderChanged | OrderItemChanged):
            events.append(event)
        emit(bus, event)

    monkeypatch.setattr(lifecycle_module, "emit", publish)
    return events


def test_an_add_tells_the_bus_of_a_cart_once(
    crud: CrudCaller, listing: Listing, heard: list[OrderChanged | OrderItemChanged]
) -> None:
    crud.post("/draft/items", json=add_body(listing, {"width": 40}), key="q1")
    # The draft's creation, then the add: every event a cart's.
    assert [(e.kind, e.cart) for e in heard] == [
        ("order.changed", True),
        ("order.changed", True),
        ("order_item.changed", True),
    ]
    heard.clear()
    # A replay and a refused add tell nothing.
    crud.post("/draft/items", json=add_body(listing, {"width": 40}), key="q1")
    crud.post("/draft/items", json=add_body(listing, {"width": 99}), key="q2")
    assert heard == []


def test_a_removal_tells_the_bus_of_a_cart_once(
    crud: CrudCaller, listing: Listing, heard: list[OrderChanged | OrderItemChanged]
) -> None:
    draft = crud.post("/draft/items", json=add_body(listing, {"width": 40}), key="r1").json()
    heard.clear()
    crud.delete(f"/draft/items/{draft['items'][0]['id']}", key="r2")
    assert [(e.kind, e.cart) for e in heard] == [("order.changed", True)]
    heard.clear()
    crud.delete(f"/draft/items/{draft['items'][0]['id']}", key="r2")
    assert heard == []


def test_the_tell_uses_the_order_the_transaction_left(
    crud: CrudCaller,
    api: TestClient,
    listing: Listing,
    heard: list[OrderChanged | OrderItemChanged],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crud.post("/draft/items", json=add_body(listing, {"width": 40}), key="s1")
    heard.clear()
    store = _orders(api).store
    read = store.order

    def submitted_after(order_id: str, *, conn: Any = None) -> Order | None:
        # Read outside a transaction, the order is already submitted (a submit that
        # committed right after the add).
        order = read(order_id, conn=conn)
        if conn is None and order is not None:
            return order.model_copy(update={"submitted_at": datetime.now(UTC)})
        return order

    monkeypatch.setattr(store, "order", submitted_after)
    crud.post("/draft/items", json=add_body(listing, {"width": 41}), key="s2")
    assert heard and all(e.cart for e in heard)


def test_a_full_draft_takes_no_item(
    crud: CrudCaller, listing: Listing, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(drafts_module, "MAX_DRAFT_ITEMS", 2)
    for n in range(2):
        r = crud.post("/draft/items", json=add_body(listing, {"width": 40 + n}), key=f"t{n}")
        assert r.status_code == 200, r.text
    r = crud.post("/draft/items", json=add_body(listing, {"width": 50}), key="t9")
    assert r.status_code == 422
    assert (r.json()["type"], r.json()["limit"]) == (f"{STOREFRONT_PROBLEMS}draft_full", 2)
    assert len(crud.get("/draft").json()["items"]) == 2


def test_the_view_reads_the_attachments_once(
    crud: CrudCaller, api: TestClient, listing: Listing, monkeypatch: pytest.MonkeyPatch
) -> None:
    for n in range(3):
        crud.post("/draft/items", json=add_body(listing, {"width": 40 + n}), key=f"u{n}")
    store = _orders(api).store
    reads: list[str] = []
    every = store.attachments

    def counted(order_id: str, *, conn: Any = None) -> Any:
        reads.append(order_id)
        return every(order_id, conn=conn)

    def per_item(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("one query per item")

    monkeypatch.setattr(store, "attachments", counted)
    monkeypatch.setattr(store, "subject_attachments", per_item)
    assert len(crud.get("/draft").json()["items"]) == 3
    assert len(reads) == 1
