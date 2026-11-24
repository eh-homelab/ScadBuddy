"""Items (#7067, spec §6.4): the catalog entry of a thing, of one kind. An item's
attributes, its manufacturer part number included, are its entity properties (`item:<id>`,
§6.3a), typed by its kind's merged schema (`kinds.item_schema`)."""

from __future__ import annotations

import builtins
from typing import Any
from uuid import UUID

import psycopg
from psycopg import Connection
from psycopg.rows import DictRow

from scadbuddy.entities.registry import EntityStore
from scadbuddy.inventory.kinds import KindArchived, KindNotFound
from scadbuddy.inventory.models import InventoryItemPatch, Item, ItemInput
from scadbuddy.orders.models import Actor

Conn = Connection[DictRow]


class ItemNotFound(LookupError):  # noqa: N818
    """No such item."""


class ItemExists(ValueError):  # noqa: N818
    """An unarchived item of the kind has that name in any case, or an item has that
    import source and ref."""


class FilamentItemRefused(ValueError):  # noqa: N818
    """Items of a `filament` kind wait for phase 3 (#7068; the plan's Ruling 9)."""


#: Every item with its kind's figures and its stock. A lot counts at its `on_hand`; its
#: value and unknown cost only while it holds stock. A unit counts while `in_stock`.
_ITEMS = """
SELECT i.id, i.kind_id, k.tracking, k.uom, i.name, i.manufacturer, i.compatible_with,
    i.low_stock, i.life_hours, i.life_months, i.saleable,
    coalesce(i.saleable, k.saleable) AS effective_saleable, i.source, i.source_ref,
    i.edited_fields, i.note, i.archived_at,
    CASE k.tracking WHEN 'lot' THEN l.on_hand WHEN 'unit' THEN u.on_hand ELSE 0 END
        AS on_hand,
    CASE k.tracking WHEN 'lot' THEN l.value WHEN 'unit' THEN u.value ELSE 0 END
        AS stock_value_minor,
    CASE k.tracking WHEN 'lot' THEN l.unknown WHEN 'unit' THEN u.unknown ELSE false END
        AS cost_unknown
FROM items i
JOIN item_kinds k ON k.id = i.kind_id
CROSS JOIN LATERAL (
    SELECT coalesce(sum(on_hand), 0) AS on_hand,
        coalesce(sum(on_hand * unit_cost_minor) FILTER (WHERE on_hand > 0), 0) AS value,
        coalesce(bool_or(cost_unknown) FILTER (WHERE on_hand > 0), false) AS unknown
    FROM stock_lots WHERE item_id = i.id
) l
CROSS JOIN LATERAL (
    SELECT count(*) AS on_hand, coalesce(sum(cost_minor), 0) AS value,
        coalesce(bool_or(cost_unknown), false) AS unknown
    FROM stock_units WHERE item_id = i.id AND status = 'in_stock'
) u
"""

#: `low_stock > 0 and on_hand <= low_stock`; never for a kind that is not counted.
_LOW = "(s.tracking IN ('lot', 'unit') AND s.low_stock > 0 AND s.on_hand <= s.low_stock)"

#: The fields an import owns until a person changes them (`edited_fields`).
_EDITABLE = (
    "name",
    "manufacturer",
    "compatible_with",
    "low_stock",
    "life_hours",
    "life_months",
    "saleable",
    "note",
)
#: The fields a null in a patch clears; any other null keeps the value.
_NULLABLE = frozenset({"life_hours", "life_months", "saleable"})


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _blank_none(text: str | None) -> str | None:
    return None if text is None or not text.strip() else text


class ItemStore:
    def __init__(self, pool: Any, entities: EntityStore) -> None:
        self.pool = pool
        self.entities = entities

    def list(
        self,
        *,
        kind: str | None = None,
        descendants: bool = True,
        q: str | None = None,
        low: bool = False,
        include_archived: bool = False,
        compatible: str | None = None,
    ) -> builtins.list[Item]:
        """Items by name. `kind` with its descendants unless `descendants` is false; `q`
        matches the name, the manufacturer or any property value, in any case; `low` only
        those at or below their `low_stock`; `compatible` those that name the printer
        model code."""
        where: builtins.list[str] = []
        params: builtins.list[Any] = []
        if not include_archived:
            where.append("s.archived_at IS NULL")
        if kind is not None:
            if descendants:
                where.append(
                    "s.kind_id IN (WITH RECURSIVE down (id, depth) AS ("
                    " SELECT id, 0 FROM item_kinds WHERE id = %s UNION ALL"
                    " SELECT k.id, down.depth + 1 FROM item_kinds k"
                    " JOIN down ON k.parent_id = down.id WHERE down.depth < 100)"
                    " SELECT id FROM down)"
                )
            else:
                where.append("s.kind_id = %s")
            params.append(kind)
        if q:
            like = f"%{_escape_like(q)}%"
            where.append(
                "(s.name ILIKE %s OR s.manufacturer ILIKE %s OR EXISTS (SELECT 1"
                " FROM entity_properties p JOIN entities e ON e.id = p.entity_id"
                " WHERE e.kind = 'item' AND e.ref = s.id::text AND CASE jsonb_typeof(p.value)"
                " WHEN 'array' THEN EXISTS (SELECT 1 FROM jsonb_array_elements_text(p.value) v"
                " WHERE v ILIKE %s) ELSE p.value #>> '{}' ILIKE %s END))"
            )
            params += [like, like, like, like]
        if low:
            where.append(_LOW)
        if compatible is not None:
            where.append("%s = ANY(s.compatible_with)")
            params.append(compatible)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT s.* FROM ({_ITEMS}) s{clause} ORDER BY lower(s.name), s.id",
                params,
            ).fetchall()
            paths = self._paths(conn)
        return [self._of(r, paths) for r in rows]

    def get(self, item_id: UUID) -> Item | None:
        with self.pool.connection() as conn:
            return self._get(conn, item_id)

    def _get(self, conn: Conn, item_id: UUID) -> Item | None:
        row = conn.execute(f"{_ITEMS} WHERE i.id = %s", (item_id,)).fetchone()
        return None if row is None else self._of(row, self._paths(conn))

    def create(self, body: ItemInput, by: Actor) -> Item:
        """`KindNotFound`, `KindArchived`, `FilamentItemRefused` (Ruling 9) or
        `ItemExists`. A blank `source_ref` is stored as null."""
        with self.pool.connection() as conn, conn.transaction():
            kind = conn.execute(
                "SELECT tracking, archived_at FROM item_kinds WHERE id = %s FOR SHARE",
                (body.kind_id,),
            ).fetchone()
            if kind is None:
                raise KindNotFound(body.kind_id)
            if kind["archived_at"] is not None:
                raise KindArchived(body.kind_id)
            if kind["tracking"] == "filament":
                raise FilamentItemRefused(body.kind_id)
            try:
                row = conn.execute(
                    "INSERT INTO items (kind_id, name, manufacturer, compatible_with,"
                    " low_stock, life_hours, life_months, saleable, source, source_ref, note)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (
                        body.kind_id,
                        body.name,
                        body.manufacturer,
                        body.compatible_with,
                        body.low_stock,
                        body.life_hours,
                        body.life_months,
                        body.saleable,
                        body.source,
                        _blank_none(body.source_ref),
                        body.note,
                    ),
                ).fetchone()
            except psycopg.errors.UniqueViolation:
                raise ItemExists(body.name) from None
            assert row is not None
            made = self._get(conn, row["id"])
        assert made is not None
        return made

    def update(self, item_id: UUID, patch: InventoryItemPatch, by: Actor) -> Item:
        """The fields `patch` names. On an imported item (`source` not `manual`) each
        field whose value changes joins `edited_fields`, which a later import skips."""
        fields = patch.model_dump(exclude_unset=True)
        changes = {k: v for k, v in fields.items() if v is not None or k in _NULLABLE}
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT * FROM items WHERE id = %s FOR UPDATE", (item_id,)
            ).fetchone()
            if row is None:
                raise ItemNotFound(str(item_id))
            changed = [k for k in _EDITABLE if k in changes and changes[k] != row[k]]
            if not changed:
                found = self._get(conn, item_id)
                assert found is not None
                return found
            edited = list(row["edited_fields"])
            if row["source"] != "manual":
                edited += [k for k in changed if k not in edited]
            sets = ", ".join(f"{k} = %s" for k in changed)
            try:
                conn.execute(
                    f"UPDATE items SET {sets}, edited_fields = %s, updated_at = now()"
                    " WHERE id = %s",
                    (*(changes[k] for k in changed), edited, item_id),
                )
            except psycopg.errors.UniqueViolation:
                raise ItemExists(str(changes.get("name", row["name"]))) from None
            found = self._get(conn, item_id)
        assert found is not None
        return found

    def archive(self, item_id: UUID) -> Item:
        with self.pool.connection() as conn, conn.transaction():
            done = conn.execute(
                "UPDATE items SET archived_at = coalesce(archived_at, now()),"
                " updated_at = now() WHERE id = %s",
                (item_id,),
            ).rowcount
            if not done:
                raise ItemNotFound(str(item_id))
            found = self._get(conn, item_id)
        assert found is not None
        return found

    def restore(self, item_id: UUID) -> Item:
        """`KindArchived` while its kind is archived; `ItemExists` when an unarchived
        item of its kind took its name meanwhile."""
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT name, kind_id FROM items WHERE id = %s FOR UPDATE", (item_id,)
            ).fetchone()
            if row is None:
                raise ItemNotFound(str(item_id))
            kind = conn.execute(
                "SELECT archived_at FROM item_kinds WHERE id = %s FOR SHARE", (row["kind_id"],)
            ).fetchone()
            assert kind is not None
            if kind["archived_at"] is not None:
                raise KindArchived(row["kind_id"])
            try:
                conn.execute(
                    "UPDATE items SET archived_at = NULL, updated_at = now() WHERE id = %s",
                    (item_id,),
                )
            except psycopg.errors.UniqueViolation:
                raise ItemExists(row["name"]) from None
            found = self._get(conn, item_id)
        assert found is not None
        return found

    @staticmethod
    def _paths(conn: Conn) -> dict[str, builtins.list[str]]:
        """Each kind's path, root first."""
        parents = {
            r["id"]: r["parent_id"]
            for r in conn.execute("SELECT id, parent_id FROM item_kinds").fetchall()
        }
        paths: dict[str, builtins.list[str]] = {}
        for kind in parents:
            path: builtins.list[str] = []
            at: str | None = kind
            while at is not None and at not in path:
                path.append(at)
                at = parents.get(at)
            paths[kind] = path[::-1]
        return paths

    @staticmethod
    def _of(row: dict[str, Any], paths: dict[str, builtins.list[str]]) -> Item:
        data = dict(row)
        tracking = data["tracking"]
        counted = tracking in ("lot", "unit")
        data["kind_path"] = paths.get(data["kind_id"], [data["kind_id"]])
        data["low"] = counted and data["low_stock"] > 0 and data["on_hand"] <= data["low_stock"]
        return Item.model_validate(data)
