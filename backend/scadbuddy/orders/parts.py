"""Parts (#5543, job cost spec 2026-11-05 §11; #7067): the hooks, rings and magnets an item
uses beside its filament, and the rule a model gives its items.

Since #7067 (Task 7) a part is an inventory item (`items`, a kind with tracking `lot`), and
its stock is in lots (`inventory/stock.py`). An order item's parts are
`order_item_parts.item_id`; a model's rule names items as `item_id` (a rule or a body that
still says `part_id` is read the same, for one release). The lifecycle takes an item's
parts from the lots when it is fulfilled (`StockStore.take`, FIFO, a cost snapshot on each
move) and gives them back when it is unfulfilled (`StockStore.give_back`, at each use's
own cost); it writes no cost entry (the plan's Pre-flight 5). It locks the order first,
then the items in `item_id` order, so two writers never wait on each other in opposite
orders.

The `/parts` routes are aliases of the inventory (`inventory/part_alias.py`, #7067 Task 9);
the models here are their bodies and answers. Every text a body takes refuses a NUL byte,
which Postgres `text` refuses (R-P2-16).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_serializer,
    field_validator,
)

from scadbuddy.inventory.models import Text
from scadbuddy.orders.currency import minor_exponent
from scadbuddy.orders.models import Actor, Colour, ColourSlot, Money
from scadbuddy.orders.store import Conn, OrderStore

#: The most of one part one unit of an item may use.
MAX_PER_UNIT = 1000
#: The most units one "Add stock" may move (its packs times the pack size;
#: ``StockTooLargeError``), and the most packs one "Add stock" records.
MAX_COUNT = 1_000_000
MAX_PACKS = 10_000
#: The most a part may hold on hand: well inside ``integer``, whatever adds come in turn.
MAX_ON_HAND = 1_000_000_000
#: A unit cost is kept to the millionth of a minor unit (``numeric(20, 6)``).
UNIT_COST = Decimal("0.000001")

#: Why a part's stock moved: a pack bought, a count corrected, an item's fulfilment took
#: it, or its unfulfil gave it back.
MoveKind = Literal["add", "adjust", "use", "return"]


class PartNotFoundError(Exception):
    def __init__(self, part_id: str) -> None:
        super().__init__(f"no part {part_id!r}")
        self.part_id = part_id


class StockTooLargeError(Exception):
    """An "Add stock" that would move more than ``MAX_COUNT`` units at once, or leave more
    than ``MAX_ON_HAND`` on hand."""


class PartExistsError(Exception):
    """Another part has the name (in any case)."""

    def __init__(self, existing_id: str) -> None:
        super().__init__(f"part {existing_id!r} has that name")
        self.existing_id = existing_id


# -- models -----------------------------------------------------------------------------------


class PartPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Text | None = Field(default=None, min_length=1, max_length=200)
    supplier: Text | None = Field(default=None, max_length=200)
    #: Where to buy it again; shown as a link, so only http(s).
    link: Text | None = Field(default=None, max_length=2000, pattern=r"^(https?://\S+)?$")
    pack_size: int | None = Field(default=None, ge=1, le=MAX_COUNT)
    pack_price_minor: Money | None = Field(default=None, ge=0, le=10**12)
    low_stock: int | None = Field(default=None, ge=0, le=MAX_COUNT)


class PartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Text = Field(min_length=1, max_length=200)
    supplier: Text = Field(default="", max_length=200)
    link: Text = Field(default="", max_length=2000, pattern=r"^(https?://\S+)?$")
    pack_size: int = Field(ge=1, le=MAX_COUNT)
    #: The pack's price with tax, in the shop's currency (past what a price point holds:
    #: 422).
    pack_price_minor: Money = Field(ge=0, le=10**12)
    low_stock: int = Field(default=0, ge=0, le=MAX_COUNT)
    #: The count already on the shelf; received as a `manual` lot at the pack price over
    #: its size when above zero.
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
    becomes the part's pack price when the part has an offer), else at the part's."""

    model_config = ConfigDict(extra="forbid")

    packs: int = Field(default=1, ge=1, le=MAX_PACKS)
    pack_price_minor: Money | None = Field(default=None, ge=0, le=10**12)
    note: Text = Field(default="", max_length=1000)


class StockAdjust(BaseModel):
    """A counted shelf: the on-hand count it really is, and why it differs."""

    model_config = ConfigDict(extra="forbid")

    on_hand: int = Field(ge=0, le=MAX_COUNT)
    note: Text = Field(min_length=1, max_length=1000)


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


def canonical(item_id: str) -> str:
    """A uuid's text in its one spelling (lower case, with hyphens); any other text as it
    is (it names no item: `part_not_found`)."""
    try:
        return str(UUID(item_id))
    except ValueError:
        return item_id


#: An item's id, read from `item_id` or the old `part_id` key (#7067: for one release).
ItemId = Annotated[
    str,
    Field(min_length=1, max_length=100, validation_alias=AliasChoices("item_id", "part_id")),
    AfterValidator(canonical),
]


class PartRuleWhen(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Used when an item has this colour in any slot (compared in any case).
    colour: Colour
    item_id: ItemId


class PartRuleLine(BaseModel):
    """One part each unit uses: the first ``when`` whose colour the item has, else
    ``item_id`` (None: nothing when no colour matched)."""

    model_config = ConfigDict(extra="forbid")

    per_unit: int = Field(default=1, ge=1, le=MAX_PER_UNIT)
    item_id: (
        Annotated[
            str,
            Field(min_length=1, max_length=100),
            AfterValidator(canonical),
        ]
        | None
    ) = Field(default=None, validation_alias=AliasChoices("item_id", "part_id"))
    when: list[PartRuleWhen] = Field(default_factory=list, max_length=50)


class PartRuleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lines: list[PartRuleLine] = Field(max_length=50)


class PartRule(PartRuleInput):
    slug: str
    updated_by: Actor | None = None
    updated_at: datetime | None = None


class ItemPart(BaseModel):
    """One part (an inventory item) one unit of an order item uses."""

    model_config = ConfigDict(extra="forbid")

    item_id: ItemId
    per_unit: int = Field(ge=1, le=MAX_PER_UNIT)


class ItemPartsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parts: list[ItemPart] = Field(max_length=50)

    @field_validator("parts")
    @classmethod
    def _once_each(cls, parts: list[ItemPart]) -> list[ItemPart]:
        ids = [p.item_id for p in parts]
        if len(set(ids)) != len(ids):
            raise ValueError("a part is named twice: give it one row with its count")
        return parts


class ItemPartRow(ItemPart):
    #: How many the item's fulfilment took out of stock; 0 until fulfilled.
    used: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def part_id(self) -> str:
        """`item_id` under its old name, for one release (#7067)."""
        return self.item_id


class PartNeed(BaseModel):
    """One part an order's items still need, against the stock on hand."""

    item_id: str
    name: str
    #: The items not yet fulfilled (nor cancelled): their parts times their quantity.
    needed: int
    #: The item's lots' `on_hand` summed (below 0 after a shortfall).
    on_hand: Decimal
    #: How many more than on hand are needed; 0 when the stock covers it.
    short: Decimal
    low: bool

    @computed_field  # type: ignore[prop-decorator]
    @property
    def part_id(self) -> str:
        """`item_id` under its old name, for one release (#7067)."""
        return self.item_id

    @field_serializer("on_hand", "short")
    def _decimal(self, value: Decimal) -> str:
        return format(value.normalize(), "f")


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
            needed[row.item_id] = needed.get(row.item_id, 0) + row.per_unit * item.quantity
    needs: list[PartNeed] = []
    for stock in parts.stock(needed):
        on_hand = stock["on_hand"]
        need = needed[stock["id"]]
        needs.append(
            PartNeed(
                item_id=stock["id"],
                name=stock["name"],
                needed=need,
                on_hand=on_hand,
                short=max(Decimal(0), need - on_hand),
                low=stock["low_stock"] > 0 and on_hand <= stock["low_stock"],
            )
        )
    return OrderParts(
        order_id=order_id,
        items=[ItemParts(item_id=i.id, parts=rows[i.id]) for i in items],
        needs=needs,
    )


# -- the rule -----------------------------------------------------------------------------


def rule_parts(lines: Iterable[PartRuleLine], colours: Iterable[ColourSlot]) -> list[ItemPart]:
    """The parts a rule gives an item of these colours, one row per part (a part two lines
    name is summed), in the rule's order."""
    have = {slot.colour.lower() for slot in colours}
    found: dict[str, int] = {}
    for line in lines:
        chosen = next((w.item_id for w in line.when if w.colour.lower() in have), line.item_id)
        if chosen is not None:
            found[chosen] = found.get(chosen, 0) + line.per_unit
    return [ItemPart(item_id=p, per_unit=min(n, MAX_PER_UNIT)) for p, n in found.items()]


# -- the store --------------------------------------------------------------------------------


class PartStore:
    """The parts' rows, over the orders' pool. A method with a ``conn`` runs in the
    caller's transaction (the lifecycle's); one without opens its own."""

    def __init__(self, store: OrderStore) -> None:
        self.store = store

    # reads

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
        """Each order item's parts, by the items' names."""
        found: dict[str, list[ItemPartRow]] = {item_id: [] for item_id in item_ids}
        if not item_ids:
            return found
        with self.store._read(conn) as c:
            rows = c.execute(
                "SELECT ip.order_item_id, ip.item_id::text AS item_id, ip.per_unit, ip.used"
                " FROM order_item_parts ip JOIN items i ON i.id = ip.item_id"
                " WHERE ip.order_item_id = ANY(%s) ORDER BY lower(i.name), i.id",
                (list(item_ids),),
            ).fetchall()
        for row in rows:
            found[row["order_item_id"]].append(
                ItemPartRow(item_id=row["item_id"], per_unit=row["per_unit"], used=row["used"])
            )
        return found

    def stock(self, item_ids: Iterable[str]) -> list[dict[str, Any]]:
        """Each item's name, `low_stock` and lots' `on_hand` summed, by name."""
        wanted = sorted(set(item_ids))
        if not wanted:
            return []
        with self.store._read(None) as c:
            rows = c.execute(
                "SELECT i.id::text AS id, i.name, i.low_stock,"
                " coalesce(sum(l.on_hand), 0) AS on_hand"
                " FROM items i LEFT JOIN stock_lots l ON l.item_id = i.id"
                " WHERE i.id = ANY(%s::uuid[]) GROUP BY i.id ORDER BY lower(i.name), i.id",
                (wanted,),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_rule(self, slug: str, body: PartRuleInput, by: Actor) -> PartRule:
        """Every item the rule names must exist and track lots; ``PartNotFoundError``
        names the first that does not. An empty rule removes it."""
        with self.store.transaction() as conn:
            named = {line.item_id for line in body.lines if line.item_id is not None}
            named |= {w.item_id for line in body.lines for w in line.when}
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

    def require(self, conn: Conn, item_ids: Iterable[str]) -> None:
        """Every id names an item whose kind tracks lots (a part's stock is in lots);
        `PartNotFoundError` names the first that does not. A text that is not a uuid is
        refused before any SQL (R-P2-16)."""
        wanted = sorted(set(item_ids))
        if not wanted:
            return
        ids: list[UUID] = []
        for item_id in wanted:
            try:
                ids.append(UUID(item_id))
            except ValueError:
                raise PartNotFoundError(item_id) from None
        rows = conn.execute(
            "SELECT i.id::text AS id FROM items i JOIN item_kinds k ON k.id = i.kind_id"
            " WHERE i.id = ANY(%s) AND k.tracking = 'lot'",
            (ids,),
        ).fetchall()
        have = {r["id"] for r in rows}
        missing = [p for p in wanted if p not in have]
        if missing:
            raise PartNotFoundError(missing[0])

    def set_item_parts(self, conn: Conn, item_id: str, parts: Sequence[ItemPart]) -> None:
        """The item's parts replaced whole (``used`` 0: only an item not fulfilled may be
        given parts)."""
        self.require(conn, [p.item_id for p in parts])
        conn.execute("DELETE FROM order_item_parts WHERE order_item_id = %s", (item_id,))
        for part in parts:
            conn.execute(
                "INSERT INTO order_item_parts (order_item_id, item_id, per_unit)"
                " VALUES (%s, %s, %s)",
                (item_id, part.item_id, part.per_unit),
            )

    def set_used(self, conn: Conn, order_item_id: str, item_id: str, used: int) -> None:
        conn.execute(
            "UPDATE order_item_parts SET used = %s WHERE order_item_id = %s AND item_id = %s",
            (used, order_item_id, item_id),
        )
