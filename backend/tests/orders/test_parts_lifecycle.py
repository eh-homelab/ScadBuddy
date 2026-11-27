"""An item's parts (#5543, job cost spec §11; #7067 Task 7): filled from its model's rule,
taken from inventory lots when it is fulfilled, given back when it is unfulfilled."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from scadbuddy.inventory.models import ItemInput, ReceiveLot, RecostResult
from scadbuddy.orders import store as store_module
from scadbuddy.orders.lifecycle import FulfilLine, FulfilmentInput, LifecycleError
from scadbuddy.orders.models import ColourSlot, OrderItem
from scadbuddy.orders.parts import (
    ItemPart,
    ItemPartsInput,
    PartNotFoundError,
    PartRuleInput,
    PartRuleLine,
    PartRuleWhen,
    order_parts,
)
from scadbuddy.orders.reference import ResolvedReference
from scadbuddy.orders.stock_start import WorkflowRefused
from scadbuddy.orders.store import OrderStore
from scadbuddy.render.pg_store import MIGRATIONS, migrate
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop, resolved
from tests.support.inventory_rows import ItemRows

pytestmark = pytest.mark.requires_postgres


@pytest.fixture(name="shop")
def _shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


def _new_item(shop: Shop, kind_id: str, name: str) -> str:
    items = ItemRows(shop.rows._pool)
    return str(items.create(ItemInput(kind_id=kind_id, name=name), BY).id)


def _recost(shop: Shop, lot: UUID) -> RecostResult:
    """A re-cost as the inline workflow runs it: it always ends before it answers."""
    result = shop.lifecycle.recost_lot(lot, BY)
    assert isinstance(result, RecostResult)
    return result


def _item(shop: Shop, name: str, on_hand: int = 50, cost: str = "5.99") -> str:
    """A `hardware` item with one lot of `on_hand` at `cost` (no lot when 0)."""
    item_id = _new_item(shop, "hardware", name)
    if on_hand:
        _receive(shop, item_id, on_hand, cost)
    return item_id


def _receive(shop: Shop, item_id: str, qty: int, cost: str) -> UUID:
    lot = shop.go.stock.receive_lot(
        UUID(item_id), ReceiveLot(qty=Decimal(qty), unit_cost_minor=Decimal(cost)), BY
    )
    return lot.id


def _on_hand(shop: Shop, item_id: str) -> Decimal:
    return sum((lot.on_hand for lot in shop.go.stock.lots(UUID(item_id))), Decimal(0))


def _order_moves(shop: Shop, order_item_id: str) -> list[tuple[Any, ...]]:
    """The order item's moves, oldest first: kind, qty, unit cost, whether it reverses one."""
    moves = shop.go.stock.moves(order_item_id=order_item_id)
    return [
        (m.kind, m.qty, m.unit_cost_minor, m.reverses_move_id is not None)
        for m in sorted(moves, key=lambda m: (m.at, m.id))
    ]


def _parts_minor(conn: Connection[DictRow], order_item_id: str) -> Decimal:
    """The order item's parts cost as job cost reads it (the plan's Task 8)."""
    row = conn.execute(
        "SELECT coalesce(-sum(amount_minor), 0) AS cost FROM stock_moves"
        " WHERE order_item_id = %s AND kind IN ('use', 'return')",
        (order_item_id,),
    ).fetchone()
    assert row is not None
    return Decimal(row["cost"])


def _pink(slug: str = "demo") -> ResolvedReference:
    plain = resolved(slug)
    return ResolvedReference(plain.reference, [ColourSlot(slot=0, colour="#FF69B4")])


def test_a_new_item_takes_its_models_rule(shop: Shop) -> None:
    silver, pink = _item(shop, "Silver hook"), _item(shop, "Pink hook")
    line = PartRuleLine(item_id=silver, when=[PartRuleWhen(colour="#ff69b4", item_id=pink)])
    shop.lifecycle.parts.set_rule("demo", PartRuleInput(lines=[line]), BY)
    order = shop.order()

    plain = shop.item(order)
    pinked = shop.lifecycle.add_item(order.id, BY, _pink(), output_id=None, quantity=2)

    got = shop.lifecycle.parts.item_parts([plain.id, pinked.id])
    assert [(p.item_id, p.per_unit) for p in got[plain.id]] == [(silver, 1)]
    assert [(p.item_id, p.per_unit) for p in got[pinked.id]] == [(pink, 1)]
    assert shop.kinds(order).count("item.parts_set") == 2


def test_a_rule_names_only_lot_items_that_exist(shop: Shop) -> None:
    hook = _item(shop, "Silver hook")
    service = _new_item(shop, "service", "Polishing")
    for missing in ("nope", service):
        with pytest.raises(PartNotFoundError):
            shop.lifecycle.parts.set_rule(
                "keychain", PartRuleInput(lines=[PartRuleLine(item_id=missing)]), BY
            )
    saved = shop.lifecycle.parts.set_rule(
        "keychain", PartRuleInput(lines=[PartRuleLine(item_id=hook.upper())]), BY
    )
    # Saved as the item's id, in its own spelling.
    assert saved.lines[0].item_id == hook
    assert shop.lifecycle.parts.rule("keychain") == saved


def test_a_rule_reads_its_old_part_id_keys() -> None:
    """A rule saved before #7067 names `part_id`; the keys read as `item_id`."""
    line = PartRuleLine.model_validate(
        {"per_unit": 2, "part_id": "a", "when": [{"colour": "#000000", "part_id": "b"}]}
    )
    assert (line.item_id, line.when[0].item_id) == ("a", "b")
    assert ItemPart.model_validate({"part_id": "c", "per_unit": 1}).item_id == "c"


def test_an_item_parts_body_names_an_item_once_in_any_case() -> None:
    upper = "5F0E9C1A-1111-4A2B-8C3D-000000000001"
    with pytest.raises(ValueError, match="named twice"):
        ItemPartsInput.model_validate(
            {
                "parts": [
                    {"item_id": upper, "per_unit": 1},
                    {"part_id": upper.lower(), "per_unit": 1},
                ]
            }
        )


def test_a_model_with_no_rule_gives_no_parts(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    assert shop.lifecycle.parts.item_parts([item.id])[item.id] == []
    assert "item.parts_set" not in shop.kinds(order)


def test_fulfil_takes_the_parts_from_the_lots_fifo(shop: Shop) -> None:
    """#7067 Task 7: 2 hooks a unit, 3 units: one `use` of -6 at the FIFO cost, with the
    order item; no `part:` cost entry (job cost reads the moves, Pre-flight 5)."""
    hook = _item(shop, "Silver hook", on_hand=10, cost="5.995")
    _receive(shop, hook, 10, "7")
    order, item = shop.accepted(quantity=3)
    shop.lifecycle.set_item_parts(order.id, item.id, [ItemPart(item_id=hook, per_unit=2)], BY)

    shop.fulfilled(order, item)

    assert _order_moves(shop, item.id) == [("use", Decimal(-6), Decimal("5.995"), False)]
    [use] = shop.go.stock.moves(order_item_id=item.id)
    assert (use.order_id, use.amount_minor) == (order.id, Decimal("-35.97"))
    assert shop.rows.cost_entries(order.id) == []
    [row] = shop.lifecycle.parts.item_parts([item.id])[item.id]
    assert row.used == 6
    assert _on_hand(shop, hook) == 14
    assert "parts.used" in shop.kinds(order)


def test_a_use_past_the_stock_goes_below_zero(shop: Shop) -> None:
    hook = _item(shop, "Silver hook", on_hand=5)
    order, item = shop.accepted(quantity=3)
    shop.lifecycle.set_item_parts(order.id, item.id, [ItemPart(item_id=hook, per_unit=2)], BY)

    shop.fulfilled(order, item)

    # 6 used of 5: stock goes below zero and never blocks.
    assert _on_hand(shop, hook) == -1
    flags = sorted(m.needs_review for m in shop.go.stock.moves(order_item_id=item.id))
    assert flags == [False, True]


def test_a_failed_print_uses_no_parts(shop: Shop) -> None:
    hook = _item(shop, "Silver hook")
    order, item = shop.accepted()
    shop.lifecycle.set_item_parts(order.id, item.id, [ItemPart(item_id=hook, per_unit=1)], BY)

    shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=1, outcome="failed"), BY
    )

    assert _on_hand(shop, hook) == 50
    assert shop.go.stock.moves(order_item_id=item.id) == []


def test_unfulfil_gives_back_at_the_uses_cost_after_a_lot_cost_change(shop: Shop) -> None:
    """Review Focus 2: the return takes each use's own cost and lot, not today's cost."""
    hook = _item(shop, "Silver hook", on_hand=10, cost="5")
    lot = shop.go.stock.lots(UUID(hook))[0].id
    order, item = shop.accepted(quantity=2)
    shop.lifecycle.set_item_parts(order.id, item.id, [ItemPart(item_id=hook, per_unit=1)], BY)
    shop.fulfilled(order, item)
    shop.go.stock.set_lot_cost(lot, Decimal(9), BY)

    shop.lifecycle.unfulfil_item(order.id, item.id, BY)

    assert _order_moves(shop, item.id) == [
        ("use", Decimal(-2), Decimal(5), False),
        ("return", Decimal(2), Decimal(5), True),
    ]
    assert {m.lot_id for m in shop.go.stock.moves(order_item_id=item.id)} == {lot}
    assert _on_hand(shop, hook) == 10
    with shop.rows.transaction() as conn:
        assert _parts_minor(conn, item.id) == 0
    [row] = shop.lifecycle.parts.item_parts([item.id])[item.id]
    assert row.used == 0
    assert "parts.returned" in shop.kinds(order)

    shop.fulfilled(order, shop.get_item(item))
    assert _on_hand(shop, hook) == 8
    assert [m.kind for m in shop.go.stock.moves(order_item_id=item.id)].count("use") == 2


def test_a_fulfilled_items_parts_are_fixed(shop: Shop) -> None:
    hook = _item(shop, "Silver hook")
    order, item = shop.accepted()
    shop.fulfilled(order, item)

    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.set_item_parts(order.id, item.id, [ItemPart(item_id=hook, per_unit=1)], BY)
    assert refused.value.slug == "item_parts_used"


@pytest.mark.parametrize("name", ["nope", "a\x00b", "5f0e9c1a-1111-4a2b-8c3d-000000000001"])
def test_an_unknown_item_is_refused(shop: Shop, name: str) -> None:
    order, item = shop.accepted()
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.set_item_parts(order.id, item.id, [ItemPart(item_id=name, per_unit=1)], BY)
    assert refused.value.slug == "part_not_found"


def test_an_item_that_is_not_lot_tracked_is_refused(shop: Shop) -> None:
    service = _new_item(shop, "service", "Polishing")
    order, item = shop.accepted()
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.set_item_parts(
            order.id, item.id, [ItemPart(item_id=service, per_unit=1)], BY
        )
    assert refused.value.slug == "part_not_found"


def test_a_part_of_unknown_cost_moves_stock_with_its_flag(shop: Shop) -> None:
    """R-P2-19: a lot bought in another currency opens cost-unknown; its takes keep the
    flag."""
    hook = _item(shop, "Euro hook", on_hand=0)
    shop.go.stock.receive_lot(
        UUID(hook), ReceiveLot(qty=Decimal(4), unit_cost_minor=Decimal(3), currency="EUR"), BY
    )
    order, item = shop.accepted()
    shop.lifecycle.set_item_parts(order.id, item.id, [ItemPart(item_id=hook, per_unit=1)], BY)

    shop.fulfilled(order, item)

    assert _on_hand(shop, hook) == 3
    [use] = shop.go.stock.moves(order_item_id=item.id)
    assert use.cost_unknown
    assert shop.rows.cost_entries(order.id) == []


def test_a_take_too_large_for_an_amount_is_refused(shop: Shop) -> None:
    """R-P2-16: a quantity times a cost past what `numeric(20,6)` holds is a refusal, never
    a database or arithmetic error."""
    hook = _item(shop, "Gold hook", on_hand=1, cost="90000000000000")
    order, item = shop.accepted(quantity=2_000_000)
    shop.lifecycle.set_item_parts(order.id, item.id, [ItemPart(item_id=hook, per_unit=1000)], BY)
    attempt = shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=item.quantity, outcome="succeeded"), BY
    )

    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.accept_fulfilment(order.id, item.id, attempt.id, BY)

    assert refused.value.slug == "bad_quantity"
    assert shop.go.stock.moves(order_item_id=item.id) == []
    with pytest.raises(LifecycleError) as estimate:
        shop.lifecycle.parts_unit_cost(item.id, "USD", "USD")
    assert (estimate.value.slug, estimate.value.unprocessable) == ("bad_quantity", True)


def test_the_quote_prices_parts_at_the_fifo_estimate(shop: Shop) -> None:
    """Ruling 10: one unit's parts at what a FIFO take of the item's quantity costs now."""
    hook = _item(shop, "Silver hook", on_hand=4, cost="5")
    _receive(shop, hook, 10, "8")
    ring = _item(shop, "Ring", on_hand=0)
    order, item = shop.accepted(quantity=3)
    shop.lifecycle.set_item_parts(
        order.id,
        item.id,
        [ItemPart(item_id=hook, per_unit=2), ItemPart(item_id=ring, per_unit=1)],
        BY,
    )

    # 6 hooks: 4 at 5 and 2 at 8 is 6 each, so 12 a unit; the ring has no lot (unknown,
    # and the estimate says so).
    assert shop.lifecycle.parts_unit_cost(item.id, "USD", "USD") == (Decimal(12), True)
    # R-P2-33: stock costs are in the shop's currency, so in another one the parts are
    # unknown; an item with no parts has none to miss.
    assert shop.lifecycle.parts_unit_cost(item.id, "EUR", "USD") == (Decimal(0), True)
    _, bare = shop.accepted(quantity=1)
    assert shop.lifecycle.parts_unit_cost(bare.id, "EUR", "USD") == (Decimal(0), False)
    assert shop.lifecycle.parts_unit_cost("no-such-item", "USD", "USD") == (Decimal(0), False)


def test_an_order_shows_what_it_is_short(shop: Shop) -> None:
    hook = _item(shop, "Silver hook", on_hand=2)
    order, item = shop.accepted(quantity=5)
    shop.lifecycle.set_item_parts(order.id, item.id, [ItemPart(item_id=hook, per_unit=1)], BY)

    found = order_parts(shop.rows, shop.lifecycle.parts, order.id)

    assert found is not None
    assert [(n.name, n.needed, n.on_hand, n.short) for n in found.short] == [
        ("Silver hook", 5, Decimal(2), Decimal(3))
    ]
    dumped = found.model_dump(mode="json")
    assert dumped["needs"][0]["part_id"] == dumped["needs"][0]["item_id"] == hook
    assert dumped["items"][0]["parts"][0]["part_id"] == hook
    shop.fulfilled(order, shop.get_item(item))
    after = order_parts(shop.rows, shop.lifecycle.parts, order.id)
    assert after is not None and after.short == []


def test_recost_writes_the_change_on_each_order(shop: Shop) -> None:
    hook = _item(shop, "Silver hook", on_hand=10, cost="5")
    lot = shop.go.stock.lots(UUID(hook))[0].id
    order, item = shop.accepted(quantity=2)
    shop.lifecycle.set_item_parts(order.id, item.id, [ItemPart(item_id=hook, per_unit=1)], BY)
    shop.fulfilled(order, item)
    shop.go.stock.set_lot_cost(lot, Decimal("7.25"), BY)

    result = _recost(shop, lot)

    assert result.moves == 1
    [changed] = result.orders
    assert (changed.order_id, changed.before_minor, changed.after_minor) == (
        order.id,
        Decimal(10),
        Decimal("14.5"),
    )
    [event] = [e for e in shop.rows.events(order.id) if e.kind == "item.parts_recosted"]
    # The `RecostLot` workflow's id marks the event (#6949 K1 Task 16).
    assert event.detail.pop("workflow").startswith(f"lot:{lot}:recost:")
    assert event.detail == {
        "lot_id": str(lot),
        "item_id": hook,
        # The route's decimal strings, a fraction kept (R-P2-13).
        "unit_cost_minor": "7.25",
        "before_unit_costs_minor": ["5"],
        "currency": "USD",
        "items": [
            {
                "order_item_id": item.id,
                "before_minor": "10",
                "after_minor": "14.5",
                "before_unknown": False,
                "after_unknown": False,
            }
        ],
    }
    # Nothing changes the second time: no event.
    assert _recost(shop, lot).orders == []
    assert shop.kinds(order).count("item.parts_recosted") == 1


# -- an item fulfilled before the parts migration (Review Focus 3) ---------------------------

_P1 = "5f0e9c1a-1111-4a2b-8c3d-000000000001"


def _index(slug: str) -> int:
    return next(i for i, m in enumerate(MIGRATIONS) if m.id.endswith(f"_{slug}"))


@pytest.mark.pg_empty  # it migrates step by step from an empty database (#7517)
def test_an_item_fulfilled_before_the_migration_unfulfils_to_zero(
    pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An item fulfilled while parts were `parts` rows, unfulfilled after Task 6's
    migration: the return reverses the converted use at the use's cost, not at the cost
    after a later dearer add, and the order item's parts cost nets to 0."""
    pool: PgPool = ConnectionPool(
        pg_conninfo,
        min_size=1,
        max_size=4,
        open=False,
        connection_class=Connection[DictRow],
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    pool.open(wait=True, timeout=30)
    try:
        with pool.connection() as conn:
            migrate(conn, MIGRATIONS[: _index("parts_to_items")])
        shop = Shop(OrderStore(pool), BlobRefs(pool))
        # The old schema has no `order_item_parts.item_id`: the item is fulfilled with no
        # parts read, then its parts rows are written as the old code wrote them. It has no
        # `order_items.parts_workflow_id` either (#6949 K1 Task 16): the fulfil reads none
        # and writes none, so no stock workflow stays open on it.
        monkeypatch.setattr(
            shop.lifecycle.parts, "item_parts", lambda ids, conn=None: {i: [] for i in ids}
        )
        monkeypatch.setattr(
            store_module,
            "_ITEM",
            store_module._ITEM.replace(", parts_workflow_id", ", NULL::text AS parts_workflow_id"),
        )
        monkeypatch.setattr(shop.rows, "set_parts_workflow", lambda *args: None)
        order, item = shop.accepted(quantity=2)
        shop.fulfilled(order, item)
        with pool.connection() as conn:
            conn.execute(
                "INSERT INTO parts (id, name, currency, pack_size, pack_price_minor,"
                " unit_cost_minor, on_hand) VALUES (%s, 'Hook', 'USD', 10, 5990, 650, 18)",
                (_P1,),
            )
            for minute, kind, delta, cost, amount, oi in (
                (1, "add", 10, "599", 5990, None),
                (2, "use", -2, "599", 1198, item.id),
                (3, "add", 10, "650", 7010, None),
            ):
                conn.execute(
                    "INSERT INTO part_stock_moves (part_id, kind, delta, on_hand_after,"
                    " unit_cost_after_minor, amount_minor, order_id, order_item_id,"
                    " created_by, created_at) VALUES (%s, %s, %s, 0, %s, %s, %s, %s, %s,"
                    " '2026-11-01T10:00:00Z'::timestamptz + make_interval(mins => %s))",
                    (
                        _P1,
                        kind,
                        delta,
                        cost,
                        amount,
                        None if oi is None else order.id,
                        oi,
                        '{"kind": "browser"}',
                        minute,
                    ),
                )
            conn.execute(
                "INSERT INTO order_item_parts (order_item_id, part_id, per_unit, used)"
                " VALUES (%s, %s, 1, 2)",
                (item.id, _P1),
            )
            conn.execute(
                "INSERT INTO order_cost_entries (order_id, order_item_id, kind, amount_minor,"
                " source, created_by) VALUES (%s, %s, 'material', 1198, %s, '{}')",
                (order.id, item.id, f"part:{item.id}:{_P1}"),
            )
            migrate(conn)
        monkeypatch.undo()

        shop.lifecycle.unfulfil_item(order.id, item.id, BY)

        assert _order_moves(shop, item.id) == [
            ("use", Decimal(-2), Decimal(599), False),
            ("return", Decimal(2), Decimal(599), True),
        ]
        with pool.connection() as conn:
            assert _parts_minor(conn, item.id) == 0
        assert shop.rows.cost_entries(order.id) == []
        [row] = shop.lifecycle.parts.item_parts([item.id])[item.id]
        assert (row.item_id, row.used) == (_P1, 0)
        assert _on_hand(shop, _P1) == 20
    finally:
        pool.close()


# -- the review of Task 7 (R-P2-28, R-P2-29, R-P2-30) ---------------------------------------


def test_a_bulk_fulfil_takes_every_part_in_item_order(shop: Shop) -> None:
    """R-P2-28, since #6949 K1 Task 16: two order items whose parts cross (the first line
    uses the later item id) take their parts in part `item_id` order, one Go `Take` each,
    each keyed by its item key and part; Go locks each take's own item."""
    one, two = sorted([_item(shop, "Hook A"), _item(shop, "Hook B")])
    order = shop.order()
    first, second = shop.item(order), shop.item(order)
    for line, part in ((first, two), (second, one)):
        shop.quoted(order, line)
        shop.lifecycle.set_item_parts(order.id, line.id, [ItemPart(item_id=part, per_unit=1)], BY)
    shop.lifecycle.accept_order(order.id, BY)

    shop.lifecycle.fulfil_items(
        order.id,
        [FulfilLine(item_id=first.id, quantity=1), FulfilLine(item_id=second.id, quantity=1)],
        BY,
    )

    takes = shop.go.named("inventory.Take")
    assert [t.arg["params"]["item_id"] for t in takes] == [one, two]
    assert [t.arg["key"] for t in takes] == [
        f"order_item:{second.id}:parts:1:{one}",
        f"order_item:{first.id}:parts:1:{two}",
    ]


def test_the_estimate_keeps_the_known_share_of_a_mixed_plan(shop: Shop) -> None:
    """R-P2-29: a take of 6 from 4 at 6 and 2 of unknown cost: the known 24 is kept (4 a
    unit), and the estimate says a part is unknown."""
    hook = _item(shop, "Silver hook", on_hand=4, cost="6")
    shop.go.stock.receive_lot(UUID(hook), ReceiveLot(qty=Decimal(10)), BY)
    order, item = shop.accepted(quantity=6)
    shop.lifecycle.set_item_parts(order.id, item.id, [ItemPart(item_id=hook, per_unit=1)], BY)

    assert shop.lifecycle.parts_unit_cost(item.id, "USD", "USD") == (Decimal(4), True)
    known = _item(shop, "Ring", on_hand=20, cost="2")
    shop.lifecycle.set_item_parts(order.id, item.id, [ItemPart(item_id=known, per_unit=2)], BY)
    assert shop.lifecycle.parts_unit_cost(item.id, "USD", "USD") == (Decimal(4), False)


def test_the_parts_events_name_the_shop_currency(shop: Shop) -> None:
    hook = _item(shop, "Silver hook", on_hand=10, cost="5")
    order, item = shop.accepted()
    shop.lifecycle.set_item_parts(order.id, item.id, [ItemPart(item_id=hook, per_unit=1)], BY)
    shop.fulfilled(order, item)
    shop.lifecycle.unfulfil_item(order.id, item.id, BY)

    details = {e.kind: e.detail for e in shop.rows.events(order.id)}
    assert details["parts.used"]["currency"] == "USD"
    assert details["parts.returned"]["currency"] == "USD"


def test_a_refused_give_back_is_recorded(shop: Shop) -> None:
    """#6949 K1 Task 16: the unfulfil commits first, so Go's refusal of its give-back is
    not undone: it is recorded as `parts.stock_refused`, and `parts.returned` names no
    part."""
    hook = _item(shop, "Silver hook", on_hand=10, cost="5")
    order, item = shop.accepted()
    shop.lifecycle.set_item_parts(order.id, item.id, [ItemPart(item_id=hook, per_unit=1)], BY)
    shop.fulfilled(order, item)
    shop.go.answers["inventory.GiveBack"] = [
        {"status": 422, "body": {"code": "bad_quantity", "status": 422, "detail": "too much"}}
    ]

    unfulfilled = shop.lifecycle.unfulfil_item(order.id, item.id, BY)

    assert isinstance(unfulfilled, OrderItem) and unfulfilled.status == "approved"
    assert unfulfilled.parts_settling is None
    events = {e.kind: e.detail for e in shop.rows.events(order.id)}
    assert events["parts.returned"]["parts"] == []
    assert events["parts.stock_refused"]["part_item_id"] == hook
    assert events["parts.stock_refused"]["problem"]["code"] == "bad_quantity"


def test_a_row_that_names_both_keys_is_refused() -> None:
    with pytest.raises(ValueError):
        ItemPart.model_validate({"item_id": "a", "part_id": "a", "per_unit": 1})
    with pytest.raises(ValueError):
        PartRuleLine.model_validate({"item_id": "a", "part_id": "b"})
    with pytest.raises(ValueError):
        PartRuleWhen.model_validate({"colour": "#000000", "item_id": "a", "part_id": "a"})


def test_a_rule_when_naming_an_unknown_item_is_refused(shop: Shop) -> None:
    hook = _item(shop, "Silver hook")
    line = PartRuleLine(item_id=hook, when=[PartRuleWhen(colour="#ff69b4", item_id="nope")])
    with pytest.raises(PartNotFoundError):
        shop.lifecycle.parts.set_rule("demo", PartRuleInput(lines=[line]), BY)
    assert shop.lifecycle.parts.rule("demo").lines == []


def test_an_empty_rule_removes_the_rule(shop: Shop) -> None:
    hook = _item(shop, "Silver hook")
    shop.lifecycle.parts.set_rule("demo", PartRuleInput(lines=[PartRuleLine(item_id=hook)]), BY)

    removed = shop.lifecycle.parts.set_rule("demo", PartRuleInput(lines=[]), BY)

    assert removed.lines == []
    rule = shop.lifecycle.parts.rule("demo")
    assert (rule.lines, rule.updated_at) == ([], None)
    order = shop.order()
    item = shop.item(order)
    assert shop.lifecycle.parts.item_parts([item.id])[item.id] == []


def _fulfilled_on_lot(shop: Shop, cost: str | None) -> tuple[Any, Any, UUID]:
    hook = _item(shop, "Silver hook", on_hand=0)
    received = ReceiveLot(qty=Decimal(10), unit_cost_minor=None if cost is None else Decimal(cost))
    lot = shop.go.stock.receive_lot(UUID(hook), received, BY).id
    order, item = shop.accepted(quantity=2)
    shop.lifecycle.set_item_parts(order.id, item.id, [ItemPart(item_id=hook, per_unit=1)], BY)
    shop.fulfilled(order, item)
    return order, item, lot


_MOVED = {
    "status": 409,
    "body": {"code": "recost_orders_moved", "status": 409, "detail": "moved", "orders": []},
}


def test_a_recost_starts_again_when_an_order_moved_in(shop: Shop) -> None:
    order, _, lot = _fulfilled_on_lot(shop, "5")
    shop.go.stock.set_lot_cost(lot, Decimal(6), BY)
    shop.go.answers["inventory.Recost"] = [dict(_MOVED)]

    result = _recost(shop, lot)

    assert isinstance(result, RecostResult)
    assert (len(shop.go.named("inventory.Recost")), result.moves) == (2, 1)
    keys = [c.arg["key"] for c in shop.go.named("inventory.Recost")]
    assert keys[0].endswith(":1") and keys[1].endswith(":2")
    assert shop.kinds(order).count("item.parts_recosted") == 1


def test_a_recost_gives_up_after_three_rounds(shop: Shop) -> None:
    order, _, lot = _fulfilled_on_lot(shop, "5")
    shop.go.answers["inventory.Recost"] = [dict(_MOVED) for _ in range(3)]

    with pytest.raises(WorkflowRefused) as refused:
        shop.lifecycle.recost_lot(lot, BY)

    assert refused.value.code == "recost_orders_moved"
    assert len(shop.go.named("inventory.Recost")) == 3
    assert "item.parts_recosted" not in shop.kinds(order)


def test_a_recost_of_a_cost_unknown_lot(shop: Shop) -> None:
    """A lot of unknown cost re-costs to no change; once its cost is set, the re-cost
    makes the order item's parts known."""
    order, item, lot = _fulfilled_on_lot(shop, None)

    assert _recost(shop, lot).orders == []
    shop.go.stock.set_lot_cost(lot, Decimal(3), BY)
    [changed] = _recost(shop, lot).orders

    assert (changed.order_item_id, changed.before_unknown, changed.after_unknown) == (
        item.id,
        True,
        False,
    )
    assert (changed.before_minor, changed.after_minor) == (Decimal(0), Decimal(6))
    assert shop.kinds(order).count("item.parts_recosted") == 1


def test_a_recost_skips_an_order_that_is_gone(shop: Shop) -> None:
    """Moves have no foreign key to orders: a move of an order that no longer exists is
    re-costed, and no event is written for it."""
    hook = _item(shop, "Silver hook", on_hand=10, cost="5")
    lot = shop.go.stock.lots(UUID(hook))[0].id
    with shop.rows.transaction() as conn:
        shop.go.stock.take(
            conn, UUID(hook), Decimal(2), BY, order_id="ord-gone", order_item_id="oi-gone"
        )
    shop.go.stock.set_lot_cost(lot, Decimal(6), BY)

    result = _recost(shop, lot)

    assert [(o.order_id, o.after_minor) for o in result.orders] == [("ord-gone", Decimal(12))]
