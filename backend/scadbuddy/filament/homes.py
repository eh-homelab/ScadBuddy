"""The home return (#7167): a spool whose home dry box is still a live box comes out of
its tray to no place, and is asked (``home_asked_at``) which spool goes into that box
(`filament/capture.py`). The person names the spool by a tag scan, a code or a pick by
hand; the server never picks it, the unloaded spool included.

`move_into` puts the named spool in the box. With ``if_occupied="replace"``, the box's own
spool is archived first at 0 g and its grams are booked as non-print waste
(`FilamentSpoolStore.archive_on`, ``cause="replaced"``). Locks, as `LocationStore.move_on`
takes them, so a plain move and a replace never deadlock: the spools the move writes
(the mover and the answered spool, in id order), then the box's place, then the box's
own spool."""

from __future__ import annotations

from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow

from scadbuddy.filament import models as m
from scadbuddy.filament.dry_box import BOX, occupant_on
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.spools import FilamentSpoolStore

Conn = Connection[DictRow]


class NotItsHomeError(ValueError):
    """``answers`` names a spool whose home box is not the move's target."""

    def __init__(self, spool_id: UUID) -> None:
        super().__init__(f"the home of spool {spool_id} is not this place")
        self.spool_id = spool_id


def live_home_on(conn: Conn, spool_id: UUID) -> UUID | None:
    """The spool's home, when it is still a live dry box (`dry_box.BOX`); else none."""
    row = conn.execute(
        "SELECT l.id FROM filament_spools s JOIN filament_locations l"
        " ON l.id = s.home_location_id JOIN filament_location_kinds k ON k.id = l.kind_id"
        f" WHERE s.id = %s AND {BOX}",
        (spool_id,),
    ).fetchone()
    return None if row is None else row["id"]


def move_into(
    conn: Conn,
    spool_id: UUID,
    location_id: UUID | None,
    *,
    if_occupied: m.IfOccupied,
    actor: str,
    spools: FilamentSpoolStore,
    answers: UUID | None = None,
) -> m.SpoolEvent | None:
    """Move ``spool_id``, the spool the person identified, to ``location_id`` on
    ``conn``, in the caller's transaction. ``refuse`` is `LocationStore.move_on` (a full
    box is `BoxFullError`). ``replace``, on a dry box that holds another spool, archives
    that spool (reason ``other``, waste ``cause`` ``replaced``) and then moves; on an
    empty box, a place that is no box, or a box that holds ``spool_id`` already, it is
    the plain move. ``answers`` is the unloaded spool whose prompt this answers: its
    ``home_asked_at`` clears, and it keeps no place unless it is ``spool_id``.
    `LookupError` for no such spool, `NotItsHomeError` when ``answers``'s home is not
    ``location_id``."""
    wanted = sorted({spool_id} if answers is None else {spool_id, answers})
    found = {
        row["id"]: row["home_location_id"]
        for row in conn.execute(
            "SELECT id, home_location_id FROM filament_spools WHERE id = ANY(%s)"
            " ORDER BY id FOR UPDATE",
            (wanted,),
        ).fetchall()
    }
    for one in wanted:
        if one not in found:
            raise LookupError(f"no spool {one}")
    if answers is not None and (location_id is None or found[answers] != location_id):
        raise NotItsHomeError(answers)
    if if_occupied == "replace" and location_id is not None:
        held = occupant_on(conn, location_id, but=spool_id)
        if held is not None:
            spools.archive_on(
                conn,
                held,
                reason="other",
                final_reading_g=None,
                final_grams=None,
                actor=actor,
                cause="replaced",
            )
    moved = LocationStore.move_on(conn, spool_id, location_id, actor=actor)
    if answers is not None and answers != spool_id:
        conn.execute("UPDATE filament_spools SET home_asked_at = NULL WHERE id = %s", (answers,))
    return moved


def move(
    spools: FilamentSpoolStore,
    spool_id: UUID,
    location_id: UUID | None,
    *,
    if_occupied: m.IfOccupied,
    actor: str,
    answers: UUID | None = None,
) -> m.SpoolEvent | None:
    """`move_into` in a transaction of its own (`POST /filament/spools/{id}/move`)."""
    with spools.pool.connection() as conn, conn.transaction():
        return move_into(
            conn,
            spool_id,
            location_id,
            if_occupied=if_occupied,
            actor=actor,
            spools=spools,
            answers=answers,
        )
