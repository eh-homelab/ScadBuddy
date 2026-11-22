"""Attachments on any entity (#6950, spec 6.2): rows of `entity_attachments` keyed by
their entity, each holding its blob in `blob_refs`, and a print archive's files, which
Bambuddy keeps (`print_attachments`), read through the same shape.

The bytes are kept as an order's are (`orders/attachments.py` `keep`, the blob
`attachment-<sha256>`), so identical bytes on two entities are one blob with two holds.
The holder kind is the orders' own (`ATTACHMENT_HOLDER`): one attachment id is one
holder however it was written. An order's entities (`ORDER_KINDS`) are read here but
written only through the order routes, whose rules (revision roles, carts, locks) live in
`OrderLifecycle`."""

from __future__ import annotations

import builtins
from datetime import datetime
from typing import Any, Literal

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from scadbuddy.bambuddy.attachments import AttachmentStore
from scadbuddy.entities.refs import EntityRef
from scadbuddy.entities.registry import EntityStore
from scadbuddy.orders.attachments import sniff
from scadbuddy.orders.models import Actor, AttachmentKind
from scadbuddy.orders.store import ATTACHMENT_HOLDER

Conn = Connection[DictRow]

#: The entities whose files the order routes write.
ORDER_KINDS = frozenset({"order", "order_item", "order_item_fulfilment", "order_item_revision"})
#: A print archive's files: Bambuddy's, read only here (not in `KINDS`).
PRINT_ARCHIVE = "print_archive"

_COLUMNS = (
    "id, subject, kind, role, filename, media_type, size_bytes, sha256, blob_key, caption,"
    " created_at, created_by"
)


class EntityAttachment(BaseModel):
    id: str
    #: ``kind:ref``: what it is attached to.
    entity: str
    kind: AttachmentKind
    role: str
    filename: str
    media_type: str
    size_bytes: int
    #: None for a print archive's file, whose bytes Bambuddy keeps.
    sha256: str | None
    caption: str
    created_at: datetime | None
    created_by: Actor | None
    #: ``store``: ScadBuddy's own blob; ``bambuddy``: a print archive's file.
    source: Literal["store", "bambuddy"]


def _of(row: dict[str, Any]) -> tuple[EntityAttachment, str]:
    return (
        EntityAttachment(
            id=row["id"],
            entity=row["subject"],
            kind=row["kind"],
            role=row["role"],
            filename=row["filename"],
            media_type=row["media_type"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            caption=row["caption"],
            created_at=row["created_at"],
            created_by=Actor.model_validate(row["created_by"]),
            source="store",
        ),
        row["blob_key"],
    )


class EntityAttachmentStore:
    def __init__(self, pool: Any, entities: EntityStore) -> None:
        self.pool = pool
        self.entities = entities

    def add(
        self,
        conn: Conn,
        ref: EntityRef,
        *,
        attachment_id: str,
        kind: AttachmentKind,
        role: str,
        filename: str,
        media_type: str,
        size_bytes: int,
        sha256: str,
        blob_key: str,
        caption: str,
        by: Actor,
    ) -> EntityAttachment:
        """The entity made on first use, the row and its hold on ``blob_key``, in one
        transaction. `EntityNotFound` when the owner's row is not there."""
        with conn.transaction():
            entity_id = self.entities.ensure(conn, ref)
            conn.execute(
                "INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES (%s, %s, %s)"
                " ON CONFLICT DO NOTHING",
                (blob_key, ATTACHMENT_HOLDER, attachment_id),
            )
            row = conn.execute(
                "INSERT INTO entity_attachments (id, subject, entity_id, kind, role, filename,"
                " media_type, size_bytes, sha256, blob_key, caption, created_by)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                f" RETURNING {_COLUMNS}",
                (
                    attachment_id,
                    str(ref),
                    entity_id,
                    kind,
                    role,
                    filename,
                    media_type,
                    size_bytes,
                    sha256,
                    blob_key,
                    caption,
                    Jsonb(by.model_dump(mode="json")),
                ),
            ).fetchone()
        assert row is not None
        return _of(row)[0]

    def list(self, ref: EntityRef) -> builtins.list[EntityAttachment]:
        """The entity's files, oldest first (an order's included)."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM entity_attachments WHERE entity_id ="
                " (SELECT id FROM entities WHERE kind = %s AND ref = %s)"
                " ORDER BY created_at, id",
                (ref.kind, ref.ref),
            ).fetchall()
        return [_of(r)[0] for r in rows]

    def get(self, attachment_id: str) -> tuple[EntityAttachment, str] | None:
        """The attachment and its blob key, or None."""
        with self.pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM entity_attachments WHERE id = %s", (attachment_id,)
            ).fetchone()
        return None if row is None else _of(row)

    def update(
        self, attachment_id: str, *, caption: str | None, role: str | None
    ) -> EntityAttachment:
        """The caption and the role; None keeps each."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "UPDATE entity_attachments SET caption = coalesce(%s, caption),"
                f" role = coalesce(%s, role) WHERE id = %s RETURNING {_COLUMNS}",
                (caption, role, attachment_id),
            ).fetchone()
        if row is None:
            raise LookupError(attachment_id)
        return _of(row)[0]

    def delete(self, conn: Conn, attachment_id: str) -> None:
        """The row and its holds, together; the blob sweep takes the bytes once nothing
        else holds them."""
        with conn.transaction():
            conn.execute(
                "DELETE FROM blob_refs WHERE holder_kind = %s AND holder_id = %s",
                (ATTACHMENT_HOLDER, attachment_id),
            )
            conn.execute("DELETE FROM entity_attachments WHERE id = %s", (attachment_id,))


async def print_archive_attachments(
    store: AttachmentStore, archive_id: int
) -> builtins.list[EntityAttachment]:
    """A print archive's photos and videos (`print_attachments`), oldest first. Bambuddy
    keeps the bytes, so there is no sha256; the media type is what the name says
    (`sniff` of no bytes)."""
    entity = str(EntityRef(PRINT_ARCHIVE, str(archive_id)))
    found: builtins.list[EntityAttachment] = []
    for row in await store.for_archive(archive_id):
        found.append(
            EntityAttachment(
                id=str(row.id),
                entity=entity,
                kind="image" if row.kind == "photo" else "video",
                # Photos and videos of the finished object (#309).
                role="finished",
                filename=row.filename,
                media_type=sniff(b"", row.filename)[1],
                size_bytes=row.size_bytes,
                sha256=None,
                caption=row.caption or "",
                created_at=row.created_at,
                created_by=None,
                source="bambuddy",
            )
        )
    return found
