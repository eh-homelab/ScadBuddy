"""Override bindings and the pending-tag queue (#6264 Task 2)."""

from __future__ import annotations

from uuid import UUID

import pytest
from psycopg import errors
from pydantic import ValidationError

from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.models import Material, SpoolCreate, SpoolPatch
from scadbuddy.filament.rfid_push import enqueue_tag
from scadbuddy.filament.spools import FilamentSpoolStore, OverrideKindError
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

_T1 = "aa:bb:cc:dd:ee:01:02:03"
_TRAY = "0123456789abcdef0123456789abcdef"


def _spool(store: FilamentSpoolStore, pool: PgPool) -> UUID:
    pla = next(
        r.id
        for r in CatalogStore(pool).list("materials")
        if isinstance(r, Material) and r.name == "PLA"
    )
    return store.create(SpoolCreate(material_id=pla), actor="ann").id


def _locks(store: FilamentSpoolStore, spool: UUID) -> list[object]:
    return [e.data for e in store.events(spool) if e.kind == "edited"]


def _kinds(store: FilamentSpoolStore, spool: UUID) -> list[str]:
    return [e.kind for e in store.events(spool)]


def _pending(pool: PgPool) -> set[UUID]:
    with pool.connection() as conn:
        return {r["tag_id"] for r in conn.execute("SELECT tag_id FROM filament_bb_tag_pending")}


def _locked(store: FilamentSpoolStore, spool: UUID) -> bool:
    read = store.get(spool)
    assert read is not None
    return read.weight_locked


def test_an_override_bind_locks_the_weight(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool)
    assert not _locked(store, spool)
    bound = store.bind_tag(spool, tray_uuid=_TRAY, kind="bambu_mifare", override=True, actor="ann")
    assert bound is not None and bound.override is True
    assert _locked(store, spool)
    assert _locks(store, spool) == [{"changes": {"weight_locked": [False, True]}}]


def test_an_override_on_an_ntag_is_refused(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool)
    with pytest.raises(OverrideKindError):
        store.bind_tag(spool, tag_uid=_T1, kind="ntag_opentag", override=True, actor="ann")
    assert store.open_binding(spool) is None
    assert _locks(store, spool) == []
    assert not _locked(store, spool)
    assert _pending(pg_pool) == set()
    with pg_pool.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM filament_tags").fetchone() == {"n": 0}


def test_a_second_override_bind_adds_no_second_lock_event(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool)
    for _ in range(2):
        store.bind_tag(spool, tray_uuid=_TRAY, kind="bambu_mifare", override=True, actor="ann")
    assert len(_locks(store, spool)) == 1
    assert _kinds(store, spool).count("tag_bound") == 1


def test_a_locked_spool_gets_no_lock_event(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool)
    store.update(spool, SpoolPatch(weight_locked=True), actor="ann")
    before = len(_locks(store, spool))
    store.bind_tag(spool, tray_uuid=_TRAY, kind="bambu_mifare", override=True, actor="ann")
    assert len(_locks(store, spool)) == before


def test_override_on_a_held_tag_updates_the_binding(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool)
    first = store.bind_tag(spool, tray_uuid=_TRAY, kind="bambu_mifare", actor="ann")
    assert first is not None and first.override is False
    again = store.bind_tag(spool, tray_uuid=_TRAY, kind="bambu_mifare", override=True, actor="ann")
    assert again is not None
    assert again.id == first.id and again.override is True
    assert _locked(store, spool)
    assert len(_locks(store, spool)) == 1
    assert _kinds(store, spool).count("tag_bound") == 1
    assert "tag_unbound" not in _kinds(store, spool)
    assert _pending(pg_pool) == {first.tag_id}


def test_a_rebind_queues_both_tags(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool)
    one = store.bind_tag(spool, tag_uid=_T1, kind="other", actor="ann")
    two = store.bind_tag(spool, tag_uid="0102", kind="other", actor="ann")
    assert one is not None and two is not None
    assert _pending(pg_pool) == {one.tag_id, two.tag_id}


def test_an_unbind_and_an_archive_queue_the_tag(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    a, b = _spool(store, pg_pool), _spool(store, pg_pool)
    one = store.bind_tag(a, tag_uid=_T1, kind="other", actor="ann")
    two = store.bind_tag(b, tag_uid="0102", kind="other", actor="ann")
    assert one is not None and two is not None
    with pg_pool.connection() as conn:
        conn.execute("DELETE FROM filament_bb_tag_pending")
    store.unbind_tag(one.tag_id, actor="ann")
    assert _pending(pg_pool) == {one.tag_id}
    store.update(b, SpoolPatch(archived=True), actor="ann")
    assert _pending(pg_pool) == {one.tag_id, two.tag_id}


def test_filament_sync_tags_defaults_to_the_pull_and_refuses_other_values() -> None:
    assert FilamentSync().tags == "bb_to_sb"
    assert FilamentSync(tags="sb_to_bb").tags == "sb_to_bb"
    for bad in ("push", "both", ""):
        with pytest.raises(ValidationError):
            FilamentSync.model_validate({"tags": bad})


def test_dropping_an_override_keeps_the_weight_locked_and_queues_the_tag(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool)
    first = store.bind_tag(spool, tray_uuid=_TRAY, kind="bambu_mifare", override=True, actor="ann")
    assert first is not None
    with pg_pool.connection() as conn:
        conn.execute("DELETE FROM filament_bb_tag_pending")
    off = store.bind_tag(spool, tray_uuid=_TRAY, kind="bambu_mifare", actor="ann")
    assert off is not None and off.id == first.id and off.override is False
    assert _locked(store, spool)
    assert len(_locks(store, spool)) == 1
    assert _pending(pg_pool) == {first.tag_id}


def test_an_existing_ntag_is_refused_an_override_whatever_kind_the_caller_names(
    pg_pool: PgPool,
) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool)
    store.bind_tag(spool, tag_uid=_T1, kind="ntag_opentag", actor="ann")
    with pytest.raises(OverrideKindError):
        store.bind_tag(spool, tag_uid=_T1, kind="bambu_mifare", override=True, actor="ann")
    assert not _locked(store, spool)


def test_requeueing_a_tag_resets_its_attempts_and_error(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool)
    bound = store.bind_tag(spool, tag_uid=_T1, kind="other", actor="ann")
    assert bound is not None
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_bb_tag_pending SET attempts = 4, last_error = 'boom'"
            " WHERE tag_id = %s",
            (bound.tag_id,),
        )
        enqueue_tag(conn, bound.tag_id)
        row = conn.execute("SELECT attempts, last_error FROM filament_bb_tag_pending").fetchone()
    assert row == {"attempts": 0, "last_error": None}


def test_a_second_rfid_state_row_is_refused(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        with pytest.raises(errors.UniqueViolation), conn.transaction():
            conn.execute("INSERT INTO filament_rfid_state (singleton) VALUES (true)")
        with pytest.raises(errors.CheckViolation), conn.transaction():
            conn.execute("INSERT INTO filament_rfid_state (singleton) VALUES (false)")


def test_deleting_a_spool_removes_its_override_slot(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool)
    with pg_pool.connection() as conn:
        printer = conn.execute(
            "INSERT INTO printers (id, serial, name) VALUES (gen_random_uuid(), 'S1', 'p')"
            " RETURNING id"
        ).fetchone()
        assert printer is not None
        conn.execute(
            "INSERT INTO filament_override_slots (printer_id, ams_id, tray_id, spool_id)"
            " VALUES (%s, 0, 0, %s)",
            (printer["id"], spool),
        )
    assert store.delete(spool) is True
    with pg_pool.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM filament_override_slots").fetchone() == {
            "n": 0
        }
