"""The entity registry (#6950, spec 6.1): which kinds exist, where their rows live, and
the `entities` rows that files, links and properties hang off.

An entity row is made when the first file, link or property attaches, and only for an
owner row that exists. The owner's table has no foreign key to it, so `orphans` finds the rows
whose owner is gone."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow

from scadbuddy.entities.actor import set_actor
from scadbuddy.entities.refs import EntityRef
from scadbuddy.orders.models import Actor

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
        _kind("customer", "customers"),
        _kind("item", "items", "uuid"),
        _kind("lot", "stock_lots", "uuid"),
        _kind("unit", "stock_units", "uuid"),
        # The filament catalogs' rows (`filament/catalog.py` `CATALOGS`).
        _kind("material", "filament_materials", "uuid"),
        _kind("subtype", "filament_subtypes", "uuid"),
        _kind("brand", "filament_brands", "uuid"),
        _kind("category", "filament_categories", "uuid"),
        _kind("effect", "filament_effects", "uuid"),
        _kind("insert_material", "filament_insert_materials", "uuid"),
        _kind("insert_type", "filament_insert_types", "uuid"),
        _kind("colour", "filament_colours", "uuid"),
        _kind("location_kind", "filament_location_kinds", "uuid"),
    )
}


#: The entity kind whose rows a table holds (a catalog's rows, #7703).
KIND_OF_TABLE: dict[str, str] = {k.table: k.name for k in KINDS.values()}


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


_UUID_TEXT = "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'"


def _ref_as_id(kind: EntityKind) -> str:
    """`e.ref` as the owner's id column takes it, so the owner's primary-key index serves
    the sweep's check; a ref that is no uuid's text is null, which matches no row."""
    if kind.id_cast != "uuid":
        return "e.ref"
    return f"CASE WHEN e.ref ~ {_UUID_TEXT} THEN e.ref::uuid END"


def repoint_entity(conn: Conn, kind: str, gone: str, kept: str, by: Actor) -> None:
    """Make `kind:kept` own what `kind:gone` owned: its files, links and properties (a key
    both have keeps `kept`'s value), and the stock moves that name it. For a merge that
    deletes `gone`'s row and keeps `kept`'s (the printers' `PRINTER_REFERENCES`), in its
    transaction; without it the sweep would take them as an orphan's. ``by`` is the
    history's actor of each key moved or removed (`set_actor`, which takes the event
    log's lock before the rows below)."""
    set_actor(conn, by)
    # Locked in id order, as `apply` locks an entity, so a property write on `gone` cannot
    # commit between the copy below and the delete, which cascades.
    rows = conn.execute(
        "SELECT ref, id FROM entities WHERE kind = %s AND ref = ANY(%s) ORDER BY id FOR UPDATE",
        (kind, [gone, kept]),
    ).fetchall()
    ids = {r["ref"]: r["id"] for r in rows}
    if gone not in ids:
        return
    if kept not in ids:
        conn.execute("UPDATE entities SET ref = %s WHERE id = %s", (kept, ids[gone]))
        conn.execute(
            "UPDATE entity_attachments SET subject = %s WHERE entity_id = %s",
            (f"{kind}:{kept}", ids[gone]),
        )
        return
    conn.execute(
        "UPDATE entity_attachments SET entity_id = %s, subject = %s WHERE entity_id = %s",
        (ids[kept], f"{kind}:{kept}", ids[gone]),
    )
    conn.execute(
        "UPDATE entity_links SET entity_id = %s WHERE entity_id = %s", (ids[kept], ids[gone])
    )
    # A stock move's foreign key would refuse the delete below (R-P2-4, #7067).
    conn.execute(
        "UPDATE stock_moves SET entity_id = %s WHERE entity_id = %s", (ids[kept], ids[gone])
    )
    conn.execute(
        "INSERT INTO entity_properties (entity_id, key, value, source, updated_at, updated_by)"
        " SELECT %s, key, value, source, updated_at, updated_by FROM entity_properties"
        " WHERE entity_id = %s ON CONFLICT (entity_id, key) DO NOTHING",
        (ids[kept], ids[gone]),
    )
    conn.execute("DELETE FROM entities WHERE id = %s", (ids[gone],))


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

    def orphans(
        self, conn: Conn, limit: int, *, guarded: Collection[str] = ()
    ) -> list[tuple[UUID, EntityRef]]:
        """Up to `limit` entities whose owner's row is gone and that no stock move names
        (a move keeps its entity, R-P2-4, #7067). One of a kind in `guarded` is taken only
        once no `entity_attachments` row names it."""
        found: list[tuple[UUID, EntityRef]] = []
        for kind in KINDS.values():
            if len(found) >= limit:
                break
            guard = (
                " AND NOT EXISTS (SELECT 1 FROM entity_attachments a WHERE a.entity_id = e.id)"
                if kind.name in guarded
                else ""
            )
            rows = conn.execute(
                "SELECT e.id, e.ref FROM entities e WHERE e.kind = %s AND NOT EXISTS"
                f" (SELECT 1 FROM {kind.table} t"
                f"  WHERE t.{kind.id_column} = {_ref_as_id(kind)}){guard}"
                " AND NOT EXISTS (SELECT 1 FROM stock_moves m WHERE m.entity_id = e.id)"
                " ORDER BY e.created_at, e.id LIMIT %s".encode(),
                (kind.name, limit - len(found)),
            ).fetchall()
            found.extend((UUID(str(r["id"])), EntityRef(kind.name, r["ref"])) for r in rows)
        return found
