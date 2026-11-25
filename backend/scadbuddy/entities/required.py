"""Whether rows are complete (#7703, spec 6.3b): each required key has a value and every
defined key's stored value fits. A create or save through a row's own route calls
`check_row` in the row's transaction; reads call `completeness_of_kind`. Nothing is stored:
a schema change needs no backfill."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow

from scadbuddy.entities.properties import (
    ITEM,
    Completeness,
    MissingRequiredProperties,
    completeness,
    schema_for,
)
from scadbuddy.entities.refs import EntityRef

Conn = Connection[DictRow]

_VALUES = (
    "SELECT e.ref, p.key, p.value FROM entities e JOIN entity_properties p"
    " ON p.entity_id = e.id WHERE e.kind = %s AND e.ref = ANY(%s)"
)


def check_row(conn: Conn, ref: EntityRef) -> None:
    """`MissingRequiredProperties` unless `ref`'s stored properties make it complete."""
    found = completeness_of_kind(conn, ref.kind, [ref.ref])[ref.ref]
    if not found.complete:
        raise MissingRequiredProperties(found.missing, found.invalid)


def completeness_of_kind(conn: Conn, kind: str, refs: Iterable[str]) -> dict[str, Completeness]:
    """Each ref's `Completeness`, from one read of the kind's schema (an item's: its item
    kind's) and one read of the values."""
    wanted = list(dict.fromkeys(refs))
    if not wanted:
        return {}
    values: dict[str, dict[str, Any]] = {ref: {} for ref in wanted}
    for row in conn.execute(_VALUES, (kind, wanted)).fetchall():
        values[row["ref"]][row["key"]] = row["value"]
    if kind != ITEM:
        schema = schema_for(conn, EntityRef(kind, wanted[0]))
        if not schema.get("required") and not schema.get("properties"):
            return {ref: Completeness() for ref in wanted}
        return {ref: completeness(schema, values[ref]) for ref in wanted}
    return _items(conn, wanted, values)


def _items(
    conn: Conn, refs: list[str], values: dict[str, dict[str, Any]]
) -> dict[str, Completeness]:
    # Imported here: the inventory imports the entities package.
    from scadbuddy.inventory.kinds import merged_schema

    ids = [UUID(ref) for ref in refs if _is_uuid(ref)]
    kind_of = {
        str(r["id"]): r["kind_id"]
        for r in conn.execute("SELECT id, kind_id FROM items WHERE id = ANY(%s)", (ids,)).fetchall()
    }
    schemas: dict[str, dict[str, Any]] = {}
    found: dict[str, Completeness] = {}
    for ref in refs:
        kind_id = kind_of.get(ref)
        if kind_id is None:
            found[ref] = Completeness()
            continue
        if kind_id not in schemas:
            schemas[kind_id] = merged_schema(conn, kind_id)
        found[ref] = completeness(schemas[kind_id], values[ref])
    return found


def _is_uuid(text: str) -> bool:
    try:
        return str(UUID(text)) == text
    except ValueError:
        return False
