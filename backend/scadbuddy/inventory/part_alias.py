"""The `/parts` routes as aliases of the inventory (#7067 Task 9, spec §6.11; they go after
one release).

A part is an item of a kind under `hardware` or `ams_part` (`hardware` for every part the
aliases or the parts migration made; inventory K1 moved both from under `part` to under
`consumable`, since `part` is durable now). Its `InventoryPart` view is read from the
item, its newest unarchived offer (supplier, link, pack size) and that offer's latest
price point (pack price and currency), and its lots (`on_hand`; `unit_cost_minor` is the
on-hand lots' weighted average, each lot at the cost it was bought at:
`foreign_unit_cost_minor` for a lot bought in another currency; a lot of unknown cost
with no cost entered is left out, as `estimate_unit_cost` leaves it, R-P2-35). With no
such lot on hand the unit cost is the pack price over the pack size, and with no price
point either it is unknown (`cost_unknown`, read as 0).

A part with no supplier has no offer (owner Ruling 5, R-P2-25): it reads as a pack of 1 at
0, its link is its newest `purchase` entity link, and "Add stock" takes the body's pack
price (none: the lot's cost is unknown) and writes no price point. A pack, on a create or an
edit, needs an offer (`PartHasNoOffer`, R-P2-34). A part's currency is its offer's latest
price point's, else its newest lot's `cost_currency`, else the shop's (R-P2-36). Every price
point an alias writes and every lot "Add stock" receives is in that currency, so `currency`
reads back the same; a receive in another currency than the shop's opens a lot of unknown
cost that keeps the cost entered (R-7067-11, R-P2-11).

Each write runs in one transaction. Input reaches SQL only checked (R-P2-16): a pack price
past what `offer_price_points.price` holds is `BadQuantity`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from scadbuddy.entities.refs import EntityRef
from scadbuddy.entities.registry import EntityStore
from scadbuddy.inventory.models import Move, ReceiveLot
from scadbuddy.inventory.stock import BadQuantity, Conn, StockStore
from scadbuddy.orders.currency import minor_exponent
from scadbuddy.orders.models import Actor
from scadbuddy.orders.parts import (
    MAX_COUNT,
    MAX_ON_HAND,
    UNIT_COST,
    InventoryPart,
    PartExistsError,
    PartInput,
    PartNotFoundError,
    PartPatch,
    StockAdd,
    StockMove,
    StockTooLargeError,
)

#: The kind a part the aliases make is (the plan's Ruling 2).
PART_KIND = "hardware"
#: `offer_price_points.price` is `numeric(12,4)`: a price holds below this.
_PRICE_LIMIT = Decimal(10) ** 8
#: How a stock move's kind reads on the old stock line.
_KINDS = {"receive": "add", "adjust": "adjust", "use": "use", "return": "return"}
#: The system actor of the price points and links the aliases write.
_SOURCE = "parts"


class PartHasNoOffer(ValueError):  # noqa: N818
    """A pack edit on a part with no supplier: it has no offer to hold the pack."""


#: Every part with its offer, price point, link and stock, by name.
_PARTS = """
WITH RECURSIVE under (id, tracking) AS (
    SELECT id, tracking FROM item_kinds WHERE id IN ('hardware', 'ams_part')
    UNION SELECT k.id, k.tracking FROM item_kinds k JOIN under u ON k.parent_id = u.id
)
SELECT i.id, i.kind_id, i.name, i.low_stock, i.created_at, i.updated_at,
    o.id AS offer_id, s.name AS supplier, o.url, o.pack_size,
    p.price, p.currency,
    (SELECT l.url FROM entities e JOIN entity_links l ON l.entity_id = e.id
     WHERE e.kind = 'item' AND e.ref = i.id::text AND l.kind = 'purchase'
     ORDER BY l.created_at DESC, l.id DESC LIMIT 1) AS purchase_url,
    (SELECT cost_currency FROM stock_lots WHERE item_id = i.id
     ORDER BY received_at DESC, id DESC LIMIT 1) AS lot_currency,
    st.on_hand, st.held, st.value
FROM items i
LEFT JOIN LATERAL (
    SELECT * FROM supplier_offers WHERE item_id = i.id AND archived_at IS NULL
    ORDER BY created_at DESC, id DESC LIMIT 1
) o ON true
LEFT JOIN filament_suppliers s ON s.id = o.supplier_id
LEFT JOIN LATERAL (
    SELECT price, currency FROM offer_price_points WHERE offer_id = o.id
    ORDER BY on_date DESC, created_at DESC, id DESC LIMIT 1
) p ON true
CROSS JOIN LATERAL (
    SELECT coalesce(sum(on_hand), 0) AS on_hand,
        coalesce(sum(on_hand) FILTER (WHERE on_hand > 0 AND NOT unpriced), 0) AS held,
        coalesce(sum(on_hand * coalesce(foreign_unit_cost_minor, unit_cost_minor))
            FILTER (WHERE on_hand > 0 AND NOT unpriced), 0) AS value
    FROM stock_lots
    CROSS JOIN LATERAL (
        SELECT cost_unknown AND foreign_unit_cost_minor IS NULL AS unpriced
    ) c
    WHERE item_id = i.id
) st
WHERE i.kind_id IN (SELECT id FROM under WHERE tracking = 'lot') AND i.archived_at IS NULL
"""


def _uuid(part_id: str) -> UUID:
    try:
        return UUID(part_id)
    except ValueError:
        raise PartNotFoundError(part_id) from None


def _price(minor: int, currency: str) -> Decimal:
    """A pack price in minor units as a price point holds it (major units); `BadQuantity`
    past `numeric(12,4)`."""
    major = Decimal(minor).scaleb(-minor_exponent(currency))
    if major >= _PRICE_LIMIT:
        raise BadQuantity(str(minor))
    return major


def _view(row: dict[str, Any], shop: str) -> InventoryPart:
    # The offer's latest price point's, else the newest lot's (a foreign lot keeps its
    # currency, R-P2-11), else the shop's (R-P2-36).
    currency = row["currency"] or row["lot_currency"] or shop
    pack_size = int(row["pack_size"]) if row["pack_size"] is not None else 1
    pack_price = (
        int(Decimal(row["price"]).scaleb(minor_exponent(currency)))
        if row["price"] is not None
        else 0
    )
    if row["held"] > 0:
        unit = Decimal(row["value"]) / Decimal(row["held"])
    else:
        unit = Decimal(pack_price) / pack_size
    # No known lot on hand and no price point: nothing says what a unit costs.
    unknown = row["held"] <= 0 and row["price"] is None
    return InventoryPart(
        id=str(row["id"]),
        name=row["name"],
        supplier=row["supplier"] or "",
        link=row["url"] or ("" if row["offer_id"] is not None else row["purchase_url"] or ""),
        currency=currency,
        pack_size=pack_size,
        pack_price_minor=pack_price,
        unit_cost_minor=unit.quantize(UNIT_COST, rounding=ROUND_HALF_UP),
        cost_unknown=unknown,
        on_hand=int(Decimal(row["on_hand"]).to_integral_value()),
        low_stock=int(Decimal(row["low_stock"]).to_integral_value()),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class PartAliases:
    def __init__(
        self,
        pool: Any,
        entities: EntityStore,
        stock: StockStore,
        shop_currency: Callable[[], str],
    ) -> None:
        self.pool = pool
        self.entities = entities
        self.stock = stock
        self._shop_currency = shop_currency

    # -- reads -----------------------------------------------------------------------------

    def parts(self) -> list[InventoryPart]:
        shop = self._shop_currency()
        with self.pool.connection() as conn:
            rows = conn.execute(f"{_PARTS} ORDER BY lower(i.name), i.id").fetchall()
        return [_view(dict(r), shop) for r in rows]

    def part(self, part_id: str) -> InventoryPart:
        """`PartNotFoundError` for an id that names no part."""
        shop = self._shop_currency()
        with self.pool.connection() as conn:
            return self._part(conn, _uuid(part_id), shop)

    def moves(self, part_id: str, *, limit: int = 200) -> list[StockMove]:
        """The part's lot moves, newest first, as the old stock line read them:
        `on_hand_after` is what the part held after each."""
        part = self.part(part_id)
        moves = self.stock.moves(item_id=UUID(part.id), limit=limit)
        on_hand = Decimal(part.on_hand)
        found: list[StockMove] = []
        for move in moves:
            kind = _KINDS.get(move.kind)
            if kind is not None and move.lot_id is not None:
                found.append(_stock_move(move, kind, on_hand))
            on_hand -= move.qty
        return found

    # -- writes ----------------------------------------------------------------------------

    def create(self, body: PartInput, *, currency: str, by: Actor) -> InventoryPart:
        """An item of kind `hardware`; an offer and its price point when a supplier is
        named, else the link as a `purchase` entity link; a `manual` lot of `on_hand` at the
        pack price over its size (of unknown cost with no supplier). `PartHasNoOffer` for a
        pack (a size other than 1, a price other than 0) with no supplier; `PartExistsError`
        for a name another part has."""
        shop = self._shop_currency()
        price = _price(body.pack_price_minor, currency)
        supplied = bool(body.supplier.strip())
        if not supplied and (body.pack_size != 1 or body.pack_price_minor != 0):
            # No supplier, no offer to hold the pack (R-P2-34).
            raise PartHasNoOffer(body.name)
        with self.pool.connection() as conn, conn.transaction():
            try:
                with conn.transaction():
                    row = conn.execute(
                        "INSERT INTO items (kind_id, name, low_stock) VALUES (%s, %s, %s)"
                        " RETURNING id",
                        (PART_KIND, body.name, body.low_stock),
                    ).fetchone()
            except psycopg.errors.UniqueViolation:
                raise PartExistsError(self._named(conn, PART_KIND, body.name)) from None
            assert row is not None
            item_id: UUID = row["id"]
            if supplied:
                offer = self._offer(conn, item_id, body.supplier, body.link, body.pack_size)
                self._price_point(conn, offer, price, currency)
            elif body.link:
                self._purchase_link(conn, item_id, body.link, by)
            if body.on_hand > 0:
                self.stock.receive_in(
                    conn,
                    item_id,
                    ReceiveLot(
                        qty=Decimal(body.on_hand),
                        # With no supplier there is no price: the lot's cost is unknown.
                        unit_cost_minor=(
                            (Decimal(body.pack_price_minor) / body.pack_size).quantize(
                                UNIT_COST, rounding=ROUND_HALF_UP
                            )
                            if supplied
                            else None
                        ),
                        currency=currency,
                        note="Count when the part was added",
                    ),
                    by,
                    shop=shop,
                )
            return self._part(conn, item_id, shop)

    def update(self, part_id: str, body: PartPatch, by: Actor) -> InventoryPart:
        """Only what is sent changes. A new supplier moves the part to a new offer at that
        supplier (the old one is archived, its pack and price copied); a blank supplier
        archives the offer, and its link stays as a purchase link. With no offer a new link
        is a `purchase` entity link, and a blank one removes the links the aliases wrote.
        `PartHasNoOffer` for a pack edit on a part with no supplier."""
        shop = self._shop_currency()
        fields = body.model_dump(exclude_none=True)
        with self.pool.connection() as conn, conn.transaction():
            part = self._lock(conn, _uuid(part_id), shop)
            item_id = UUID(part.id)
            if "name" in fields:
                try:
                    with conn.transaction():
                        conn.execute(
                            "UPDATE items SET name = %s WHERE id = %s", (fields["name"], item_id)
                        )
                except psycopg.errors.UniqueViolation:
                    kind = conn.execute(
                        "SELECT kind_id FROM items WHERE id = %s", (item_id,)
                    ).fetchone()
                    assert kind is not None
                    raise PartExistsError(
                        self._named(conn, kind["kind_id"], fields["name"])
                    ) from None
            if "low_stock" in fields:
                conn.execute(
                    "UPDATE items SET low_stock = %s WHERE id = %s", (fields["low_stock"], item_id)
                )
            offer = before = self._current_offer(conn, item_id)
            fresh = False
            supplier = fields.get("supplier")
            if supplier is not None and supplier.strip().upper() != part.supplier.strip().upper():
                if offer is not None:
                    conn.execute(
                        "UPDATE supplier_offers SET archived_at = now() WHERE id = %s", (offer,)
                    )
                offer = None
                if supplier.strip():
                    offer = self._offer(conn, item_id, supplier, part.link, part.pack_size)
                    fresh = True
                    if before is not None and "pack_price_minor" not in fields:
                        # The old offer's pack price moves with the part.
                        self._price_point(
                            conn, offer, _price(part.pack_price_minor, part.currency), part.currency
                        )
                elif before is not None and part.link and "link" not in fields:
                    # The old offer's link stays the part's, as a purchase link.
                    self._purchase_link(conn, item_id, part.link, by)
            if "link" in fields:
                if offer is not None:
                    conn.execute(
                        "UPDATE supplier_offers SET url = %s, updated_at = now() WHERE id = %s",
                        (fields["link"] or None, offer),
                    )
                elif not fields["link"]:
                    self._drop_purchase_links(conn, item_id)
                elif before is not None or fields["link"] != part.link:
                    # The link it has now is the archived offer's, or another one.
                    self._purchase_link(conn, item_id, fields["link"], by)
            for key in ("pack_size", "pack_price_minor"):
                if key in fields and offer is None:
                    raise PartHasNoOffer(part.id)
            if "pack_size" in fields and offer is not None:
                conn.execute(
                    "UPDATE supplier_offers SET pack_size = %s, updated_at = now() WHERE id = %s",
                    (fields["pack_size"], offer),
                )
            if (
                "pack_price_minor" in fields
                and offer is not None
                and (fresh or fields["pack_price_minor"] != part.pack_price_minor)
            ):
                self._price_point(
                    conn,
                    offer,
                    _price(fields["pack_price_minor"], part.currency),
                    part.currency,
                )
            conn.execute("UPDATE items SET updated_at = now() WHERE id = %s", (item_id,))
            return self._part(conn, item_id, shop)

    def add_stock(self, part_id: str, body: StockAdd, by: Actor) -> InventoryPart:
        """`packs` times `pack_size` received as one `manual` lot at `packs` times the pack
        price, in the part's currency: the body's `pack_price_minor` (with an offer, also a
        new `manual` price point), else the offer's latest. A part with no offer takes a pack
        of 1 and the body's price, and writes no price point (R-P2-25); with no price either,
        the lot's cost is unknown. `StockTooLargeError` past one change's most."""
        shop = self._shop_currency()
        with self.pool.connection() as conn, conn.transaction():
            part = self._lock(conn, _uuid(part_id), shop)
            item_id = UUID(part.id)
            count = body.packs * part.pack_size
            if count > MAX_COUNT or part.on_hand + count > MAX_ON_HAND:
                raise StockTooLargeError(
                    f"{body.packs} packs of {part.pack_size} is {count} units: one stock change"
                    f" moves at most {MAX_COUNT}, and a part holds at most {MAX_ON_HAND}"
                )
            offer = self._current_offer(conn, item_id)
            price: int | None = None
            if body.pack_price_minor is not None:
                price = body.pack_price_minor
                if offer is not None and price != part.pack_price_minor:
                    self._price_point(conn, offer, _price(price, part.currency), part.currency)
            elif offer is not None:
                price = part.pack_price_minor
            self.stock.receive_in(
                conn,
                item_id,
                ReceiveLot(
                    qty=Decimal(count),
                    total_cost_minor=None if price is None else Decimal(body.packs * price),
                    currency=part.currency,
                    note=body.note,
                ),
                by,
                shop=shop,
            )
            return self._part(conn, item_id, shop)

    def adjust(self, part_id: str, on_hand: int, note: str, by: Actor) -> InventoryPart:
        """A count of the whole part (`StockStore.count_item`)."""
        item_id = _uuid(self.part(part_id).id)
        self.stock.count_item(item_id, Decimal(on_hand), note, by)
        return self.part(part_id)

    # -- helpers ---------------------------------------------------------------------------

    def _part(self, conn: Conn, item_id: UUID, shop: str) -> InventoryPart:
        row = conn.execute(f"{_PARTS} AND i.id = %s", (item_id,)).fetchone()
        if row is None:
            raise PartNotFoundError(str(item_id))
        return _view(dict(row), shop)

    def _lock(self, conn: Conn, item_id: UUID, shop: str) -> InventoryPart:
        conn.execute("SELECT 1 FROM items WHERE id = %s FOR UPDATE", (item_id,))
        return self._part(conn, item_id, shop)

    @staticmethod
    def _named(conn: Conn, kind_id: str, name: str) -> str:
        """The unarchived item of `kind_id` with `name` in any case: the one the unique
        index `items_kind_name` names in a clash."""
        row = conn.execute(
            "SELECT id::text AS id FROM items WHERE kind_id = %s AND lower(name) = lower(%s)"
            " AND archived_at IS NULL",
            (kind_id, name),
        ).fetchone()
        return "" if row is None else str(row["id"])

    @staticmethod
    def _current_offer(conn: Conn, item_id: UUID) -> UUID | None:
        row = conn.execute(
            "SELECT id FROM supplier_offers WHERE item_id = %s AND archived_at IS NULL"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return None if row is None else UUID(str(row["id"]))

    @staticmethod
    def _offer(conn: Conn, item_id: UUID, supplier: str, link: str, pack_size: int) -> UUID:
        """A new offer of the item at the unarchived supplier of that name in any case,
        made when there is none (as the parts migration matches them)."""
        name = supplier.strip()
        found = conn.execute(
            "SELECT id FROM filament_suppliers WHERE upper(btrim(name)) = upper(%s)"
            " AND archived_at IS NULL ORDER BY created_at, id LIMIT 1",
            (name,),
        ).fetchone()
        if found is None:
            # A create of the same name meanwhile: `filament_suppliers_name` keeps one.
            conn.execute(
                "INSERT INTO filament_suppliers (name) VALUES (%s) ON CONFLICT DO NOTHING",
                (name,),
            )
            found = conn.execute(
                "SELECT id FROM filament_suppliers WHERE upper(name) = upper(%s)"
                " AND archived_at IS NULL",
                (name,),
            ).fetchone()
        assert found is not None
        row = conn.execute(
            "INSERT INTO supplier_offers (supplier_id, item_id, sku, url, pack_size, created_at)"
            " VALUES (%s, %s, 'PART-' || left(%s::text, 8), %s, %s, clock_timestamp())"
            " RETURNING id",
            (found["id"], item_id, item_id, link or None, pack_size),
        ).fetchone()
        assert row is not None
        return UUID(str(row["id"]))

    @staticmethod
    def _price_point(conn: Conn, offer: UUID, price: Decimal, currency: str) -> None:
        conn.execute(
            "INSERT INTO offer_price_points (offer_id, price, currency, on_date, source,"
            " created_at) VALUES (%s, %s, %s, %s, 'manual', clock_timestamp())",
            (offer, price, currency, datetime.now(UTC).date()),
        )

    def _drop_purchase_links(self, conn: Conn, item_id: UUID) -> None:
        """The item's `purchase` links the aliases wrote (`source` 'parts') or the parts
        migration wrote ('parts_migration'), so a blank link clears either."""
        conn.execute(
            "DELETE FROM entity_links WHERE kind = 'purchase' AND source IN (%s, 'parts_migration')"
            " AND entity_id IN (SELECT id FROM entities WHERE kind = 'item' AND ref = %s)",
            (_SOURCE, str(item_id)),
        )

    def _purchase_link(self, conn: Conn, item_id: UUID, url: str, by: Actor) -> None:
        entity = self.entities.ensure(conn, EntityRef(kind="item", ref=str(item_id)))
        conn.execute(
            "INSERT INTO entity_links (entity_id, url, kind, position, source, created_by)"
            " VALUES (%s, %s, 'purchase', (SELECT coalesce(max(position) + 1, 0)"
            " FROM entity_links WHERE entity_id = %s), %s, %s)",
            (entity, url, entity, _SOURCE, Jsonb(by.model_dump(mode="json"))),
        )


def _stock_move(move: Move, kind: str, on_hand_after: Decimal) -> StockMove:
    amount: int | None = None
    if not move.cost_unknown:
        amount = int(abs(move.amount_minor).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return StockMove.model_validate(
        {
            "id": str(move.id),
            "part_id": str(move.item_id),
            "kind": kind,
            "delta": int(move.qty),
            "on_hand_after": int(on_hand_after),
            "unit_cost_after_minor": move.unit_cost_minor,
            "amount_minor": amount,
            "note": move.reason,
            "order_id": move.order_id,
            "order_item_id": move.order_item_id,
            "created_by": move.actor,
            "created_at": move.at,
        }
    )
