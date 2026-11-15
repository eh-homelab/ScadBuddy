"""A flow run's images (#2477), ``ai_flow_blobs``, for ``api/crud_agent_forget.py`` (#5299
step 6): the writes and the read the agent ran itself before (``agent/src/temporal/
flowBlobs.ts`` ``PgFlowBlobs``). The agent seals each image under the run's payload key in
the context ``flow-blob:<subject>:<name>`` (``flows/images.py`` ``blob_context``) and sends the
sealed bytes; nothing here opens them. The run page lists and opens them through
``flows/images.py``, with the same statements.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from psycopg import Connection
from psycopg.rows import tuple_row

from scadbuddy.agent_data.forget import LOCK
from scadbuddy.workflows.payload_codec import payload_key_lock

FLOW_SUBJECT_PATTERN = r"^flow-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
BLOB_NAME = re.compile(r"^[0-9a-f]{64}\.(png|jpg|gif|webp)$")
#: Each image type, by the extension its name takes (the agent's ``sessions/blobs.ts``).
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
#: The most images one run keeps (#2578; the agent's ``FLOW_IMAGES_MAX``).
FLOW_IMAGES_MAX = 256

LIST = (
    "SELECT name, media_type, size FROM ai_flow_blobs WHERE subject = %s ORDER BY created_at, name"
)
GET = "SELECT media_type, data FROM ai_flow_blobs WHERE subject = %s AND name = %s"


@dataclass(frozen=True)
class SealedImage:
    name: str
    media_type: str
    #: The image's own byte count, which the run page lists without opening it.
    size: int
    #: Sealed by the agent under the run's key.
    data: bytes


def put(
    conn: Connection[Any], subject: str, images: list[SealedImage], *, cap: int | None = None
) -> list[str] | None:
    """Keeps a run's images; one already there (the same name) is left as it is. The names
    the run holds afterwards, of those given (a new one past ``cap`` is not kept); None for a
    forgotten run. ``cap`` is ``FLOW_IMAGES_MAX`` unless given. Under the lock forgetting
    takes, so no row lands behind a forget that just committed, and two calls of one run
    never both take the last room."""
    with conn.transaction():
        cur = conn.cursor(row_factory=tuple_row)
        cur.execute(LOCK, (payload_key_lock(subject),))
        forgotten = cur.execute(
            "SELECT 1 FROM ai_forgotten_subjects WHERE subject = %s", (subject,)
        ).fetchone()
        if forgotten is not None:
            return None
        held = {
            row[0]
            for row in cur.execute(
                "SELECT name FROM ai_flow_blobs WHERE subject = %s", (subject,)
            ).fetchall()
        }
        fresh: list[SealedImage] = []
        for image in images:
            if image.name not in held and all(f.name != image.name for f in fresh):
                fresh.append(image)
        room = (FLOW_IMAGES_MAX if cap is None else cap) - len(held)
        for image in fresh[: max(0, room)]:
            cur.execute(
                "INSERT INTO ai_flow_blobs (subject, name, media_type, size, data)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (subject, name) DO NOTHING",
                (subject, image.name, image.media_type, image.size, image.data),
            )
            held.add(image.name)
        return [name for name in dict.fromkeys(i.name for i in images) if name in held]


def get(conn: Connection[Any], subject: str, name: str) -> tuple[str, bytes] | None:
    """One of the run's images as stored (its type, its sealed bytes), or None."""
    row = conn.cursor(row_factory=tuple_row).execute(GET, (subject, name)).fetchone()
    return None if row is None else (row[0], bytes(row[1]))
