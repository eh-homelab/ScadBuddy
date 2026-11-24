"""Lots, units and moves (#7067, spec §6.4 and §6.6).

- A lot item's stock is in lots, each with its own cost (D2). `take` uses FIFO by
  `(received_at, id)` unless the caller names a lot, one move per lot. Stock may go
  negative: what no lot holds goes on the newest lot at its cost, flagged `needs_review`;
  an item with no lot opens a zero-cost `adjustment` lot with its cost unknown (spec §6.6).
- A move's cost is a snapshot (D14): a later lot cost change moves no past move, except
  through `recost_lot` ("Re-cost past uses"). `give_back` returns each use at the use's own
  cost, to the same lot.
- A unit item's stock is its units `in_stock`; a status change across that line writes one
  move of the unit.
- A kind with tracking `none` is not counted: every call here refuses its items with
  `ItemNotCounted` (the plan's Ruling 1). Filament items are refused (Ruling 9).
- Every write keeps `stock_lots.on_hand` equal to the sum of the lot's moves, and every
  `amount_minor` is `round(qty * unit_cost_minor, 6)` (the `stock_moves_amount` CHECK).

Locks: the item `FOR SHARE` (the parent), then the rows being changed `FOR UPDATE`. A take
locks all of the item's lots in FIFO order, so takes, give-backs and counts of one item run
one after another. A caller that takes or gives back several items in one transaction (an
order's parts, the plan's Task 7) locks its own rows (the order and its items) first, then
every item it will touch with `lock_items` (in `item_id` order: each item, its lots, and
the lock that opens a first lot), before the first take, so two such callers cannot
deadlock (R-P2-28)."""

from __future__ import annotations

import builtins
from collections.abc import Callable, Iterable
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, NamedTuple
from uuid import UUID

import psycopg
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from scadbuddy.entities.refs import BadEntityRef, EntityRef
from scadbuddy.entities.registry import EntityStore
from scadbuddy.inventory.items import FilamentItemRefused, ItemNotFound
from scadbuddy.inventory.models import (
    AdjustInput,
    Lot,
    LotPatch,
    Move,
    MoveKind,
    ReceiveLot,
    ReceiveUnits,
    RecostedItem,
    RecostResult,
    Unit,
    UnitPatch,
    UnitStatusInput,
    UseInput,
)
from scadbuddy.orders.models import Actor

Conn = Connection[DictRow]


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
#: The kinds of move "Re-cost past uses" re-snapshots (the plan's Task 4).
_RECOST_KINDS = ("use", "return", "adjust")
#: A unit's move when it leaves `in_stock` for this status.
_LEAVES = {"in_service": "use", "sold": "sell", "retired": "retire", "scrapped": "scrap"}


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


class StockStore:
    def __init__(self, pool: Any, entities: EntityStore, shop_currency: Callable[[], str]) -> None:
        self.pool = pool
        self.entities = entities
        self._shop_currency = shop_currency

    # -- reads ---------------------------------------------------------------------------

    def lots(self, item_id: UUID) -> builtins.list[Lot]:
        """The item's lots in FIFO order."""
        with self.pool.connection() as conn:
            _item(conn, item_id, "lot", lock=False)
            rows = conn.execute(
                "SELECT * FROM stock_lots WHERE item_id = %s ORDER BY received_at, id",
                (item_id,),
            ).fetchall()
        return [Lot.model_validate(dict(r)) for r in rows]

    def lot(self, lot_id: UUID) -> Lot | None:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM stock_lots WHERE id = %s", (lot_id,)).fetchone()
        return None if row is None else Lot.model_validate(dict(row))

    def units(self, item_id: UUID, status: str | None = None) -> builtins.list[Unit]:
        """The item's units, oldest first; only those of `status` when given."""
        with self.pool.connection() as conn:
            _item(conn, item_id, "unit", lock=False)
            rows = conn.execute(
                "SELECT * FROM stock_units WHERE item_id = %s"
                " AND (%s::text IS NULL OR status = %s) ORDER BY received_at, id",
                (item_id, status, status),
            ).fetchall()
        return [Unit.model_validate(dict(r)) for r in rows]

    def unit(self, unit_id: UUID) -> Unit | None:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM stock_units WHERE id = %s", (unit_id,)).fetchone()
        return None if row is None else Unit.model_validate(dict(row))

    def moves(
        self,
        *,
        item_id: UUID | None = None,
        lot_id: UUID | None = None,
        unit_id: UUID | None = None,
        order_item_id: str | None = None,
        limit: int = 200,
    ) -> builtins.list[Move]:
        """Moves matching every filter given, newest first."""
        where: builtins.list[str] = []
        params: builtins.list[Any] = []
        for column, value in (
            ("item_id", item_id),
            ("lot_id", lot_id),
            ("unit_id", unit_id),
            ("order_item_id", order_item_id),
        ):
            if value is not None:
                where.append(f"{column} = %s")
                params.append(value)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM stock_moves{clause} ORDER BY at DESC, id DESC LIMIT %s",
                (*params, limit),
            ).fetchall()
        return [Move.model_validate(dict(r)) for r in rows]

    def estimate_unit_cost(self, conn: Conn, item_id: UUID, qty: Decimal) -> tuple[Decimal, bool]:
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

    # -- lots ----------------------------------------------------------------------------

    def receive_lot(self, item_id: UUID, body: ReceiveLot, by: Actor) -> Lot:
        """A new lot (`origin = manual`) and its `receive` move. A cost in a currency other
        than the shop's opens it at 0 with its cost unknown, `cost_currency` set and the
        cost given kept as `foreign_unit_cost_minor` (R-7067-11, D11, R-P2-11); a receive
        with no cost opens at 0 with its cost unknown too."""
        # The shop's currency is a settings read on a connection of its own: take it first.
        shop = self._shop_currency()
        currency = body.currency or shop
        given: Decimal | None = body.unit_cost_minor
        if given is None and body.total_cost_minor is not None:
            given = _six(body.total_cost_minor / body.qty)
        cost_currency: str | None = None
        foreign: Decimal | None = None
        if currency != shop:
            cost, unknown, cost_currency, foreign = Decimal(0), True, currency, given
        elif given is not None:
            cost, unknown = given, False
        else:
            cost, unknown = Decimal(0), True
        with self.pool.connection() as conn, conn.transaction():
            item = _item(conn, item_id, "lot", lock=True)
            _positive(item["uom"], body.qty)
            try:
                row = conn.execute(
                    "INSERT INTO stock_lots (item_id, location_id, received_qty, on_hand,"
                    " unit_cost_minor, cost_unknown, cost_currency, foreign_unit_cost_minor,"
                    " origin, note, received_at, created_by)"
                    " VALUES (%s, %s, %s, 0, %s, %s, %s, %s, 'manual', %s,"
                    " coalesce(%s, clock_timestamp()), %s) RETURNING *",
                    (
                        item_id,
                        body.location_id,
                        body.qty,
                        cost,
                        unknown,
                        cost_currency,
                        foreign,
                        body.note,
                        body.received_at,
                        Jsonb(by.model_dump(mode="json")),
                    ),
                ).fetchone()
            except psycopg.errors.ForeignKeyViolation:
                raise LocationNotFound(str(body.location_id)) from None
            assert row is not None
            _move(
                conn,
                item_id=item_id,
                lot_id=row["id"],
                kind="receive",
                qty=body.qty,
                unit_cost=cost,
                cost_unknown=unknown,
                by=by,
                at=row["received_at"],
            )
            return _lot(conn, row["id"])

    def adjust_lot(self, lot_id: UUID, body: AdjustInput, by: Actor) -> Lot:
        """A count of one lot: one `adjust` move of the difference at the lot's cost,
        none when the count matches."""
        with self.pool.connection() as conn, conn.transaction():
            lot = self._lock_lot(conn, lot_id)
            item = _item(conn, lot["item_id"], "lot", lock=False)
            _whole(item["uom"], body.on_hand)
            delta = body.on_hand - lot["on_hand"]
            if delta:
                _move(
                    conn,
                    item_id=lot["item_id"],
                    lot_id=lot_id,
                    kind="adjust",
                    qty=delta,
                    unit_cost=lot["unit_cost_minor"],
                    cost_unknown=lot["cost_unknown"],
                    reason=body.note,
                    by=by,
                )
            return _lot(conn, lot_id)

    def count_item(
        self, item_id: UUID, on_hand: Decimal, note: str, by: Actor
    ) -> builtins.list[Move]:
        """A count of the whole item, as `adjust` moves, one per lot it changes, in FIFO
        order. No lot keeps an offsetting negative balance (R-P2-15): each negative lot is
        brought to 0, then a lower count takes the difference FIFO from the lots with
        stock, and a higher one adds it to the newest lot at its cost (a new `adjustment`
        lot of unknown cost when there is none)."""
        if not note.strip():
            raise ValueError("a count needs a note")
        with self.pool.connection() as conn, conn.transaction():
            item = _item(conn, item_id, "lot", lock=True)
            if on_hand < 0:
                raise BadQuantity(str(on_hand))
            _whole(item["uom"], on_hand)
            lots = _lock_lots(conn, item_id)
            change = {lot["id"]: -lot["on_hand"] for lot in lots if lot["on_hand"] < 0}
            delta = on_hand - sum(
                (lot["on_hand"] for lot in lots if lot["on_hand"] > 0), Decimal(0)
            )
            if delta < 0:
                for lot, part, _ in _fifo(lots, -delta, None):
                    change[lot["id"]] = change.get(lot["id"], Decimal(0)) - part
            elif delta > 0:
                lots = lots or _open_lots(conn, item_id, by)
                newest = lots[-1]["id"]
                change[newest] = change.get(newest, Decimal(0)) + delta
            return [
                _move(
                    conn,
                    item_id=item_id,
                    lot_id=lot["id"],
                    kind="adjust",
                    qty=change[lot["id"]],
                    unit_cost=lot["unit_cost_minor"],
                    cost_unknown=lot["cost_unknown"],
                    reason=note,
                    by=by,
                )
                for lot in lots
                if change.get(lot["id"])
            ]

    def set_lot_cost(self, lot_id: UUID, unit_cost_minor: Decimal, by: Actor) -> Lot:
        """The lot's cost now, known. No move changes (D14); `recost_lot` re-snapshots.
        `cost_currency` and `foreign_unit_cost_minor` stay: what it was bought in and at
        (R-P2-12)."""
        with self.pool.connection() as conn, conn.transaction():
            _set_cost(conn, self._lock_lot(conn, lot_id), unit_cost_minor)
            return _lot(conn, lot_id)

    def update_lot(self, lot_id: UUID, patch: LotPatch) -> Lot:
        """The fields `patch` names, in one transaction: a cost as `set_lot_cost` sets it
        (no past move changes, D14), the location (null clears it) and the note.
        `LotNotFound`, `LocationNotFound` or `BadQuantity`."""
        fields = patch.model_dump(exclude_unset=True)
        cost = fields.get("unit_cost_minor")
        with self.pool.connection() as conn, conn.transaction():
            lot = self._lock_lot(conn, lot_id)
            if cost is not None:
                _set_cost(conn, lot, cost)
            if "location_id" in fields:
                try:
                    conn.execute(
                        "UPDATE stock_lots SET location_id = %s WHERE id = %s",
                        (fields["location_id"], lot_id),
                    )
                except psycopg.errors.ForeignKeyViolation:
                    raise LocationNotFound(str(fields["location_id"])) from None
            if fields.get("note") is not None:
                conn.execute(
                    "UPDATE stock_lots SET note = %s WHERE id = %s", (fields["note"], lot_id)
                )
            return _lot(conn, lot_id)

    def recost_lot(self, conn: Conn, lot_id: UUID) -> RecostResult:
        """ "Re-cost past uses" (spec §6.6), in the caller's transaction: every `use`,
        `return` and `adjust` move of the lot takes the lot's cost now. Names each order
        item whose parts cost (`-sum(amount_minor)` of its `use` and `return` moves) or
        unknown flag changed, with both before and after. The after figure is the before
        figure plus what this update changed (`RETURNING old.*, new.*`), so a move another
        transaction writes meanwhile on another lot does not count as a change."""
        lot = self._lock_lot(conn, lot_id)
        cost, unknown = lot["unit_cost_minor"], lot["cost_unknown"]
        largest = conn.execute(
            "SELECT max(abs(qty)) AS qty FROM stock_moves WHERE lot_id = %s AND kind = ANY(%s)",
            (lot_id, list(_RECOST_KINDS)),
        ).fetchone()
        if largest is not None and largest["qty"] is not None:
            _amount(largest["qty"], cost)
        touched = [
            r["order_item_id"]
            for r in conn.execute(
                "SELECT DISTINCT order_item_id FROM stock_moves WHERE lot_id = %s"
                " AND kind = ANY(%s) AND order_item_id IS NOT NULL",
                (lot_id, list(_RECOST_KINDS)),
            ).fetchall()
        ]
        before = _parts_costs(conn, touched, lot_id)
        rows = conn.execute(
            "UPDATE stock_moves SET unit_cost_minor = %s, amount_minor = round(qty * %s, 6),"
            " cost_unknown = %s WHERE lot_id = %s AND kind = ANY(%s)"
            " AND (unit_cost_minor <> %s OR cost_unknown <> %s)"
            " RETURNING order_item_id, kind, old.unit_cost_minor AS old_cost,"
            " old.amount_minor AS old_amount,"
            " new.amount_minor AS new_amount",
            (cost, cost, unknown, lot_id, list(_RECOST_KINDS), cost, unknown),
        ).fetchall()
        delta: dict[str, Decimal] = {}
        for r in rows:
            if r["order_item_id"] is not None and r["kind"] in ("use", "return"):
                oi = r["order_item_id"]
                delta[oi] = delta.get(oi, Decimal(0)) + r["new_amount"] - r["old_amount"]
        orders: builtins.list[RecostedItem] = []
        for oi in sorted(before, key=lambda oi: (before[oi].order_id or "", oi)):
            was = before[oi]
            # The lot's `use` and `return` moves now all carry its flag.
            item = RecostedItem(
                order_id=was.order_id,
                order_item_id=oi,
                before_minor=was.cost,
                after_minor=was.cost - delta.get(oi, Decimal(0)),
                before_unknown=was.unknown,
                after_unknown=was.others_unknown or (was.on_lot and unknown),
            )
            if (item.before_minor, item.before_unknown) != (item.after_minor, item.after_unknown):
                orders.append(item)
        return RecostResult(
            item_id=lot["item_id"],
            unit_cost_minor=cost,
            cost_unknown=unknown,
            before_unit_costs_minor=sorted({r["old_cost"] for r in rows}),
            moves=len(rows),
            orders=orders,
        )

    # -- takes ---------------------------------------------------------------------------

    def lock_items(self, conn: Conn, item_ids: Iterable[UUID]) -> None:
        """Every item's locks a take of it takes, in the caller's transaction, item by item
        in `item_id` order: the item `FOR SHARE`, its lots `FOR UPDATE` in FIFO order, and
        for an item with no lot the lock that opens its first one (`_open_lots`). A caller
        that then takes several items takes no lock out of this order (R-P2-28)."""
        for item_id in sorted(set(item_ids)):
            _item(conn, item_id, "lot", lock=True)
            if not _lock_lots(conn, item_id):
                _open_lock(conn, item_id)

    def take(
        self,
        conn: Conn,
        item_id: UUID,
        qty: Decimal,
        by: Actor,
        *,
        lot_id: UUID | None = None,
        order_id: str | None = None,
        order_item_id: str | None = None,
        entity_id: UUID | None = None,
        reason: str = "",
        kind: Literal["use", "adjust"] = "use",
    ) -> builtins.list[Move]:
        """`qty` out of the item's lots, in the caller's transaction: the named lot first,
        then FIFO; what no lot holds goes on the newest lot at its cost with
        `needs_review`, and an item with no lot opens a zero-cost `adjustment` lot of
        unknown cost (spec §6.6, the plan's Review Focus 1). One move per lot."""
        item = _item(conn, item_id, "lot", lock=True)
        _positive(item["uom"], qty)
        lots = _lock_lots(conn, item_id)
        # Before a lot is opened, so a caller that goes on in its transaction keeps none.
        if lot_id is not None and all(lot["id"] != lot_id for lot in lots):
            raise LotNotFound(str(lot_id))
        lots = lots or _open_lots(conn, item_id, by)
        return [
            _move(
                conn,
                item_id=item_id,
                lot_id=lot["id"],
                kind=kind,
                qty=-part,
                unit_cost=lot["unit_cost_minor"],
                cost_unknown=lot["cost_unknown"],
                needs_review=short,
                reason=reason,
                order_id=order_id,
                order_item_id=order_item_id,
                entity_id=entity_id,
                by=by,
            )
            for lot, part, short in _fifo(lots, qty, lot_id)
        ]

    def give_back(
        self, conn: Conn, *, order_item_id: str, item_id: UUID, by: Actor
    ) -> builtins.list[Move]:
        """One `return` per `use` move of the order item and item not yet reversed, in the
        caller's transaction: the same lot, the same quantity, the use's own cost (D14,
        the plan's Review Focus 2). A second call writes nothing."""
        _item(conn, item_id, "lot", lock=True)
        # The lots first, as `take` locks them: a second give-back waits here, then reads
        # the returns the first wrote.
        _lock_lots(conn, item_id)
        uses = conn.execute(
            "SELECT * FROM stock_moves m WHERE m.order_item_id = %s AND m.item_id = %s"
            " AND m.kind = 'use' AND m.lot_id IS NOT NULL AND NOT EXISTS"
            " (SELECT 1 FROM stock_moves r WHERE r.reverses_move_id = m.id)"
            " ORDER BY m.at, m.id",
            (order_item_id, item_id),
        ).fetchall()
        return [
            _move(
                conn,
                item_id=item_id,
                lot_id=use["lot_id"],
                kind="return",
                qty=-use["qty"],
                unit_cost=use["unit_cost_minor"],
                cost_unknown=use["cost_unknown"],
                reverses=use["id"],
                order_id=use["order_id"],
                order_item_id=order_item_id,
                entity_id=use["entity_id"],
                by=by,
            )
            for use in uses
        ]

    def use(self, item_id: UUID, body: UseInput, by: Actor) -> builtins.list[Move]:
        """`take` in a transaction of its own, on what `body.entity` names when given."""
        with self.pool.connection() as conn, conn.transaction():
            item = _item(conn, item_id, "lot", lock=True)
            _positive(item["uom"], body.qty)
            entity_id = self._entity(conn, body.entity)
            return self.take(
                conn,
                item_id,
                body.qty,
                by,
                lot_id=body.lot_id,
                entity_id=entity_id,
                reason=body.reason,
            )

    # -- units ---------------------------------------------------------------------------

    def receive_units(self, item_id: UUID, body: ReceiveUnits, by: Actor) -> builtins.list[Unit]:
        """`count` units `in_stock`, each with a `receive` move. With no `cost_minor` their
        cost is unknown. A blank serial is stored as null."""
        cost = body.cost_minor if body.cost_minor is not None else Decimal(0)
        unknown = body.cost_minor is None
        serials: builtins.list[str | None] = (
            list(body.serials) if body.serials else [None] * body.count
        )
        made: builtins.list[UUID] = []
        with self.pool.connection() as conn, conn.transaction():
            _item(conn, item_id, "unit", lock=True)
            for raw in serials:
                serial = raw.strip() if raw is not None and raw.strip() else None
                try:
                    row = conn.execute(
                        "INSERT INTO stock_units (item_id, serial, name, location_id,"
                        " cost_minor, cost_unknown, origin, created_by)"
                        " VALUES (%s, %s, %s, %s, %s, %s, 'manual', %s) RETURNING id, received_at",
                        (
                            item_id,
                            serial,
                            body.name,
                            body.location_id,
                            cost,
                            unknown,
                            Jsonb(by.model_dump(mode="json")),
                        ),
                    ).fetchone()
                except psycopg.errors.UniqueViolation:
                    raise SerialExists(str(serial)) from None
                except psycopg.errors.ForeignKeyViolation:
                    raise LocationNotFound(str(body.location_id)) from None
                assert row is not None
                _move(
                    conn,
                    item_id=item_id,
                    unit_id=row["id"],
                    kind="receive",
                    qty=Decimal(1),
                    unit_cost=cost,
                    cost_unknown=unknown,
                    by=by,
                    at=row["received_at"],
                )
                made.append(row["id"])
            rows = conn.execute(
                "SELECT * FROM stock_units WHERE id = ANY(%s) ORDER BY received_at, id", (made,)
            ).fetchall()
        return [Unit.model_validate(dict(r)) for r in rows]

    def set_unit_status(self, unit_id: UUID, body: UnitStatusInput, by: Actor) -> Unit:
        """A new status. Leaving `in_stock` writes a move of -1 (`in_service` a `use`,
        `sold` a `sell`, `retired` a `retire`, `scrapped` a `scrap`), coming back to it a
        `return` of +1, at the unit's cost; a change between two statuses out of stock
        moves no stock. Sets `in_service_at` on first service and `retired_at` on retire
        or scrap. `UnitInstalled` for a unit fitted in another (#7278)."""
        with self.pool.connection() as conn, conn.transaction():
            unit = self._lock_unit(conn, unit_id)
            if unit["installed_in_unit_id"] is not None:
                raise UnitInstalled(str(unit_id))
            status = body.status
            if status != unit["status"]:
                was_in, now_in = unit["status"] == "in_stock", status == "in_stock"
                if was_in != now_in:
                    _move(
                        conn,
                        item_id=unit["item_id"],
                        unit_id=unit_id,
                        kind="return" if now_in else _LEAVES[status],  # type: ignore[arg-type]
                        qty=Decimal(1 if now_in else -1),
                        unit_cost=unit["cost_minor"],
                        cost_unknown=unit["cost_unknown"],
                        entity_id=self._entity(conn, body.entity),
                        reason=body.note,
                        by=by,
                    )
                conn.execute(
                    "UPDATE stock_units SET status = %s,"
                    " in_service_at = CASE WHEN %s = 'in_service'"
                    "  THEN coalesce(in_service_at, now()) ELSE in_service_at END,"
                    " retired_at = CASE WHEN %s IN ('retired', 'scrapped') THEN now()"
                    "  WHEN %s IN ('in_stock', 'in_service') THEN NULL ELSE retired_at END"
                    " WHERE id = %s",
                    (status, status, status, status, unit_id),
                )
            row = conn.execute("SELECT * FROM stock_units WHERE id = %s", (unit_id,)).fetchone()
        assert row is not None
        return Unit.model_validate(dict(row))

    def update_unit(self, unit_id: UUID, patch: UnitPatch) -> Unit:
        """The fields `patch` names, in one transaction: the name, the serial (null or
        blank clears it) and the location (null clears it). No move. `UnitNotFound`,
        `SerialExists` or `LocationNotFound`."""
        fields = patch.model_dump(exclude_unset=True)
        with self.pool.connection() as conn, conn.transaction():
            self._lock_unit(conn, unit_id)
            if fields.get("name") is not None:
                conn.execute(
                    "UPDATE stock_units SET name = %s WHERE id = %s", (fields["name"], unit_id)
                )
            if "serial" in fields:
                raw = fields["serial"]
                serial = raw.strip() if raw is not None and raw.strip() else None
                try:
                    conn.execute(
                        "UPDATE stock_units SET serial = %s WHERE id = %s", (serial, unit_id)
                    )
                except psycopg.errors.UniqueViolation:
                    raise SerialExists(str(serial)) from None
            if "location_id" in fields:
                try:
                    conn.execute(
                        "UPDATE stock_units SET location_id = %s WHERE id = %s",
                        (fields["location_id"], unit_id),
                    )
                except psycopg.errors.ForeignKeyViolation:
                    raise LocationNotFound(str(fields["location_id"])) from None
            row = conn.execute("SELECT * FROM stock_units WHERE id = %s", (unit_id,)).fetchone()
        assert row is not None
        return Unit.model_validate(dict(row))

    # -- helpers -------------------------------------------------------------------------

    def _entity(self, conn: Conn, text: str | None) -> UUID | None:
        """The registry row of `kind:ref`, made on first use; `BadEntityRef` (a NUL byte
        too, which Postgres `text` refuses), `UnknownEntityKind` or `EntityNotFound`."""
        if text is None:
            return None
        if "\x00" in text:
            raise BadEntityRef(text)
        return self.entities.ensure(conn, EntityRef.parse(text))

    @staticmethod
    def _lock_lot(conn: Conn, lot_id: UUID) -> dict[str, Any]:
        """The lot `FOR UPDATE`, after its item `FOR SHARE`."""
        found = conn.execute("SELECT item_id FROM stock_lots WHERE id = %s", (lot_id,)).fetchone()
        if found is None:
            raise LotNotFound(str(lot_id))
        _item(conn, found["item_id"], "lot", lock=True)
        row = conn.execute(
            "SELECT * FROM stock_lots WHERE id = %s FOR UPDATE", (lot_id,)
        ).fetchone()
        assert row is not None
        return dict(row)

    @staticmethod
    def _lock_unit(conn: Conn, unit_id: UUID) -> dict[str, Any]:
        """The unit `FOR UPDATE`, after its item `FOR SHARE`."""
        found = conn.execute("SELECT item_id FROM stock_units WHERE id = %s", (unit_id,)).fetchone()
        if found is None:
            raise UnitNotFound(str(unit_id))
        _item(conn, found["item_id"], "unit", lock=True)
        row = conn.execute(
            "SELECT * FROM stock_units WHERE id = %s FOR UPDATE", (unit_id,)
        ).fetchone()
        assert row is not None
        return dict(row)


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


def _lock_lots(conn: Conn, item_id: UUID) -> builtins.list[dict[str, Any]]:
    """The item's lots `FOR UPDATE`, in FIFO order (one lock order for every caller)."""
    rows = conn.execute(
        "SELECT * FROM stock_lots WHERE item_id = %s ORDER BY received_at, id FOR UPDATE",
        (item_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _open_lots(conn: Conn, item_id: UUID, by: Actor) -> builtins.list[dict[str, Any]]:
    """For an item that had no lot: a zero-cost `adjustment` lot of unknown cost. Two
    callers that both found none wait on one lock, and the second takes the lot the first
    opened."""
    _open_lock(conn, item_id)
    lots = _lock_lots(conn, item_id)
    if lots:
        return lots
    row = conn.execute(
        "INSERT INTO stock_lots (item_id, received_qty, on_hand, unit_cost_minor,"
        " cost_unknown, origin, created_by) VALUES (%s, 0, 0, 0, true, 'adjustment', %s)"
        " RETURNING *",
        (item_id, Jsonb(by.model_dump(mode="json"))),
    ).fetchone()
    assert row is not None
    return [dict(row)]


def _open_lock(conn: Conn, item_id: UUID) -> None:
    """The transaction's lock on opening the item's first lot."""
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext('stock_lots_open'), hashtext(%s))",
        (str(item_id),),
    )


def _move(
    conn: Conn,
    *,
    item_id: UUID,
    kind: MoveKind,
    qty: Decimal,
    unit_cost: Decimal,
    cost_unknown: bool,
    by: Actor,
    lot_id: UUID | None = None,
    unit_id: UUID | None = None,
    needs_review: bool = False,
    reverses: UUID | None = None,
    reason: str = "",
    order_id: str | None = None,
    order_item_id: str | None = None,
    entity_id: UUID | None = None,
    at: datetime | None = None,
) -> Move:
    """One move, and its lot's `on_hand` moved by `qty`."""
    row = conn.execute(
        "INSERT INTO stock_moves (at, item_id, lot_id, unit_id, kind, qty, unit_cost_minor,"
        " amount_minor, cost_unknown, needs_review, reverses_move_id, reason, order_id,"
        " order_item_id, entity_id, actor) VALUES (coalesce(%s, clock_timestamp()), %s, %s,"
        " %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (
            at,
            item_id,
            lot_id,
            unit_id,
            kind,
            qty,
            unit_cost,
            _amount(qty, unit_cost),
            cost_unknown,
            needs_review,
            reverses,
            reason,
            order_id,
            order_item_id,
            entity_id,
            Jsonb(by.model_dump(mode="json")),
        ),
    ).fetchone()
    assert row is not None
    if lot_id is not None:
        moved = conn.execute(
            "UPDATE stock_lots SET on_hand = on_hand + %s WHERE id = %s"
            " AND abs(on_hand + %s) < %s RETURNING id",
            (qty, lot_id, qty, _LIMIT),
        ).fetchone()
        if moved is None:
            raise BadQuantity(str(qty))
    return Move.model_validate(dict(row))


def _set_cost(conn: Conn, lot: dict[str, Any], unit_cost_minor: Decimal) -> None:
    """The locked lot's cost now, known. `BadQuantity` for a cost below 0, with more than
    six places, or at which the lot's stock (received or on hand) is worth more than an
    amount holds."""
    if unit_cost_minor < 0 or unit_cost_minor != _six(unit_cost_minor):
        raise BadQuantity(str(unit_cost_minor))
    _amount(max(abs(lot["on_hand"]), lot["received_qty"]), unit_cost_minor)
    conn.execute(
        "UPDATE stock_lots SET unit_cost_minor = %s, cost_unknown = false WHERE id = %s",
        (unit_cost_minor, lot["id"]),
    )


def _lot(conn: Conn, lot_id: UUID) -> Lot:
    row = conn.execute("SELECT * FROM stock_lots WHERE id = %s", (lot_id,)).fetchone()
    assert row is not None
    return Lot.model_validate(dict(row))


class _Parts(NamedTuple):
    order_id: str | None
    #: `-sum(amount_minor)` of the order item's `use` and `return` moves.
    cost: Decimal
    #: Any of them of unknown cost.
    unknown: bool
    #: Any of them, on another lot or a unit, of unknown cost.
    others_unknown: bool
    #: Any of them on the lot.
    on_lot: bool


def _parts_costs(conn: Conn, order_item_ids: builtins.list[str], lot_id: UUID) -> dict[str, _Parts]:
    """Each order item's parts cost, as job cost reads it (the plan's Task 8), with what
    `recost_lot` needs to know of `lot_id`."""
    rows = conn.execute(
        "SELECT order_item_id, max(order_id) AS order_id, -sum(amount_minor) AS cost,"
        " bool_or(cost_unknown) AS unknown,"
        " coalesce(bool_or(cost_unknown) FILTER (WHERE lot_id IS DISTINCT FROM %s), false)"
        " AS others_unknown, coalesce(bool_or(lot_id = %s), false) AS on_lot"
        " FROM stock_moves WHERE order_item_id = ANY(%s) AND kind IN ('use', 'return')"
        " GROUP BY order_item_id",
        (lot_id, lot_id, order_item_ids),
    ).fetchall()
    return {
        r["order_item_id"]: _Parts(
            r["order_id"], r["cost"], r["unknown"], r["others_unknown"], r["on_lot"]
        )
        for r in rows
    }
