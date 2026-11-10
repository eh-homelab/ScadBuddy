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

from scadbuddy.api import orders as orders_api
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.main import create_app
from scadbuddy.orders.attachments import keep_file
from scadbuddy.orders.lifecycle import Lifecycle, ManualQuoteInput, NewModelItem
from scadbuddy.orders.models import Actor, Order, OrderItem, Revision
from scadbuddy.orders.numbers import next_number
from scadbuddy.orders.storefront import order_page
from scadbuddy.orders.storefront.accounts import SignedIn, account_owns_order, attach_order
from scadbuddy.orders.storefront.links import revoke_order_links
from scadbuddy.orders.storefront.mail import issue_link
from scadbuddy.orders.storefront.order_page import ITEM_WORDS, ORDER_WORDS, customer_file_key
from scadbuddy.orders.storefront.visitors import VisitorStore, visitor_actor
from scadbuddy.orders.storefront.wire import CustomerOrderView
from scadbuddy.payments import store as payments
from scadbuddy.payments.fake import FAKE
from scadbuddy.store.content import ContentStore
from scadbuddy.storefront.wire import ORDER_VIEW_FIELDS, SESSION_HEADER, STOREFRONT_PROBLEMS
from tests.orders.storefront.test_accounts import sign_in
from tests.orders.storefront.test_drafts import CRUD, CrudCaller, _orders, _state
from tests.orders.test_lifecycle import resolved
from tests.support.store import s3  # noqa: F401  (fixture)

pytestmark = pytest.mark.requires_postgres
FAKE_CONNECT = {"settings": {"account": "a"}, "secrets": {"signing_secret": "s3cret"}}

BY = Actor(kind="browser")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _kek(tmp_path: Path) -> Path:
    kek = tmp_path / "kek"
    kek.write_text(base64.b64encode(os.urandom(32)).decode())
    return kek


@pytest.fixture
def api(settings: Settings, tmp_path: Path) -> Iterator[TestClient]:
    """The app with a KEK: the order link's token is sealed under it."""
    update = {"secret_key_file": _kek(tmp_path), "payments_fake_provider": True}
    with TestClient(create_app(settings.model_copy(update=update))) as client:
        yield client


def connect_fake(api: TestClient) -> None:
    """The fake provider connected, so a cancelled request's page is closed through it."""
    connected = api.put("/api/v1/payments/providers/fake", json=FAKE_CONNECT)
    assert connected.status_code == 200, connected.text


@pytest.fixture
def crud(api: TestClient) -> CrudCaller:
    return CrudCaller(api)


class Reader:
    """The edge's calls for one reader: a link's token, or (``order_id``) a signed-in
    account's order, in the body, never a path; the store session as the edge sends it."""

    def __init__(
        self,
        crud: CrudCaller,
        token: Any = None,
        *,
        order_id: str | None = None,
        session: str | None = None,
    ) -> None:
        self.crud = crud
        self.token = token
        self.order_id = order_id
        self.session = session

    def _who(self) -> dict[str, Any]:
        return {"order_id": self.order_id} if self.order_id is not None else {"token": self.token}

    def _post(self, path: str, body: dict[str, Any], key: str | None) -> Any:
        headers = {**self.crud.bearer}
        if key is not None:
            headers["Idempotency-Key"] = key
        if self.session is not None:
            headers[SESSION_HEADER] = self.session
        return self.crud.api.post(CRUD + path, json={**self._who(), **body}, headers=headers)

    def view(self) -> Any:
        return self._post("/order/view", {}, None)

    def answer(self, revision_id: str, answer: str, note: str = "", *, key: str | None) -> Any:
        body = {"answer": answer, "note": note}
        return self._post(f"/order/revisions/{revision_id}/answer", body, key)

    def accept(self, quote_id: str, *, key: str | None) -> Any:
        return self._post(f"/order/quotes/{quote_id}/accept", {}, key)

    def cancel(self, reason: str = "", *, key: str | None) -> Any:
        return self._post("/order/cancel", {"reason": reason}, key)


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
    api: TestClient,
    order: Order,
    item: OrderItem,
    rev: Revision,
    data: bytes = PNG,
    *,
    attachment_id: str | None = None,
    role: str = "render",
    caption: str = "",
    kind: str = "image",
    media_type: str = "image/png",
) -> None:
    digest = sha256(data).hexdigest()
    assert api.portal is not None
    key = api.portal.call(keep_file, _state(api).store.blobs, io.BytesIO(data), digest)
    _orders(api).lifecycle.add_attachment(
        order.id,
        item.id,
        BY,
        attachment_id=attachment_id or f"att-{rev.id}",
        kind=kind,  # type: ignore[arg-type]
        role=role,  # type: ignore[arg-type]
        filename="internal-name-v3.png",
        media_type=media_type,
        size_bytes=len(data),
        sha256=digest,
        blob_key=key,
        caption=caption,
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


def open_deposit(api: TestClient, made: Any) -> str:
    """An open fake-provider deposit request on the order, with a hosted page ``r1``."""
    with _orders(api).store.transaction() as conn:
        deposit = payments.insert_request(
            conn,
            order_id=made.order.id,
            kind="deposit",
            provider="fake",
            amount_minor=500,
            currency="USD",
            created_by=BY,
        )
        payments.set_request_link(
            conn, deposit.id, provider_ref="r1", hosted_url="https://pay.example/1", link_error=None
        )
    return deposit.id


def test_the_customers_last_accept_closes_an_open_deposit_requests_page(
    api: TestClient, crud: CrudCaller, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5923 review: the accept that accepts the order cancels its open requests, and their
    provider pages close right after the commit; the order's projects are then made."""
    monkeypatch.setattr(FAKE, "cancelled", [])
    connect_fake(api)
    ensured: list[str] = []

    async def ensure(store: object, integrations: object, order_id: str) -> None:
        ensured.append(order_id)

    monkeypatch.setattr(orders_api, "ensure_projects", ensure)
    made = make_order(api, crud)
    with _orders(api).store.transaction() as conn:
        deposit = payments.insert_request(
            conn,
            order_id=made.order.id,
            kind="deposit",
            provider="fake",
            amount_minor=500,
            currency="USD",
            created_by=BY,
        )
        payments.set_request_link(
            conn, deposit.id, provider_ref="r1", hosted_url="https://pay.example/1", link_error=None
        )
    [item] = items_of(api, made)
    last = made.reader.answer(newest(api, item).id, "accepted", key="k1")
    assert last.json()["status"] == ORDER_WORDS["accepted"]
    with _orders(api).store.transaction() as conn:
        found = {r.id: r.status for r in payments.requests_for_order(conn, made.order.id)}
    assert found[deposit.id] == "cancelled"
    assert FAKE.cancelled == ["r1"]
    assert ensured == [made.order.id]


def test_the_customers_cancel_closes_an_open_deposit_requests_page(
    api: TestClient, crud: CrudCaller, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FAKE, "cancelled", [])
    connect_fake(api)
    made = make_order(api, crud)
    deposit = open_deposit(api, made)
    r = made.reader.cancel(key="k1")
    assert r.status_code == 200, r.text
    with _orders(api).store.transaction() as conn:
        found = {q.id: q.status for q in payments.requests_for_order(conn, made.order.id)}
    assert found[deposit] == "cancelled"
    assert FAKE.cancelled == ["r1"]


def test_a_paused_order_offers_no_answer(api: TestClient, crud: CrudCaller) -> None:
    """#5965 review: a design on an order the shop put on hold cannot be answered (the
    lifecycle refuses it, ``order_on_hold``), so the page does not offer it."""
    made = make_order(api, crud)
    _orders(api).lifecycle.hold(made.order.id, BY)
    [rev] = made.reader.view().json()["items"][0]["revisions"]
    assert rev["can_answer"] is False


def _unname_quote(api: TestClient, item: OrderItem, after_send: bool = True) -> None:
    """The item's quote names no design; its `created_at` is after the first design's
    send (the quote was made then) unless ``after_send`` is false (made before it)."""
    stamp = "now()" if after_send else "now() - interval '1 hour'"
    with _orders(api).store.transaction() as conn:
        conn.execute(
            f"UPDATE order_item_quotes SET revision_id = NULL, created_at = {stamp}"
            " WHERE order_item_id = %s",
            (item.id,),
        )


def test_a_price_that_names_no_design_shows_with_the_newest_sent_one(
    api: TestClient, crud: CrudCaller
) -> None:
    """#5965 review: a manual price may name no design; it prices the item as the
    customer saw it when the price was made, so it shows once a design was sent by then."""
    made = make_order(api, crud)
    [item] = items_of(api, made)
    _unname_quote(api, item)
    quote = made.reader.view().json()["items"][0]["quote"]
    assert quote is not None and quote["unit_price_minor"] == 1250


def test_a_price_made_before_any_design_was_sent_is_hidden(
    api: TestClient, crud: CrudCaller
) -> None:
    made = make_order(api, crud)
    [item] = items_of(api, made)
    _unname_quote(api, item, after_send=False)
    assert made.reader.view().json()["items"][0]["quote"] is None


def test_a_price_that_names_no_design_does_not_follow_a_design_sent_after_it(
    api: TestClient, crud: CrudCaller
) -> None:
    """The price was made while design 1 was the newest sent, so it shows from then on,
    whatever is sent later. Accepting it follows `Lifecycle.accept_quote`: a price that
    names no design is accepted against the item's current design, here design 2,
    unanswered, so the page does not offer it, and the lifecycle refuses it too."""
    made = make_order(api, crud, approval="design_first")
    [item] = items_of(api, made)
    first = newest(api, item)
    _unname_quote(api, item)
    life = _orders(api).lifecycle
    assert made.reader.answer(first.id, "accepted", key="k1").status_code == 200
    life.add_revision(made.order.id, item.id, BY, note="again")
    second = newest(api, item)
    attach(api, made.order, item, second)
    life.send_revision(made.order.id, item.id, second.id, BY)
    [rev1, rev2] = made.reader.view().json()["items"][0]["revisions"]
    assert (rev1["answer"], rev2["answer"]) == ("accepted", None)
    quote = made.reader.view().json()["items"][0]["quote"]
    assert quote is not None and quote["unit_price_minor"] == 1250
    assert quote["can_accept"] is False
    [row] = _orders(api).store.quotes(item.id)
    refused = made.reader.accept(row.id, key="k2")
    assert refused.status_code == 409 and slug(refused) == "revision_not_accepted"


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


def test_cancel_is_refused_once_a_request_is_paid(api: TestClient, crud: CrudCaller) -> None:
    made = make_order(api, crud)
    with _orders(api).store.transaction() as conn:
        deposit = payments.insert_request(
            conn,
            order_id=made.order.id,
            kind="deposit",
            provider="fake",
            amount_minor=500,
            currency="USD",
            created_by=BY,
        )
        payments.set_request_status(conn, deposit.id, "paid")
    assert made.reader.view().json()["can_cancel"] is False
    r = made.reader.cancel(key="k1")
    assert r.status_code == 409 and slug(r) == "order_paid"
    assert _orders(api).store.order(made.order.id).status == "draft"  # type: ignore[union-attr]


def test_cancel_is_refused_once_a_payment_is_recorded(api: TestClient, crud: CrudCaller) -> None:
    made = make_order(api, crud)
    _orders(api).lifecycle.add_payment(made.order.id, BY, amount_minor=500, method="cash")
    assert made.reader.view().json()["can_cancel"] is False
    r = made.reader.cancel(key="k1")
    assert r.status_code == 409 and slug(r) == "order_paid"


def test_an_accept_and_a_cancel_sent_again_are_done_once(api: TestClient, crud: CrudCaller) -> None:
    made = make_order(api, crud, approval="design_first")
    [item] = items_of(api, made)
    made.reader.answer(newest(api, item).id, "accepted", key="k1")
    [quote] = _orders(api).store.quotes(item.id)
    first = made.reader.accept(quote.id, key="a1")
    again = made.reader.accept(quote.id, key="a1")
    assert (first.status_code, again.status_code) == (200, 200)
    assert first.json() == again.json()
    other = make_order(api, crud, visitor="c")
    one = other.reader.cancel("no", key="c1")
    two = other.reader.cancel("no", key="c1")
    assert (one.status_code, two.status_code) == (200, 200)
    cancels = [
        e
        for e in _orders(api).store.events(other.order.id)
        if e.kind == "order.status" and e.detail["to"] == "cancelled"
    ]
    assert len(cancels) == 1


def test_a_revision_shows_its_colours(api: TestClient, crud: CrudCaller) -> None:
    made = make_order(api, crud)
    [rev] = made.reader.view().json()["items"][0]["revisions"]
    assert rev["colours"] == [{"slot": 0, "colour": "#ff0000", "name": None}]


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


def _puts(monkeypatch: pytest.MonkeyPatch, fail: bool = False) -> list[str | None]:
    """Each copy the view puts on the content store, by its key; ``fail`` makes it raise."""
    keys: list[str | None] = []
    real = ContentStore.put

    async def put(self: ContentStore, *args: Any, **kwargs: Any) -> Any:
        keys.append(kwargs.get("key"))
        if fail:
            raise OSError("the store is down")
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(ContentStore, "put", put)
    return keys


def test_a_revisions_pictures_are_presigned_once(
    s3_api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    crud = CrudCaller(s3_api)
    made = make_order(s3_api, crud)
    puts = _puts(monkeypatch)
    [item] = made.reader.view().json()["items"]
    [picture] = item["revisions"][0]["pictures"]
    # The shop's own file name never reaches the customer.
    assert picture["kind"] == "image" and picture["caption"] == "Design 1, picture 1"
    assert "internal-name" not in str(item)
    got = httpx.get(picture["url"])
    assert got.status_code == 200 and got.content == PNG
    assert got.headers["content-type"] == "image/png"
    # A second view reuses the copy the first made.
    again = made.reader.view().json()["items"][0]["revisions"][0]["pictures"]
    assert len(again) == 1 and again[0]["url"]
    assert puts == [customer_file_key(sha256(PNG).hexdigest())]


def test_a_shared_picture_is_held_by_every_attachment_that_shows_it(s3_api: TestClient) -> None:
    crud = CrudCaller(s3_api)
    made = make_order(s3_api, crud, count=2)
    made.reader.view()
    key = customer_file_key(sha256(PNG).hexdigest())
    held = rows(s3_api, "SELECT holder_id FROM blob_refs WHERE key = %s ORDER BY holder_id", key)
    revisions = [newest(s3_api, item).id for item in items_of(s3_api, made)]
    assert [r["holder_id"] for r in held] == sorted(f"att-{r}" for r in revisions)


def test_only_the_designs_own_files_are_shown(s3_api: TestClient) -> None:
    crud = CrudCaller(s3_api)
    made = make_order(s3_api, crud)
    [item] = items_of(s3_api, made)
    rev = newest(s3_api, item)
    attach(s3_api, made.order, item, rev, PNG + b"x", attachment_id="own", role="other")
    attach(
        s3_api,
        made.order,
        item,
        rev,
        PNG + b"y",
        attachment_id="print",
        role="test_print",
        caption="The test print",
    )
    [shown] = made.reader.view().json()["items"][0]["revisions"]
    assert [p["caption"] for p in shown["pictures"]] == ["Design 1, picture 1", "The test print"]


def test_a_failed_copy_lists_the_picture_without_a_url(
    s3_api: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    crud = CrudCaller(s3_api)
    made = make_order(s3_api, crud)
    _puts(monkeypatch, fail=True)
    r = made.reader.view()
    assert r.status_code == 200, r.text
    [picture] = r.json()["items"][0]["revisions"][0]["pictures"]
    assert picture["url"] is None
    assert made.token not in caplog.text


def test_a_picture_past_the_size_bound_is_not_copied(
    s3_api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    crud = CrudCaller(s3_api)
    made = make_order(s3_api, crud)
    monkeypatch.setattr(order_page, "MAX_PICTURE_COPY_BYTES", len(PNG) - 1)
    puts = _puts(monkeypatch)
    [picture] = made.reader.view().json()["items"][0]["revisions"][0]["pictures"]
    assert picture["url"] is None and puts == []


GLB = b"glTF" + (2).to_bytes(4, "little") + (12).to_bytes(4, "little")


def test_a_revisions_3d_preview_is_presigned_for_the_viewer(s3_api: TestClient) -> None:
    crud = CrudCaller(s3_api)
    made = make_order(s3_api, crud)
    [item] = items_of(s3_api, made)
    attach(
        s3_api,
        made.order,
        item,
        newest(s3_api, item),
        GLB,
        attachment_id="glb",
        kind="other",
        media_type="model/gltf-binary",
    )
    pictures = made.reader.view().json()["items"][0]["revisions"][0]["pictures"]
    [model] = [p for p in pictures if p["kind"] == "model"]
    assert model["caption"] == "Design 1, picture 2"
    got = httpx.get(model["url"])
    assert got.status_code == 200 and got.content == GLB
    assert got.headers["content-type"] == "model/gltf-binary"


def test_the_crud_side_lists_the_order_area(crud: CrudCaller) -> None:
    assert "order" in crud.get("/areas").json()["areas"]


# -- the signed-in account path (``/o?order=<id>``) ------------------------------------------


def signed_in(api: TestClient, email: str = "Ada@Example.com") -> SignedIn:
    with _orders(api).store.transaction() as conn:
        return sign_in(conn, email)


def owns(api: TestClient, account_id: str, order_id: str) -> bool:
    with _orders(api).store.transaction() as conn:
        return account_owns_order(conn, account_id, order_id)


def test_a_token_view_while_signed_in_attaches_the_order(api: TestClient, crud: CrudCaller) -> None:
    made = make_order(api, crud)
    me = signed_in(api)
    assert not owns(api, me.account.id, made.order.id)
    assert Reader(crud, made.token, session=me.session_token).view().status_code == 200
    assert owns(api, me.account.id, made.order.id)
    # A view with no session attaches nothing.
    other = make_order(api, crud, visitor="o")
    other.reader.view()
    assert not owns(api, me.account.id, other.order.id)


def test_an_account_reads_its_own_order(api: TestClient, crud: CrudCaller) -> None:
    made = make_order(api, crud)
    me = signed_in(api)
    Reader(crud, made.token, session=me.session_token).view()
    mine = Reader(crud, order_id=made.order.id, session=me.session_token).view()
    assert mine.status_code == 200, mine.text
    assert mine.json()["number"] == made.order.number


def test_an_order_another_account_owns_or_none_is_an_empty_404(
    api: TestClient, crud: CrudCaller
) -> None:
    made = make_order(api, crud)
    owner = signed_in(api)
    Reader(crud, made.token, session=owner.session_token).view()
    stranger = signed_in(api, "bob@example.com")
    [item] = items_of(api, made)
    for reader in (
        Reader(crud, order_id=made.order.id, session=stranger.session_token),
        Reader(crud, order_id=made.order.id),
        Reader(crud, order_id=made.order.id, session="not-a-session"),
        Reader(crud, order_id="nope", session=owner.session_token),
    ):
        for r in (
            reader.view(),
            reader.answer(newest(api, item).id, "accepted", key="k1"),
            reader.cancel(key="k2"),
        ):
            assert (r.status_code, r.content) == (404, b"")
    assert newest(api, item).answer is None


def test_an_account_answers_and_accepts_as_the_orders_customer(
    api: TestClient, crud: CrudCaller
) -> None:
    made = make_order(api, crud, count=2)
    me = signed_in(api)
    Reader(crud, made.token, session=me.session_token).view()
    mine = Reader(crud, order_id=made.order.id, session=me.session_token)
    a, b = items_of(api, made)
    first = mine.answer(newest(api, a).id, "accepted", key="k1")
    assert first.status_code == 200, first.text
    assert mine.answer(newest(api, a).id, "accepted", key="k1").status_code == 200
    last = mine.answer(newest(api, b).id, "accepted", key="k2")
    assert last.json()["status"] == ORDER_WORDS["accepted"]
    accepted = [
        e
        for e in _orders(api).store.events(made.order.id)
        if e.kind == "order.status" and e.detail["to"] == "accepted"
    ]
    [by] = [e.by for e in accepted]
    assert by.principal == f"customer:{made.order.customer_id}"
    assert by.session == f"account:{me.account.id}"


def test_a_cart_the_account_owns_is_an_empty_404(api: TestClient, crud: CrudCaller) -> None:
    me = signed_in(api)
    orders = _orders(api)
    found = VisitorStore(_state(api).projection.pool).ensure("c" * 43)
    cart = orders.lifecycle.ensure_draft(found.id, visitor_actor(found), currency="USD")
    with orders.store.transaction() as conn:
        attach_order(conn, me.account.id, cart.id)
    assert owns(api, me.account.id, cart.id)
    reader = Reader(crud, order_id=cart.id, session=me.session_token)
    for r in (reader.view(), reader.cancel(key="k1")):
        assert (r.status_code, r.content) == (404, b"")


def test_one_key_of_two_accounts_never_replays_the_others_reply(
    api: TestClient, crud: CrudCaller
) -> None:
    a_made = make_order(api, crud, visitor="a")
    b_made = make_order(api, crud, visitor="b")
    a, b = signed_in(api), signed_in(api, "bob@example.com")
    Reader(crud, a_made.token, session=a.session_token).view()
    Reader(crud, b_made.token, session=b.session_token).view()
    ra = Reader(crud, order_id=a_made.order.id, session=a.session_token)
    rb = Reader(crud, order_id=b_made.order.id, session=b.session_token)
    first = ra.cancel("changed my mind", key="shared")
    assert first.status_code == 200, first.text
    second = rb.cancel("changed my mind", key="shared")
    assert second.status_code != 200 or second.json() != first.json()
    assert _orders(api).store.order(b_made.order.id).status != "cancelled"  # type: ignore[union-attr]


def link_rows(api: TestClient, account_id: str) -> list[dict[str, Any]]:
    return rows(
        api,
        "SELECT order_id, link_id FROM storefront_account_orders WHERE account_id = %s",
        account_id,
    )


def live_link_id(api: TestClient, order_id: str) -> str:
    [row] = rows(
        api,
        "SELECT id FROM storefront_order_links WHERE order_id = %s AND revoked_at IS NULL",
        order_id,
    )
    return str(row["id"])


def test_a_revoke_removes_a_link_made_attachment(api: TestClient, crud: CrudCaller) -> None:
    made = make_order(api, crud)
    me = signed_in(api)
    Reader(crud, made.token, session=me.session_token).view()
    assert link_rows(api, me.account.id) == [
        {"order_id": made.order.id, "link_id": live_link_id(api, made.order.id)}
    ]
    with _orders(api).store.transaction() as conn:
        revoke_order_links(conn, made.order.id)
    assert link_rows(api, me.account.id) == []
    gone = Reader(crud, order_id=made.order.id, session=me.session_token).view()
    assert (gone.status_code, gone.content) == (404, b"")


def test_a_submit_made_attachment_survives_a_revoke(api: TestClient, crud: CrudCaller) -> None:
    made = make_order(api, crud)
    me = signed_in(api)
    with _orders(api).store.transaction() as conn:
        attach_order(conn, me.account.id, made.order.id)
    with _orders(api).store.transaction() as conn:
        revoke_order_links(conn, made.order.id)
    assert link_rows(api, me.account.id) == [{"order_id": made.order.id, "link_id": None}]
    assert Reader(crud, order_id=made.order.id, session=me.session_token).view().status_code == 200


def test_a_submit_row_is_not_overwritten_by_a_later_link_attach(
    api: TestClient, crud: CrudCaller
) -> None:
    made = make_order(api, crud)
    me = signed_in(api)
    with _orders(api).store.transaction() as conn:
        attach_order(conn, me.account.id, made.order.id)
    assert Reader(crud, made.token, session=me.session_token).view().status_code == 200
    assert link_rows(api, me.account.id) == [{"order_id": made.order.id, "link_id": None}]
    with _orders(api).store.transaction() as conn:
        revoke_order_links(conn, made.order.id)
    assert owns(api, me.account.id, made.order.id)


def test_after_a_link_is_revoked_a_view_through_another_link_attaches_again(
    api: TestClient, crud: CrudCaller
) -> None:
    made = make_order(api, crud)
    me = signed_in(api)
    Reader(crud, made.token, session=me.session_token).view()
    orders = _orders(api)
    with orders.store.transaction() as conn:
        token_b = issue_link(conn, orders.lifecycle.kek, made.order.id, made.order.label, "a@b.co")
    assert link_rows(api, me.account.id) == []  # Link A is revoked; so is its attachment.
    assert Reader(crud, made.token, session=me.session_token).view().status_code == 404
    assert Reader(crud, token_b, session=me.session_token).view().status_code == 200
    assert link_rows(api, me.account.id) == [
        {"order_id": made.order.id, "link_id": live_link_id(api, made.order.id)}
    ]


def test_the_view_that_answers_a_write_does_not_attach(api: TestClient, crud: CrudCaller) -> None:
    made = make_order(api, crud)
    me = signed_in(api)
    r = Reader(crud, made.token, session=me.session_token).cancel(key="k1")
    assert r.status_code == 200, r.text
    assert link_rows(api, me.account.id) == []
    assert not owns(api, me.account.id, made.order.id)


def test_an_attach_through_a_revoked_link_inserts_nothing(
    api: TestClient, crud: CrudCaller
) -> None:
    """Security review: an attach that lost the race with a revoke finds the link revoked
    under its FOR SHARE lock and attaches nothing."""
    made = make_order(api, crud)
    me = signed_in(api)
    link = live_link_id(api, made.order.id)
    with _orders(api).store.transaction() as conn:
        revoke_order_links(conn, made.order.id)
    with _orders(api).store.transaction() as conn:
        attach_order(conn, me.account.id, made.order.id, link)
    assert link_rows(api, me.account.id) == []
    assert not owns(api, me.account.id, made.order.id)


def test_a_row_whose_link_is_revoked_owns_nothing(api: TestClient, crud: CrudCaller) -> None:
    """Security review: even a link-made row left behind reads as not owned once its link
    is revoked."""
    made = make_order(api, crud)
    me = signed_in(api)
    link = live_link_id(api, made.order.id)
    with _orders(api).store.transaction() as conn:
        attach_order(conn, me.account.id, made.order.id, link)
        conn.execute(
            "UPDATE storefront_order_links SET revoked_at = clock_timestamp(),"
            " token_sealed = NULL, dek_sealed = NULL, kek_id = NULL WHERE id = %s",
            (link,),
        )
    assert link_rows(api, me.account.id) == [{"order_id": made.order.id, "link_id": link}]
    assert not owns(api, me.account.id, made.order.id)
    gone = Reader(crud, order_id=made.order.id, session=me.session_token).view()
    assert (gone.status_code, gone.content) == (404, b"")
