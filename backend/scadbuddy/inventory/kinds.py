"""Item kinds (#7067, spec §6.4): a tree of rows. A kind's `attribute_schema` documents and
types its items' properties and never restricts them (§6.3a); a child kind inherits its
parent's keys, and its own subschema for a key wins. Kinds are rows, never an allowlist in
code (spec §4)."""

from __future__ import annotations

import builtins
from typing import Any
from uuid import UUID

import psycopg
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from scadbuddy.entities.actor import set_actor
from scadbuddy.entities.properties import check_schema
from scadbuddy.inventory.models import ItemKind, KindInput, KindPatch, empty_schema
from scadbuddy.orders.models import Actor

Conn = Connection[DictRow]


class KindNotFound(LookupError):  # noqa: N818
    """No such kind (or no such parent)."""


class KindExists(ValueError):  # noqa: N818
    """A kind with that id is there already."""


class KindCycle(ValueError):  # noqa: N818
    """The parent named is the kind or one of its descendants."""


class KindArchived(ValueError):  # noqa: N818
    """An archived kind named as a new parent, or as the kind of a new or restored
    item."""


class KindInUse(ValueError):  # noqa: N818
    """A change the kind's items or built-in status forbid: a tracking change on a
    built-in kind or on one with items, or the archive of a kind with unarchived items."""


_KIND = (
    "SELECT k.id, k.name, k.parent_id, k.tracking, k.uom, k.attribute_schema, k.life_basis,"
    " k.saleable, k.builtin, k.archived_at,"
    " (SELECT count(*) FROM items i WHERE i.kind_id = k.id AND i.archived_at IS NULL)"
    " AS item_count FROM item_kinds k"
)

#: The kind and its ancestors, with each one's depth below the kind (the kind is 0).
_ANCESTORS = (
    "WITH RECURSIVE up (id, parent_id, attribute_schema, depth) AS ("
    " SELECT id, parent_id, attribute_schema, 0 FROM item_kinds WHERE id = %s"
    " UNION ALL SELECT k.id, k.parent_id, k.attribute_schema, up.depth + 1"
    " FROM item_kinds k JOIN up ON k.id = up.parent_id WHERE up.depth < 100)"
)


def _of(row: dict[str, Any]) -> ItemKind:
    return ItemKind.model_validate(dict(row))


def merged_schema(conn: Conn, kind_id: str) -> dict[str, Any]:
    """The kind's schema merged down from its root: each ancestor's `properties`, then the
    kind's own, so a child's key wins. The empty object schema for no such kind."""
    rows = conn.execute(
        _ANCESTORS + " SELECT attribute_schema FROM up ORDER BY depth DESC", (kind_id,)
    ).fetchall()
    properties: dict[str, Any] = {}
    for row in rows:
        properties.update(row["attribute_schema"].get("properties", {}))
    return {"type": "object", "properties": properties}


def item_schema(conn: Conn, item_id: str) -> dict[str, Any]:
    """The merged schema of the item's kind; the empty object schema for an id that names
    no item."""
    try:
        value = UUID(item_id)
    except ValueError:
        return empty_schema()
    row = conn.execute("SELECT kind_id FROM items WHERE id = %s", (value,)).fetchone()
    return empty_schema() if row is None else merged_schema(conn, row["kind_id"])


def keys_in_use(conn: Conn, kind_id: str) -> list[tuple[str, int]]:
    """Each property key the items of the kind and its descendants have (archived items
    too, as `EntityPropertyStore.keys_in_use`), with how many have it, by key."""
    rows = conn.execute(
        "WITH RECURSIVE down (id, depth) AS ("
        " SELECT id, 0 FROM item_kinds WHERE id = %s UNION ALL"
        " SELECT k.id, down.depth + 1 FROM item_kinds k"
        " JOIN down ON k.parent_id = down.id WHERE down.depth < 100)"
        " SELECT p.key, count(*) AS n FROM entity_properties p"
        " JOIN entities e ON e.id = p.entity_id AND e.kind = 'item'"
        " JOIN items i ON i.id::text = e.ref"
        " WHERE i.kind_id IN (SELECT id FROM down)"
        ' GROUP BY p.key ORDER BY p.key COLLATE "C"',
        (kind_id,),
    ).fetchall()
    return [(r["key"], int(r["n"])) for r in rows]


class KindStore:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    merged_schema = staticmethod(merged_schema)
    item_schema = staticmethod(item_schema)

    def list(self, include_archived: bool = False) -> builtins.list[ItemKind]:
        """Every kind, by id."""
        where = "" if include_archived else " WHERE k.archived_at IS NULL"
        with self.pool.connection() as conn:
            rows = conn.execute(_KIND + where + " ORDER BY k.id").fetchall()
        return [_of(r) for r in rows]

    def get(self, kind_id: str) -> ItemKind | None:
        with self.pool.connection() as conn:
            return self._get(conn, kind_id)

    @staticmethod
    def _get(conn: Conn, kind_id: str) -> ItemKind | None:
        row = conn.execute(_KIND + " WHERE k.id = %s", (kind_id,)).fetchone()
        return None if row is None else _of(row)

    def create(self, body: KindInput, by: Actor) -> ItemKind:
        """`BadPropertySchema` for a schema outside the allowed subset, `KindNotFound` for
        an unknown parent, `KindArchived` for an archived one, `KindExists` for a taken
        id."""
        check_schema(body.attribute_schema)
        with self.pool.connection() as conn, conn.transaction():
            # The schema's history names `by`; the log's lock first (`entities/actor.py`).
            set_actor(conn, by)
            if body.parent_id is not None:
                self._lock_parent(conn, body.parent_id)
            try:
                conn.execute(
                    "INSERT INTO item_kinds (id, name, parent_id, tracking, uom,"
                    " attribute_schema, life_basis, saleable) VALUES (%s, %s, %s, %s, %s, %s,"
                    " %s, %s)",
                    (
                        body.id,
                        body.name,
                        body.parent_id,
                        body.tracking,
                        body.uom,
                        Jsonb(body.attribute_schema),
                        body.life_basis,
                        body.saleable,
                    ),
                )
            except psycopg.errors.UniqueViolation:
                raise KindExists(body.id) from None
            made = self._get(conn, body.id)
        assert made is not None
        return made

    def update(self, kind_id: str, patch: KindPatch, by: Actor) -> ItemKind:
        """The fields `patch` names, or nothing: `BadPropertySchema` leaves the old schema,
        `KindCycle` refuses a parent at or below the kind, `KindArchived` an archived
        parent, `KindInUse` a tracking change on a built-in kind or one with items."""
        if patch.attribute_schema is not None:
            check_schema(patch.attribute_schema)
        fields = patch.model_dump(exclude_unset=True)
        # Only `parent_id` may be set to null; another null keeps the value.
        changes = {k: v for k, v in fields.items() if v is not None or k == "parent_id"}
        with self.pool.connection() as conn, conn.transaction():
            set_actor(conn, by)
            row = conn.execute(
                "SELECT tracking, builtin FROM item_kinds WHERE id = %s FOR UPDATE", (kind_id,)
            ).fetchone()
            if row is None:
                raise KindNotFound(kind_id)
            if "tracking" in changes and changes["tracking"] != row["tracking"]:
                if row["builtin"]:
                    raise KindInUse(f"{kind_id} is built in; its tracking stays")
                if self._has_items(conn, kind_id, archived_too=True):
                    raise KindInUse(f"{kind_id} has items; its tracking stays")
            parent = changes.get("parent_id")
            if parent is not None:
                self._lock_parent(conn, parent)
                if kind_id in self._ancestors(conn, parent):
                    raise KindCycle(f"{parent} is {kind_id} or below it")
            if changes:
                sets = ", ".join(f"{k} = %s" for k in changes)
                values = [Jsonb(v) if k == "attribute_schema" else v for k, v in changes.items()]
                conn.execute(
                    f"UPDATE item_kinds SET {sets}, updated_at = now() WHERE id = %s",
                    (*values, kind_id),
                )
            kind = self._get(conn, kind_id)
        assert kind is not None
        return kind

    def archive(self, kind_id: str) -> ItemKind:
        """`KindInUse` while the kind has unarchived items."""
        with self.pool.connection() as conn, conn.transaction():
            if (
                conn.execute(
                    "SELECT 1 FROM item_kinds WHERE id = %s FOR UPDATE", (kind_id,)
                ).fetchone()
                is None
            ):
                raise KindNotFound(kind_id)
            if self._has_items(conn, kind_id, archived_too=False):
                raise KindInUse(f"{kind_id} has items; archive them first")
            conn.execute(
                "UPDATE item_kinds SET archived_at = coalesce(archived_at, now()),"
                " updated_at = now() WHERE id = %s",
                (kind_id,),
            )
            kind = self._get(conn, kind_id)
        assert kind is not None
        return kind

    @staticmethod
    def _lock_parent(conn: Conn, parent_id: str) -> None:
        """One tree change at a time, so two moves cannot each pass the cycle check and
        make a cycle together; then the parent, held until commit so it cannot be
        archived meanwhile. `KindNotFound` or `KindArchived`."""
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('item_kinds_tree'))")
        row = conn.execute(
            "SELECT archived_at FROM item_kinds WHERE id = %s FOR SHARE", (parent_id,)
        ).fetchone()
        if row is None:
            raise KindNotFound(parent_id)
        if row["archived_at"] is not None:
            raise KindArchived(parent_id)

    def restore(self, kind_id: str) -> ItemKind:
        with self.pool.connection() as conn:
            done = conn.execute(
                "UPDATE item_kinds SET archived_at = NULL, updated_at = now() WHERE id = %s",
                (kind_id,),
            ).rowcount
            if not done:
                raise KindNotFound(kind_id)
            kind = self._get(conn, kind_id)
        assert kind is not None
        return kind

    def keys_in_use(self, kind_id: str) -> builtins.list[tuple[str, int]]:
        with self.pool.connection() as conn:
            return keys_in_use(conn, kind_id)

    def schema_of(
        self, kind_id: str
    ) -> tuple[ItemKind, dict[str, Any], builtins.list[tuple[str, int]]] | None:
        """The kind, its merged schema and its keys in use, from one snapshot; None for
        no such kind."""
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            kind = self._get(conn, kind_id)
            if kind is None:
                return None
            return kind, merged_schema(conn, kind_id), keys_in_use(conn, kind_id)

    @staticmethod
    def _has_items(conn: Conn, kind_id: str, *, archived_too: bool) -> bool:
        live = "" if archived_too else " AND archived_at IS NULL"
        return (
            conn.execute(
                "SELECT 1 FROM items WHERE kind_id = %s" + live + " LIMIT 1",
                (kind_id,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _ancestors(conn: Conn, kind_id: str) -> set[str]:
        """The kind and its ancestors."""
        rows = conn.execute(_ANCESTORS + " SELECT id FROM up", (kind_id,)).fetchall()
        return {r["id"] for r in rows}
