"""Whether rows are complete (#7703, spec 6.3b): each required key has a value and every
defined key's stored value fits. A create or save through a row's own route calls
`check_row` in the row's transaction; reads call `completeness_of_kind`. Nothing is stored:
a schema change needs no backfill."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow
from pydantic import JsonValue

from scadbuddy.entities.actor import set_actor
from scadbuddy.entities.properties import (
    ITEM,
    Completeness,
    EntityPropertyStore,
    MissingRequiredProperties,
    PropertyPatch,
    completeness,
    schema_for,
)
from scadbuddy.entities.refs import EntityRef
from scadbuddy.entities.registry import EntityStore
from scadbuddy.orders.models import Actor

Conn = Connection[DictRow]

_VALUES = (
    "SELECT e.ref, p.key, p.value FROM entities e JOIN entity_properties p"
    " ON p.entity_id = e.id WHERE e.kind = %s AND e.ref = ANY(%s)"
)


#: Every method `save_properties_on` calls runs on the connection it is given.
_PROPERTIES = EntityPropertyStore(None, EntityStore(None))

#: The fields a row's save may send and still not be checked (spec 6.3b): an archive, a
#: restore or a re-order is no form save.
EXEMPT_FIELDS = frozenset({"archived", "sort_order"})


def hold_for_properties(conn: Conn, properties: Mapping[str, JsonValue] | None, by: Actor) -> None:
    """When ``properties`` will be written, `set_actor` now: the event log's lock, then
    ``by`` as the history's actor (#6949). A create or save calls it first in its
    transaction, before it locks its own row, so it never holds that row while
    `save_properties_on` waits for the log (a printer merge holds the log and then locks
    spool, location and type rows)."""
    if properties:
        set_actor(conn, by)


def save_properties_on(
    conn: Conn,
    ref: EntityRef,
    properties: Mapping[str, JsonValue] | None,
    by: Actor,
    *,
    check: bool,
) -> None:
    """A create's or save's ``properties`` on the row it just wrote, in its transaction,
    as ``manual`` (spec 6.3b); then, with ``check``, `check_row`. The caller calls
    `hold_for_properties` before it locks that row."""
    if properties:
        _PROPERTIES.apply_on(conn, ref, PropertyPatch(set=dict(properties)), by)
    if check:
        check_row(conn, ref)


class Marked(Protocol):
    """A row a read marks: its id, whether it is archived and the two lists."""

    @property
    def id(self) -> Any: ...
    @property
    def archived_at(self) -> datetime | None: ...

    missing_properties: list[str]
    invalid_properties: list[str]


def mark_incomplete(conn: Conn, kind: str, rows: Sequence[Marked]) -> None:
    """Each unarchived row's missing and ill-fitting keys (spec 6.3b); an archived row is
    never marked."""
    by_ref = {str(row.id): row for row in rows if row.archived_at is None}
    for ref, found in completeness_of_kind(conn, kind, by_ref).items():
        by_ref[ref].missing_properties = list(found.missing)
        by_ref[ref].invalid_properties = [key for key, _ in found.invalid]


def checks_save(fields_set: Iterable[str]) -> bool:
    """Whether a save that sends ``fields_set`` is checked: not one that sends only
    `EXEMPT_FIELDS` (spec 6.3b)."""
    return bool(set(fields_set) - EXEMPT_FIELDS)


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
    schema: dict[str, Any] = {}
    if kind != ITEM:
        schema = schema_for(conn, EntityRef(kind, wanted[0]))
        if not schema.get("required") and not schema.get("properties"):
            return {ref: Completeness() for ref in wanted}  # no values to read
    values: dict[str, dict[str, Any]] = {ref: {} for ref in wanted}
    for row in conn.execute(_VALUES, (kind, wanted)).fetchall():
        values[row["ref"]][row["key"]] = row["value"]
    if kind != ITEM:
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
