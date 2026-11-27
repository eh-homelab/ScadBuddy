"""Find the row a typed code names (#6955). An archived row still resolves."""

from __future__ import annotations

from typing import Literal, NamedTuple
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow
from pydantic import BaseModel

from scadbuddy.codes.parse import Code, format_code

Conn = Connection[DictRow]


Kind = Literal["location", "spool", "item_kind"]


class Lookup(NamedTuple):
    table: str
    kind: Kind


#: One entry per prefix. A new prefix is a new entry; the SQL below is built from `table`
#: only, which is never user input.
PREFIXES: dict[str, Lookup] = {
    "L": Lookup("filament_locations", "location"),
    "S": Lookup("filament_spools", "spool"),
    "K": Lookup("item_kinds", "item_kind"),
}

#: Spools have no name column.
_NAME = {"filament_locations": "name", "filament_spools": "NULL", "item_kinds": "name"}


class Resolved(BaseModel):
    kind: Kind
    #: A location's and a spool's id is a uuid; an item kind's is its text id (`printer`,
    #: `k12`).
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
        f"SELECT id, {_NAME[table]} AS name, archived_at FROM {table} WHERE number = %s",
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
