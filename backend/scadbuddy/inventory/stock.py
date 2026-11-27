"""Reads of lots, units and moves that Python keeps (#7067, spec §6.4 and §6.6; #6949 K1
Task 16, R-K1-16).

Go (scadbuddy-core) is the one writer of the stock tables (design §6.1a): an order's parts
leave and come back through `workflows/order_stock.py`, which calls Go's activities
`inventory.Take`, `inventory.GiveBack`, `inventory.Recost` and `inventory.Repoint`. What is
left here only reads: `estimate_unit_cost` (what a FIFO take would cost now, for the order's
quote, `Lifecycle.parts_unit_cost`), and `parts_moves` and `parts_moved` (an order
item's parts as its moves say, for job cost). The errors stay, as the offers and the
problem map name them. `tests/test_no_python_stock_writes.py` keeps it so."""

from __future__ import annotations

import builtins
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, NamedTuple
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow

Conn = Connection[DictRow]


class ItemNotFound(LookupError):  # noqa: N818
    """No such item."""


class FilamentItemRefused(ValueError):  # noqa: N818
    """Items of a `filament` kind wait for phase 3 (#7068; the plan's Ruling 9)."""


class LotNotFound(LookupError):  # noqa: N818
    """No such lot, or not a lot of the item."""


class LotEmpty(ValueError):  # noqa: N818
    """The named lot has no stock on hand. A named lot is taken or refused, never
    silently replaced by another (R-P2-14)."""


class UnitNotFound(LookupError):  # noqa: N818
    """No such unit."""


class LocationNotFound(LookupError):  # noqa: N818
    """No such location (`filament_locations`)."""


class WrongTracking(ValueError):  # noqa: N818
    """A lot call on a unit item, or a unit call on a lot item."""


class ItemNotCounted(ValueError):  # noqa: N818
    """The item's kind has tracking `none`: no lots, units or moves (Ruling 1)."""


class BadQuantity(ValueError):  # noqa: N818
    """A quantity or a cost out of range: below 0 (or 0 where a move needs one), more than
    six decimal places, a fraction of an `each` item, or past what `numeric(20,6)` holds
    (a quantity, a lot's stock, or a move's amount)."""


class UnitInstalled(ValueError):  # noqa: N818
    """The unit is fitted in another; install and remove are #7278's."""


class SerialExists(ValueError):  # noqa: N818
    """Another unit of the item has that serial."""


_SIX = Decimal("0.000001")
#: `numeric(20,6)` holds an absolute value below this.
_LIMIT = Decimal(10) ** 14


def _six(value: Decimal) -> Decimal:
    """Six places, half away from zero, as Postgres `round` (the `stock_moves_amount`
    CHECK)."""
    return value.quantize(_SIX, rounding=ROUND_HALF_UP)


def _amount(qty: Decimal, unit_cost: Decimal) -> Decimal:
    """`round(qty * unit_cost, 6)` (the `stock_moves_amount` CHECK), with `qty` and the
    amount inside `numeric(20,6)`; else `BadQuantity`."""
    exact = qty * unit_cost
    if abs(qty) >= _LIMIT or abs(exact) >= _LIMIT:
        raise BadQuantity(str(exact))
    amount = _six(exact)
    if abs(amount) >= _LIMIT:
        raise BadQuantity(str(amount))
    return amount


def _whole(uom: str, qty: Decimal) -> None:
    """An `each` item takes whole numbers only; every quantity has at most six places."""
    if qty != _six(qty) or (uom == "each" and qty != qty.to_integral_value()):
        raise BadQuantity(str(qty))


def _positive(uom: str, qty: Decimal) -> None:
    if qty <= 0:
        raise BadQuantity(str(qty))
    _whole(uom, qty)


def _fifo(
    lots: builtins.list[dict[str, Any]], qty: Decimal, lot_id: UUID | None
) -> builtins.list[tuple[dict[str, Any], Decimal, bool]]:
    """What a take of `qty` takes from each lot, as `(lot, qty, shortfall)`: the named lot
    first, then FIFO over the lots with stock; the rest on the newest lot. `lots` are in
    FIFO order and not empty. A named lot with no stock on hand is `LotEmpty` (R-P2-14)."""
    order = lots
    if lot_id is not None:
        named = [lot for lot in lots if lot["id"] == lot_id]
        if not named:
            raise LotNotFound(str(lot_id))
        if named[0]["on_hand"] <= 0:
            raise LotEmpty(str(lot_id))
        order = named + [lot for lot in lots if lot["id"] != lot_id]
    left = qty
    out: builtins.list[tuple[dict[str, Any], Decimal, bool]] = []
    for lot in order:
        if left <= 0:
            break
        if lot["on_hand"] <= 0:
            continue
        part = min(lot["on_hand"], left)
        out.append((lot, part, False))
        left -= part
    if left > 0:
        out.append((lots[-1], left, True))
    return out


def estimate_unit_cost(conn: Conn, item_id: UUID, qty: Decimal) -> tuple[Decimal, bool]:
    """What `take` of `qty` would charge per unit now for the part of it of known cost
    (the known lots' amounts over `qty`), and whether any of it is of unknown cost
    (R-P2-29). Reads only. An item with no lot is `(0, True)`, as `take` would open a
    zero-cost lot of unknown cost. `BadQuantity` where `take` would refuse it: a
    quantity, or a part of it at its lot's cost, past what `numeric(20,6)` holds
    (R-P2-16)."""
    item = _item(conn, item_id, "lot", lock=False)
    _positive(item["uom"], qty)
    lots = conn.execute(
        "SELECT * FROM stock_lots WHERE item_id = %s ORDER BY received_at, id", (item_id,)
    ).fetchall()
    if not lots:
        return Decimal(0), True
    plan = _fifo([dict(r) for r in lots], qty, None)
    total = Decimal(0)
    unknown = False
    for lot, part, _ in plan:
        amount = _amount(part, lot["unit_cost_minor"])
        if lot["cost_unknown"]:
            unknown = True
        else:
            total += amount
    return _six(total / qty), unknown


def _item(
    conn: Conn, item_id: UUID, tracking: Literal["lot", "unit"], *, lock: bool
) -> dict[str, Any]:
    """The item's tracking and unit, `FOR SHARE` when `lock` (so its kind cannot change
    meanwhile; `KindInUse` refuses a tracking change on a kind with items).
    `ItemNotFound`, `ItemNotCounted` (Ruling 1), `FilamentItemRefused` (Ruling 9) or
    `WrongTracking`."""
    row = conn.execute(
        "SELECT k.tracking, k.uom FROM items i JOIN item_kinds k ON k.id = i.kind_id"
        f" WHERE i.id = %s{' FOR SHARE OF i' if lock else ''}",
        (item_id,),
    ).fetchone()
    if row is None:
        raise ItemNotFound(str(item_id))
    if row["tracking"] == "none":
        raise ItemNotCounted(str(item_id))
    if row["tracking"] == "filament":
        raise FilamentItemRefused(str(item_id))
    if row["tracking"] != tracking:
        raise WrongTracking(str(item_id))
    return dict(row)


class PartsMoved(NamedTuple):
    """An order item's parts as its stock moves say (#7067): what job cost reads
    (`OrderStore.cost_rows`) and what a re-cost compares (R-P2-31)."""

    #: `-sum(amount_minor)` of its known-cost `use` and `return` moves.
    cost: Decimal
    #: A `use` that no `return` names is of unknown cost.
    unknown: bool
    #: Some `use` is not given back.
    open: bool


def parts_moves(conn: Conn, order_item_ids: Iterable[str]) -> builtins.list[dict[str, Any]]:
    """The order items' `use` and `return` moves, each with `open`: a `use` that no
    `return` names (`reverses_move_id`, `give_back`). `parts_moved` sums them."""
    rows = conn.execute(
        "SELECT m.id, m.order_id, m.order_item_id, m.kind, m.amount_minor, m.cost_unknown,"
        " m.kind = 'use' AND NOT EXISTS"
        "  (SELECT 1 FROM stock_moves r WHERE r.reverses_move_id = m.id) AS open"
        " FROM stock_moves m WHERE m.order_item_id = ANY(%s) AND m.kind IN ('use', 'return')",
        (list(order_item_ids),),
    ).fetchall()
    return [dict(r) for r in rows]


def parts_moved(moves: Iterable[dict[str, Any]]) -> dict[str, PartsMoved]:
    """Each order item's parts from its moves (`parts_moves`): the one reading of job
    cost and of a re-cost's before and after (R-P2-31)."""
    found: dict[str, PartsMoved] = {}
    for m in moves:
        was = found.get(m["order_item_id"], PartsMoved(Decimal(0), False, False))
        found[m["order_item_id"]] = PartsMoved(
            was.cost - (Decimal(0) if m["cost_unknown"] else m["amount_minor"]),
            was.unknown or (m["open"] and m["cost_unknown"]),
            was.open or m["open"],
        )
    return found
