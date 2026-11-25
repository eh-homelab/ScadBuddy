"""Attachments on any entity (#6950, spec 6.2): one row per file, a `blob_refs` hold per
row, and identical bytes on two entities kept once."""

from __future__ import annotations

import hashlib
import uuid

import pytest

from scadbuddy.entities.attachments import EntityAttachmentStore
from scadbuddy.entities.refs import EntityRef
from scadbuddy.entities.registry import EntityNotFound, EntityStore
from scadbuddy.filament.catalog import KIND_OF_TABLE
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


def _add(store: EntityAttachmentStore, pool: PgPool, ref: EntityRef) -> str:
    attachment_id = str(uuid.uuid4())
    with pool.connection() as conn:
        store.add(
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
            caption="on the shelf",
            by=BY,
        )
    return attachment_id


def _holds(pool: PgPool) -> dict[str, str]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT holder_id, key FROM blob_refs WHERE holder_kind = %s", (ATTACHMENT_HOLDER,)
        ).fetchall()
    return {r["holder_id"]: r["key"] for r in rows}


@pytest.fixture
def attachments(pg_pool: PgPool) -> EntityAttachmentStore:
    return EntityAttachmentStore(pg_pool, EntityStore(pg_pool))


def test_an_added_file_is_listed_under_its_entity(
    attachments: EntityAttachmentStore, pg_pool: PgPool
) -> None:
    ref = _spool(pg_pool)
    added = _add(attachments, pg_pool, ref)
    listed = attachments.list(ref)
    assert [a.id for a in listed] == [added]
    first = listed[0]
    assert (first.entity, first.kind, first.role, first.source) == (
        str(ref),
        "image",
        "photo",
        "store",
    )
    assert (first.sha256, first.caption, first.created_by) == (SHA, "on the shelf", BY)
    found = attachments.get(added)
    assert found is not None
    assert found[1] == KEY


def test_the_same_bytes_on_two_entities_are_one_blob_with_two_holds(
    attachments: EntityAttachmentStore, pg_pool: PgPool
) -> None:
    first = _add(attachments, pg_pool, _spool(pg_pool))
    second = _add(attachments, pg_pool, _spool(pg_pool))
    assert _holds(pg_pool) == {first: KEY, second: KEY}

    with pg_pool.connection() as conn:
        attachments.delete(conn, first)
    assert _holds(pg_pool) == {second: KEY}
    assert attachments.get(first) is None
    assert attachments.get(second) is not None


def test_a_missing_owner_is_refused_and_registers_nothing(
    attachments: EntityAttachmentStore, pg_pool: PgPool
) -> None:
    with pytest.raises(EntityNotFound):
        _add(attachments, pg_pool, EntityRef("spool", str(uuid.uuid4())))
    with pg_pool.connection() as conn:
        entities = conn.execute("SELECT count(*) AS n FROM entities").fetchone()
        rows = conn.execute("SELECT count(*) AS n FROM entity_attachments").fetchone()
    assert entities is not None and rows is not None
    assert (entities["n"], rows["n"]) == (0, 0)
    assert _holds(pg_pool) == {}


def test_update_changes_only_what_it_is_given(
    attachments: EntityAttachmentStore, pg_pool: PgPool
) -> None:
    added = _add(attachments, pg_pool, _spool(pg_pool))
    changed = attachments.update(added, caption=None, role="label")
    assert (changed.caption, changed.role) == ("on the shelf", "label")
    changed = attachments.update(added, caption="", role=None)
    assert (changed.caption, changed.role) == ("", "label")


CATALOG_KINDS = {
    "material": "filament_materials",
    "subtype": "filament_subtypes",
    "brand": "filament_brands",
    "category": "filament_categories",
    "effect": "filament_effects",
    "insert_material": "filament_insert_materials",
    "insert_type": "filament_insert_types",
    "colour": "filament_colours",
    "location_kind": "filament_location_kinds",
}


def test_every_catalog_is_an_entity_kind() -> None:
    assert set(CATALOG_KINDS.values()) == set(KIND_OF_TABLE)


@pytest.mark.parametrize(("kind", "table"), CATALOG_KINDS.items())
def test_a_catalog_row_takes_a_file(
    attachments: EntityAttachmentStore, pg_pool: PgPool, kind: str, table: str
) -> None:
    extra = {"insert_type": ("empty_weight_g", 250), "colour": ("rgba", "#112233FF")}.get(kind)
    columns, values = ["name"], ["Kept with files"]
    if extra is not None:
        columns.append(extra[0])
        values.append(extra[1])  # type: ignore[arg-type]
    marks = ", ".join(["%s"] * len(values))
    with pg_pool.connection() as conn:
        row = conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks}) RETURNING id".encode(),
            values,
        ).fetchone()
    assert row is not None
    ref = EntityRef(kind, str(row["id"]))
    added = _add(attachments, pg_pool, ref)
    assert [a.id for a in attachments.list(ref)] == [added]
