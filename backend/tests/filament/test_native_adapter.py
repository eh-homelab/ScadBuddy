"""The native spool store over the filament tables (#6261, Task 8): a native spool is a
filament spool, and a native print's uses are ledger events."""

from __future__ import annotations

from uuid import UUID

import pytest
from psycopg.types.json import Jsonb

from scadbuddy.filament.spools import FilamentSpoolStore, SpoolInUseError
from scadbuddy.spools.native import (
    NativeSpoolCreate,
    NativeSpoolExistsError,
    NativeSpoolPatch,
    NativeSpoolStore,
    SpoolUse,
)
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

RUN = "run-6261"
TRAY = "A1B2C3D4E5F60718293A4B5C6D7E8F90"


def _run(pool: PgPool, *subtasks: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO print_runs (id, output_id, idempotency_key, status, result)"
            " VALUES (%s, 'out', 'k', 'succeeded', %s)",
            (RUN, Jsonb({"lan": [{"subtask_name": s} for s in subtasks]})),
        )


def _kinds(spools: FilamentSpoolStore, spool_id: UUID) -> list[tuple[str, str | None]]:
    return [(e.kind, e.ref_id) for e in spools.events(spool_id)]


def test_a_native_spool_is_a_filament_spool(pg_pool: PgPool) -> None:
    native, spools = NativeSpoolStore(pg_pool), FilamentSpoolStore(pg_pool)
    made = native.create(
        NativeSpoolCreate(
            material="Glitter PLA",
            colour="#ff8800",
            brand="Bambu",
            name="Orange",
            label_weight_g=1000,
            remaining_g=100,
            tray_uuid=TRAY,
            preset="GFA00",
        )
    )
    spool = spools.get(made.id)
    assert spool is not None
    assert spool.rgba == "#FF8800FF" and spool.colour_name == "Orange"
    assert spool.grams_left == 100
    assert _kinds(spools, made.id)[0] == ("created", None)
    binding = spools.open_binding(made.id)
    assert binding is not None
    assert (made.colour, made.material, made.brand, made.preset) == (
        "#FF8800",
        "Glitter PLA",
        "Bambu",
        "GFA00",
    )
    assert (made.tray_uuid, made.remaining_g) == (TRAY, 100)
    with pg_pool.connection() as conn:
        found = conn.execute(
            "SELECT count(*) AS n FROM filament_materials WHERE name = 'Glitter PLA'"
        ).fetchone()
    assert found is not None and found["n"] == 1

    _run(pg_pool, "a", "b")
    native.record_uses(RUN, "a", [SpoolUse(slot_id=1, spool_id=made.id, grams=12.5)])
    native.record_uses(RUN, "b", [SpoolUse(slot_id=2, spool_id=made.id, grams=30)])
    # The estimates come off while the print runs.
    assert native.get(made.id).remaining_g == 57.5  # type: ignore[union-attr]
    assert native.settle_uses(RUN, ["done", "failed"]) == {made.id}
    spool = spools.get(made.id)
    assert spool is not None and spool.grams_left == 87.5
    events = spools.events(made.id)
    ended = {e.ref_id: (e.source, e.delta_g, e.data) for e in events if e.kind == "print_ended"}
    assert ended == {
        f"{RUN}/a/1": ("print_actual", -12.5, {"basis": "sliced"}),
        f"{RUN}/b/2": ("print_actual", 0.0, {"basis": "not_applied"}),
    }
    started = [e for e in events if e.kind == "print_started"]
    assert {(e.source, e.ref_kind, e.delta_g) for e in started} == {
        ("print_estimate", "print_run", -12.5),
        ("print_estimate", "print_run", -30.0),
    }
    assert native.settle_uses(RUN, ["done", "done"]) == set()


def test_a_migrated_estimate_is_closed_by_a_native_settle(pg_pool: PgPool) -> None:
    """A ``print_started`` the 1302Z migration wrote (no ``data``) settles like a new one."""
    native, spools = NativeSpoolStore(pg_pool), FilamentSpoolStore(pg_pool)
    made = native.create(NativeSpoolCreate(material="PLA", remaining_g=50))
    _run(pg_pool, "p1")
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO filament_spool_events (spool_id, kind, source, delta_g, ref_kind, ref_id)"
            " VALUES (%s, 'print_started', 'print_estimate', -5, 'print_run', %s)",
            (made.id, f"{RUN}/p1/3"),
        )
    assert native.settle_uses(RUN, ["done"]) == {made.id}
    assert native.get(made.id).remaining_g == 45  # type: ignore[union-attr]
    assert ("print_ended", f"{RUN}/p1/3") in _kinds(spools, made.id)


def test_archive_frees_the_tag_and_a_restore_takes_it_back(pg_pool: PgPool) -> None:
    native, spools = NativeSpoolStore(pg_pool), FilamentSpoolStore(pg_pool)
    first = native.create(NativeSpoolCreate(tray_uuid=TRAY))
    # A spool with no material is the migration's "Unknown", which reads as none.
    assert first.material is None
    kept = spools.get(first.id)
    assert kept is not None and kept.material_id is not None
    archived = native.update(first.id, NativeSpoolPatch(archived=True))
    assert archived is not None and archived.tray_uuid == TRAY
    assert spools.open_binding(first.id) is None
    native.update(first.id, NativeSpoolPatch(archived=False))
    assert spools.open_binding(first.id) is not None
    with pytest.raises(NativeSpoolExistsError):
        native.create(NativeSpoolCreate(tray_uuid=TRAY))


def test_a_patch_writes_the_ledger(pg_pool: PgPool) -> None:
    native, spools = NativeSpoolStore(pg_pool), FilamentSpoolStore(pg_pool)
    made = native.create(NativeSpoolCreate(material="PLA", remaining_g=100, preset="GFA00"))
    patched = native.update(
        made.id, NativeSpoolPatch(remaining_g=60, colour="#00ff00", preset=None)
    )
    assert patched is not None
    assert (patched.remaining_g, patched.colour, patched.preset) == (60, "#00FF00", None)
    kinds = [e.kind for e in spools.events(made.id)]
    assert kinds == ["created", "edited", "weighed"]
    weighed = spools.events(made.id)[-1]
    assert (weighed.source, weighed.grams_after) == ("person", 60)


def test_a_spool_that_printed_is_not_deleted(pg_pool: PgPool) -> None:
    native = NativeSpoolStore(pg_pool)
    made = native.create(NativeSpoolCreate(material="PLA", remaining_g=100))
    _run(pg_pool, "a")
    native.record_uses(RUN, "a", [SpoolUse(slot_id=1, spool_id=made.id, grams=1)])
    native.settle_uses(RUN, ["done"])
    with pytest.raises(SpoolInUseError):
        native.delete(made.id)
    assert native.get(made.id) is not None


def test_a_restore_refuses_a_tag_another_spool_now_holds(pg_pool: PgPool) -> None:
    native, spools = NativeSpoolStore(pg_pool), FilamentSpoolStore(pg_pool)
    first = native.create(NativeSpoolCreate(material="PLA", tray_uuid=TRAY))
    native.update(first.id, NativeSpoolPatch(archived=True))
    second = native.create(NativeSpoolCreate(material="PLA", tray_uuid=TRAY))
    with pytest.raises(NativeSpoolExistsError):
        native.update(first.id, NativeSpoolPatch(archived=False))
    kept = native.get(first.id)
    assert kept is not None and kept.archived_at is not None
    binding = spools.open_binding(second.id)
    assert binding is not None and native.get(second.id).tray_uuid == TRAY  # type: ignore[union-attr]


def test_a_slot_sent_again_with_another_spool_books_once(pg_pool: PgPool) -> None:
    native, spools = NativeSpoolStore(pg_pool), FilamentSpoolStore(pg_pool)
    red = native.create(NativeSpoolCreate(material="PLA", remaining_g=100))
    blue = native.create(NativeSpoolCreate(material="PLA", remaining_g=100))
    native.record_uses(RUN, "a", [SpoolUse(slot_id=1, spool_id=red.id, grams=10)])
    native.record_uses(RUN, "a", [SpoolUse(slot_id=1, spool_id=blue.id, grams=10)])
    assert ("print_started", f"{RUN}/a/1") in _kinds(spools, red.id)
    assert [kind for kind, _ in _kinds(spools, blue.id)] == ["created"]
    assert native.get(blue.id).remaining_g == 100  # type: ignore[union-attr]


def test_a_null_remaining_patch_writes_no_event(pg_pool: PgPool) -> None:
    native, spools = NativeSpoolStore(pg_pool), FilamentSpoolStore(pg_pool)
    made = native.create(NativeSpoolCreate(material="PLA", remaining_g=100))
    patched = native.update(made.id, NativeSpoolPatch(remaining_g=None))
    assert patched is not None and patched.remaining_g == 100
    assert [e.kind for e in spools.events(made.id)] == ["created"]
