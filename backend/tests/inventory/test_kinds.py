"""Item kinds (#7067, spec 6.4): a tree of rows whose schemas merge down it, never an
allowlist in code."""

from __future__ import annotations

from typing import Any

import pytest

from scadbuddy.entities.properties import BadPropertySchema
from scadbuddy.inventory.items import ItemStore
from scadbuddy.inventory.kinds import (
    KindArchived,
    KindCycle,
    KindExists,
    KindInUse,
    KindNotFound,
    KindStore,
)
from scadbuddy.inventory.models import ItemInput, KindInput, KindPatch
from scadbuddy.orders.models import Actor
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

BY = Actor(kind="browser")


def _schema(**props: Any) -> dict[str, Any]:
    return {"type": "object", "properties": props}


def _fastener(kinds: KindStore, **schema: Any) -> None:
    kinds.create(
        KindInput(
            id="fastener",
            name="Fastener",
            parent_id="part",
            tracking="lot",
            attribute_schema=_schema(**schema),
        ),
        BY,
    )


def test_a_child_kind_merges_its_parents_schema(kinds: KindStore, pg_pool: PgPool) -> None:
    kinds.update("part", KindPatch(attribute_schema=_schema(mpn={"type": "string"})), BY)
    _fastener(kinds, thread={"type": "string"})

    with pg_pool.connection() as conn:
        merged = kinds.merged_schema(conn, "fastener")
    assert merged["type"] == "object"
    assert merged["properties"] == {"mpn": {"type": "string"}, "thread": {"type": "string"}}


def test_a_childs_key_overrides_its_parents(kinds: KindStore, pg_pool: PgPool) -> None:
    kinds.update("part", KindPatch(attribute_schema=_schema(mpn={"type": "string"})), BY)
    _fastener(kinds, mpn={"type": "integer"})

    with pg_pool.connection() as conn:
        assert kinds.merged_schema(conn, "fastener")["properties"]["mpn"] == {"type": "integer"}
        assert kinds.merged_schema(conn, "part")["properties"]["mpn"] == {"type": "string"}


def test_a_kind_under_its_own_descendant_is_a_cycle(kinds: KindStore) -> None:
    _fastener(kinds)
    kinds.create(KindInput(id="bolt", name="Bolt", parent_id="fastener", tracking="lot"), BY)

    with pytest.raises(KindCycle):
        kinds.update("part", KindPatch(parent_id="bolt"), BY)
    with pytest.raises(KindCycle):
        kinds.update("fastener", KindPatch(parent_id="fastener"), BY)
    kind = kinds.get("part")
    assert kind is not None and kind.parent_id == "durable"


def test_a_restricting_schema_is_refused_and_the_old_one_stays(kinds: KindStore) -> None:
    kinds.update("part", KindPatch(attribute_schema=_schema(mpn={"type": "string"})), BY)

    with pytest.raises(BadPropertySchema):
        kinds.update(
            "part",
            KindPatch(attribute_schema={"type": "object", "additionalProperties": False}),
            BY,
        )
    with pytest.raises(BadPropertySchema):
        kinds.create(
            KindInput(
                id="bearing",
                name="Bearing",
                tracking="lot",
                attribute_schema={"type": "object", "required": ["mpn"]},
            ),
            BY,
        )
    part = kinds.get("part")
    assert part is not None
    assert part.attribute_schema == _schema(mpn={"type": "string"})
    assert kinds.get("bearing") is None


def test_a_builtin_kinds_tracking_never_changes(kinds: KindStore) -> None:
    with pytest.raises(KindInUse):
        kinds.update("nozzle", KindPatch(tracking="lot"), BY)
    nozzle = kinds.get("nozzle")
    assert nozzle is not None and nozzle.tracking == "unit"


def test_a_kind_with_items_keeps_its_tracking(kinds: KindStore, items: ItemStore) -> None:
    _fastener(kinds)
    items.create(ItemInput(kind_id="fastener", name="M3 bolt"), BY)

    with pytest.raises(KindInUse):
        kinds.update("fastener", KindPatch(tracking="unit"), BY)
    kinds.update("fastener", KindPatch(name="Fasteners"), BY)


def test_a_kind_with_items_is_not_archived(kinds: KindStore, items: ItemStore) -> None:
    _fastener(kinds)
    made = items.create(ItemInput(kind_id="fastener", name="M3 bolt"), BY)

    with pytest.raises(KindInUse):
        kinds.archive("fastener")
    items.archive(made.id)
    kinds.archive("fastener")
    assert [k.id for k in kinds.list()].count("fastener") == 0
    assert "fastener" in [k.id for k in kinds.list(include_archived=True)]
    kinds.restore("fastener")
    assert "fastener" in [k.id for k in kinds.list()]


def test_create_refuses_a_taken_id_and_an_unknown_parent(kinds: KindStore) -> None:
    with pytest.raises(KindExists):
        kinds.create(KindInput(id="part", name="Part", tracking="lot"), BY)
    with pytest.raises(KindNotFound):
        kinds.create(KindInput(id="widget", name="Widget", parent_id="nope", tracking="lot"), BY)
    with pytest.raises(KindNotFound):
        kinds.update("nope", KindPatch(name="Nope"), BY)


def test_list_counts_each_kinds_items(kinds: KindStore, items: ItemStore) -> None:
    items.create(ItemInput(kind_id="hardware", name="M3 nut"), BY)
    items.create(ItemInput(kind_id="hardware", name="M3 bolt"), BY)

    counts = {k.id: k.item_count for k in kinds.list()}
    assert counts["hardware"] == 2
    assert counts["part"] == 0
    hardware = kinds.get("hardware")
    assert hardware is not None and hardware.builtin and hardware.item_count == 2


def test_an_items_schema_is_its_kinds_merged(
    kinds: KindStore, items: ItemStore, pg_pool: PgPool
) -> None:
    kinds.update("consumable", KindPatch(attribute_schema=_schema(mpn={"type": "string"})), BY)
    made = items.create(ItemInput(kind_id="hardware", name="M3 nut"), BY)

    with pg_pool.connection() as conn:
        assert kinds.item_schema(conn, str(made.id))["properties"] == {"mpn": {"type": "string"}}
        assert kinds.item_schema(conn, "not-a-uuid") == _schema()
        assert kinds.item_schema(conn, "00000000-0000-0000-0000-000000000000") == _schema()


def test_a_null_parent_makes_a_root(kinds: KindStore) -> None:
    _fastener(kinds)

    moved = kinds.update("fastener", KindPatch(parent_id=None), BY)
    assert moved.parent_id is None


def test_a_tracking_change_counts_archived_items(kinds: KindStore, items: ItemStore) -> None:
    _fastener(kinds)
    made = items.create(ItemInput(kind_id="fastener", name="M3 bolt"), BY)
    items.archive(made.id)

    with pytest.raises(KindInUse):
        kinds.update("fastener", KindPatch(tracking="unit"), BY)


def test_archive_and_restore_name_an_unknown_kind(kinds: KindStore) -> None:
    with pytest.raises(KindNotFound):
        kinds.archive("nope")
    with pytest.raises(KindNotFound):
        kinds.restore("nope")


def test_an_uncounted_kind_takes_an_item(kinds: KindStore, items: ItemStore) -> None:
    kinds.create(KindInput(id="design_fee", name="Design fee", tracking="none"), BY)

    made = items.create(ItemInput(kind_id="design_fee", name="One hour"), BY)
    assert made.tracking == "none"


def test_an_archived_parent_is_refused(kinds: KindStore) -> None:
    kinds.create(KindInput(id="widget", name="Widget", tracking="lot"), BY)
    kinds.archive("widget")

    with pytest.raises(KindArchived):
        kinds.create(KindInput(id="gizmo", name="Gizmo", parent_id="widget", tracking="lot"), BY)
    _fastener(kinds)
    with pytest.raises(KindArchived):
        kinds.update("fastener", KindPatch(parent_id="widget"), BY)
