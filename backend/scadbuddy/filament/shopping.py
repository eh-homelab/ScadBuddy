"""The shopping list (#6266, epic #6260, spec §5.8): the spools a person means to buy.

An item is text, not a join: it keeps its material, subtype, brand and colour name when its
type is archived. Given a ``type_id`` and no text, the names come from the type. No trigger
keeps ``updated_at``: each write here sets it.

An empty spool adds its type (#6868): `add_for_empty_on` runs in the transaction of the
ledger write that moved the spool into empty (`ledger.record`), so the item is added with
that write or not at all. It orders nothing: spec §2 keeps reorder automation out.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Final, Literal
from uuid import UUID

from psycopg import Connection, errors
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.filament import purchasing_models as pm

Conn = Connection[DictRow]
_Items = list[pm.ShoppingItem]

logger = logging.getLogger(__name__)

_COLUMNS = ", ".join(pm.ShoppingItem.model_fields)
_TEXT = ("material", "subtype", "brand", "colour_name")
_TYPE_NAMES: Final = (
    "SELECT mat.name AS material, sub.name AS subtype, b.name AS brand,"
    " t.colour_name AS colour_name FROM filament_spool_types t"
    " JOIN filament_materials mat ON mat.id = t.material_id"
    " LEFT JOIN filament_subtypes sub ON sub.id = t.subtype_id"
    " LEFT JOIN filament_brands b ON b.id = t.brand_id WHERE t.id = %s"
)

#: The stored setting (#6868, Settings → Filament): whether an empty spool adds its type.
#: Stored only, on when it has no row; read in the emptying write's transaction.
AUTO_ADD_SETTING: Final = "filament_shopping_auto_add_empty"
#: What moved a spool into empty: its grams reached 0, or it was archived.
EmptyCause = Literal["ran_out", "archived"]
_CAUSE_WORDS: Final[dict[str, str]] = {"ran_out": "ran out", "archived": "was archived"}
#: The transaction-scoped try-lock that one item key is checked and added under.
_LOCK: Final = "scadbuddy:filament:shopping_auto:"


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
                names = conn.execute(_TYPE_NAMES, (body.type_id,)).fetchone()
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


def add_for_empty_on(
    conn: Conn, spool_id: UUID, *, cause: EmptyCause, at: datetime
) -> pm.ShoppingItem | None:
    """Add the spool's type to the list for its move into empty at ``at``, on ``conn``'s
    transaction; ``None`` when nothing is added. Nothing is added with `AUTO_ADD_SETTING`
    off, or while a ``pending`` item holds the spool's type. A spool with no type adds a
    text item of its own material, subtype, brand and colour name, and a pending item with
    no type and the same names (case ignored) holds it. The note names the spool and when.

    The check and the insert run under a transaction try-lock of the item's key: a writer
    that holds it is adding that same item, so this one adds none and never waits (the
    caller holds the spool's row lock). It runs in a savepoint: a failure is logged and
    never undoes or fails the write that emptied the spool."""
    try:
        with conn.transaction():
            return _add_for_empty(conn, spool_id, cause, at)
    except Exception:
        logger.exception("the empty spool's shopping item was not added")
        return None


def _add_for_empty(
    conn: Conn, spool_id: UUID, cause: EmptyCause, at: datetime
) -> pm.ShoppingItem | None:
    switch = conn.execute(
        "SELECT value FROM settings WHERE name = %s", (AUTO_ADD_SETTING,)
    ).fetchone()
    # Stored as JSON: no row (the default) is on, and only `false` is off.
    if switch is not None and switch["value"] is False:
        return None
    spool = conn.execute(
        "SELECT s.number, s.type_id, mat.name AS material, sub.name AS subtype,"
        " b.name AS brand, s.colour_name FROM filament_spools s"
        " LEFT JOIN filament_materials mat ON mat.id = s.material_id"
        " LEFT JOIN filament_subtypes sub ON sub.id = s.subtype_id"
        " LEFT JOIN filament_brands b ON b.id = s.brand_id WHERE s.id = %s",
        (spool_id,),
    ).fetchone()
    if spool is None:
        return None
    type_id: UUID | None = spool["type_id"]
    if type_id is not None:
        names = conn.execute(_TYPE_NAMES, (type_id,)).fetchone()
        if names is None:
            return None
        values = {n: names[n] for n in _TEXT}
        key = str(type_id)
    else:
        values = {n: spool[n] for n in _TEXT}
        if not values["material"]:
            return None
        key = "text:" + "|".join((values[n] or "").casefold() for n in _TEXT)
    locked = conn.execute(
        "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS ok", (_LOCK + key,)
    ).fetchone()
    if locked is None or not locked["ok"]:
        return None
    if type_id is not None:
        pending = conn.execute(
            "SELECT 1 FROM filament_shopping_items WHERE type_id = %s AND status = 'pending'"
            " LIMIT 1",
            (type_id,),
        ).fetchone()
    else:
        pending = conn.execute(
            "SELECT 1 FROM filament_shopping_items WHERE type_id IS NULL"
            " AND status = 'pending' AND lower(material) = lower(%s)"
            " AND lower(coalesce(subtype, '')) = lower(%s)"
            " AND lower(coalesce(brand, '')) = lower(%s)"
            " AND lower(coalesce(colour_name, '')) = lower(%s) LIMIT 1",
            [values[n] or "" for n in _TEXT],
        ).fetchone()
    if pending is not None:
        return None
    when = at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    note = f"Spool #{spool['number']} {_CAUSE_WORDS[cause]} on {when}."
    columns = ["type_id", *_TEXT, "note", "empty_spool_id"]
    added = conn.execute(
        f"INSERT INTO filament_shopping_items ({', '.join(columns)})"
        f" VALUES ({', '.join('%s' for _ in columns)}) RETURNING {_COLUMNS}",
        [type_id, *(values[n] for n in _TEXT), note, spool_id],
    ).fetchone()
    assert added is not None
    return pm.ShoppingItem.model_validate(added)
