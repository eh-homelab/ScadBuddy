"""Item kinds' schemas (#7067, spec §6.4): a tree of rows. A kind's `attribute_schema`
documents and types its items' properties and never restricts them (§6.3a); a child kind
inherits its parent's keys, and its own subschema for a key wins. scadbuddy-core writes the
kinds since K1 (#6949); the entities' property checks (`entities/`) and the property
schema route (`api/entity_properties.py`) still read the schemas here."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow

from scadbuddy.inventory.models import empty_schema

Conn = Connection[DictRow]


#: The kind and its ancestors, with each one's depth below the kind (the kind is 0).
_ANCESTORS = (
    "WITH RECURSIVE up (id, parent_id, attribute_schema, depth) AS ("
    " SELECT id, parent_id, attribute_schema, 0 FROM item_kinds WHERE id = %s"
    " UNION ALL SELECT k.id, k.parent_id, k.attribute_schema, up.depth + 1"
    " FROM item_kinds k JOIN up ON k.id = up.parent_id WHERE up.depth < 100)"
)


def merged_schema(conn: Conn, kind_id: str) -> dict[str, Any]:
    """The kind's schema merged down from its root: each ancestor's `properties`, then the
    kind's own, so a child's key wins, and (R-K1-33) each `required` key, ancestor first
    and once, as Go's `mergedSchema`; no `required` when none is. The empty object schema
    for no such kind."""
    rows = conn.execute(
        _ANCESTORS + " SELECT attribute_schema FROM up ORDER BY depth DESC", (kind_id,)
    ).fetchall()
    properties: dict[str, Any] = {}
    required: list[str] = []
    for row in rows:
        properties.update(row["attribute_schema"].get("properties", {}))
        required += [k for k in row["attribute_schema"].get("required") or () if k not in required]
    merged: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        merged["required"] = required
    return merged


#: Each special type's `requires_properties`, as Go's catalog (`handlers.Builtin`) has them;
#: `tools/go/core/internal/inventory/store/testdata/special_requires.json` holds both sides'
#: test vector.
SPECIAL_REQUIRES: dict[str, list[str]] = {
    "filament_spool": ["rgba", "diameter_mm"],
    "printer": ["model_code"],
}


def kind_item_schema(conn: Conn, kind_id: str) -> dict[str, Any]:
    """What an item of the kind is checked against (R-K1-33), as Go's `itemSchema`: the
    merged schema, its `required` followed by the special type's `requires_properties`,
    each key once. A chain sets at most one special type; with two, Go resolves none and
    adds no key, and so does this."""
    schema = merged_schema(conn, kind_id)
    rows = conn.execute(
        "WITH RECURSIVE up (id, parent_id, special_type, depth) AS ("
        " SELECT id, parent_id, special_type, 0 FROM item_kinds WHERE id = %s"
        " UNION ALL SELECT k.id, k.parent_id, k.special_type, up.depth + 1"
        " FROM item_kinds k JOIN up ON k.id = up.parent_id WHERE up.depth < 100)"
        " SELECT special_type FROM up WHERE special_type IS NOT NULL",
        (kind_id,),
    ).fetchall()
    if len(rows) != 1:
        return schema
    required = list(schema.get("required", []))
    required += [k for k in SPECIAL_REQUIRES.get(rows[0]["special_type"], ()) if k not in required]
    if required:
        schema["required"] = required
    return schema


def item_schema(conn: Conn, item_id: str) -> dict[str, Any]:
    """The item's kind's `kind_item_schema`; the empty object schema for an id that names
    no item."""
    try:
        value = UUID(item_id)
    except ValueError:
        return empty_schema()
    row = conn.execute("SELECT kind_id FROM items WHERE id = %s", (value,)).fetchone()
    return empty_schema() if row is None else kind_item_schema(conn, row["kind_id"])


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
