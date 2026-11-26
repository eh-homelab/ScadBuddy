"""#7143: the rows of ``filament_unknown_trays``, and the wire types of the print block.

A row exists while a printer reports a tray as loaded and no spool holds it. Capture
writes it (`note_on`). Linking a spool clears it (`clear_on`). Only the API process
reads these tables (#5295): the print worker gets the block from a CRUD route.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from typing import Final, Protocol
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from scadbuddy.bambuddy.trays import tray_label
from scadbuddy.spools.models import SeenTray

Conn = Connection[DictRow]

#: The problem code of a print refused for an unknown spool.
UNKNOWN_SPOOL_LOADED: Final = "unknown_spool_loaded"
#: The problem code of a resolve that finds the tray changed since the owner looked.
TRAY_CHANGED: Final = "tray_changed"

_COLUMNS = (
    "id, printer_id, ams_id, tray_id, tray_uuid, tag_uid, material, colour_rgba, remain_pct,"
    " first_seen_at, seen_at"
)


class UnknownTrayRow(BaseModel):
    id: UUID
    printer_id: UUID
    ams_id: int
    tray_id: int
    tray_uuid: str | None
    tag_uid: str | None
    material: str
    colour_rgba: str | None
    remain_pct: int | None
    first_seen_at: datetime
    seen_at: datetime


class UnknownSpoolRef(BaseModel):
    id: UUID
    slot: str


class UnknownSpoolBlock(BaseModel):
    count: int
    trays: list[UnknownSpoolRef]


class UnknownSpoolReads(Protocol):
    async def blocking(self, printer_id: str) -> list[UnknownSpoolRef]: ...


def note_on(conn: Conn, printer_id: UUID, seen: SeenTray, seen_at: datetime) -> UnknownTrayRow:
    """Record the tray. ``seen_at`` moves only when a reported field changes (D1): it is a
    version of the tray, not a heartbeat."""
    row = conn.execute(
        "INSERT INTO filament_unknown_trays AS t (printer_id, ams_id, tray_id, tray_uuid,"
        " tag_uid, material, colour_rgba, remain_pct, first_seen_at, seen_at)"
        " VALUES (%(p)s, %(a)s, %(t)s, %(u)s, %(g)s, %(m)s, %(c)s, %(r)s, %(at)s, %(at)s)"
        " ON CONFLICT (printer_id, ams_id, tray_id) DO UPDATE SET"
        " tray_uuid = EXCLUDED.tray_uuid, tag_uid = EXCLUDED.tag_uid,"
        " material = EXCLUDED.material, colour_rgba = EXCLUDED.colour_rgba,"
        " remain_pct = EXCLUDED.remain_pct, seen_at = EXCLUDED.seen_at"
        " WHERE (t.tray_uuid, t.tag_uid, t.material, t.colour_rgba, t.remain_pct)"
        " IS DISTINCT FROM (EXCLUDED.tray_uuid, EXCLUDED.tag_uid, EXCLUDED.material,"
        " EXCLUDED.colour_rgba, EXCLUDED.remain_pct)"
        f" RETURNING {_COLUMNS}",
        {
            "p": printer_id,
            "a": seen.ams_id,
            "t": seen.tray_id,
            "u": seen.tray_uuid,
            "g": seen.tag_uid,
            "m": seen.material or "",
            "c": seen.rgba,
            "r": seen.remain,
            "at": seen_at,
        },
    ).fetchone()
    if row is None:
        # Nothing changed, so the upsert wrote nothing. Read the row as it stands.
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM filament_unknown_trays"
            " WHERE printer_id = %s AND ams_id = %s AND tray_id = %s",
            (printer_id, seen.ams_id, seen.tray_id),
        ).fetchone()
    assert row is not None
    return UnknownTrayRow.model_validate(row)


def clear_on(conn: Conn, printer_id: UUID, ams_id: int, tray_id: int) -> bool:
    """Delete the tray's row. True when there was one."""
    cur = conn.execute(
        "DELETE FROM filament_unknown_trays WHERE printer_id = %s AND ams_id = %s AND tray_id = %s",
        (printer_id, ams_id, tray_id),
    )
    return cur.rowcount > 0


def rows_on(conn: Conn, printer_id: UUID | None = None) -> list[UnknownTrayRow]:
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM filament_unknown_trays"
        " WHERE (%(p)s::uuid IS NULL OR printer_id = %(p)s::uuid)"
        " ORDER BY printer_id, ams_id, tray_id",
        {"p": printer_id},
    ).fetchall()
    return [UnknownTrayRow.model_validate(r) for r in rows]


class PoolUnknownTrays:
    """`UnknownSpoolReads` over a pool, for the API process."""

    def __init__(self, pool: ConnectionPool[Conn]) -> None:
        self._pool = pool

    async def blocking(self, printer_id: str) -> list[UnknownSpoolRef]:
        def read() -> list[UnknownSpoolRef]:
            with self._pool.connection() as conn:
                rows = rows_on(conn, UUID(printer_id))
            return [UnknownSpoolRef(id=r.id, slot=tray_label(r.ams_id, r.tray_id)) for r in rows]

        return await asyncio.to_thread(read)


def blocked_detail(printer_name: str, refs: Sequence[UnknownSpoolRef]) -> str:
    """The text of a print refused for unknown spools."""
    slots = [r.slot for r in refs]
    named = slots[0] if len(slots) == 1 else f"{', '.join(slots[:-1])} and {slots[-1]}"
    return f"{printer_name} has an unknown spool in {named}: link it on the Filament page first."
