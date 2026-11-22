"""Links on any entity (#6950, spec 6.3): rows of `entity_links` keyed by their entity,
ordered by `position`. A person's edit makes a link `manual`; an import (a later phase)
adds its links with its own `source` and never doubles one a person already kept."""

from __future__ import annotations

import builtins
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.entities.refs import EntityRef
from scadbuddy.entities.registry import EntityStore
from scadbuddy.orders.models import Actor

Conn = Connection[DictRow]

MAX_URL = 2000
KIND_PATTERN = r"^[a-z_]{1,32}$"
_COLUMNS = "id, entity_id, url, kind, title, note, position, source, created_at, created_by"


def is_web_url(url: str) -> bool:
    """An ``http`` or ``https`` URL with a host."""
    try:
        parts = urlsplit(url)
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return False


class EntityLink(BaseModel):
    id: UUID
    #: ``kind:ref``: what it hangs off.
    entity: str
    url: str
    kind: str
    title: str
    note: str
    position: int
    #: ``manual`` for a person's link; an import's name otherwise.
    source: str
    created_at: datetime
    created_by: Actor


class LinkInput(BaseModel):
    url: str = Field(max_length=MAX_URL, description="An `http` or `https` URL with a host")
    kind: str = Field(default="other", pattern=KIND_PATTERN)
    title: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=1000)
    position: int | None = Field(default=None, ge=0, description="None: after the last")


class LinkPatch(BaseModel):
    """A field left out stays."""

    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(default=None, max_length=MAX_URL)
    kind: str | None = Field(default=None, pattern=KIND_PATTERN)
    title: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=1000)
    position: int | None = Field(default=None, ge=0)


def _of(row: dict[str, Any], ref: EntityRef) -> EntityLink:
    return EntityLink(
        id=row["id"],
        entity=str(ref),
        url=row["url"],
        kind=row["kind"],
        title=row["title"],
        note=row["note"],
        position=row["position"],
        source=row["source"],
        created_at=row["created_at"],
        created_by=Actor.model_validate(row["created_by"]),
    )


class EntityLinkStore:
    def __init__(self, pool: Any, entities: EntityStore) -> None:
        self.pool = pool
        self.entities = entities

    def list(self, ref: EntityRef) -> builtins.list[EntityLink]:
        """The entity's links by position, then age."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM entity_links WHERE entity_id ="
                " (SELECT id FROM entities WHERE kind = %s AND ref = %s)"
                " ORDER BY position, created_at, id",
                (ref.kind, ref.ref),
            ).fetchall()
        return [_of(r, ref) for r in rows]

    def add(self, ref: EntityRef, body: LinkInput, by: Actor) -> EntityLink:
        """`EntityNotFound` when the owner's row is not there."""
        with self.pool.connection() as conn, conn.transaction():
            return self._insert(conn, ref, body, "manual", by)

    def add_from_source(
        self, conn: Conn, ref: EntityRef, body: LinkInput, source: str, by: Actor
    ) -> EntityLink | None:
        """An import's link; None when a person already kept one with the same URL."""
        with conn.transaction():
            entity_id = self.entities.ensure(conn, ref)
            kept = conn.execute(
                "SELECT 1 FROM entity_links WHERE entity_id = %s AND url = %s"
                " AND source = 'manual'",
                (entity_id, body.url),
            ).fetchone()
            if kept is not None:
                return None
            return self._insert(conn, ref, body, source, by)

    def _insert(
        self, conn: Conn, ref: EntityRef, body: LinkInput, source: str, by: Actor
    ) -> EntityLink:
        entity_id = self.entities.ensure(conn, ref)
        # One writer at a time per entity, so positions do not tie.
        conn.execute("SELECT 1 FROM entities WHERE id = %s FOR UPDATE", (entity_id,))
        position = body.position
        if position is None:
            last = conn.execute(
                "SELECT max(position) AS last FROM entity_links WHERE entity_id = %s",
                (entity_id,),
            ).fetchone()
            assert last is not None
            position = 0 if last["last"] is None else last["last"] + 1
        else:
            conn.execute(
                "UPDATE entity_links SET position = position + 1"
                " WHERE entity_id = %s AND position >= %s",
                (entity_id, position),
            )
        row = conn.execute(
            "INSERT INTO entity_links (entity_id, url, kind, title, note, position, source,"
            f" created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
            (
                entity_id,
                body.url,
                body.kind,
                body.title,
                body.note,
                position,
                source,
                Jsonb(by.model_dump(mode="json")),
            ),
        ).fetchone()
        assert row is not None
        return _of(row, ref)

    def update(self, ref: EntityRef, link_id: UUID, body: LinkPatch) -> EntityLink:
        """A person's edit: the fields given change and the link is `manual`. Another
        entity's link, or none, is a `LookupError`."""
        with self.pool.connection() as conn, conn.transaction():
            entity_id = self.entities.find(conn, ref)
            if entity_id is None:
                raise LookupError(str(link_id))
            conn.execute("SELECT 1 FROM entities WHERE id = %s FOR UPDATE", (entity_id,))
            if body.position is not None:
                conn.execute(
                    "UPDATE entity_links SET position = position + 1"
                    " WHERE entity_id = %s AND position >= %s AND id <> %s",
                    (entity_id, body.position, link_id),
                )
            row = conn.execute(
                "UPDATE entity_links SET url = coalesce(%s, url), kind = coalesce(%s, kind),"
                " title = coalesce(%s, title), note = coalesce(%s, note),"
                " position = coalesce(%s, position), source = 'manual'"
                f" WHERE id = %s AND entity_id = %s RETURNING {_COLUMNS}",
                (
                    body.url,
                    body.kind,
                    body.title,
                    body.note,
                    body.position,
                    link_id,
                    entity_id,
                ),
            ).fetchone()
            if row is None:
                raise LookupError(str(link_id))
        return _of(row, ref)

    def delete(self, ref: EntityRef, link_id: UUID) -> None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "DELETE FROM entity_links WHERE id = %s AND entity_id ="
                " (SELECT id FROM entities WHERE kind = %s AND ref = %s) RETURNING id",
                (link_id, ref.kind, ref.ref),
            ).fetchone()
        if row is None:
            raise LookupError(str(link_id))
