"""The spool store: spools, their tags and their ledger (#6261, spec §5.3-5.5)."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

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


def test_a_reading_from_the_future_is_refused() -> None:
    soon = datetime.now(UTC) + timedelta(minutes=1)
    assert SpoolEventAdd(kind="weighed", grams=1, at=soon).at == soon
    with pytest.raises(ValidationError, match="future"):
        SpoolEventAdd(kind="weighed", grams=1, at=datetime.now(UTC) + timedelta(hours=1))
    with pytest.raises(ValidationError):
        SpoolEventAdd(kind="weighed", grams=1, at=datetime(2026, 11, 1))  # no offset


def test_binds_in_opposite_directions_do_not_deadlock(pg_pool: PgPool) -> None:
    # Two tags swap between two spools at once, over and over. Each bind locks the
    # other's spool too; with the locks taken in one order, every round completes.
    store = FilamentSpoolStore(pg_pool)
    a, b = _spool(store, pg_pool), _spool(store, pg_pool)
    tag_aa = store.bind_tag(a, tag_uid="AA01", kind="other", actor="ann")
    tag_bb = store.bind_tag(b, tag_uid="BB02", kind="other", actor="ann")
    assert tag_aa is not None and tag_bb is not None
    rounds = 15
    gate = threading.Barrier(2)

    def swap(uid: str, targets: tuple[UUID, UUID]) -> None:
        for i in range(rounds):
            gate.wait(timeout=30)
            store.bind_tag(targets[i % 2], tag_uid=uid, kind="other", actor="ann")

    with ThreadPoolExecutor(max_workers=2) as pool:
        moves = [pool.submit(swap, "AA01", (b, a)), pool.submit(swap, "BB02", (a, b))]
        for move in moves:
            move.result(timeout=120)  # a DeadlockDetected would surface here
    # The last round moved AA01 to b and BB02 to a, in either order.
    on_a, on_b = store.open_binding(a), store.open_binding(b)
    assert on_a is not None and on_b is not None
    assert (on_a.tag_id, on_b.tag_id) == (tag_bb.tag_id, tag_aa.tag_id)


def test_a_bind_to_a_missing_spool_makes_no_tag(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    missing = UUID(int=7)
    assert store.bind_tag(missing, tag_uid="CC03", kind="other", actor="ann") is None
    with pg_pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM filament_tags WHERE tag_uid = 'CC03'"
        ).fetchone()
    assert row is not None and row["n"] == 0


def test_archiving_a_spool_takes_its_tag_off_and_restoring_binds_nothing(
    pg_pool: PgPool,
) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool)
    bound = store.bind_tag(spool, tag_uid="A1B2C3D4", kind="bambu_mifare", actor="ann")
    assert bound is not None
    store.archive(spool, True, actor="ann")
    assert store.open_binding(spool) is None
    unbound = [e for e in store.events(spool) if e.kind == "tag_unbound"]
    assert [e.data for e in unbound] == [{"tag_id": str(bound.tag_id), "reason": "archived"}]
    assert _kinds(store, spool)[-2:] == ["tag_unbound", "archived"]
    store.archive(spool, False, actor="ann")
    assert store.open_binding(spool) is None
    # The tag is free for another spool.
    other = _spool(store, pg_pool)
    assert store.bind_tag(other, tag_uid="A1B2C3D4", kind="bambu_mifare", actor="ann")
