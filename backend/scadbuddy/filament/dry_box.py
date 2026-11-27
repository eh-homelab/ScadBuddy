"""Dry boxes (#7167): a place whose kind is dry storage and whose capacity is 1 holds one
spool, and a spool keeps the last dry box it sat in as its home (``home_location_id``).

Every writer that can put a spool in a box goes through `LocationStore.move_on`, which
asks `occupant_on` before it writes. (`locations.repoint_printer_locations` writes
``location_id`` directly, but it moves spools between printer places only, and those are
never boxes.) The place's row is locked ``FOR NO KEY UPDATE`` first and the occupant read
after, so two moves into one box serialize and the second sees the first's spool. A tray
place and the external holder are never boxes (their kinds are not dry storage)."""

from __future__ import annotations

from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow

Conn = Connection[DictRow]

#: A live dry box, as a place ``l`` joined to its kind ``k``: the one meaning that the
#: migration's backfill (`20261127T0754Z_filament_dry_boxes.sql`) uses too. A place with a
#: printer (a tray, an AMS unit, the external holder, a printer's root) is never a box,
#: whatever its kind and capacity; a kind's archive state does not change it.
BOX = "k.dry_storage AND l.capacity = 1 AND l.archived_at IS NULL AND l.printer_id IS NULL"


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
    empty box and for a place that is no box. Locks the box's row ``FOR NO KEY UPDATE``
    first (the lock `LocationStore.move_on` takes on every target, so none is upgraded),
    then reads, in the caller's transaction."""
    # Two statements: in READ COMMITTED the second takes a new snapshot, so it sees a kind
    # flip (`check_kind_flip`) that committed while the first waited for the lock.
    conn.execute(
        "SELECT id FROM filament_locations WHERE id = %s FOR NO KEY UPDATE", (location_id,)
    )
    if not is_dry_box(conn, location_id):
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


def lock_kind_places(conn: Conn, kind_id: UUID) -> list[UUID]:
    """Lock (FOR NO KEY UPDATE, in id order) the places of ``kind_id`` that may become
    dry boxes: capacity 1, no printer, not archived. Call it before the kind's row is
    written, so the order is a move's (place, then the kind): a move that waits on the
    lock then sees the new flag."""
    rows = conn.execute(
        "SELECT id FROM filament_locations WHERE kind_id = %s AND capacity = 1"
        " AND archived_at IS NULL AND printer_id IS NULL ORDER BY id FOR NO KEY UPDATE",
        (kind_id,),
    ).fetchall()
    return [row["id"] for row in rows]


def check_kind_flip(conn: Conn, places: list[UUID]) -> None:
    """`BoxHasManyError` when one of ``places`` (`lock_kind_places`) holds two unarchived
    spools or more."""
    if not places:
        return
    many = conn.execute(
        "SELECT 1 FROM filament_spools WHERE location_id = ANY(%s) AND archived_at IS NULL"
        " GROUP BY location_id HAVING count(*) > 1 LIMIT 1",
        (places,),
    ).fetchone()
    if many is not None:
        raise BoxHasManyError()
