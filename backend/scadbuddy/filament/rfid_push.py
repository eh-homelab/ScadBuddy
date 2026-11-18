"""The queue of tags whose binding changed and that Bambuddy has not been told of (#6264)."""

from __future__ import annotations

from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow

Conn = Connection[DictRow]


def enqueue_tag(conn: Conn, tag_id: UUID) -> None:
    """Queue ``tag_id`` for the push, on ``conn``'s transaction. A tag already queued is
    queued again from the start: its attempts and last error are cleared."""
    conn.execute(
        "INSERT INTO filament_bb_tag_pending (tag_id) VALUES (%s)"
        " ON CONFLICT (tag_id) DO UPDATE SET queued_at = now(), attempts = 0, last_error = NULL",
        (tag_id,),
    )
