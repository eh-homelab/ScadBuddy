"""The sweep of entities whose owner is gone (#6950, spec D4 phase 1): each orphan's
files and their holds go, then its row, which takes its links with it. An order's
entity goes once the order code has removed its files."""

from __future__ import annotations

import hashlib
import uuid

import pytest

from scadbuddy.entities.actor import SWEEP_ACTOR
from scadbuddy.entities.attachments import EntityAttachmentStore
from scadbuddy.entities.links import EntityLinkStore, LinkInput
from scadbuddy.entities.properties import (
    MAX_KEYS,
    BadPropertyValue,
    EntityPropertyStore,
    PropertyPatch,
)
from scadbuddy.entities.refs import EntityRef
from scadbuddy.entities.registry import EntityStore
from scadbuddy.entities.sweep import sweep_orphan_entities
from scadbuddy.orders.models import Actor
from scadbuddy.orders.store import ATTACHMENT_HOLDER
from scadbuddy.printers.models import PrinterCreate
from scadbuddy.printers.store import PrinterStore
from tests.conftest import PgPool
from tests.support.order_stock import GoStock, InlineStarter
from tests.support.printers import MERGED_BY

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
    props = EntityPropertyStore(pg_pool, entities)
    props.apply(spool, PropertyPatch(set={"lot_number": "L1"}), BY)
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
    assert _count(pg_pool, "SELECT count(*) AS n FROM entity_properties") == 0
    with pg_pool.connection() as conn:
        assert entities.find(conn, spool) is None
        assert entities.find(conn, item) is not None
    assert [link.url for link in links.list(item)] == ["https://example.com/item"]


def test_an_orphaned_order_kind_with_files_is_left_to_the_order_code(pg_pool: PgPool) -> None:
    entities = EntityStore(pg_pool)
    attachments = EntityAttachmentStore(pg_pool, entities)
    item = _order_item(pg_pool)
    with pg_pool.connection() as conn:
        entity_id = entities.ensure(conn, item)
        conn.execute(
            "INSERT INTO entity_attachments (id, order_id, subject, entity_id, kind, filename,"
            " media_type, size_bytes, sha256, blob_key, created_by)"
            " VALUES ('a1', 'o1', 'order_item:i1', %s, 'image', 'x.png', 'image/png', 5, %s,"
            " %s, '{}')",
            (entity_id, SHA, KEY),
        )
        conn.execute("INSERT INTO entities (kind, ref) VALUES ('order_item', 'gone')")
        conn.execute("DELETE FROM order_items WHERE id = 'i1'")

    # The bare orphan goes; the one whose file the order still holds stays.
    assert sweep_orphan_entities(entities, attachments) == 1

    with pg_pool.connection() as conn:
        assert entities.find(conn, EntityRef("order_item", "gone")) is None
        assert entities.find(conn, item) == entity_id
    assert _count(pg_pool, "SELECT count(*) AS n FROM entity_attachments WHERE id = 'a1'") == 1


def test_a_sweep_takes_at_most_its_limit(pg_pool: PgPool) -> None:
    entities = EntityStore(pg_pool)
    attachments = EntityAttachmentStore(pg_pool, entities)
    links = EntityLinkStore(pg_pool, entities)
    spools = [_spool(pg_pool) for _ in range(3)]
    for spool in spools:
        links.add(spool, LinkInput(url="https://example.com/s"), BY)
    with pg_pool.connection() as conn:
        conn.execute(
            "DELETE FROM filament_spools WHERE id = ANY(%s)",
            ([uuid.UUID(s.ref) for s in spools],),
        )

    assert sweep_orphan_entities(entities, attachments, limit=2) == 2
    assert _count(pg_pool, "SELECT count(*) AS n FROM entities") == 1
    assert sweep_orphan_entities(entities, attachments, limit=2) == 1
    assert _count(pg_pool, "SELECT count(*) AS n FROM entities") == 0


@pytest.mark.parametrize("kept_has_files", [False, True])
def test_a_printer_merge_keeps_the_gone_printers_files_and_links(
    pg_pool: PgPool, kept_has_files: bool
) -> None:
    """Review of #6961: a merge deletes the gone printer's row, but its files, links and
    properties (#7026) move to the kept printer, so the sweep finds no orphan; the kept
    printer's value wins a key both have."""
    entities = EntityStore(pg_pool)
    attachments = EntityAttachmentStore(pg_pool, entities)
    links = EntityLinkStore(pg_pool, entities)
    printers = PrinterStore(pg_pool, None)
    synthetic = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C", by=MERGED_BY)
    gone = EntityRef("printer", synthetic.id)
    manual = _attach(attachments, pg_pool, gone)
    links.add(gone, LinkInput(url="https://example.com/support"), BY)
    props = EntityPropertyStore(pg_pool, entities)
    props.apply(gone, PropertyPatch(set={"a": "gone", "b": "gone"}), BY)
    mine = printers.create(PrinterCreate(serial="0938AB000000006", name="Mine"))
    kept = EntityRef("printer", mine.id)
    if kept_has_files:
        links.add(kept, LinkInput(url="https://example.com/mine"), BY)
        props.apply(kept, PropertyPatch(set={"b": "kept"}), BY)

    assert printers.ensure_bambuddy(
        4, serial="0938AB000000006", name="H2C", model="H2C", by=MERGED_BY
    ).id == (mine.id)

    assert sweep_orphan_entities(entities, attachments) == 0
    assert [a.id for a in attachments.list(kept)] == [manual]
    assert sorted(link.url for link in links.list(kept)) == sorted(
        ["https://example.com/support"] + (["https://example.com/mine"] if kept_has_files else [])
    )
    assert {p.key: p.value for p in props.list(kept)} == {
        "a": "gone",
        "b": "kept" if kept_has_files else "gone",
    }
    with pg_pool.connection() as conn:
        assert entities.find(conn, gone) is None
        row = conn.execute(
            "SELECT subject FROM entity_attachments WHERE id = %s", (manual,)
        ).fetchone()
    assert row is not None and row["subject"] == f"printer:{mine.id}"


def test_an_owner_back_under_the_same_id_keeps_its_links(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #6961: `orphans` reads outside the delete's transaction, so the sweep
    checks the owner again before it removes anything."""
    entities = EntityStore(pg_pool)
    attachments = EntityAttachmentStore(pg_pool, entities)
    links = EntityLinkStore(pg_pool, entities)
    spool = _spool(pg_pool)
    links.add(spool, LinkInput(url="https://example.com/spool"), BY)
    with pg_pool.connection() as conn:
        entity_id = entities.find(conn, spool)
    assert entity_id is not None
    monkeypatch.setattr(entities, "orphans", lambda conn, limit, guarded=(): [(entity_id, spool)])

    assert sweep_orphan_entities(entities, attachments) == 0

    assert [link.url for link in links.list(spool)] == ["https://example.com/spool"]


def test_a_merge_past_the_key_limit_may_shrink_but_not_grow(pg_pool: PgPool) -> None:
    """Spec 6.3a: a merge never drops keys, so an entity can end past `MAX_KEYS`; a patch
    after it may change and remove keys, never add one."""
    entities = EntityStore(pg_pool)
    props = EntityPropertyStore(pg_pool, entities)
    printers = PrinterStore(pg_pool, None)
    synthetic = printers.ensure_bambuddy(5, serial=None, name="H2C", model="H2C", by=MERGED_BY)
    gone = EntityRef("printer", synthetic.id)
    props.apply(gone, PropertyPatch(set={f"g{i}": i for i in range(MAX_KEYS)}), BY)
    mine = printers.create(PrinterCreate(serial="0938AB000000007", name="Mine"))
    kept = EntityRef("printer", mine.id)
    props.apply(kept, PropertyPatch(set={f"k{i}": i for i in range(10)}), BY)
    printers.ensure_bambuddy(5, serial="0938AB000000007", name="H2C", model="H2C", by=MERGED_BY)
    assert len(props.list(kept)) == MAX_KEYS + 10

    with pytest.raises(BadPropertyValue):
        props.apply(kept, PropertyPatch(set={"new": 1}), BY)
    assert len(props.apply(kept, PropertyPatch(set={"g0": "changed"}), BY)) == MAX_KEYS + 10
    assert len(props.apply(kept, PropertyPatch(set={"g1": None}), BY)) == MAX_KEYS + 9


def _move_on(pool: PgPool, ref: EntityRef) -> uuid.UUID:
    """A `use` move of a lot item that names `ref` (R-P2-4)."""
    entities = EntityStore(pool)
    with pool.connection() as conn, conn.transaction():
        entity_id = entities.ensure(conn, ref)
        item = conn.execute(
            "INSERT INTO items (kind_id, name) VALUES ('hardware', 'Grease') RETURNING id"
        ).fetchone()
        assert item is not None
        lot = conn.execute(
            "INSERT INTO stock_lots (item_id, received_qty, on_hand, unit_cost_minor, origin,"
            " created_by) VALUES (%s, 0, -1, 0, 'adjustment', '{}') RETURNING id",
            (item["id"],),
        ).fetchone()
        assert lot is not None
        conn.execute(
            "INSERT INTO stock_moves (item_id, lot_id, kind, qty, unit_cost_minor, amount_minor,"
            " entity_id, actor) VALUES (%s, %s, 'use', -1, 0, 0, %s, '{}')",
            (item["id"], lot["id"], entity_id),
        )
    return entity_id


def test_an_orphan_a_stock_move_names_is_kept(pg_pool: PgPool) -> None:
    """R-P2-4: `stock_moves.entity_id` has a plain foreign key, so the sweep leaves an
    entity a move names (its history) and goes on to the others."""
    entities = EntityStore(pg_pool)
    attachments = EntityAttachmentStore(pg_pool, entities)
    links = EntityLinkStore(pg_pool, entities)
    named, bare = _spool(pg_pool), _spool(pg_pool)
    entity_id = _move_on(pg_pool, named)
    links.add(named, LinkInput(url="https://example.com/named"), BY)
    links.add(bare, LinkInput(url="https://example.com/bare"), BY)
    with pg_pool.connection() as conn:
        conn.execute(
            "DELETE FROM filament_spools WHERE id = ANY(%s)",
            ([uuid.UUID(named.ref), uuid.UUID(bare.ref)],),
        )

    assert sweep_orphan_entities(entities, attachments) == 1

    with pg_pool.connection() as conn:
        assert entities.find(conn, named) == entity_id
        assert entities.find(conn, bare) is None
    assert sweep_orphan_entities(entities, attachments) == 0


def test_a_sweep_rechecks_the_moves_under_its_lock(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An orphan read before a move named it is still kept."""
    entities = EntityStore(pg_pool)
    attachments = EntityAttachmentStore(pg_pool, entities)
    spool = _spool(pg_pool)
    entity_id = _move_on(pg_pool, spool)
    with pg_pool.connection() as conn:
        conn.execute("DELETE FROM filament_spools WHERE id = %s", (uuid.UUID(spool.ref),))
    monkeypatch.setattr(entities, "orphans", lambda conn, limit, guarded=(): [(entity_id, spool)])

    assert sweep_orphan_entities(entities, attachments) == 0


def test_a_printer_merge_moves_the_stock_moves_to_the_kept_printer(pg_pool: PgPool) -> None:
    """R-P2-4: a merge deletes the gone printer's entity, so a move that named it names
    the kept printer after it, as its files and links do. Since #6949 K1 Task 16 the move
    goes through `MergePrinter` (Go's `Repoint`, here its stand-in), not `repoint_entity`."""
    entities = EntityStore(pg_pool)
    printers = PrinterStore(pg_pool, None)
    printers.merges = InlineStarter(None, GoStock(pg_pool), lambda: printers)
    synthetic = printers.ensure_bambuddy(6, serial=None, name="H2C", model="H2C", by=MERGED_BY)
    gone = EntityRef("printer", synthetic.id)
    _move_on(pg_pool, gone)
    mine = printers.create(PrinterCreate(serial="0938AB000000008", name="Mine"))
    kept = EntityRef("printer", mine.id)
    with pg_pool.connection() as conn:
        kept_id = entities.ensure(conn, kept)

    printers.ensure_bambuddy(6, serial="0938AB000000008", name="H2C", model="H2C", by=MERGED_BY)

    with pg_pool.connection() as conn:
        assert entities.find(conn, gone) is None
        row = conn.execute("SELECT entity_id FROM stock_moves").fetchone()
    assert row is not None and row["entity_id"] == kept_id


def test_an_entity_a_relation_names_survives_the_sweep(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`relations` has plain foreign keys to `entities` (design §6.3), so the sweep leaves
    an orphan that a relation names, as it leaves one a stock move names (R-P2-4), and
    checks again under its lock."""
    entities = EntityStore(pg_pool)
    attachments = EntityAttachmentStore(pg_pool, entities)
    links = EntityLinkStore(pg_pool, entities)
    named, bare = _spool(pg_pool), _spool(pg_pool)
    with pg_pool.connection() as conn, conn.transaction():
        from_id = entities.ensure(conn, named)
        to_id = entities.ensure(conn, EntityRef("item_kind", "printer"))
        conn.execute(
            "INSERT INTO relations (type_id, from_entity, to_entity, created_by)"
            " VALUES ('compatible_with', %s, %s, '{}')",
            (from_id, to_id),
        )
    links.add(bare, LinkInput(url="https://example.com/bare"), BY)
    with pg_pool.connection() as conn:
        conn.execute(
            "DELETE FROM filament_spools WHERE id = ANY(%s)",
            ([uuid.UUID(named.ref), uuid.UUID(bare.ref)],),
        )

    assert sweep_orphan_entities(entities, attachments) == 1

    with pg_pool.connection() as conn:
        assert entities.find(conn, named) == from_id
        assert entities.find(conn, bare) is None
    monkeypatch.setattr(entities, "orphans", lambda conn, limit, guarded=(): [(from_id, named)])
    assert sweep_orphan_entities(entities, attachments) == 0


def test_the_sweep_publishes_what_it_removes(pg_pool: PgPool) -> None:
    """#6949: the orphan's key goes as one `remove` event, credited to the sweep."""
    entities = EntityStore(pg_pool)
    spool = _spool(pg_pool)
    EntityPropertyStore(pg_pool, entities).apply(spool, PropertyPatch(set={"lot_number": "L1"}), BY)
    with pg_pool.connection() as conn:
        conn.execute("DELETE FROM filament_spools WHERE id = %s", (uuid.UUID(spool.ref),))
        mark = conn.execute("SELECT coalesce(max(seq), 0) AS n FROM events").fetchone()
    assert mark is not None

    assert sweep_orphan_entities(entities, EntityAttachmentStore(pg_pool, entities)) == 1

    with pg_pool.connection() as conn:
        rows = conn.execute(
            "SELECT payload FROM events WHERE kind = 'entity_property.changed' AND seq > %s",
            (mark["n"],),
        ).fetchall()
    assert [(r["payload"]["op"], r["payload"]["key"]) for r in rows] == [("remove", "lot_number")]
    assert Actor.model_validate(rows[0]["payload"]["actor"]) == SWEEP_ACTOR
