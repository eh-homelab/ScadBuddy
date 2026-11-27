"""Find the row a typed code names (#6955). An archived row still resolves."""

from __future__ import annotations

from typing import Literal, NamedTuple
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow
from pydantic import BaseModel

from scadbuddy.codes.parse import Code, format_code

Conn = Connection[DictRow]


Kind = Literal["location", "spool", "item_kind", "spool_type", "item"]


class Lookup(NamedTuple):
    table: str
    kind: Kind


#: One entry per prefix. A new prefix is a new entry; the SQL below is built from `table`
#: only, which is never user input.
PREFIXES: dict[str, Lookup] = {
    "L": Lookup("filament_locations", "location"),
    "S": Lookup("filament_spools", "spool"),
    "K": Lookup("item_kinds", "item_kind"),
    "T": Lookup("filament_spool_types", "spool_type"),
    "I": Lookup("items", "item"),
}

#: The SQL for each table's name. Spools have no name; a type's name is its code and colour.
#: Public because the label sheet reads it too.
NAME_SQL: dict[str, str] = {
    "filament_locations": "name",
    "filament_spools": "NULL",
    "item_kinds": "name",
    "filament_spool_types": "concat_ws(' ', code, colour_name)",
    "items": "name",
}


class Resolved(BaseModel):
    kind: Kind
    #: A location's, spool's, spool type's and item's id is a uuid; an item kind's is its
    #: text id (`printer`, `k12`).
    id: UUID | str
    code: str
    name: str | None
    archived: bool


def resolve(conn: Conn, code: Code) -> Resolved | None:
    lookup = PREFIXES.get(code.prefix)
    if lookup is None:
        return None
    table, kind = lookup
    row = conn.execute(
        f"SELECT id, {NAME_SQL[table]} AS name, archived_at FROM {table} WHERE number = %s",
        (code.number,),
    ).fetchone()
    if row is None:
        return None
    return Resolved(
        kind=kind,
        id=row["id"],
        code=format_code(code.prefix, code.number),
        name=row["name"],
        archived=row["archived_at"] is not None,
    )
