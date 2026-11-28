"""Phase 2's Python stock writes (#7067), kept for the tests only (#6949 K1 Task 16): Go is
the one writer of the stock tables now, and its own tests cover its activities. A Python
test that needs lots, units and moves to stand on, or a stand-in for Go's `inventory.Take`,
`inventory.GiveBack` and `inventory.Recost` (`tests/support/order_stock.py`), uses this
`StockStore`, which writes the rows as phase 2 did."""

from __future__ import annotations

import builtins
from collections.abc import Callable, Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from scadbuddy.entities.refs import BadEntityRef, EntityRef
from scadbuddy.entities.registry import EntityStore
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
from scadbuddy.inventory.stock import (
    _LIMIT,
    BadQuantity,
    Conn,
    LocationNotFound,
    LotNotFound,
    SerialExists,
    UnitInstalled,
    UnitNotFound,
    _amount,
    _fifo,
    _item,
    _positive,
    _six,
    _whole,
    estimate_unit_cost,
    parts_moved,
    parts_moves,
)
from scadbuddy.orders.models import Actor

#: The kinds of move "Re-cost past uses" re-snapshots (the plan's Task 4).
_RECOST_KINDS = ("use", "return", "adjust")
#: A unit's move when it leaves `in_stock` for this status.
_LEAVES = {"in_service": "use", "sold": "sell", "retired": "retire", "scrapped": "scrap"}


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
        return estimate_unit_cost(conn, item_id, qty)

    # -- lots ----------------------------------------------------------------------------

    def receive_lot(self, item_id: UUID, body: ReceiveLot, by: Actor) -> Lot:
        """A new lot (`origin = manual`) and its `receive` move. A cost in a currency other
        than the shop's opens it at 0 with its cost unknown, `cost_currency` set and the
        cost given kept as `foreign_unit_cost_minor` (R-7067-11, D11, R-P2-11); a receive
        with no cost opens at 0 with its cost unknown too."""
        # The shop's currency is a settings read on a connection of its own: take it first.
        shop = self._shop_currency()
        with self.pool.connection() as conn, conn.transaction():
            return self.receive_in(conn, item_id, body, by, shop=shop)

    def receive_in(
        self, conn: Conn, item_id: UUID, body: ReceiveLot, by: Actor, *, shop: str
    ) -> Lot:
        """`receive_lot` in the caller's transaction, with the shop's currency read by the
        caller (`shop`)."""
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
        item = _item(conn, item_id, "lot", lock=True)
        _positive(item["uom"], body.qty)
        # Every lot insert takes the item's open lock first, so no lot appears under a
        # caller that holds `lock_items` (R-P2-32).
        _open_lock(conn, item_id)
        try:
            with conn.transaction():
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
            reason=body.note,
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
            # The lock order of `lock_items`: the open lock, then the lots (R-P2-32).
            _open_lock(conn, item_id)
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
        item whose parts cost or unknown flag changed, as job cost reads them
        (`parts_moved`: the known-cost moves, and a `use` not given back of unknown cost),
        with both before and after (R-P2-31). The after figure is the moves read before
        with this update's changes (`RETURNING new.*`), so a move another transaction
        writes meanwhile on another lot does not count as a change."""
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
        moves = parts_moves(conn, touched)
        rows = conn.execute(
            "UPDATE stock_moves SET unit_cost_minor = %s, amount_minor = round(qty * %s, 6),"
            " cost_unknown = %s WHERE lot_id = %s AND kind = ANY(%s)"
            " AND (unit_cost_minor <> %s OR cost_unknown <> %s)"
            " RETURNING id, old.unit_cost_minor AS old_cost, new.amount_minor AS amount_minor,"
            " new.cost_unknown AS cost_unknown",
            (cost, cost, unknown, lot_id, list(_RECOST_KINDS), cost, unknown),
        ).fetchall()
        changed = {r["id"]: r for r in rows}
        after_moves = [
            {**m, **{k: changed[m["id"]][k] for k in ("amount_minor", "cost_unknown")}}
            if m["id"] in changed
            else m
            for m in moves
        ]
        before, after = parts_moved(moves), parts_moved(after_moves)
        order_of: dict[str, str | None] = {}
        for m in moves:
            order_of[m["order_item_id"]] = order_of.get(m["order_item_id"]) or m["order_id"]
        orders: builtins.list[RecostedItem] = []
        for oi in sorted(before, key=lambda oi: (order_of[oi] or "", oi)):
            item = RecostedItem(
                order_id=order_of[oi],
                order_item_id=oi,
                before_minor=before[oi].cost,
                after_minor=after[oi].cost,
                before_unknown=before[oi].unknown,
                after_unknown=after[oi].unknown,
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
        in `item_id` order: the item `FOR SHARE`, the lock that every lot insert takes
        (`_open_lock`), then its lots `FOR UPDATE` in FIFO order. A caller that then takes
        several items takes no lock out of this order (R-P2-28), and no receive or count
        adds a lot to an item it holds until it ends (R-P2-32)."""
        for item_id in sorted(set(item_ids)):
            _item(conn, item_id, "lot", lock=True)
            _open_lock(conn, item_id)
            _lock_lots(conn, item_id)

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
    """The transaction's lock on adding a lot to the item: every lot insert
    (`receive_in`, `_open_lots`) takes it first, and so does `lock_items`."""
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
