"""The sweep of entities whose owner is gone (#6950, spec D4 phase 1): each orphan's
files and their holds go, then its row, which takes its links with it. An order's
entities are the order code's to clean up."""

from __future__ import annotations

import hashlib
import uuid

import pytest

from scadbuddy.entities.attachments import EntityAttachmentStore
from scadbuddy.entities.links import EntityLinkStore, LinkInput
from scadbuddy.entities.refs import EntityRef
from scadbuddy.entities.registry import EntityStore
from scadbuddy.entities.sweep import sweep_orphan_entities
from scadbuddy.orders.models import Actor
from scadbuddy.orders.store import ATTACHMENT_HOLDER
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

BY = Actor(kind="browser")
SHA = hashlib.sha256(b"photo").hexdigest()
KEY = f"attachment-{SHA}"


def _spool(pool: PgPool) -> EntityRef:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO filament_spools (material_id)"
            " SELECT id FROM filament_materials WHERE name = 'PLA' RETURNING id"
        ).fetchone()
    assert row is not None
    return EntityRef("spool", str(row["id"]))


def _order_item(pool: PgPool) -> EntityRef:
    with pool.connection() as conn:
        conn.execute("INSERT INTO customers (id, display_name) VALUES ('c1', 'C')")
        conn.execute(
            "INSERT INTO orders (id, number, customer_id, currency, created_by)"
            " VALUES ('o1', 'ORD-2026-1', 'c1', 'USD', '{}')"
        )
        conn.execute(
            "INSERT INTO order_items (id, order_id, position, quantity, title, kind)"
            " VALUES ('i1', 'o1', 0, 1, 'Bracket', 'custom')"
        )
    return EntityRef("order_item", "i1")


def _attach(attachments: EntityAttachmentStore, pool: PgPool, ref: EntityRef) -> str:
    attachment_id = str(uuid.uuid4())
    with pool.connection() as conn:
        attachments.add(
            conn,
            ref,
            attachment_id=attachment_id,
            kind="image",
            role="photo",
            filename="spool.png",
            media_type="image/png",
            size_bytes=5,
            sha256=SHA,
            blob_key=KEY,
            caption="",
            by=BY,
        )
    return attachment_id


def _count(pool: PgPool, sql: str, *args: object) -> int:
    with pool.connection() as conn:
        row = conn.execute(sql.encode(), args).fetchone()
    assert row is not None
    return int(row["n"])


def test_an_orphan_loses_its_files_holds_and_links_and_an_owned_order_item_stays(
    pg_pool: PgPool,
) -> None:
    entities = EntityStore(pg_pool)
    attachments = EntityAttachmentStore(pg_pool, entities)
    links = EntityLinkStore(pg_pool, entities)
    spool = _spool(pg_pool)
    attachment = _attach(attachments, pg_pool, spool)
    links.add(spool, LinkInput(url="https://example.com/spool"), BY)
    item = _order_item(pg_pool)
    links.add(item, LinkInput(url="https://example.com/item"), BY)
    with pg_pool.connection() as conn:
        conn.execute("DELETE FROM filament_spools WHERE id = %s", (uuid.UUID(spool.ref),))

    assert sweep_orphan_entities(entities, attachments) == 1

    assert (
        _count(pg_pool, "SELECT count(*) AS n FROM entity_attachments WHERE id = %s", attachment)
        == 0
    )
    assert (
        _count(
            pg_pool,
            "SELECT count(*) AS n FROM blob_refs WHERE holder_kind = %s AND holder_id = %s",
            ATTACHMENT_HOLDER,
            attachment,
        )
        == 0
    )
    assert (
        _count(
            pg_pool,
            "SELECT count(*) AS n FROM entity_links WHERE url = %s",
            "https://example.com/spool",
        )
        == 0
    )
    with pg_pool.connection() as conn:
        assert entities.find(conn, spool) is None
        assert entities.find(conn, item) is not None
    assert [link.url for link in links.list(item)] == ["https://example.com/item"]


def test_an_orphaned_order_kind_is_left_to_the_order_code(pg_pool: PgPool) -> None:
    entities = EntityStore(pg_pool)
    attachments = EntityAttachmentStore(pg_pool, entities)
    with pg_pool.connection() as conn:
        conn.execute("INSERT INTO entities (kind, ref) VALUES ('order_item', 'gone')")

    assert sweep_orphan_entities(entities, attachments) == 0

    with pg_pool.connection() as conn:
        assert entities.find(conn, EntityRef("order_item", "gone")) is not None
