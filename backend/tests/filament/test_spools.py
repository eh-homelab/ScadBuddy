"""The spool store: spools, their tags and their ledger (#6261, spec §5.3-5.5)."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.models import (
    Material,
    SpoolCreate,
    SpoolEventAdd,
    SpoolPatch,
    SpoolTypeCreate,
)
from scadbuddy.filament.spools import (
    FilamentSpoolStore,
    SpoolInUseError,
    SpoolTypelessError,
    TagUnnamedError,
)
from scadbuddy.filament.types import SpoolTypeStore, TypeTempError
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres


def _material(pool: PgPool) -> UUID:
    rows = CatalogStore(pool).list("materials")
    return next(r.id for r in rows if isinstance(r, Material) and r.name == "PLA")


def _spool(store: FilamentSpoolStore, pool: PgPool, **over: object) -> UUID:
    body = SpoolCreate.model_validate({"material_id": _material(pool)} | over)
    return store.create(body, actor="ann").id


def _kinds(store: FilamentSpoolStore, spool: UUID) -> list[str]:
    return [e.kind for e in store.events(spool)]


def test_a_spool_is_made_with_a_created_event_and_its_effective_values(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    kind = SpoolTypeStore(pg_pool).create(
        SpoolTypeCreate(
            code="PLA-RED",
            material_id=_material(pg_pool),
            rgba="#FF0000",
            label_weight_g=1000,
            nozzle_temp_max=230,
        )
    )
    made = store.create(SpoolCreate(type_id=kind.id, grams_left=990, note="hi"), actor="ann")
    assert made.grams_left == 990
    assert made.effective.rgba == "#FF0000FF"
    assert made.effective.label_weight_g == 1000
    assert made.overridden == []
    events = store.events(made.id)
    assert [(e.kind, e.source, e.grams_after, e.actor) for e in events] == [
        ("created", "person", 990, "ann")
    ]
    assert [s.id for s in store.list(needs_type=False)] == [made.id]
    assert store.list(needs_type=True) == []


def test_a_backwards_effective_range_is_refused(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    kind = SpoolTypeStore(pg_pool).create(
        SpoolTypeCreate(
            code="PLA-BLU",
            material_id=_material(pg_pool),
            rgba="#0000FF",
            label_weight_g=1000,
            nozzle_temp_max=230,
        )
    )
    with pytest.raises(TypeTempError):
        store.create(SpoolCreate(type_id=kind.id, nozzle_temp_min=240), actor="ann")
    made = store.create(SpoolCreate(type_id=kind.id), actor="ann")
    with pytest.raises(TypeTempError):
        store.update(made.id, SpoolPatch(nozzle_temp_min=240), actor="ann")
    assert store.list() == [store.get(made.id)]


def test_a_patch_that_leaves_no_type_and_no_material_is_refused(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool)
    with pytest.raises(SpoolTypelessError):
        store.update(spool, SpoolPatch(material_id=None), actor="ann")


def test_an_edit_records_each_change(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool, note="old")
    changed = store.update(spool, SpoolPatch(note="new", rgba="#00FF00"), actor="bob")
    assert changed is not None
    assert changed.note == "new"
    assert changed.overridden == ["material_id", "rgba"]
    edits = [e for e in store.events(spool) if e.kind == "edited"]
    assert len(edits) == 1
    assert edits[0].actor == "bob"
    assert edits[0].data == {"changes": {"note": ["old", "new"], "rgba": [None, "#00FF00FF"]}}
    store.update(spool, SpoolPatch(note="new"), actor="bob")
    assert _kinds(store, spool).count("edited") == 1


def test_rebinding_a_tag_moves_it(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    a, b = _spool(store, pg_pool), _spool(store, pg_pool)
    first = store.bind_tag(a, tag_uid="aa:bb:cc:dd", kind="bambu_mifare", actor="ann")
    assert first is not None
    again = store.bind_tag(b, tag_uid="AABBCCDD", kind="bambu_mifare", actor="ann")
    assert again is not None
    assert again.tag_id == first.tag_id
    assert again.spool_id == b
    assert store.open_binding(a) is None
    assert _kinds(store, a) == ["created", "tag_bound", "tag_unbound"]
    assert _kinds(store, b) == ["created", "tag_bound"]
    unbound = store.unbind_tag(first.tag_id, actor="ann")
    assert unbound is not None and unbound.unbound_at is not None
    assert store.unbind_tag(first.tag_id, actor="ann") is None
    assert _kinds(store, b)[-1] == "tag_unbound"
    with pytest.raises(TagUnnamedError):
        store.bind_tag(a, tag_uid="0000", kind="other", actor="ann")


def test_a_spool_with_prints_is_not_deleted_but_one_without_is(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    used, fresh = _spool(store, pg_pool), _spool(store, pg_pool)
    store.bind_tag(fresh, tray_uuid="AB" * 16, kind="bambu_mifare", actor="ann")
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO filament_spool_events (spool_id, kind, source, delta_g)"
            " VALUES (%s, 'print_ended', 'print_actual', -3)",
            (used,),
        )
    with pytest.raises(SpoolInUseError):
        store.delete(used)
    assert store.get(used) is not None
    assert store.delete(fresh) is True
    assert store.get(fresh) is None
    assert store.delete(fresh) is False


def test_a_weighing_and_a_drying_update_the_derived_values(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool)
    store.add_event(spool, SpoolEventAdd(kind="weighed", grams=640), actor="ann")
    store.add_event(spool, SpoolEventAdd(kind="dried", temp_c=55, hours=Decimal("8")), actor="ann")
    now = store.get(spool)
    assert now is not None
    assert (now.grams_left, now.last_weighed_g, now.last_dried_temp_c) == (640, 640, 55)
    assert now.last_dried_at is not None
    archived = store.archive(spool, True, actor="ann")
    assert archived is not None and archived.archived_at is not None
    assert store.list() == []
    assert _kinds(store, spool)[-1] == "archived"
    restored = store.archive(spool, False, actor="ann")
    assert restored is not None and restored.archived_at is None
    assert _kinds(store, spool)[-1] == "restored"
