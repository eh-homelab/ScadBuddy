"""A spool's effective empty weight (#7682, spec
`docs/superpowers/specs/2026-11-25-weighing-reconciliation-design.md` §5.1): what a
weighing takes off the scale's reading to get the filament.

Today it is the insert type's ``empty_weight_g``: the spool's own insert, else its
type's, the resolution `insert_weights.check_on` makes. Once #7278 lands, a spool with a
carrier and parts gets their sum here, with ``source`` ``parts``; callers do not change.
"""

from __future__ import annotations

from typing import Literal, NamedTuple
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow

from scadbuddy.filament.insert_weights import is_placeholder

Conn = Connection[DictRow]


class EmptyWeight(NamedTuple):
    grams: int
    source: Literal["insert"]
    #: The insert's ``empty_weight_basis``, except ``placeholder`` whenever
    #: `insert_weights.is_placeholder` says so of the basis and the note.
    basis: str
    name: str
    note: str | None


def effective_on(conn: Conn, spool_id: UUID) -> EmptyWeight | None:
    """The spool's effective empty weight; ``None`` for no insert or no such spool."""
    row = conn.execute(
        "SELECT i.empty_weight_g, i.empty_weight_basis, i.name, i.note"
        " FROM filament_spools s LEFT JOIN filament_spool_types t ON t.id = s.type_id"
        " LEFT JOIN filament_insert_types i"
        " ON i.id = coalesce(s.insert_type_id, t.insert_type_id)"
        " WHERE s.id = %s",
        (spool_id,),
    ).fetchone()
    if row is None or row["empty_weight_g"] is None:
        return None
    basis = row["empty_weight_basis"]
    if is_placeholder(basis, row["note"]):
        basis = "placeholder"
    return EmptyWeight(row["empty_weight_g"], "insert", basis, row["name"], row["note"])
