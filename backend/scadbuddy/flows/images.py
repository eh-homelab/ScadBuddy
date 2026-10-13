"""A flow run's images (#2477): what its tool calls returned, kept by the agent in
`ai_flow_blobs` under the run's subject `flow-<run id>`, sealed under the subject's payload
key (`agent/src/temporal/flowBlobs.ts`). The run page lists them and fetches each through
`GET /workflow-runs/{id}/images/{name}`; forgetting the run deletes them with its key
(`forget.py`)."""

from __future__ import annotations

import re
from typing import Literal

import psycopg
from pydantic import BaseModel

from scadbuddy.core.secrets import open_bytes
from scadbuddy.workflows.payload_codec import Connect, PayloadKeys

BLOB_NAME = re.compile(r"^[0-9a-f]{64}\.(png|jpg|gif|webp)$")

MediaType = Literal["image/png", "image/jpeg", "image/gif", "image/webp"]


def blob_context(subject: str, name: str) -> str:
    """The context an image is sealed in under its run's key (flowBlobs.ts `flowBlobContext`)."""
    return f"flow-blob:{subject}:{name}"


class RunImage(BaseModel):
    """One of a run's images: a pointer, as the flow holds it. `bytes` is its size."""

    name: str
    mediaType: MediaType  # noqa: N815
    bytes: int


async def run_images(connect: Connect, run_id: str) -> list[RunImage]:
    """The run's images, oldest first, without opening any."""
    try:
        async with connect() as conn:
            cur = await conn.execute(
                "SELECT name, media_type, size FROM ai_flow_blobs WHERE subject = %s"
                " ORDER BY created_at, name",
                (f"flow-{run_id}",),
            )
            rows = await cur.fetchall()
    except psycopg.errors.UndefinedTable:
        # The agent, whose migration makes the table, has not started on this database.
        return []
    return [RunImage(name=r[0], mediaType=r[1], bytes=r[2]) for r in rows]


async def open_image(
    connect: Connect, keys: PayloadKeys, run_id: str, name: str
) -> tuple[MediaType, bytes] | None:
    """One of the run's images, opened; None when the run has no such image. A forgotten
    run's key is gone: SubjectForgottenError."""
    if not BLOB_NAME.match(name):
        return None
    subject = f"flow-{run_id}"
    try:
        async with connect() as conn:
            cur = await conn.execute(
                "SELECT media_type, data FROM ai_flow_blobs WHERE subject = %s AND name = %s",
                (subject, name),
            )
            row = await cur.fetchone()
    except psycopg.errors.UndefinedTable:
        return None
    if row is None:
        return None
    key = await keys.key_for(subject, False)
    media_type: MediaType = row[0]
    return media_type, open_bytes(key, bytes(row[1]), blob_context(subject, name))
