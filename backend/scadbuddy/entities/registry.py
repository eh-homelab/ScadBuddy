"""The entity registry (#6950, spec 6.1): which kinds exist, where their rows live, and
the `entities` rows that files and links hang off.

An entity row is made when the first file or link attaches, and only for an owner row
that exists. The owner's table has no foreign key to it, so `orphans` finds the rows
whose owner is gone."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow

from scadbuddy.entities.refs import EntityRef

Conn = Connection[DictRow]


@dataclass(frozen=True)
class EntityKind:
    """The owner's table of a kind: what an existence check and the sweep read."""

    name: str
    table: str
    id_column: str
    id_cast: str = "text"


def _kind(name: str, table: str, id_cast: str = "text") -> EntityKind:
    return EntityKind(name, table, "id", id_cast)


KINDS: dict[str, EntityKind] = {
    k.name: k
    for k in (
        _kind("order", "orders"),
        _kind("order_item", "order_items"),
        _kind("order_item_fulfilment", "order_item_fulfilments"),
        _kind("order_item_revision", "order_item_revisions"),
        _kind("spool", "filament_spools", "uuid"),
        _kind("spool_type", "filament_spool_types", "uuid"),
        _kind("supplier", "filament_suppliers", "uuid"),
        _kind("invoice", "filament_invoices", "uuid"),
        _kind("location", "filament_locations", "uuid"),
        _kind("printer", "printers", "uuid"),
        _kind("part", "parts"),
        _kind("customer", "customers"),
    )
}


class UnknownEntityKind(LookupError):  # noqa: N818
    """No such kind is registered."""


class EntityNotFound(LookupError):  # noqa: N818
    """The owner's row is not there."""


def _id_value(kind: EntityKind, ref: str) -> str | UUID | None:
    """The owner's id as the column takes it, or None when `ref` cannot be one."""
    if kind.id_cast != "uuid":
        return ref
    try:
        value = UUID(ref)
    except ValueError:
        return None
    return value if str(value) == ref else None


class EntityStore:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    @staticmethod
    def _kind(ref: EntityRef) -> EntityKind:
        try:
            return KINDS[ref.kind]
        except KeyError:
            raise UnknownEntityKind(ref.kind) from None

    def exists(self, conn: Conn, ref: EntityRef) -> bool:
        """Whether the owner's row is there."""
        kind = self._kind(ref)
        value = _id_value(kind, ref.ref)
        if value is None:
            return False
        row = conn.execute(
            f"SELECT 1 FROM {kind.table} WHERE {kind.id_column} = %s".encode(),
            (value,),
        ).fetchone()
        return row is not None

    def ensure(self, conn: Conn, ref: EntityRef) -> UUID:
        """The registry row of `ref`, made on first use."""
        if not self.exists(conn, ref):
            raise EntityNotFound(str(ref))
        row = conn.execute(
            "INSERT INTO entities (kind, ref) VALUES (%s, %s)"
            " ON CONFLICT (kind, ref) DO UPDATE SET kind = EXCLUDED.kind RETURNING id",
            (ref.kind, ref.ref),
        ).fetchone()
        assert row is not None
        return UUID(str(row["id"]))

    def find(self, conn: Conn, ref: EntityRef) -> UUID | None:
        row = conn.execute(
            "SELECT id FROM entities WHERE kind = %s AND ref = %s", (ref.kind, ref.ref)
        ).fetchone()
        return None if row is None else UUID(str(row["id"]))

    def orphans(self, conn: Conn, limit: int) -> list[tuple[UUID, EntityRef]]:
        """Up to `limit` entities whose owner's row is gone."""
        found: list[tuple[UUID, EntityRef]] = []
        for kind in KINDS.values():
            if len(found) >= limit:
                break
            rows = conn.execute(
                "SELECT e.id, e.ref FROM entities e WHERE e.kind = %s AND NOT EXISTS"
                f" (SELECT 1 FROM {kind.table} t"
                f"  WHERE t.{kind.id_column}::text = e.ref)"
                " ORDER BY e.created_at, e.id LIMIT %s".encode(),
                (kind.name, limit - len(found)),
            ).fetchall()
            found.extend((UUID(str(r["id"])), EntityRef(kind.name, r["ref"])) for r in rows)
        return found
