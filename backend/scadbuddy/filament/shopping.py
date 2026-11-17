"""The shopping list (#6266, epic #6260, spec §5.8): the spools a person means to buy.

An item is text, not a join: it keeps its material, subtype, brand and colour name when its
type is archived. Given a ``type_id`` and no text, the names come from the type. No trigger
keeps ``updated_at``: each write here sets it.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg import Connection, errors
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.filament import purchasing_models as pm

Conn = Connection[DictRow]
_Items = list[pm.ShoppingItem]

_COLUMNS = ", ".join(pm.ShoppingItem.model_fields)
_TEXT = ("material", "subtype", "brand", "colour_name")


class ShoppingRefError(ValueError):
    """An item names a spool type that does not exist."""

    def __init__(self) -> None:
        super().__init__("no such spool type")


class ShoppingStore:
    """The shopping list over a pool whose connections autocommit and return dict rows.
    Synchronous: callers run it in a thread."""

    def __init__(self, pool: ConnectionPool[Conn]) -> None:
        self._pool = pool

    def list(self, *, status: pm.ShoppingStatus | None = None) -> _Items:
        """Pending items first, then the newest."""
        where = "" if status is None else " WHERE status = %s"
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM filament_shopping_items{where}"
                " ORDER BY (status = 'pending') DESC, created_at DESC, id",
                () if status is None else (status,),
            ).fetchall()
        return [pm.ShoppingItem.model_validate(row) for row in rows]

    def add(self, body: pm.ShoppingItemCreate) -> pm.ShoppingItem:
        values: dict[str, Any] = {n: getattr(body, n) for n in _TEXT}
        with self._pool.connection() as conn:
            if body.type_id is not None:
                names = conn.execute(
                    "SELECT mat.name AS material, sub.name AS subtype, b.name AS brand,"
                    " t.colour_name AS colour_name FROM filament_spool_types t"
                    " JOIN filament_materials mat ON mat.id = t.material_id"
                    " LEFT JOIN filament_subtypes sub ON sub.id = t.subtype_id"
                    " LEFT JOIN filament_brands b ON b.id = t.brand_id WHERE t.id = %s",
                    (body.type_id,),
                ).fetchone()
                if names is None:
                    raise ShoppingRefError()
                values = {n: values[n] or names[n] for n in _TEXT}
            columns = ["type_id", *_TEXT, "quantity_spools", "note"]
            data = [body.type_id, *(values[n] for n in _TEXT), body.quantity_spools, body.note]
            try:
                row = conn.execute(
                    f"INSERT INTO filament_shopping_items ({', '.join(columns)})"
                    f" VALUES ({', '.join('%s' for _ in columns)}) RETURNING {_COLUMNS}",
                    data,
                ).fetchone()
            except errors.ForeignKeyViolation:
                raise ShoppingRefError() from None
        assert row is not None
        return pm.ShoppingItem.model_validate(row)

    def update(self, item_id: UUID, patch: pm.ShoppingItemPatch) -> pm.ShoppingItem | None:
        sent = [n for n in ("quantity_spools", "note") if n in patch.model_fields_set]
        sets = [f"{n} = %s" for n in sent]
        values: list[Any] = [getattr(patch, n) for n in sent]
        if "status" in patch.model_fields_set and patch.status is not None:
            sets.append("status = %s")
            values.append(patch.status)
            sets.append(
                "purchased_at = COALESCE(purchased_at, now())"
                if patch.status == "purchased"
                else "purchased_at = NULL"
            )
        sets.append("updated_at = now()")
        with self._pool.connection() as conn:
            row = conn.execute(
                f"UPDATE filament_shopping_items SET {', '.join(sets)} WHERE id = %s"
                f" RETURNING {_COLUMNS}",
                [*values, item_id],
            ).fetchone()
        return None if row is None else pm.ShoppingItem.model_validate(row)

    def remove(self, item_id: UUID) -> bool:
        with self._pool.connection() as conn:
            deleted = conn.execute(
                "DELETE FROM filament_shopping_items WHERE id = %s", (item_id,)
            ).rowcount
        return deleted > 0

    def clear(self, *, purchased_only: bool) -> int:
        where = " WHERE status = 'purchased'" if purchased_only else ""
        with self._pool.connection() as conn:
            return conn.execute(f"DELETE FROM filament_shopping_items{where}").rowcount
