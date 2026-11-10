"""The customer's order page on the CRUD side (#5283, spec 2026-11-02 §5.2, §5.6, §6, §7,
plan Task 5): ``POST …/storefront/order/view`` and the customer's writes, each reached by
the order link's token in the body. A token that reads nothing gets an empty 404; the view
is allowlisted; each write runs as the customer, under the order's lock, once per
``Idempotency-Key``."""

from __future__ import annotations

import base64
import io
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, get_args, get_origin

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.main import create_app
from scadbuddy.orders.attachments import keep_file
from scadbuddy.orders.lifecycle import Lifecycle, ManualQuoteInput, NewModelItem
from scadbuddy.orders.models import Actor, Order, OrderItem, Revision
from scadbuddy.orders.numbers import next_number
from scadbuddy.orders.storefront.links import revoke_order_links
from scadbuddy.orders.storefront.mail import issue_link
from scadbuddy.orders.storefront.order_page import ITEM_WORDS, ORDER_WORDS
from scadbuddy.orders.storefront.visitors import VisitorStore, visitor_actor
from scadbuddy.orders.storefront.wire import CustomerOrderView
from scadbuddy.payments import store as payments
from scadbuddy.storefront.wire import ORDER_VIEW_FIELDS, STOREFRONT_PROBLEMS
from tests.orders.storefront.test_drafts import CRUD, CrudCaller, _orders, _state
from tests.orders.test_lifecycle import resolved
from tests.support.store import s3  # noqa: F401  (fixture)

pytestmark = pytest.mark.requires_postgres

BY = Actor(kind="browser")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _kek(tmp_path: Path) -> Path:
    kek = tmp_path / "kek"
    kek.write_text(base64.b64encode(os.urandom(32)).decode())
    return kek


@pytest.fixture
def api(settings: Settings, tmp_path: Path) -> Iterator[TestClient]:
    """The app with a KEK: the order link's token is sealed under it."""
    with TestClient(
        create_app(settings.model_copy(update={"secret_key_file": _kek(tmp_path)}))
    ) as client:
        yield client


@pytest.fixture
def crud(api: TestClient) -> CrudCaller:
    return CrudCaller(api)


class Reader:
    """The edge's calls for one link holder: the token in the body, never a path."""

    def __init__(self, crud: CrudCaller, token: Any) -> None:
        self.crud = crud
        self.token = token

    def _post(self, path: str, body: dict[str, Any], key: str | None) -> Any:
        headers = {**self.crud.bearer}
        if key is not None:
            headers["Idempotency-Key"] = key
        return self.crud.api.post(CRUD + path, json=body, headers=headers)

    def view(self) -> Any:
        return self._post("/order/view", {"token": self.token}, None)

    def answer(self, revision_id: str, answer: str, note: str = "", *, key: str | None) -> Any:
        body = {"token": self.token, "answer": answer, "note": note}
        return self._post(f"/order/revisions/{revision_id}/answer", body, key)

    def accept(self, quote_id: str, *, key: str | None) -> Any:
        return self._post(f"/order/quotes/{quote_id}/accept", {"token": self.token}, key)

    def cancel(self, reason: str = "", *, key: str | None) -> Any:
        return self._post("/order/cancel", {"token": self.token, "reason": reason}, key)


class Made:
    def __init__(self, order: Order, token: str, reader: Reader) -> None:
        self.order = order
        self.token = token
        self.reader = reader


def make_order(
    api: TestClient,
    crud: CrudCaller,
    count: int = 1,
    approval: str = "together",
    visitor: str = "v",
) -> Made:
    """A submitted storefront order of ``count`` model items, each with a manual quote on
    revision 1, a render on it and the revision sent, and its customer link."""
    orders = _orders(api)
    life: Lifecycle = orders.lifecycle
    pool = _state(api).projection.pool
    customer = orders.store.insert_customer(
        display_name="Ada", company=None, notes="", tags=[], contacts=[], addresses=[]
    )
    found = VisitorStore(pool).ensure(visitor * 43)
    by = visitor_actor(found)
    order = life.ensure_draft(found.id, by, currency="USD")
    life.add_items(
        order.id, by, [NewModelItem(resolved=resolved(), quantity=1) for _ in range(count)]
    )
    with orders.store.transaction() as conn:
        conn.execute(
            "UPDATE orders SET number = %s, customer_id = %s, submitted_at = %s WHERE id = %s",
            (next_number(conn, datetime.now(UTC).year), customer.id, datetime.now(UTC), order.id),
        )
    for item in orders.store.items(order.id):
        life.update_item(order.id, item.id, BY, approval=approval)  # type: ignore[arg-type]
        life.record_manual_quote(
            order.id, item.id, ManualQuoteInput(unit_price_minor=1250, label="typed"), BY
        )
        [rev] = orders.store.revisions(item.id)
        attach(api, order, item, rev)
        life.send_revision(order.id, item.id, rev.id, BY)
    found_order = orders.store.order(order.id)
    assert found_order is not None
    with orders.store.transaction() as conn:
        token = issue_link(conn, life.kek, order.id, found_order.label, "ada@example.com")
    return Made(found_order, token, Reader(crud, token))


def attach(
    api: TestClient, order: Order, item: OrderItem, rev: Revision, data: bytes = PNG
) -> None:
    digest = sha256(data).hexdigest()
    assert api.portal is not None
    key = api.portal.call(keep_file, _state(api).store.blobs, io.BytesIO(data), digest)
    _orders(api).lifecycle.add_attachment(
        order.id,
        item.id,
        BY,
        attachment_id=f"att-{rev.id}",
        kind="image",
        role="render",
        filename="render.png",
        media_type="image/png",
        size_bytes=len(data),
        sha256=digest,
        blob_key=key,
        caption="",
        revision_id=rev.id,
    )


def items_of(api: TestClient, made: Made) -> list[OrderItem]:
    return _orders(api).store.items(made.order.id)


def newest(api: TestClient, item: OrderItem) -> Revision:
    return _orders(api).store.revisions(item.id)[-1]


def rows(api: TestClient, sql: str, *args: Any) -> list[dict[str, Any]]:
    with _state(api).projection.pool.connection() as conn:
        return list(conn.execute(sql, args).fetchall())


def slug(response: Any) -> str:
    return str(response.json()["type"]).removeprefix(STOREFRONT_PROBLEMS)


# -- reading ---------------------------------------------------------------------------


def test_a_token_reads_only_its_own_order(api: TestClient, crud: CrudCaller) -> None:
    a = make_order(api, crud, visitor="a")
    b = make_order(api, crud, visitor="b")
    seen = a.reader.view()
    assert seen.status_code == 200, seen.text
    assert seen.json()["number"] == a.order.number
    [b_item] = items_of(api, b)
    # A's token names B's revision: not A's to answer, and B's stays as it was.
    r = a.reader.answer(newest(api, b_item).id, "accepted", key="k1")
    assert r.status_code == 404 and slug(r) == "revision_not_found"
    assert newest(api, b_item).answer is None
    [b_quote] = _orders(api).store.quotes(b_item.id)
    r = a.reader.accept(b_quote.id, key="k2")
    assert r.status_code == 404 and slug(r) == "quote_not_found"


@pytest.mark.parametrize("token", ["unknown", "", "x" * 43, "x" * 5000, 5, None, ["a"], {"a": 1}])
def test_a_token_that_reads_nothing_is_an_empty_404(
    api: TestClient, crud: CrudCaller, token: Any
) -> None:
    made = make_order(api, crud)
    [item] = items_of(api, made)
    rev = newest(api, item)
    [quote] = _orders(api).store.quotes(item.id)
    reader = Reader(crud, token)
    for r in (
        reader.view(),
        reader.answer(rev.id, "accepted", key="k1"),
        reader.accept(quote.id, key="k2"),
        reader.cancel(key="k3"),
    ):
        assert (r.status_code, r.content) == (404, b"")
    assert newest(api, item).answer is None


def test_a_revoked_token_gets_the_same_404_as_an_unknown_one(
    api: TestClient, crud: CrudCaller
) -> None:
    made = make_order(api, crud)
    assert made.reader.view().status_code == 200
    with _orders(api).store.transaction() as conn:
        revoke_order_links(conn, made.order.id)
    revoked = made.reader.view()
    unknown = Reader(crud, "u" * 43).view()
    assert (revoked.status_code, revoked.content) == (unknown.status_code, unknown.content)
    assert (revoked.status_code, revoked.content) == (404, b"")
    assert revoked.headers.get("content-type") == unknown.headers.get("content-type")


def test_the_first_view_stamps_opened_at(api: TestClient, crud: CrudCaller) -> None:
    made = make_order(api, crud)
    [before] = rows(api, "SELECT opened_at FROM storefront_order_links")
    assert before["opened_at"] is None
    made.reader.view()
    [first] = rows(api, "SELECT opened_at FROM storefront_order_links")
    assert first["opened_at"] is not None
    made.reader.view()
    [again] = rows(api, "SELECT opened_at FROM storefront_order_links")
    assert again["opened_at"] == first["opened_at"]


def test_the_view_shows_the_order_in_the_customers_words(api: TestClient, crud: CrudCaller) -> None:
    made = make_order(api, crud, count=1)
    view = made.reader.view().json()
    assert view["status"] == ORDER_WORDS["draft"]
    [item] = view["items"]
    assert item["status"] == ITEM_WORDS["quoted"]
    [rev] = item["revisions"]
    assert rev["number"] == 1 and rev["can_answer"] is True and rev["answer"] is None
    assert item["quote"]["unit_price_minor"] == 1250
    assert item["quote"]["total_minor"] == 1250
    # A together item's price is accepted with its design, not on its own.
    assert item["quote"]["can_accept"] is False
    assert view["can_cancel"] is True
    assert view["payments"] == [] and view["shipments"] == []


# -- the allowlist ---------------------------------------------------------------------


def _fields(model: type[BaseModel], prefix: str = "") -> dict[str, set[str]]:
    """Each model's field names, by its dotted path from the view."""
    found: dict[str, set[str]] = {prefix: set(model.model_fields)}
    for name, field in model.model_fields.items():
        for inner in _models(field.annotation):
            found |= _fields(inner, f"{prefix}.{name}" if prefix else name)
    return found


def _models(annotation: Any) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    if get_origin(annotation) is not None or get_args(annotation):
        return [m for arg in get_args(annotation) for m in _models(arg)]
    return []


def test_the_view_is_allowlisted() -> None:
    """A field added to the customer's view fails this until it is added to the edge's
    allowlist (`storefront/wire.py` `ORDER_VIEW_FIELDS`) too: the allowlist is the
    review that nothing the shop keeps to itself reaches a customer."""
    assert _fields(CustomerOrderView) == {k: set(v) for k, v in ORDER_VIEW_FIELDS.items()}
    words = {name for names in ORDER_VIEW_FIELDS.values() for name in names}
    for banned in ("cost", "margin", "markup", "rate", "notes", "event", "by", "provider"):
        assert not [w for w in words if banned in w.split("_")], banned


def test_the_answer_holds_only_allowlisted_fields(api: TestClient, crud: CrudCaller) -> None:
    made = make_order(api, crud)
    with _orders(api).store.transaction() as conn:
        opened = payments.insert_request(
            conn,
            order_id=made.order.id,
            kind="deposit",
            provider="fake",
            amount_minor=500,
            currency="USD",
            created_by=BY,
        )
        payments.set_request_link(
            conn,
            opened.id,
            provider_ref="ref-1",
            hosted_url="https://pay.example/1",
            link_error=None,
        )

    def walk(node: Any, path: str) -> None:
        if isinstance(node, list):
            for value in node:
                walk(value, path)
        elif isinstance(node, dict):
            assert set(node) <= set(ORDER_VIEW_FIELDS[path]), path
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else key)

    view = made.reader.view().json()
    walk(view, "")
    assert "ref-1" not in str(view)


# -- payments and shipments -------------------------------------------------------------


def test_the_view_shows_pay_links_and_paid_requests(api: TestClient, crud: CrudCaller) -> None:
    made = make_order(api, crud)
    with _orders(api).store.transaction() as conn:
        first = payments.insert_request(
            conn,
            order_id=made.order.id,
            kind="deposit",
            provider="fake",
            amount_minor=500,
            currency="USD",
            created_by=BY,
        )
        payments.set_request_link(
            conn, first.id, provider_ref="r1", hosted_url="https://pay.example/1", link_error=None
        )
        second = payments.insert_request(
            conn,
            order_id=made.order.id,
            kind="balance",
            provider="fake",
            amount_minor=750,
            currency="USD",
            created_by=BY,
        )
        payments.set_request_link(
            conn, second.id, provider_ref="r2", hosted_url="https://pay.example/2", link_error=None
        )
        payments.set_request_status(conn, second.id, "paid")
    shown = made.reader.view().json()["payments"]
    assert [(p["kind"], p["amount_minor"], p["status"], p["pay_url"]) for p in shown] == [
        ("deposit", 500, "open", "https://pay.example/1"),
        ("balance", 750, "paid", None),
    ]


# -- answers -----------------------------------------------------------------------------


def test_a_write_without_an_idempotency_key_is_428(api: TestClient, crud: CrudCaller) -> None:
    made = make_order(api, crud)
    [item] = items_of(api, made)
    r = made.reader.answer(newest(api, item).id, "accepted", key=None)
    assert r.status_code == 428
    assert made.reader.cancel(key=None).status_code == 428
    assert newest(api, item).answer is None


def test_two_answers_to_one_revision(api: TestClient, crud: CrudCaller) -> None:
    made = make_order(api, crud, count=2)
    a, _b = items_of(api, made)
    rev = newest(api, a)
    first = made.reader.answer(rev.id, "accepted", key="tab-1")
    assert first.status_code == 200, first.text
    # The same key again is the same answer.
    again = made.reader.answer(rev.id, "accepted", key="tab-1")
    assert again.status_code == 200
    # Another tab's answer comes second.
    other = made.reader.answer(rev.id, "changes_requested", "bigger", key="tab-2")
    assert other.status_code == 409 and slug(other) == "revision_answered"
    assert newest(api, a).answer == "accepted"


def test_asking_for_changes_needs_a_note(api: TestClient, crud: CrudCaller) -> None:
    made = make_order(api, crud)
    [item] = items_of(api, made)
    rev = newest(api, item)
    r = made.reader.answer(rev.id, "changes_requested", "  ", key="k1")
    assert r.status_code == 422 and slug(r) == "note"
    bad = made.reader.answer(rev.id, "superseded", key="k2")
    assert bad.status_code == 422 and slug(bad) == "answer_body_invalid"
    ok = made.reader.answer(rev.id, "changes_requested", "Make it <b>bigger</b>", key="k3")
    assert ok.status_code == 200
    [shown] = ok.json()["items"][0]["revisions"]
    assert (shown["answer"], shown["answer_note"]) == ("changes_requested", "Make it <b>bigger</b>")


def test_the_last_acceptance_accepts_the_order_as_the_customer(
    api: TestClient, crud: CrudCaller
) -> None:
    made = make_order(api, crud, count=2)
    a, b = items_of(api, made)
    assert made.reader.answer(newest(api, a).id, "accepted", key="k1").status_code == 200
    assert _orders(api).store.order(made.order.id).status == "draft"  # type: ignore[union-attr]
    last = made.reader.answer(newest(api, b).id, "accepted", key="k2")
    assert last.status_code == 200, last.text
    assert last.json()["status"] == ORDER_WORDS["accepted"]
    events = _orders(api).store.events(made.order.id)
    accepted = [e for e in events if e.kind == "order.status" and e.detail["to"] == "accepted"]
    [by] = [e.by for e in accepted]
    [link] = rows(api, "SELECT id FROM storefront_order_links")
    assert by.kind == "customer" and by.principal == f"customer:{made.order.customer_id}"
    assert by.session == f"link:{link['id']}"


def test_a_design_first_price_is_the_customers_to_accept(api: TestClient, crud: CrudCaller) -> None:
    made = make_order(api, crud, approval="design_first")
    [item] = items_of(api, made)
    [quote] = _orders(api).store.quotes(item.id)
    early = made.reader.accept(quote.id, key="k0")
    assert early.status_code == 409 and slug(early) == "revision_not_accepted"
    view = made.reader.answer(newest(api, item).id, "accepted", key="k1").json()
    assert view["items"][0]["quote"]["can_accept"] is True
    r = made.reader.accept(quote.id, key="k2")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == ORDER_WORDS["accepted"]


# -- cancel ------------------------------------------------------------------------------


def test_the_customer_cancels_before_anything_is_approved(
    api: TestClient, crud: CrudCaller
) -> None:
    made = make_order(api, crud)
    r = made.reader.cancel("Changed my mind", key="k1")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == ORDER_WORDS["cancelled"]
    assert r.json()["can_cancel"] is False
    [event] = [
        e
        for e in _orders(api).store.events(made.order.id)
        if e.kind == "order.status" and e.detail["to"] == "cancelled"
    ]
    assert event.by.kind == "customer" and event.detail["reason"] == "Changed my mind"


def test_cancel_is_refused_after_an_approval(api: TestClient, crud: CrudCaller) -> None:
    made = make_order(api, crud, count=2)
    a, _b = items_of(api, made)
    made.reader.answer(newest(api, a).id, "accepted", key="k1")
    assert made.reader.view().json()["can_cancel"] is False
    r = made.reader.cancel("too late", key="k2")
    assert r.status_code == 409 and slug(r) == "order_approved"
    assert _orders(api).store.order(made.order.id).status == "draft"  # type: ignore[union-attr]


def test_the_ledger_never_keeps_the_token(api: TestClient, crud: CrudCaller) -> None:
    made = make_order(api, crud)
    [item] = items_of(api, made)
    made.reader.answer(newest(api, item).id, "changes_requested", "wider", key="k1")
    kept = rows(api, "SELECT body::text AS body FROM crud_requests")
    assert kept and not [r for r in kept if made.token in r["body"]]


# -- pictures ----------------------------------------------------------------------------


@pytest.fixture
def s3_api(
    settings: Settings,
    s3: S3Settings,  # noqa: F811
    tmp_path: Path,
) -> Iterator[TestClient]:
    on_s3 = settings.model_copy(
        update={
            "store_backend": "s3",
            "store_s3_endpoint": s3.endpoint,
            "store_s3_bucket": s3.bucket,
            "store_s3_prefix": s3.prefix,
            "store_s3_access_key_id": s3.access_key_id,
            "store_s3_secret_access_key": s3.secret_access_key,
            "secret_key_file": _kek(tmp_path),
        }
    )
    with TestClient(create_app(on_s3)) as client:
        yield client


def test_a_revisions_pictures_are_presigned(s3_api: TestClient) -> None:
    crud = CrudCaller(s3_api)
    made = make_order(s3_api, crud)
    [item] = made.reader.view().json()["items"]
    [picture] = item["revisions"][0]["pictures"]
    assert picture["kind"] == "image" and picture["filename"] == "render.png"
    got = httpx.get(picture["url"])
    assert got.status_code == 200 and got.content == PNG
    assert got.headers["content-type"] == "image/png"
    # A second view reuses the copy it made.
    again = made.reader.view().json()["items"][0]["revisions"][0]["pictures"]
    assert len(again) == 1


def test_the_crud_side_lists_the_order_area(crud: CrudCaller) -> None:
    assert "order" in crud.get("/areas").json()["areas"]
