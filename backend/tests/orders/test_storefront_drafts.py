"""Storefront drafts (#5282, spec 2026-11-02 §5.4, §6.1): a visitor's cart is an order
with no customer and no number until it is submitted, one per visitor. The visitor row
holds only the sha256 of the visitor id."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any, cast

import psycopg
import pytest
from psycopg import Connection
from psycopg.rows import DictRow, dict_row

from scadbuddy.core.events import OrderChanged, OrderItemChanged
from scadbuddy.core.problems import ApiError
from scadbuddy.orders.lifecycle import (
    LifecycleError,
    ManualQuoteInput,
    NewCustomItem,
    NewModelItem,
)
from scadbuddy.orders.models import Order, OrderFilter
from scadbuddy.orders.printing import print_check
from scadbuddy.orders.printing import test_print_check as check_test_print
from scadbuddy.orders.quote import SliceQuoteRequest, quote_check
from scadbuddy.orders.store import OrderStore
from scadbuddy.orders.storefront.visitors import Visitor, VisitorStore, visitor_actor
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop, resolved
from tests.orders.test_quote import DIALOG, POLICY

pytestmark = pytest.mark.requires_postgres


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def shop(rows: OrderStore) -> Shop:
    return Shop(rows)


@pytest.fixture
def visitors(pool: PgPool) -> VisitorStore:
    return VisitorStore(pool)


@pytest.fixture
def visitor(visitors: VisitorStore) -> Visitor:
    return visitors.ensure("v" * 43)


@pytest.fixture
def owner_conn(pg_conninfo: str, pool: PgPool) -> Iterator[Connection[DictRow]]:
    # ``pool`` first: it migrates the schema.
    with psycopg.connect(pg_conninfo, autocommit=True, row_factory=dict_row) as conn:
        yield conn


def order_counter(conn: Connection[DictRow]) -> list[Any]:
    return [r["last"] for r in conn.execute("SELECT last FROM order_numbers ORDER BY year")]


@pytest.fixture
def draft_with_item(shop: Shop, visitor: Visitor) -> Order:
    order = shop.lifecycle.ensure_draft(visitor.id, visitor_actor(visitor), currency="USD")
    shop.lifecycle.add_items(
        order.id, visitor_actor(visitor), [NewModelItem(resolved=resolved(), quantity=1)]
    )
    return order


def test_a_draft_needs_no_customer_and_no_number(
    shop: Shop, visitor: Visitor, owner_conn: Connection[DictRow]
) -> None:
    before = order_counter(owner_conn)
    by = visitor_actor(visitor)
    order = shop.lifecycle.ensure_draft(visitor.id, by, currency="USD")
    assert (order.source, order.customer_id, order.number, order.submitted_at) == (
        "storefront",
        None,
        None,
        None,
    )
    assert (order.storefront_visitor_id, order.status, order.created_by) == (
        visitor.id,
        "draft",
        by,
    )
    assert shop.lifecycle.ensure_draft(visitor.id, by, currency="USD").id == order.id
    assert order_counter(owner_conn) == before
    [created] = shop.rows.events(order.id)
    assert (created.kind, created.detail, created.by) == (
        "order.created",
        {"source": "storefront"},
        by,
    )
    assert shop.rows.draft_of(visitor.id) == order


def test_the_visitors_actor_names_its_row() -> None:
    now = datetime.now(UTC)
    actor = visitor_actor(Visitor(id="r1", created_at=now, last_seen_at=now))
    assert (actor.kind, actor.principal, actor.session) == (
        "visitor",
        "storefront_visitor:r1",
        None,
    )


def test_two_ensures_at_once_make_one_draft(
    shop: Shop, visitor: Visitor, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both read "no draft" before either inserts: the unique index refuses the second
    # insert, and that call answers with the winner.
    barrier = threading.Barrier(2)
    real = OrderStore.draft_of
    first_reads: set[int] = set()

    def draft_of(self: OrderStore, visitor_id: str, **kw: Any) -> Order | None:
        found = real(self, visitor_id, **kw)
        me = threading.get_ident()
        if me not in first_reads:
            first_reads.add(me)
            barrier.wait(timeout=10)
        return found

    monkeypatch.setattr(OrderStore, "draft_of", draft_of)
    made: list[Order] = []

    def ensure() -> None:
        made.append(shop.lifecycle.ensure_draft(visitor.id, visitor_actor(visitor), currency="USD"))

    threads = [threading.Thread(target=ensure) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(made) == 2 and made[0].id == made[1].id
    drafts = shop.rows.orders(OrderFilter(source="storefront", unsubmitted=True), None, 10)[0]
    assert [d.id for d in drafts] == [made[0].id]


def test_a_back_office_order_still_needs_its_customer_and_number(
    owner_conn: Connection[DictRow],
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        owner_conn.execute("INSERT INTO orders (currency, created_by) VALUES ('USD', '{}')")
    # A storefront order once submitted needs them too.
    with pytest.raises(psycopg.errors.CheckViolation):
        owner_conn.execute(
            "INSERT INTO orders (currency, created_by, source, submitted_at)"
            " VALUES ('USD', '{}', 'storefront', now())"
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        owner_conn.execute(
            "INSERT INTO orders (currency, created_by, source) VALUES ('USD', '{}', 'shop')"
        )


def test_one_draft_per_visitor(
    shop: Shop, visitor: Visitor, owner_conn: Connection[DictRow]
) -> None:
    shop.lifecycle.ensure_draft(visitor.id, visitor_actor(visitor), currency="USD")
    with pytest.raises(psycopg.errors.UniqueViolation):
        owner_conn.execute(
            "INSERT INTO orders (currency, created_by, source, storefront_visitor_id)"
            " VALUES ('USD', '{}', 'storefront', %s)",
            (visitor.id,),
        )


def test_the_visitor_is_found_by_its_token_hash_only(
    visitors: VisitorStore, owner_conn: Connection[DictRow]
) -> None:
    v = visitors.ensure("a" * 43)
    assert visitors.find("a" * 43) == v and visitors.find("b" * 43) is None
    row = owner_conn.execute("SELECT token_sha256 FROM storefront_visitors").fetchone()
    assert row is not None and row["token_sha256"] == sha256_hex("a" * 43)
    assert visitors.ensure("a" * 43).id == v.id


def test_find_touches_last_seen_at_at_most_once_a_minute(
    visitors: VisitorStore, owner_conn: Connection[DictRow]
) -> None:
    v = visitors.ensure("a" * 43)
    owner_conn.execute(
        "UPDATE storefront_visitors SET last_seen_at = now() - interval '30 seconds'"
    )
    recent = visitors.find("a" * 43)
    assert recent is not None and recent.last_seen_at < v.last_seen_at
    owner_conn.execute("UPDATE storefront_visitors SET last_seen_at = now() - interval '2 minutes'")
    touched = visitors.find("a" * 43)
    assert touched is not None and touched.last_seen_at > recent.last_seen_at
    assert touched.id == v.id


def _accept(shop: Shop, order: Order) -> object:
    return shop.lifecycle.accept_order(order.id, BY)


def _manual_quote(shop: Shop, order: Order) -> object:
    [item] = shop.rows.items(order.id)
    return shop.lifecycle.record_manual_quote(
        order.id, item.id, ManualQuoteInput(unit_price_minor=100, label="typed"), BY
    )


def _only_item(shop: Shop, order: Order) -> str:
    [item] = shop.rows.items(order.id)
    return item.id


def _custom_on_cart(shop: Shop, order: Order) -> str:
    """A custom item put on the cart by hand: no route may price one there."""
    with shop.rows.transaction() as conn:
        item = shop.rows.insert_custom_item(
            conn,
            order.id,
            title="Design",
            quantity=1,
            notes="",
            sku=None,
            requires_printing=False,
        )
    return item.id


ACTS: dict[str, Callable[[Shop, Order], object]] = {
    "accept": _accept,
    "manual_quote": _manual_quote,
    "accept_quote": lambda shop, order: shop.lifecycle.accept_quote(
        order.id, _only_item(shop, order), "any", BY
    ),
    "slice_quote": lambda shop, order: shop.lifecycle.record_slice_quote(
        order.id,
        _only_item(shop, order),
        cast(Any, None),
        [],
        sliced_sha256="0" * 64,
        sliced_blob_key="k",
        by=BY,
        revision_id=None,
    ),
    "claim_print": lambda shop, order: shop.lifecycle.claim_print(
        order.id,
        _only_item(shop, order),
        BY,
        request_id="r1",
        sliced_sha256=None,
        sliced_blob_key=None,
    ),
    "add_custom_item": lambda shop, order: shop.lifecycle.add_items(
        order.id, BY, [NewCustomItem(title="Design", quantity=1, unit_price_minor=500)]
    ),
    "add_custom_item_unaccepted": lambda shop, order: shop.lifecycle.add_items(
        order.id,
        BY,
        [NewCustomItem(title="Design", quantity=1, unit_price_minor=500, accept=False)],
    ),
    "price_custom_item": lambda shop, order: shop.lifecycle.update_item(
        order.id, _custom_on_cart(shop, order), BY, unit_price_minor=700
    ),
    "cancel": lambda shop, order: shop.lifecycle.cancel_order(order.id, BY),
    "hold": lambda shop, order: shop.lifecycle.hold(order.id, BY),
}


@pytest.mark.parametrize("act", list(ACTS))
def test_the_shop_cannot_act_on_an_unsubmitted_draft(
    shop: Shop, draft_with_item: Order, act: str
) -> None:
    events = shop.kinds(draft_with_item)
    revisions = [shop.rows.revisions(i.id) for i in shop.rows.items(draft_with_item.id)]
    with pytest.raises(LifecycleError) as refused:
        ACTS[act](shop, draft_with_item)
    assert refused.value.slug == "order_not_submitted"
    assert str(refused.value) == f"cart {draft_with_item.id} is not yet submitted"
    # The refusal wrote nothing: no quote, no audit row, no repaired revision.
    items = shop.rows.items(draft_with_item.id)
    assert all(shop.rows.quotes(i.id) == [] for i in items)
    assert shop.kinds(draft_with_item) == events
    assert [shop.rows.revisions(i.id) for i in items][: len(revisions)] == revisions
    after = shop.get(draft_with_item)
    assert (after.status, after.on_hold) == ("draft", False)


def test_a_refusal_names_a_cart_by_its_id(shop: Shop, draft_with_item: Order) -> None:
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.update_item(draft_with_item.id, "nobody", BY, title="T")
    assert str(refused.value) == f"no item nobody of order cart {draft_with_item.id}"
    assert draft_with_item.label == f"cart {draft_with_item.id}"
    assert shop.order().label.startswith("ORD-")


def test_a_bad_visitor_id_is_refused(visitors: VisitorStore) -> None:
    for bad in ("a" * 42, "a" * 44, "a" * 42 + "+", "a" * 42 + "=", ""):
        with pytest.raises(ValueError):
            visitors.ensure(bad)
        with pytest.raises(ValueError):
            visitors.find(bad)
    assert visitors.find("-_" + "A" * 41) is None


def test_the_list_hides_drafts_unless_asked(shop: Shop, draft_with_item: Order) -> None:
    placed = shop.order()

    def ids(**filters: Any) -> list[str]:
        return [o.id for o in shop.rows.orders(OrderFilter(**filters), None, 10)[0]]

    assert ids() == [placed.id]
    assert ids(source="back_office") == [placed.id]
    assert ids(source="storefront") == []
    assert ids(unsubmitted=True) == [draft_with_item.id]
    assert ids(source="storefront", unsubmitted=True) == [draft_with_item.id]
    assert ids(source="back_office", unsubmitted=True) == []
    assert placed.source == "back_office" and placed.number is not None


def test_the_quote_and_print_checks_refuse_an_unsubmitted_draft(
    shop: Shop, draft_with_item: Order
) -> None:
    [item] = shop.rows.items(draft_with_item.id)
    request = SliceQuoteRequest.model_validate(DIALOG)
    with pytest.raises(ApiError) as quoting:
        quote_check(shop.rows, POLICY, draft_with_item.id, item.id, request)
    # Nothing past the order's row is read: no outputs, no blobs.
    with pytest.raises(ApiError) as printing:
        asyncio.run(
            print_check(shop.rows, cast(Any, None), cast(Any, None), draft_with_item.id, item.id)
        )
    with pytest.raises(ApiError) as testing:
        asyncio.run(
            check_test_print(
                shop.lifecycle,
                cast(Any, None),
                cast(Any, None),
                draft_with_item.id,
                item.id,
                "any",
                by=BY,
            )
        )
    for refused in (quoting, printing, testing):
        assert refused.value.status == 409
        assert refused.value.type == "https://scadbuddy.dev/problems/orders/order_not_submitted"


def test_a_carts_events_say_it_is_a_cart(shop: Shop, draft_with_item: Order) -> None:
    # #5282: the flow triggers skip them (`flow_triggers/events.py` `trigger_event`).
    placed = shop.order()
    shop.item(placed)
    told = [e for e in shop.events if isinstance(e, OrderChanged | OrderItemChanged)]
    assert {e.cart for e in told if e.order_id == draft_with_item.id} == {True}
    assert {e.cart for e in told if e.order_id == placed.id} == {False}
