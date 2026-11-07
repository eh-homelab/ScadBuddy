"""Parts inventory (#5543, job cost spec 2026-11-05 §11): the hooks, rings and magnets an
item uses beside its filament, their stock, and the rule a model gives its items.

A part's ``unit_cost_minor`` is a rate in minor units with its fraction kept (a pack of 100
for 599 is 5.99 each). It starts at the pack's price over its size; each "Add stock" makes it
the weighted average of the stock on hand and the new pack (:func:`average`). A cost entry
made from it is rounded once (:func:`use_amount`), so an order keeps the cost it was given.

Every stock change is a ``part_stock_moves`` row written in its own transaction under the
part's row lock. The lifecycle takes the parts' locks after the order's, in id order
(:meth:`PartStore.lock_parts`), and the part routes take only the part's: no two writers
wait on each other in opposite orders. Stock can go below zero; nothing here refuses a use.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, computed_field, field_serializer, field_validator

from scadbuddy.orders.currency import minor_exponent
from scadbuddy.orders.models import Actor, Colour, ColourSlot, Money
from scadbuddy.orders.store import Conn, OrderStore

#: The most of one part one unit of an item may use.
MAX_PER_UNIT = 1000
#: The most units one stock change may move, and the most packs one "Add stock" records.
MAX_COUNT = 1_000_000
MAX_PACKS = 10_000
#: A unit cost is kept to the millionth of a minor unit (``numeric(20, 6)``).
UNIT_COST = Decimal("0.000001")

#: Why a part's stock moved: a pack bought, a count corrected, an item's fulfilment took
#: it, or its unfulfil gave it back.
MoveKind = Literal["add", "adjust", "use", "return"]

_PART = (
    "id, name, supplier, link, currency, pack_size, pack_price_minor, unit_cost_minor,"
    " on_hand, low_stock, created_at, updated_at"
)
_MOVE = (
    "id, part_id, kind, delta, on_hand_after, unit_cost_after_minor, amount_minor, note,"
    " order_id, order_item_id, created_by, created_at"
)


class PartNotFoundError(Exception):
    def __init__(self, part_id: str) -> None:
        super().__init__(f"no part {part_id!r}")
        self.part_id = part_id


class PartExistsError(Exception):
    """Another part has the name (in any case)."""

    def __init__(self, existing_id: str) -> None:
        super().__init__(f"part {existing_id!r} has that name")
        self.existing_id = existing_id


# -- models -----------------------------------------------------------------------------------


class PartPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    supplier: str | None = Field(default=None, max_length=200)
    #: Where to buy it again; shown as a link, so only http(s).
    link: str | None = Field(default=None, max_length=2000, pattern=r"^(https?://\S+)?$")
    pack_size: int | None = Field(default=None, ge=1, le=MAX_COUNT)
    pack_price_minor: Money | None = Field(default=None, ge=0, le=10**12)
    low_stock: int | None = Field(default=None, ge=0, le=MAX_COUNT)


class PartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    supplier: str = Field(default="", max_length=200)
    link: str = Field(default="", max_length=2000, pattern=r"^(https?://\S+)?$")
    pack_size: int = Field(ge=1, le=MAX_COUNT)
    #: The pack's price with tax, in the shop's currency.
    pack_price_minor: Money = Field(ge=0, le=10**12)
    low_stock: int = Field(default=0, ge=0, le=MAX_COUNT)
    #: The count already on the shelf; recorded as an ``adjust`` move when above zero.
    on_hand: int = Field(default=0, ge=0, le=MAX_COUNT)


class InventoryPart(BaseModel):
    id: str
    name: str
    supplier: str
    link: str
    currency: str
    pack_size: int
    pack_price_minor: Money
    #: One unit's cost in minor units, a decimal string with its fraction.
    unit_cost_minor: Decimal
    on_hand: int
    low_stock: int
    created_at: datetime
    updated_at: datetime

    @field_serializer("unit_cost_minor")
    def _decimal(self, value: Decimal) -> str:
        return format(value.normalize(), "f")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def low(self) -> bool:
        """On hand at or below the low-stock level, which 0 turns off."""
        return self.low_stock > 0 and self.on_hand <= self.low_stock

    @computed_field  # type: ignore[prop-decorator]
    @property
    def minor_exponent(self) -> int:
        return minor_exponent(self.currency)


class StockAdd(BaseModel):
    """Packs bought: ``packs`` of the part's pack, at ``pack_price_minor`` each when sent (it
    becomes the part's pack price), else at the part's."""

    model_config = ConfigDict(extra="forbid")

    packs: int = Field(default=1, ge=1, le=MAX_PACKS)
    pack_price_minor: Money | None = Field(default=None, ge=0, le=10**12)
    note: str = Field(default="", max_length=1000)


class StockAdjust(BaseModel):
    """A counted shelf: the on-hand count it really is, and why it differs."""

    model_config = ConfigDict(extra="forbid")

    on_hand: int = Field(ge=-MAX_COUNT, le=MAX_COUNT)
    note: str = Field(min_length=1, max_length=1000)


class StockMove(BaseModel):
    id: str
    part_id: str
    kind: MoveKind
    delta: int
    on_hand_after: int
    unit_cost_after_minor: Decimal
    amount_minor: Money | None
    note: str
    order_id: str | None
    order_item_id: str | None
    created_by: Actor
    created_at: datetime

    @field_serializer("unit_cost_after_minor")
    def _decimal(self, value: Decimal) -> str:
        return format(value.normalize(), "f")


class PartRuleWhen(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Used when an item has this colour in any slot (compared in any case).
    colour: Colour
    part_id: str = Field(min_length=1, max_length=100)


class PartRuleLine(BaseModel):
    """One part each unit uses: the first ``when`` whose colour the item has, else
    ``part_id`` (None: nothing when no colour matched)."""

    model_config = ConfigDict(extra="forbid")

    per_unit: int = Field(default=1, ge=1, le=MAX_PER_UNIT)
    part_id: str | None = Field(default=None, min_length=1, max_length=100)
    when: list[PartRuleWhen] = Field(default_factory=list, max_length=50)


class PartRuleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lines: list[PartRuleLine] = Field(max_length=50)


class PartRule(PartRuleInput):
    slug: str
    updated_by: Actor | None = None
    updated_at: datetime | None = None


class ItemPart(BaseModel):
    """One part one unit of an item uses."""

    model_config = ConfigDict(extra="forbid")

    part_id: str = Field(min_length=1, max_length=100)
    per_unit: int = Field(ge=1, le=MAX_PER_UNIT)


class ItemPartsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parts: list[ItemPart] = Field(max_length=50)

    @field_validator("parts")
    @classmethod
    def _once_each(cls, parts: list[ItemPart]) -> list[ItemPart]:
        ids = [p.part_id for p in parts]
        if len(set(ids)) != len(ids):
            raise ValueError("a part is named twice: give it one row with its count")
        return parts


class ItemPartRow(ItemPart):
    #: How many the item's fulfilment took out of stock; 0 until fulfilled.
    used: int = 0


class PartNeed(BaseModel):
    """One part an order's items still need, against the stock on hand."""

    part_id: str
    name: str
    #: The items not yet fulfilled (nor cancelled): their parts times their quantity.
    needed: int
    on_hand: int
    #: How many more than on hand are needed; 0 when the stock covers it.
    short: int
    low: bool


class ItemParts(BaseModel):
    item_id: str
    parts: list[ItemPartRow]


class OrderParts(BaseModel):
    """``GET /orders/{id}/parts``."""

    order_id: str
    items: list[ItemParts]
    needs: list[PartNeed]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def short(self) -> list[PartNeed]:
        return [n for n in self.needs if n.short > 0]


def order_parts(store: OrderStore, parts: PartStore, order_id: str) -> OrderParts | None:
    """The order's items' parts, and what its open items need against the stock now."""
    if store.order(order_id) is None:
        return None
    items = store.items(order_id)
    rows = parts.item_parts([i.id for i in items])
    needed: dict[str, int] = {}
    for item in items:
        if item.status in ("fulfilled", "cancelled"):
            continue
        for row in rows[item.id]:
            needed[row.part_id] = needed.get(row.part_id, 0) + row.per_unit * item.quantity
    stock = {p.id: p for p in parts.parts() if p.id in needed}
    return OrderParts(
        order_id=order_id,
        items=[ItemParts(item_id=i.id, parts=rows[i.id]) for i in items],
        needs=[
            PartNeed(
                part_id=part.id,
                name=part.name,
                needed=needed[part.id],
                on_hand=part.on_hand,
                short=max(0, needed[part.id] - part.on_hand),
                low=part.low,
            )
            for part in stock.values()
        ],
    )


# -- arithmetic -------------------------------------------------------------------------------


def average(on_hand: int, unit_cost: Decimal, count: int, amount: int | Decimal) -> Decimal:
    """The unit cost once ``count`` units costing ``amount`` in all join ``on_hand`` units at
    ``unit_cost``: their weighted average. With nothing on hand (or less than nothing), the
    new units' own cost."""
    incoming = Decimal(amount) / count
    if on_hand <= 0:
        return incoming.quantize(UNIT_COST, rounding=ROUND_HALF_UP)
    total = Decimal(on_hand) * unit_cost + Decimal(amount)
    return (total / (on_hand + count)).quantize(UNIT_COST, rounding=ROUND_HALF_UP)


def use_amount(unit_cost: Decimal, count: int) -> Money:
    """``count`` units' cost in whole minor units, rounded once, half up."""
    return int((unit_cost * count).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def rule_parts(lines: Iterable[PartRuleLine], colours: Iterable[ColourSlot]) -> list[ItemPart]:
    """The parts a rule gives an item of these colours, one row per part (a part two lines
    name is summed), in the rule's order."""
    have = {slot.colour.lower() for slot in colours}
    found: dict[str, int] = {}
    for line in lines:
        chosen = next((w.part_id for w in line.when if w.colour.lower() in have), line.part_id)
        if chosen is not None:
            found[chosen] = found.get(chosen, 0) + line.per_unit
    return [ItemPart(part_id=p, per_unit=min(n, MAX_PER_UNIT)) for p, n in found.items()]


# -- the store --------------------------------------------------------------------------------


class PartStore:
    """The parts' rows, over the orders' pool. A method with a ``conn`` runs in the
    caller's transaction (the lifecycle's); one without opens its own."""

    def __init__(self, store: OrderStore) -> None:
        self.store = store

    # reads

    def parts(self, *, conn: Conn | None = None) -> list[InventoryPart]:
        with self.store._read(conn) as c:
            rows = c.execute(f"SELECT {_PART} FROM parts ORDER BY lower(name), id").fetchall()
        return [InventoryPart.model_validate(r) for r in rows]

    def part(self, part_id: str, *, conn: Conn | None = None) -> InventoryPart | None:
        with self.store._read(conn) as c:
            row = c.execute(f"SELECT {_PART} FROM parts WHERE id = %s", (part_id,)).fetchone()
        return None if row is None else InventoryPart.model_validate(row)

    def moves(self, part_id: str, *, limit: int = 200) -> list[StockMove]:
        """The part's newest moves first."""
        with self.store._read(None) as c:
            rows = c.execute(
                f"SELECT {_MOVE} FROM part_stock_moves WHERE part_id = %s"
                " ORDER BY created_at DESC, id DESC LIMIT %s",
                (part_id, limit),
            ).fetchall()
        return [StockMove.model_validate(r) for r in rows]

    def rule(self, slug: str, *, conn: Conn | None = None) -> PartRule:
        """The model's rule; one with no lines when none was saved."""
        with self.store._read(conn) as c:
            row = c.execute(
                "SELECT slug, lines, updated_by, updated_at FROM model_part_rules WHERE slug = %s",
                (slug,),
            ).fetchone()
        return PartRule(slug=slug, lines=[]) if row is None else PartRule.model_validate(row)

    def item_parts(
        self, item_ids: Sequence[str], *, conn: Conn | None = None
    ) -> dict[str, list[ItemPartRow]]:
        found: dict[str, list[ItemPartRow]] = {item_id: [] for item_id in item_ids}
        if not item_ids:
            return found
        with self.store._read(conn) as c:
            rows = c.execute(
                "SELECT ip.order_item_id, ip.part_id, ip.per_unit, ip.used FROM order_item_parts ip"
                " JOIN parts p ON p.id = ip.part_id"
                " WHERE ip.order_item_id = ANY(%s) ORDER BY lower(p.name), p.id",
                (list(item_ids),),
            ).fetchall()
        for row in rows:
            found[row["order_item_id"]].append(
                ItemPartRow(part_id=row["part_id"], per_unit=row["per_unit"], used=row["used"])
            )
        return found

    def unit_cost_of_item(self, item_id: str, currency: str) -> Decimal:
        """One unit of the item's parts at today's unit costs, of the parts priced in
        ``currency`` (a part in another cannot be added in)."""
        with self.store._read(None) as c:
            row = c.execute(
                "SELECT coalesce(sum(ip.per_unit * p.unit_cost_minor), 0) AS cost"
                " FROM order_item_parts ip JOIN parts p ON p.id = ip.part_id"
                " WHERE ip.order_item_id = %s AND p.currency = %s",
                (item_id, currency),
            ).fetchone()
        assert row is not None
        return Decimal(row["cost"])

    def used_at(self, conn: Conn, item_id: str, part_id: str) -> Decimal | None:
        """The unit cost the item's last ``use`` of the part took it at."""
        row = conn.execute(
            "SELECT unit_cost_after_minor FROM part_stock_moves"
            " WHERE order_item_id = %s AND part_id = %s AND kind = 'use'"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (item_id, part_id),
        ).fetchone()
        return None if row is None else Decimal(row["unit_cost_after_minor"])

    # part writes, each in its own transaction under the part's lock

    def create(self, body: PartInput, *, currency: str, by: Actor) -> InventoryPart:
        unit = (Decimal(body.pack_price_minor) / body.pack_size).quantize(
            UNIT_COST, rounding=ROUND_HALF_UP
        )
        with self.store.transaction() as conn:
            self._name_free(conn, body.name, None)
            row = conn.execute(
                "INSERT INTO parts (name, supplier, link, currency, pack_size, pack_price_minor,"
                " unit_cost_minor, low_stock) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                f" RETURNING {_PART}",
                (
                    body.name,
                    body.supplier,
                    body.link,
                    currency,
                    body.pack_size,
                    body.pack_price_minor,
                    unit,
                    body.low_stock,
                ),
            ).fetchone()
            assert row is not None
            part = InventoryPart.model_validate(row)
            if body.on_hand > 0:
                part = self.move(
                    conn, part, "adjust", body.on_hand, by, note="Count when the part was added"
                )
        return part

    def update(self, part_id: str, body: PartPatch) -> InventoryPart:
        """Only what is sent changes. The pack is what the next "Add stock" records, so the
        unit cost stays, unless nothing is on hand to average: then it is the new pack's."""
        with self.store.transaction() as conn:
            part = self._lock(conn, part_id)
            if body.name is not None:
                self._name_free(conn, body.name, part.id)
            fields = body.model_dump(exclude_none=True)
            if not fields:
                return part
            merged = part.model_copy(update=fields)
            unit = part.unit_cost_minor
            if part.on_hand <= 0 and ("pack_size" in fields or "pack_price_minor" in fields):
                unit = (Decimal(merged.pack_price_minor) / merged.pack_size).quantize(
                    UNIT_COST, rounding=ROUND_HALF_UP
                )
            row = conn.execute(
                "UPDATE parts SET name = %s, supplier = %s, link = %s, pack_size = %s,"
                " pack_price_minor = %s, low_stock = %s, unit_cost_minor = %s,"
                f" updated_at = now() WHERE id = %s RETURNING {_PART}",
                (
                    merged.name,
                    merged.supplier,
                    merged.link,
                    merged.pack_size,
                    merged.pack_price_minor,
                    merged.low_stock,
                    unit,
                    part.id,
                ),
            ).fetchone()
            assert row is not None
        return InventoryPart.model_validate(row)

    def add_stock(self, part_id: str, body: StockAdd, by: Actor) -> InventoryPart:
        with self.store.transaction() as conn:
            part = self._lock(conn, part_id)
            price = (
                part.pack_price_minor if body.pack_price_minor is None else body.pack_price_minor
            )
            if price != part.pack_price_minor:
                conn.execute(
                    "UPDATE parts SET pack_price_minor = %s WHERE id = %s", (price, part.id)
                )
                part = part.model_copy(update={"pack_price_minor": price})
            count = body.packs * part.pack_size
            return self.move(
                conn, part, "add", count, by, amount_minor=body.packs * price, note=body.note
            )

    def adjust(self, part_id: str, body: StockAdjust, by: Actor) -> InventoryPart:
        with self.store.transaction() as conn:
            part = self._lock(conn, part_id)
            return self.move(conn, part, "adjust", body.on_hand - part.on_hand, by, note=body.note)

    def set_rule(self, slug: str, body: PartRuleInput, by: Actor) -> PartRule:
        """Every part the rule names must exist; ``PartNotFoundError`` names the first that
        does not. An empty rule removes it."""
        with self.store.transaction() as conn:
            named = {line.part_id for line in body.lines if line.part_id is not None}
            named |= {w.part_id for line in body.lines for w in line.when}
            self.require(conn, named)
            if not body.lines:
                conn.execute("DELETE FROM model_part_rules WHERE slug = %s", (slug,))
                return PartRule(slug=slug, lines=[])
            row = conn.execute(
                "INSERT INTO model_part_rules (slug, lines, updated_by) VALUES (%s, %s, %s)"
                " ON CONFLICT (slug) DO UPDATE SET lines = EXCLUDED.lines,"
                " updated_by = EXCLUDED.updated_by, updated_at = clock_timestamp()"
                " RETURNING slug, lines, updated_by, updated_at",
                (
                    slug,
                    Jsonb([line.model_dump(mode="json") for line in body.lines]),
                    Jsonb(by.model_dump(mode="json")),
                ),
            ).fetchone()
            assert row is not None
        return PartRule.model_validate(row)

    # in the caller's transaction (the lifecycle's)

    def require(self, conn: Conn, part_ids: Iterable[str]) -> None:
        wanted = sorted(set(part_ids))
        if not wanted:
            return
        rows = conn.execute("SELECT id FROM parts WHERE id = ANY(%s)", (wanted,)).fetchall()
        have = {r["id"] for r in rows}
        missing = [p for p in wanted if p not in have]
        if missing:
            raise PartNotFoundError(missing[0])

    def lock_parts(self, conn: Conn, part_ids: Iterable[str]) -> dict[str, InventoryPart]:
        """The parts under their row locks, taken in id order."""
        wanted = sorted(set(part_ids))
        if not wanted:
            return {}
        rows = conn.execute(
            f"SELECT {_PART} FROM parts WHERE id = ANY(%s) ORDER BY id FOR UPDATE", (wanted,)
        ).fetchall()
        return {r["id"]: InventoryPart.model_validate(r) for r in rows}

    def set_item_parts(self, conn: Conn, item_id: str, parts: Sequence[ItemPart]) -> None:
        """The item's parts replaced whole (``used`` 0: only an item not fulfilled may be
        given parts)."""
        self.require(conn, [p.part_id for p in parts])
        conn.execute("DELETE FROM order_item_parts WHERE order_item_id = %s", (item_id,))
        for part in parts:
            conn.execute(
                "INSERT INTO order_item_parts (order_item_id, part_id, per_unit)"
                " VALUES (%s, %s, %s)",
                (item_id, part.part_id, part.per_unit),
            )

    def set_used(self, conn: Conn, item_id: str, part_id: str, used: int) -> None:
        conn.execute(
            "UPDATE order_item_parts SET used = %s WHERE order_item_id = %s AND part_id = %s",
            (used, item_id, part_id),
        )

    def move(
        self,
        conn: Conn,
        part: InventoryPart,
        kind: MoveKind,
        delta: int,
        by: Actor,
        *,
        amount_minor: int | None = None,
        unit_cost: Decimal | None = None,
        note: str = "",
        order_id: str | None = None,
        order_item_id: str | None = None,
    ) -> InventoryPart:
        """Move ``part``'s stock by ``delta`` (its row lock held) and write the line. An
        ``add`` averages its ``amount_minor`` in, a ``return`` its units at ``unit_cost``
        (the cost they were taken at, unrounded); a ``use`` or an ``adjust`` keeps the
        unit cost."""
        unit = part.unit_cost_minor
        if kind == "add" and delta > 0 and amount_minor is not None:
            unit = average(part.on_hand, part.unit_cost_minor, delta, amount_minor)
        elif kind == "return" and delta > 0 and unit_cost is not None:
            unit = average(part.on_hand, part.unit_cost_minor, delta, unit_cost * delta)
        row = conn.execute(
            "UPDATE parts SET on_hand = on_hand + %s, unit_cost_minor = %s, updated_at = now()"
            f" WHERE id = %s RETURNING {_PART}",
            (delta, unit, part.id),
        ).fetchone()
        assert row is not None
        moved = InventoryPart.model_validate(row)
        conn.execute(
            "INSERT INTO part_stock_moves (part_id, kind, delta, on_hand_after,"
            " unit_cost_after_minor, amount_minor, note, order_id, order_item_id, created_by)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                part.id,
                kind,
                delta,
                moved.on_hand,
                moved.unit_cost_minor,
                amount_minor,
                note,
                order_id,
                order_item_id,
                Jsonb(by.model_dump(mode="json")),
            ),
        )
        return moved

    # helpers

    def _lock(self, conn: Conn, part_id: str) -> InventoryPart:
        row = conn.execute(
            f"SELECT {_PART} FROM parts WHERE id = %s FOR UPDATE", (part_id,)
        ).fetchone()
        if row is None:
            raise PartNotFoundError(part_id)
        return InventoryPart.model_validate(row)

    def _name_free(self, conn: Conn, name: str, own_id: str | None) -> None:
        row = conn.execute(
            "SELECT id FROM parts WHERE lower(name) = lower(%s) AND id IS DISTINCT FROM %s",
            (name, own_id),
        ).fetchone()
        if row is not None:
            raise PartExistsError(row["id"])
