"""Inventory rows written straight into the tables (#6949 K1): Go owns the kind and item
writes now, so a Python test that needs an item to stand on inserts it as Go's save would
leave it. Only the columns a test reads are set; the rest take their defaults."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb

from scadbuddy.inventory.models import ItemInput, KindInput
from scadbuddy.orders.models import Actor


def insert_kind(pool: Any, kind_id: str | None = None, **columns: Any) -> str:
    """A kind under `part` unless `parent_id` says otherwise, counted in lots."""
    values: dict[str, Any] = {
        # Never `k` and digits only: that id is a numbered kind's (item_kinds_made_id_check).
        "id": kind_id or columns.pop("id", None) or f"kind_{uuid.uuid4().hex[:12]}",
        "name": "Fasteners",
        "parent_id": "part",
        "tracking": "lot",
        **columns,
    }
    if "attribute_schema" in values:
        values["attribute_schema"] = Jsonb(values["attribute_schema"])
    with pool.connection() as conn:
        conn.execute(
            sql.SQL("INSERT INTO item_kinds ({}) VALUES ({})").format(
                sql.SQL(", ").join(map(sql.Identifier, values)),
                sql.SQL(", ").join(sql.Placeholder() * len(values)),
            ),
            list(values.values()),
        )
    return str(values["id"])


def insert_item(pool: Any, kind_id: str = "hardware", **columns: Any) -> UUID:
    """An item of `kind_id` with a unique name unless `name` is given."""
    values: dict[str, Any] = {
        "kind_id": kind_id,
        "name": f"M3 bolt {uuid.uuid4().hex[:6]}",
        **columns,
    }
    with pool.connection() as conn:
        row = conn.execute(
            sql.SQL("INSERT INTO items ({}) VALUES ({}) RETURNING id").format(
                sql.SQL(", ").join(map(sql.Identifier, values)),
                sql.SQL(", ").join(sql.Placeholder() * len(values)),
            ),
            list(values.values()),
        ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


@dataclass(frozen=True)
class RowItem:
    id: UUID
    kind_id: str
    #: The lots' sum, or the units in stock (only `ItemRows.get` reads it).
    on_hand: Decimal = Decimal(0)


class ItemRows:
    """The item rows a test of a Python store stands on, written as Go writes them: the
    shape of the old `ItemStore` calls those tests made (`create`, `get`, `archive`)."""

    def __init__(self, pool: Any) -> None:
        self.pool = pool

    def create(self, body: ItemInput, by: Actor | None = None) -> RowItem:
        fields = body.model_dump(exclude_unset=True, exclude={"steps"})
        fields.setdefault("name", body.name)
        item = insert_item(self.pool, **fields)
        return RowItem(item, body.kind_id)

    def get(self, item_id: UUID) -> RowItem | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT i.id, i.kind_id, CASE k.tracking"
                " WHEN 'unit' THEN (SELECT count(*) FROM stock_units u"
                "  WHERE u.item_id = i.id AND u.status = 'in_stock')"
                " ELSE (SELECT coalesce(sum(on_hand), 0) FROM stock_lots l"
                "  WHERE l.item_id = i.id) END AS on_hand"
                " FROM items i JOIN item_kinds k ON k.id = i.kind_id WHERE i.id = %s",
                (item_id,),
            ).fetchone()
        return None if row is None else RowItem(row["id"], row["kind_id"], Decimal(row["on_hand"]))

    def archive(self, item_id: UUID) -> None:
        with self.pool.connection() as conn:
            conn.execute("UPDATE items SET archived_at = now() WHERE id = %s", (item_id,))


class KindRows:
    """A kind row, as `ItemRows` writes items."""

    def __init__(self, pool: Any) -> None:
        self.pool = pool

    def create(self, body: KindInput, by: Actor | None = None) -> str:
        return insert_kind(self.pool, **body.model_dump(exclude_unset=True, exclude={"steps"}))
