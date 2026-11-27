"""Dry boxes (#7167): a place whose kind is dry storage and whose capacity is 1 holds one
spool, and a spool keeps the last dry box it sat in as its home (``home_location_id``).

Every writer of ``filament_spools.location_id`` goes through `LocationStore.move_on`,
which asks `occupant_on` before it writes. The place's row is locked ``FOR UPDATE`` first
and the occupant read after, so two moves into one box serialize and the second sees the
first's spool. A tray place and the external holder are never boxes (their kinds are not
dry storage)."""

from __future__ import annotations

from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow

Conn = Connection[DictRow]

#: A live dry box, as a place ``l`` joined to its kind ``k``: the one meaning that the
#: migration's backfill (`20261127T0754Z_filament_dry_boxes.sql`) uses too.
BOX = "k.dry_storage AND l.capacity = 1 AND l.archived_at IS NULL"


class BoxFullError(ValueError):
    """The dry box already holds ``occupant``, an unarchived spool."""

    def __init__(self, occupant: UUID) -> None:
        super().__init__(f"the dry box already holds spool {occupant}")
        self.occupant = occupant


class BoxHasManyError(ValueError):
    """A place that holds more than one spool cannot become a dry box."""

    def __init__(self) -> None:
        super().__init__("the place holds more than one spool, so it cannot be a dry box")


def is_dry_box(conn: Conn, location_id: UUID) -> bool:
    """The place is unarchived, of a dry-storage kind, and has capacity 1."""
    return (
        conn.execute(
            "SELECT 1 FROM filament_locations l JOIN filament_location_kinds k"
            f" ON k.id = l.kind_id WHERE l.id = %s AND {BOX}",
            (location_id,),
        ).fetchone()
        is not None
    )


def occupant_on(conn: Conn, location_id: UUID, *, but: UUID | None = None) -> UUID | None:
    """The unarchived spool in the dry box ``location_id`` (not ``but``); none for an
    empty box and for a place that is no box. Locks the box's row ``FOR UPDATE`` first,
    then reads, in the caller's transaction."""
    box = conn.execute(
        "SELECT l.id FROM filament_locations l JOIN filament_location_kinds k"
        f" ON k.id = l.kind_id WHERE l.id = %s AND {BOX} FOR UPDATE OF l",
        (location_id,),
    ).fetchone()
    if box is None:
        return None
    row = conn.execute(
        "SELECT id FROM filament_spools WHERE location_id = %s AND archived_at IS NULL"
        " AND id IS DISTINCT FROM %s ORDER BY number LIMIT 1",
        (location_id, but),
    ).fetchone()
    return None if row is None else row["id"]


def keep_home_on(conn: Conn, spool_id: UUID, location_id: UUID | None) -> None:
    """After a move to ``location_id``: a dry box becomes the spool's home, any other
    place leaves the home as it was, and the spool has not been asked about its home
    since this move (``home_asked_at`` cleared)."""
    box = location_id is not None and is_dry_box(conn, location_id)
    conn.execute(
        "UPDATE filament_spools SET home_location_id = CASE WHEN %s THEN %s"
        " ELSE home_location_id END, home_asked_at = NULL WHERE id = %s",
        (box, location_id, spool_id),
    )


def check_not_many(conn: Conn, location_id: UUID) -> None:
    """`BoxHasManyError` when the place is a dry box that holds two unarchived spools or
    more (call it with the place's row locked)."""
    if not is_dry_box(conn, location_id):
        return
    many = conn.execute(
        "SELECT 1 FROM filament_spools WHERE location_id = %s AND archived_at IS NULL"
        " OFFSET 1 LIMIT 1",
        (location_id,),
    ).fetchone()
    if many is not None:
        raise BoxHasManyError()
