"""What Bambuddy answered to the sync's writes (#6265 Task 5): links, hashes, the seen
copies, the echo event, errors and the instance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from psycopg.types.json import Jsonb

from scadbuddy.bambuddy.errors import map_transport
from scadbuddy.bambuddy.models import BbLocation, BbSupplier
from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament import bb_sync_plan
from scadbuddy.filament.bambuddy_pull import Pulls, pull
from scadbuddy.filament.bb_sync_models import BbWrite, BbWriteResult, Kind, Op, ResultsReport
from scadbuddy.filament.bb_sync_plan import UNSEEN_KEY, field_hash
from scadbuddy.filament.bb_sync_results import adopt_instance, apply_results
from scadbuddy.filament.component import Filament
from scadbuddy.filament.models import (
    InsertTypeCreate,
    InsertTypePatch,
    LocationCreate,
    LocationPatch,
    SpoolPatch,
    SpoolTypePatch,
)
from scadbuddy.filament.purchasing_models import SupplierCreate, SupplierPatch
from scadbuddy.filament.rfid_push import error_label
from scadbuddy.filament.spools import SpoolBambuddyCreatingError, SpoolBambuddyLinkedError
from tests.conftest import PgPool
from tests.filament.test_bb_sync_plan import (
    INSTANCE,
    SPOOLS,
    _bb_pushed,
    _filament,
    _intents,
    _kind,
    _link,
    _linked,
    _matching,
    _only,
    _plan,
    _refuse,
    _row,
    _snap,
    _spool,
    _type,
)

pytestmark = pytest.mark.requires_postgres

TWO_WAY_LOCATIONS = SPOOLS.model_copy(update={"spools": "off", "locations": "two_way"})


def _bb_now(minutes: int = 0) -> str:
    """A Bambuddy time (naive UTC) ``minutes`` from now."""
    return (datetime.now(UTC) + timedelta(minutes=minutes)).replace(tzinfo=None).isoformat()


def _answer(bb_id: int, **over: Any) -> dict[str, Any]:
    """A recorded Bambuddy spool row with no weighing, drying or preset, so a pull of it
    books nothing of its own."""
    quiet: dict[str, Any] = {
        "last_weighed_at": None,
        "last_scale_weight": None,
        "last_dried_at": None,
        "slicer_filament": None,
        "slicer_filament_name": None,
        "k_profiles": [],
        "updated_at": _bb_now(),
    }
    return _row(bb_id, **(quiet | over))


def _apply(
    f: Filament,
    writes: list[BbWrite],
    results: list[BbWriteResult],
    instance: str = INSTANCE,
    pass_id: str | None = None,
    unconfirmed: list[tuple[Kind, UUID]] | None = None,
) -> ResultsReport:
    return apply_results(
        f,
        writes,
        results,
        instance=instance,
        now=datetime.now(UTC),
        pass_id=pass_id,
        unconfirmed=unconfirmed or [],
    )


def _ok(write: BbWrite, answer: dict[str, Any] | None, bb_id: int | None = None) -> BbWriteResult:
    return BbWriteResult(key=write.key, ok=True, bb_id=bb_id or write.bb_id, answer=answer)


def _refused(write: BbWrite, status: int = 422) -> BbWriteResult:
    return BbWriteResult(key=write.key, ok=False, error=f"bambuddy-refused:{status}", status=status)


def _write(
    kind: Kind,
    sb_id: UUID,
    op: Op,
    bb_id: int | None,
    body: dict[str, Any] | list[Any] | None = None,
    hashes: dict[str, str] | None = None,
) -> BbWrite:
    return BbWrite(
        key=f"{kind}:{sb_id}:{op}",
        kind=kind,
        sb_id=sb_id,
        op=op,
        bb_id=bb_id,
        body={} if body is None else body,
        hashes=hashes or {},
    )


def _body(write: BbWrite) -> dict[str, Any]:
    assert isinstance(write.body, dict)
    return write.body


def _link_row(pool: PgPool, spool: UUID) -> dict[str, Any] | None:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT * FROM filament_bb_links WHERE spool_id = %s", (spool,)
        ).fetchone()
    return None if row is None else dict(row)


def _errors(pool: PgPool) -> list[tuple[str, UUID, str, int]]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT kind, sb_id, error, attempts FROM filament_bb_push_errors ORDER BY kind"
        ).fetchall()
    return [(r["kind"], r["sb_id"], r["error"], r["attempts"]) for r in rows]


def _instance(pool: PgPool) -> str | None:
    with pool.connection() as conn:
        row = conn.execute("SELECT instance FROM filament_bb_sync_state").fetchone()
    assert row is not None
    instance: str | None = row["instance"]
    return instance


def _pushed_events(f: Filament, spool: UUID) -> list[Any]:
    return [
        e
        for e in f.spools.events(spool)
        if e.kind == "synced_from_bb" and (e.data or {}).get("pushed")
    ]


def _created(pool: PgPool, f: Filament) -> tuple[UUID, BbWrite, dict[str, Any]]:
    """A spool the plan creates in Bambuddy as row 60, and `apply_results` links."""
    spool = _spool(f, _type(f))
    create = _only(_plan(f, _snap([])).writes, "create")
    answer = _answer(60, **_body(create))
    assert _apply(f, [create], [_ok(create, answer, 60)]).linked == 1
    return spool, create, answer


# -- creates -------------------------------------------------------------------------


def test_a_created_spool_is_linked_with_its_hashes(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool, create, answer = _created(pg_pool, f)
    link = _link_row(pg_pool, spool)
    assert link is not None
    assert link["bb_spool_id"] == 60
    assert link["pushed_fields"] == create.hashes
    assert link["bb_seen"]["color_name"] == answer["color_name"]
    assert link["bb_seen_at"] is not None
    # R-T4-11: a full answer's K-profiles are stored with its seen copy.
    assert link["bb_k_profiles"] == []
    assert _intents(pg_pool) == []
    (event,) = _pushed_events(f, spool)
    assert event.data["changes"] == {}
    assert event.data["bb"]["colour_name"] == answer["color_name"]
    assert event.grams_after is None


def test_a_new_link_queues_the_spools_tags(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    binding = f.spools.bind_tag(
        spool, tag_uid="AABBCCDD11223344", kind="ntag_opentag", actor="person"
    )
    assert binding is not None
    with pg_pool.connection() as conn:
        conn.execute("DELETE FROM filament_bb_tag_pending")
    create = _only(_plan(f, _snap([])).writes, "create")
    _apply(f, [create], [_ok(create, _answer(60, **_body(create)), 60)])
    with pg_pool.connection() as conn:
        pending = [r["tag_id"] for r in conn.execute("SELECT tag_id FROM filament_bb_tag_pending")]
    assert pending == [binding.tag_id]


def test_a_pushed_value_is_not_pulled_back(pg_pool: PgPool) -> None:
    """Decision 9: the pull compares with the push's event, so it books no reading and
    takes no change from the row the push made."""
    f = _filament(pg_pool)
    spool, _, answer = _created(pg_pool, f)
    f.spools.update(spool, SpoolPatch(colour_name="Brick"), actor="person")
    update = _only(_plan(f, _snap([answer])).writes, "update")
    assert update.body == {"color_name": "Brick"}
    after = answer | update.body | {"updated_at": _bb_now(1)}
    assert _apply(f, [update], [_ok(update, after)]).updated == 1
    events = len(f.spools.events(spool))
    report = pull(f, [BbSpool.model_validate(after)], now=datetime.now(UTC))
    assert (report.updated, report.linked, report.created) == (0, 0, 0)
    assert len(f.spools.events(spool)) == events
    sb = f.spools.get(spool)
    assert sb is not None and sb.colour_name == "Brick"


def test_a_create_without_an_answer_keeps_its_intent(pg_pool: PgPool) -> None:
    """The create has no Bambuddy id to link: the intent stays, so a later pass adopts
    the row the create made (Decision 10)."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    create = _only(_plan(f, _snap([])).writes, "create")
    assert _apply(f, [create], [_ok(create, None)]) == ResultsReport()
    assert _link_row(pg_pool, spool) is None
    assert _intents(pg_pool) == [("spool", spool)]


# -- 404s, probes and deletes ----------------------------------------------------------


@pytest.mark.parametrize("op", ["update", "probe", "archive"])
def test_a_404_drops_the_link(pg_pool: PgPool, op: Op) -> None:
    """Decision 13: Bambuddy says the row is gone. The link, its hashes and its error row
    go."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    _link(pg_pool, spool, 50, pushed={"note": field_hash(None)})
    write = _write("spool", spool, op, 50, {"note": "x"} if op == "update" else {})
    _apply(f, [write], [_refused(write, 500)])
    assert _errors(pg_pool) == [("spool", spool, "bambuddy-refused:500", 1)]
    assert _apply(f, [write], [_refused(write, 404)]).unlinked == 1
    assert _link_row(pg_pool, spool) is None
    assert _errors(pg_pool) == []


def test_a_404_on_a_list_keeps_the_link(pg_pool: PgPool) -> None:
    """A ``PUT …/suppliers`` answers 404 for a supplier Bambuddy does not know, not only
    for a missing spool (``write_semantics.replace_all_lists``): the link stays, and the
    write counts failed. A missing spool shows in the next listing and its probe."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    _link(pg_pool, spool, 50)
    write = _write("spool", spool, "suppliers", 50, [], {"suppliers": field_hash([])})
    assert _apply(f, [write], [_refused(write, 404)]).failed == 1
    assert _link_row(pg_pool, spool) is not None
    assert _errors(pg_pool) == [("spool", spool, "bambuddy-refused:404", 1)]


def test_a_probe_that_finds_the_spool_keeps_the_link(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    _link(pg_pool, spool, 50)
    probe = _write("spool", spool, "probe", 50)
    # A probe writes nothing to Bambuddy, so it counts nothing (decision 91, R-T5 I-1).
    assert _apply(f, [probe], [_ok(probe, _answer(50))]) == ResultsReport()
    link = _link_row(pg_pool, spool)
    assert link is not None and link["bb_spool_id"] == 50
    assert link["bb_seen"] is not None and link["bb_seen_at"] is not None
    assert _pushed_events(f, spool) == []


#: Each row kind's link column and a Bambuddy answer for its row 40 (#6688).
_ROW_LINKS: dict[str, tuple[str, str, dict[str, Any]]] = {
    "supplier": ("filament_suppliers", "bb_supplier_id", {"id": 40, "name": "Shop"}),
    "location": ("filament_locations", "bb_location_id", {"id": 40, "name": "Shelf A"}),
    "insert_type": (
        "filament_insert_types",
        "bb_catalog_id",
        {"id": 40, "name": "Cardboard", "weight": 250},
    ),
    "filament": (
        "filament_spool_types",
        "bb_filament_id",
        {"id": 40, "name": "GFA00-RED", "type": "PLA"},
    ),
}


def _row_link(pool: PgPool, kind: str, sb_id: UUID) -> dict[str, Any]:
    table, column, _ = _ROW_LINKS[kind]
    with pool.connection() as conn:
        row = conn.execute(
            f"SELECT {column} AS bb_id, bb_seen, bb_seen_at FROM {table} WHERE id = %s", (sb_id,)
        ).fetchone()
    assert row is not None
    return dict(row)


@pytest.mark.parametrize("kind", list(_ROW_LINKS))
def test_a_row_probe_404_drops_the_link(pg_pool: PgPool, kind: Kind) -> None:
    """R-6688-3: the probe's 404 says Bambuddy deleted the row: the link, its hashes, its
    seen copy and its error row go. A probe that failed otherwise keeps the link and
    records an error (R-6688-3)."""
    f = _filament(pg_pool)
    sb_id = _linked(pg_pool, f, kind)
    probe = _write(kind, sb_id, "probe", 40)
    assert _apply(f, [probe], [_refused(probe, 503)]).failed == 1
    assert _row_link(pg_pool, kind, sb_id)["bb_id"] == 40
    assert _errors(pg_pool) == [(kind, sb_id, "bambuddy-refused:503", 1)]
    timeout = BbWriteResult(key=probe.key, ok=False, error="bambuddy-unreachable")
    assert _apply(f, [probe], [timeout]).failed == 1
    assert _row_link(pg_pool, kind, sb_id)["bb_id"] == 40
    assert _apply(f, [probe], [_refused(probe, 404)]).unlinked == 1
    assert _row_link(pg_pool, kind, sb_id) == {"bb_id": None, "bb_seen": None, "bb_seen_at": None}
    assert _errors(pg_pool) == []


@pytest.mark.parametrize("kind", list(_ROW_LINKS))
def test_a_row_probe_that_finds_the_row_keeps_the_link(pg_pool: PgPool, kind: Kind) -> None:
    """R-6688-1: the found row is stored as seen now, after the snapshot, so a later
    round of the pass neither probes it again nor writes it from the old listing."""
    f = _filament(pg_pool)
    sb_id = _linked(pg_pool, f, kind)
    probe = _write(kind, sb_id, "probe", 40)
    assert _apply(f, [probe], [_ok(probe, _ROW_LINKS[kind][2])]) == ResultsReport()
    link = _row_link(pg_pool, kind, sb_id)
    assert link["bb_id"] == 40 and link["bb_seen_at"] is not None


def test_a_spool_names_its_deleted_location_only_once_it_is_made_again(
    pg_pool: PgPool,
) -> None:
    """R-6688-4, R-6688-5, the plan and the results together over one snapshot: round 1
    probes the location Bambuddy deleted, the 404 drops its link; round 2 makes it again;
    round 3 sends the spool with the new id. No round sends the deleted id."""
    f = _filament(pg_pool)
    sync = SPOOLS.model_copy(update={"locations": "sb_to_bb"})
    spool = _spool(f, _type(f))
    shelf = _linked(pg_pool, f, "location")
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_spools SET location_id = %s WHERE id = %s", (shelf, spool))
    row = _matching(pg_pool, spool, 50, sync) | {"location_id": None}
    _link(pg_pool, spool, 50)
    snap = _snap([row], locations=[], read_at=datetime.now(UTC) - timedelta(seconds=1))

    first = _plan(f, snap, sync)
    probe = _only(first.writes, "probe")
    assert [w.kind for w in first.writes] == ["location"] and first.deferred == 1
    assert _apply(f, first.writes, [_refused(probe, 404)]).unlinked == 1

    second = _plan(f, snap, sync)
    create = _only(second.writes, "create")
    assert [w.kind for w in second.writes] == ["location"] and second.deferred == 1
    made = {"id": 41, "name": "Shelf A", "identifier": None}
    assert _apply(f, second.writes, [_ok(create, made, 41)]).linked == 1

    third = _plan(f, snap, sync)
    update = _only(third.writes, "update")
    assert (update.kind, update.sb_id, _body(update)) == ("spool", spool, {"location_id": 41})
    assert third.deferred == 0


def test_a_spool_probe_beside_its_locations_probe_keeps_its_seen_location(
    pg_pool: PgPool,
) -> None:
    """R-6688-10: ``results`` compares a spool's answer with SB's values as the plan made
    them: a location probed in the same round may be gone, so its id is no SB value, and
    the answer's ``location_id`` is not taken into ``bb_seen`` as SB's."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    shelf = _linked(pg_pool, f, "location")
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_spools SET location_id = %s WHERE id = %s", (shelf, spool))
    _link(pg_pool, spool, 50, seen={"location_id": 7}, seen_at=datetime.now(UTC))
    location = _write("location", shelf, "probe", 40)
    probe = _write("spool", spool, "probe", 50)
    results = [_refused(location, 503), _ok(probe, _answer(50, location_id=40))]
    _apply(f, [location, probe], results)
    link = _link_row(pg_pool, spool)
    assert link is not None and link["bb_seen"]["location_id"] == 7


def test_a_spool_probe_while_its_locations_probe_failed_keeps_its_seen_location(
    pg_pool: PgPool,
) -> None:
    """R-6688-11: the location's probe failed earlier in this pass (``unsure``), so this
    round sends no write for it, and only the plan knows it is unconfirmed. ``results``
    takes the plan's ``unconfirmed`` rows, so a spool's probe in this round still does not
    take the answer's ``location_id`` into ``bb_seen`` as SB's."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    shelf = _linked(pg_pool, f, "location")
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_spools SET location_id = %s WHERE id = %s", (shelf, spool))
    _link(
        pg_pool, spool, 50, seen={"location_id": 7}, seen_at=datetime.now(UTC) - timedelta(hours=1)
    )
    read_at = datetime.now(UTC) - timedelta(minutes=1)
    _refuse(pg_pool, "location", shelf)
    sync = SPOOLS.model_copy(update={"locations": "sb_to_bb"})
    planned = _plan(f, _snap([], locations=[], read_at=read_at), sync)
    probe = _only(planned.writes, "probe")
    assert (probe.kind, probe.sb_id) == ("spool", spool)
    assert planned.unconfirmed == [("location", shelf)]
    _apply(
        f,
        planned.writes,
        [_ok(probe, _answer(50, location_id=40))],
        unconfirmed=planned.unconfirmed,
    )
    link = _link_row(pg_pool, spool)
    assert link is not None and link["bb_seen"]["location_id"] == 7


def test_a_spool_naming_an_archived_deleted_location_goes_without_it(pg_pool: PgPool) -> None:
    """R-6688-7: the probe of an archived location is planned in this round, so a later
    round of this pass can settle the spool that names it: it counts ``deferred``, not
    ``blocked``. The 404 drops the link, the archived row is not made again, and the next
    round sends the spool without ``location_id``."""
    f = _filament(pg_pool)
    sync = SPOOLS.model_copy(update={"locations": "sb_to_bb"})
    spool = _spool(f, _type(f))
    shelf = _linked(pg_pool, f, "location")
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_spools SET location_id = %s WHERE id = %s", (shelf, spool))
        conn.execute("UPDATE filament_locations SET archived_at = now() WHERE id = %s", (shelf,))
    row = _matching(pg_pool, spool, 50, sync) | {"location_id": None, "color_name": "Other"}
    _link(pg_pool, spool, 50)
    snap = _snap([row], locations=[], read_at=datetime.now(UTC) - timedelta(seconds=1))

    first = _plan(f, snap, sync)
    probe = _only(first.writes, "probe")
    assert [w.kind for w in first.writes] == ["location"]
    assert (first.deferred, first.blocked) == (1, 0)
    assert _apply(f, first.writes, [_refused(probe, 404)]).unlinked == 1

    second = _plan(f, snap, sync)
    update = _only(second.writes, "update")
    assert (update.kind, update.sb_id) == ("spool", spool)
    assert "location_id" not in _body(update) and "color_name" in _body(update)
    assert [w for w in second.writes if w.kind == "location"] == []
    assert (second.deferred, second.blocked) == (0, 0)


def test_a_spool_whose_link_was_dropped_can_be_deleted(pg_pool: PgPool) -> None:
    """Regression (Decision 13): ``delete`` refuses only while a link exists."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    _link(pg_pool, spool, 50)
    probe = _write("spool", spool, "probe", 50)
    _apply(f, [probe], [_refused(probe, 404)])
    assert f.spools.delete(spool) is True


def _timed_out(write: BbWrite) -> BbWriteResult:
    """What `bb_sync_run._call` records when Bambuddy's answer did not come in time."""
    exc = map_transport(httpx.ReadTimeout("no answer"), what="create a spool")
    return BbWriteResult(key=write.key, ok=False, error=error_label(exc))


def test_a_spool_whose_create_has_no_answer_is_not_deleted(pg_pool: PgPool) -> None:
    """Review M9: the create may have made a Bambuddy row whose answer was lost. Deleting
    the spool would drop the intent, and the pull would then import that row as a new SB
    spool, so the delete is refused while the intent is open."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    _only(_plan(f, _snap([])).writes, "create")
    with pytest.raises(SpoolBambuddyCreatingError):
        f.spools.delete(spool)
    assert _intents(pg_pool) == [("spool", spool)]
    assert f.spools.get(spool) is not None


@pytest.mark.parametrize("answer", ["timeout", "internal", "refused"])
def test_a_spool_whose_create_failed_is_not_deleted(pg_pool: PgPool, answer: str) -> None:
    """Review M9: a timed-out or `internal` create (the write may have reached Bambuddy)
    leaves an error row as a refusal does, and the intent stays. The delete is refused
    while the intent is open, error row or not."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    create = _only(_plan(f, _snap([])).writes, "create")
    result = {
        "timeout": _timed_out(create),
        "internal": BbWriteResult(key=create.key, ok=False, error="internal"),
        "refused": _refused(create),
    }[answer]
    assert _apply(f, [create], [result]).failed == 1
    assert _intents(pg_pool) == [("spool", spool)] and len(_errors(pg_pool)) == 1
    with pytest.raises(SpoolBambuddyCreatingError):
        f.spools.delete(spool)
    assert f.spools.get(spool) is not None
    assert _intents(pg_pool) == [("spool", spool)] and len(_errors(pg_pool)) == 1


def test_a_spool_deletes_once_its_create_intent_is_closed(pg_pool: PgPool) -> None:
    """Review M9: the next full pass (Sync now) retries a timed-out create, and its answer
    closes the intent and links the spool. The delete is then refused for the link
    only, and once Bambuddy's row is gone (a probe's 404 drops the link) it works."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    first = _only(_plan(f, _snap([])).writes, "create")
    _apply(f, [first], [_timed_out(first)])
    retry = _only(_plan(f, _snap([])).writes, "create")
    answer = _answer(60, **_body(retry))
    assert _apply(f, [retry], [_ok(retry, answer, 60)]).linked == 1
    assert _intents(pg_pool) == [] and _errors(pg_pool) == []
    with pytest.raises(SpoolBambuddyLinkedError) as refused:
        f.spools.delete(spool)
    assert not isinstance(refused.value, SpoolBambuddyCreatingError)
    probe = _write("spool", spool, "probe", 60)
    _apply(f, [probe], [_refused(probe, 404)])
    assert f.spools.delete(spool) is True
    assert f.spools.get(spool) is None
    assert _intents(pg_pool) == [] and _errors(pg_pool) == []


def test_archiving_an_item_clears_its_sync_errors(pg_pool: PgPool) -> None:
    """R-T3-5: an archived row's error leaves the status. The intent of a supplier,
    location, insert type or spool type goes too (the plan closes it anyway, R-T4-20);
    a spool's intent stays, so a restore adopts the row its lost create made."""
    f = _filament(pg_pool)
    kind = _type(f)
    spool = _spool(f, kind)
    supplier = f.suppliers.create(SupplierCreate(name="Shop")).id
    place = f.locations.create(LocationCreate(name="Shelf", kind_id=_kind(f, "Shelf"))).id
    made = f.catalog.create("insert-types", InsertTypeCreate(name="Core", empty_weight_g=200))
    insert: UUID = getattr(made, "id")  # noqa: B009
    items: list[tuple[Kind, UUID]] = [
        ("spool", spool),
        ("supplier", supplier),
        ("location", place),
        ("insert_type", insert),
        ("filament", kind),
    ]
    with pg_pool.connection() as conn:
        for item in items:
            conn.execute("INSERT INTO filament_bb_creates (kind, sb_id) VALUES (%s, %s)", item)
            conn.execute(
                "INSERT INTO filament_bb_push_errors (kind, sb_id, error) VALUES (%s, %s, 'x')",
                item,
            )
    f.spools.update(spool, SpoolPatch(archived=True), actor="person")
    f.suppliers.update(supplier, SupplierPatch(archived=True))
    f.locations.update(place, LocationPatch(archived=True))
    f.catalog.update("insert-types", insert, InsertTypePatch(archived=True))
    f.types.update(kind, SpoolTypePatch(archived=True), actor="person")
    assert _errors(pg_pool) == []
    assert _intents(pg_pool) == [("spool", spool)]


# -- errors ----------------------------------------------------------------------------


def test_a_failure_keeps_the_hashes_and_counts_attempts(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    _link(pg_pool, spool, 50, pushed={"note": field_hash("old")})
    write = _write("spool", spool, "update", 50, {"note": "new"}, {"note": field_hash("new")})
    assert _apply(f, [write], [_refused(write)]).failed == 1
    _apply(f, [write], [_refused(write)])
    assert _errors(pg_pool) == [("spool", spool, "bambuddy-refused:422", 2)]
    link = _link_row(pg_pool, spool)
    assert link is not None and link["pushed_fields"] == {"note": field_hash("old")}
    _apply(f, [write], [_ok(write, _answer(50, note="new"))])
    assert _errors(pg_pool) == []
    link = _link_row(pg_pool, spool)
    assert link is not None and link["pushed_fields"] == {"note": field_hash("new")}


# -- what is checked -------------------------------------------------------------------


def test_an_unknown_key_is_ignored(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    create = _only(_plan(f, _snap([])).writes, "create")
    stray = BbWriteResult(key=f"spool:{spool}:update", ok=True, bb_id=60, answer=_answer(60))
    assert _apply(f, [create], [stray]) == ResultsReport()
    assert _link_row(pg_pool, spool) is None
    assert _intents(pg_pool) == [("spool", spool)]


def test_a_write_whose_bb_id_disagrees_with_the_link_is_ignored(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    _link(pg_pool, spool, 50)
    write = _write("spool", spool, "update", 51, {"note": "x"}, {"note": field_hash("x")})
    assert _apply(f, [write], [_ok(write, _answer(51, note="x"))]) == ResultsReport()
    assert _apply(f, [write], [_refused(write, 404)]) == ResultsReport()
    link = _link_row(pg_pool, spool)
    assert link is not None
    assert (link["bb_spool_id"], link["pushed_fields"], link["bb_seen"]) == (50, {}, None)
    assert _errors(pg_pool) == []


def test_a_create_for_a_linked_item_is_ignored(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    _link(pg_pool, spool, 50)
    with pg_pool.connection() as conn:
        conn.execute("INSERT INTO filament_bb_creates (kind, sb_id) VALUES ('spool', %s)", (spool,))
    create = _write("spool", spool, "create", None, {"material": "PLA"})
    assert _apply(f, [create], [_ok(create, _answer(70), 70)]) == ResultsReport()
    link = _link_row(pg_pool, spool)
    assert link is not None and link["bb_spool_id"] == 50


def test_a_write_for_no_such_item_is_ignored(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    missing = UUID(int=7)
    write = _write("location", missing, "update", 4, {"name": "X"}, {"name": field_hash("X")})
    assert _apply(f, [write], [_refused(write)]) == ResultsReport()
    assert _errors(pg_pool) == []


def test_an_adoption_links_the_row_its_intent_found(pg_pool: PgPool) -> None:
    """R-T4-10: an ``update`` on an unlinked spool with an open intent links it, unless
    another SB spool holds that Bambuddy row."""
    f = _filament(pg_pool)
    kind = _type(f)
    spool, other = _spool(f, kind), _spool(f, kind)
    with pg_pool.connection() as conn:
        conn.execute("INSERT INTO filament_bb_creates (kind, sb_id) VALUES ('spool', %s)", (spool,))
    _link(pg_pool, other, 60)
    taken = _write("spool", spool, "update", 60)
    assert _apply(f, [taken], [_ok(taken, _answer(60))]) == ResultsReport()
    free = _write("spool", spool, "update", 61)
    assert _apply(f, [free], [_ok(free, _answer(61))]).linked == 1
    link = _link_row(pg_pool, spool)
    assert link is not None and link["bb_spool_id"] == 61
    assert _intents(pg_pool) == []


def test_an_update_on_an_unlinked_item_without_an_intent_is_ignored(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    write = _write("spool", spool, "update", 61)
    assert _apply(f, [write], [_ok(write, _answer(61))]) == ResultsReport()
    assert _link_row(pg_pool, spool) is None


# -- the instance (Decision 12) -----------------------------------------------------------


def test_the_first_link_records_the_instance(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    assert _instance(pg_pool) is None
    _created(pg_pool, f)
    assert _instance(pg_pool) == INSTANCE


def test_a_result_that_links_nothing_records_no_instance(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    kind = _type(f)
    spool = _spool(f, kind)
    _link(pg_pool, spool, 50)
    probe = _write("spool", spool, "probe", 50)
    _apply(f, [probe], [_ok(probe, _answer(50))])
    _spool(f, kind)
    create = _only(_plan(f, _snap([_answer(50)])).writes, "create")
    _apply(f, [create], [_refused(create)])
    assert _instance(pg_pool) is None


def test_results_for_another_instance_are_ignored(pg_pool: PgPool) -> None:
    """Decision 12: a link's Bambuddy id means nothing on another Bambuddy."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_bb_sync_state SET instance = 'ffffffffffff'")
    with pg_pool.connection() as conn:
        conn.execute("INSERT INTO filament_bb_creates (kind, sb_id) VALUES ('spool', %s)", (spool,))
    create = _write("spool", spool, "create", None, {"material": "PLA"})
    assert _apply(f, [create], [_ok(create, _answer(60), 60)]) == ResultsReport()
    assert _link_row(pg_pool, spool) is None


# -- what is stored as seen (R-T4-11, R-T4-28) --------------------------------------------


def test_an_answer_does_not_make_an_unsent_field_seen(pg_pool: PgPool) -> None:
    """R-T4-28: a person changed the note in Bambuddy, and SB's write sent only the
    colour name. The answer's note does not become seen: the old seen value stays."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    _link(pg_pool, spool, 50, seen={"note": None, "color_name": "Red"}, seen_at=datetime.now(UTC))
    write = _write(
        "spool", spool, "update", 50, {"color_name": "Brick"}, {"color_name": field_hash("Brick")}
    )
    _apply(f, [write], [_ok(write, _answer(50, color_name="Brick", note="a person's"))])
    link = _link_row(pg_pool, spool)
    assert link is not None
    assert link["bb_seen"]["note"] is None
    assert link["bb_seen"]["color_name"] == "Brick"


def test_a_list_answer_is_stored_for_its_field(pg_pool: PgPool) -> None:
    """R-T3-6: a list ``PUT`` answers its list, not the spool's row, so only that field is
    stored; the rest of the seen copy stays."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    _link(pg_pool, spool, 50, seen={"note": "kept"}, seen_at=datetime.now(UTC))
    k_rows = [{"printer_id": 2, "extruder": 0, "nozzle_diameter": "0.4", "k_value": 0.02}]
    k_write = _write("spool", spool, "k_profiles", 50, k_rows, {"k_profiles": "h"})
    links = [
        {
            "supplier_id": 3,
            "supplier_article_number": "SKU",
            "quoted_price_per_kg": 15,
            "is_purchase_source": True,
        }
    ]
    s_write = _write("spool", spool, "suppliers", 50, links, {"suppliers": "s"})
    k_answer = {"k_profiles": [{"id": 9, "spool_id": 50, **k_rows[0]}]}
    s_answer = {"suppliers": [{"id": 4, "supplier_name": "Shop", **links[0]}]}
    _apply(f, [k_write, s_write], [_ok(k_write, k_answer), _ok(s_write, s_answer)])
    link = _link_row(pg_pool, spool)
    assert link is not None
    assert link["bb_k_profiles"][0]["k_value"] == 0.02
    assert link["bb_seen"]["note"] == "kept"
    assert link["bb_seen"]["suppliers"] == [
        {
            "supplier_id": 3,
            "supplier_article_number": "SKU",
            "quoted_price_per_kg": 15.0,
            "is_purchase_source": True,
        }
    ]
    assert link["pushed_fields"] == {"k_profiles": "h", "suppliers": "s"}


def _child_under_parent(pool: PgPool, f: Filament) -> tuple[UUID, UUID]:
    """Two linked locations under ``two_way``: ``Room`` (Bambuddy row 3) and its child
    ``Room / Shelf A`` (row 4), SB identifier ``A1``, Bambuddy's ``B7``."""
    shelf = _kind(f, "Shelf")
    room = f.locations.create(LocationCreate(name="Room", kind_id=shelf)).id
    child = f.locations.create(
        LocationCreate(name="Shelf A", kind_id=shelf, parent_id=room, identifier="A1")
    ).id
    with pool.connection() as conn:
        for sb_id, bb_id in ((room, 3), (child, 4)):
            conn.execute(
                "UPDATE filament_locations SET bb_location_id = %s,"
                " updated_at = now() - interval '1 hour' WHERE id = %s",
                (bb_id, sb_id),
            )
    return room, child


def _locations(*rows: tuple[int, str, str | None]) -> list[dict[str, Any]]:
    return [{"id": i, "name": n, "identifier": ident} for i, n, ident in rows]


def test_a_parent_rename_never_pushes_the_childs_identifier(pg_pool: PgPool) -> None:
    """R-T4-28, the re-review's scenario 1: a seeded child whose identifier differs. The
    parent's rename pushes the child's new path only; its answer carries Bambuddy's
    identifier, which does not become seen, so no later pass pushes ``A1``."""
    f = _filament(pg_pool)
    room, child = _child_under_parent(pg_pool, f)
    bb = _locations((3, "Room", None), (4, "Room / Shelf A", "B7"))
    seeding = _plan(f, _snap([], locations=bb), TWO_WAY_LOCATIONS)
    assert seeding.writes == []
    f.locations.update(room, LocationPatch(name="Hall"))
    writes = _plan(f, _snap([], locations=bb), TWO_WAY_LOCATIONS).writes
    assert {w.bb_id: w.body for w in writes} == {
        3: {"name": "Hall"},
        4: {"name": "Hall / Shelf A"},
    }
    renamed = _locations((3, "Hall", None), (4, "Hall / Shelf A", "B7"))
    answers = {row["id"]: row for row in renamed}
    _apply(f, writes, [_ok(w, answers[w.bb_id or 0]) for w in writes])
    with pg_pool.connection() as conn:
        row = conn.execute(
            "SELECT bb_seen FROM filament_locations WHERE id = %s", (child,)
        ).fetchone()
    assert row is not None and "identifier" not in row["bb_seen"]
    assert _bb_pushed(pg_pool, "filament_locations", child)[UNSEEN_KEY] == {
        "identifier": {"sb": field_hash("A1"), "bb": field_hash("B7")}
    }
    for _ in range(2):
        later = _plan(f, _snap([], locations=renamed), TWO_WAY_LOCATIONS).writes
        assert [w for w in later if isinstance(w.body, dict) and "identifier" in w.body] == []


def test_an_adopted_person_made_row_keeps_its_identifier(pg_pool: PgPool) -> None:
    """R-T4-32: SB location ``A1`` adopts Bambuddy's ``B7`` (a person made it) under
    ``two_way``. `apply_results` links it with the marks kept, and the next full pass
    plans no write that carries ``identifier``."""
    f = _filament(pg_pool)
    shelf = f.locations.create(
        LocationCreate(name="Shelf A", kind_id=_kind(f, "Shelf"), identifier="A1")
    ).id
    bb = _locations((4, "Shelf A", "B7"))
    (adopt,) = _plan(f, _snap([], locations=bb), TWO_WAY_LOCATIONS).writes
    assert (adopt.op, adopt.bb_id, adopt.body) == ("update", 4, {})
    assert _apply(f, [adopt], [_ok(adopt, bb[0])]).linked == 1
    with pg_pool.connection() as conn:
        row = conn.execute(
            "SELECT bb_location_id, bb_seen FROM filament_locations WHERE id = %s", (shelf,)
        ).fetchone()
    assert row is not None
    assert (row["bb_location_id"], row["bb_seen"]) == (4, {"name": "Shelf A"})
    pushed = _bb_pushed(pg_pool, "filament_locations", shelf)
    assert pushed["name"] == field_hash("Shelf A")
    assert pushed[UNSEEN_KEY] == {"identifier": {"sb": field_hash("A1"), "bb": field_hash("B7")}}
    assert _intents(pg_pool) == []
    assert _plan(f, _snap([], locations=bb), TWO_WAY_LOCATIONS).writes == []


def test_a_404_on_a_linked_row_drops_its_link(pg_pool: PgPool) -> None:
    """R-T4-13: a 404 on any linked item drops the link and what it pushed and saw."""
    f = _filament(pg_pool)
    shelf = f.locations.create(LocationCreate(name="Shelf A", kind_id=_kind(f, "Shelf"))).id
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_locations SET bb_location_id = 4, bb_seen = %s, bb_seen_at = now(),"
            " bb_pushed = %s WHERE id = %s",
            (Jsonb({"name": "Shelf A"}), Jsonb({"name": "h"}), shelf),
        )
    write = _write("location", shelf, "update", 4, {"name": "B"}, {"name": "h2"})
    assert _apply(f, [write], [_refused(write, 404)]).unlinked == 1
    with pg_pool.connection() as conn:
        row = conn.execute(
            "SELECT bb_location_id, bb_pushed, bb_seen, bb_seen_at FROM filament_locations"
            " WHERE id = %s",
            (shelf,),
        ).fetchone()
    assert row is not None and tuple(row.values()) == (None, {}, None, None)


def test_a_filament_entrys_answer_is_stored_as_seen(pg_pool: PgPool) -> None:
    """R-T4-25: filament catalog entries keep a seen copy, as the other rows do."""
    f = _filament(pg_pool)
    kind = _type(f)
    sync = SPOOLS.model_copy(update={"spools": "two_way"})
    bb = {"id": 8, "name": "GFA00-RED", "type": "PLA", "brand": None, "color_hex": "#ff0000"}
    (create,) = [w for w in _plan(f, _snap([], filaments=[]), sync).writes if w.kind == "filament"]
    assert _apply(f, [create], [_ok(create, bb, 8)]).linked == 1
    with pg_pool.connection() as conn:
        row = conn.execute(
            "SELECT bb_filament_id, bb_seen, bb_seen_at FROM filament_spool_types WHERE id = %s",
            (kind,),
        ).fetchone()
    assert row is not None
    assert row["bb_filament_id"] == 8 and row["bb_seen_at"] is not None
    assert row["bb_seen"]["color_hex"] == "#FF0000"


def test_a_list_put_on_an_unseeded_link_leaves_it_to_seeding(pg_pool: PgPool) -> None:
    """R-T5-10: a list ``PUT`` answers one field. On a link with no full seen copy it does
    not stand for one: the next full pass still seeds the fields that agree."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    _link(pg_pool, spool, 50)
    write = _write("spool", spool, "suppliers", 50, [], {"suppliers": field_hash([])})
    _apply(f, [write], [_ok(write, {"suppliers": []})])
    link = _link_row(pg_pool, spool)
    assert link is not None and link["bb_seeded"] is False
    _plan(f, _snap([_matching(pg_pool, spool, 50)]))
    link = _link_row(pg_pool, spool)
    assert link is not None and link["bb_seeded"] is True
    assert {"material", "color_name"} <= set(link["bb_seen"])
    assert link["bb_seen"]["suppliers"] == []


def test_results_read_each_kinds_rows_once(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-T5-11: one `apply_results` call reads a kind's rows, and the spool references,
    once, not once per result."""
    f = _filament(pg_pool)
    shelf = _kind(f, "Shelf")
    places = [
        f.locations.create(LocationCreate(name=f"Shelf {n}", kind_id=shelf)).id for n in "ABC"
    ]
    calls = {"locations": 0, "refs": 0}
    kind = bb_sync_plan.ROW_KINDS["location"]

    def items(conn: Any) -> Any:
        calls["locations"] += 1
        return kind.items(conn)

    real_read = bb_sync_plan.BbRefs.read

    def read(conn: Any) -> Any:
        calls["refs"] += 1
        return real_read(conn)

    monkeypatch.setitem(
        bb_sync_plan.ROW_KINDS,
        "location",
        bb_sync_plan.RowKind(kind.table, kind.bb_column, kind.readable, items),
    )
    monkeypatch.setattr(bb_sync_plan.BbRefs, "read", staticmethod(read))
    writes, results = [], []
    for n, place in enumerate(places):
        with pg_pool.connection() as conn:
            conn.execute(
                "INSERT INTO filament_bb_creates (kind, sb_id) VALUES ('location', %s)", (place,)
            )
        write = _write("location", place, "create", None, {"name": f"Shelf {'ABC'[n]}"})
        writes.append(write)
        results.append(_ok(write, {"id": 10 + n, "name": f"Shelf {'ABC'[n]}"}, 10 + n))
    spools = [_spool(f, _type(f, code=f"GFA00-{n}")) for n in range(2)]
    for n, spool in enumerate(spools):
        _link(pg_pool, spool, 50 + n)
        write = _write("spool", spool, "update", 50 + n, {"note": None}, {"note": "h"})
        writes.append(write)
        results.append(_ok(write, _answer(50 + n)))
    report = _apply(f, writes, results)
    assert (report.linked, report.updated) == (3, 2)
    assert calls == {"locations": 1, "refs": 1}


def test_results_of_one_pass_sent_again_add_one_attempt(pg_pool: PgPool) -> None:
    """R-T6-5: the same plan round's refusal again adds no attempt, whatever the time
    between; another round's adds one at once, with no ageing; a round with no id is
    its own."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    _link(pg_pool, spool, 50, pushed={"note": field_hash("old")})
    write = _write("spool", spool, "update", 50, {"note": "new"}, {"note": field_hash("new")})
    for pass_id, attempts in (("a", 1), ("a", 1), ("b", 2), ("b", 2), (None, 3), (None, 4)):
        _apply(f, [write], [_refused(write)], pass_id=pass_id)
        assert _errors(pg_pool) == [("spool", spool, "bambuddy-refused:422", attempts)]


# -- a pull between a push's creates and their results (Decision 10, review I1) -----------

_PULL_ROWS = Pulls.of(FilamentSync(suppliers="two_way", locations="two_way"))
TWO_WAY_ROWS = TWO_WAY_LOCATIONS.model_copy(update={"suppliers": "two_way"})


def _places(pool: PgPool) -> list[tuple[str, str | None, int | None]]:
    """Each SB place: its name, its parent's name and its Bambuddy id."""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT l.name, p.name AS parent, l.bb_location_id FROM filament_locations l"
            " LEFT JOIN filament_locations p ON p.id = l.parent_id ORDER BY l.name"
        ).fetchall()
    return [(r["name"], r["parent"], r["bb_location_id"]) for r in rows]


def test_a_pull_between_creates_and_results_leaves_the_rows_to_the_push(
    pg_pool: PgPool,
) -> None:
    """Review I1: the push creates a root place, a nested one and a supplier; before its
    results come, the pull reads them in Bambuddy. The pull leaves each to its open intent,
    so the results link them: one SB row per place, no intent left open."""
    f = _filament(pg_pool)
    shelf = _kind(f, "Shelf")
    root = f.locations.create(LocationCreate(name="Shelf A", kind_id=shelf)).id
    f.locations.create(LocationCreate(name="Bin 1", kind_id=shelf, parent_id=root))
    f.suppliers.create(SupplierCreate(name="Micro Center"))
    writes = _plan(f, _snap([], locations=[], suppliers=[]), TWO_WAY_ROWS).writes
    creates = [w for w in writes if w.op == "create"]
    names = sorted(_body(w)["name"] for w in creates)
    assert names == ["Micro Center", "Shelf A", "Shelf A / Bin 1"]
    ids = {"Shelf A": 8, "Shelf A / Bin 1": 7, "Micro Center": 9}
    answers = {w.key: {"id": ids[_body(w)["name"]], **_body(w)} for w in creates}

    report = pull(
        f,
        [],
        now=datetime.now(UTC),
        pulls=_PULL_ROWS,
        locations=[BbLocation.model_validate(a) for a in answers.values() if a["id"] != 9],
        suppliers=[BbSupplier.model_validate(a) for a in answers.values() if a["id"] == 9],
    )
    assert (report.locations, report.suppliers, report.claimed) == (0, 0, 3)

    done = _apply(f, creates, [_ok(w, answers[w.key], answers[w.key]["id"]) for w in creates])
    assert done.linked == 3
    assert _places(pg_pool) == [("Bin 1", "Shelf A", 7), ("Shelf A", None, 8)]
    assert _intents(pg_pool) == []


def test_a_link_another_row_holds_is_ignored_not_raised(pg_pool: PgPool) -> None:
    """Review I1: a result whose Bambuddy row another SB row already holds is ignored,
    and the round's other results are still recorded."""
    f = _filament(pg_pool)
    shelf = _kind(f, "Shelf")
    a = f.locations.create(LocationCreate(name="Shelf A", kind_id=shelf)).id
    f.locations.create(LocationCreate(name="Shelf B", kind_id=shelf))
    creates = [
        w for w in _plan(f, _snap([], locations=[]), TWO_WAY_LOCATIONS).writes if w.op == "create"
    ]
    # Another row took Bambuddy's row 7 between the create and its results.
    f.locations.create(LocationCreate(name="Elsewhere", kind_id=shelf))
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_locations SET bb_location_id = 7 WHERE name = 'Elsewhere'")
    ids = {"Shelf A": 7, "Shelf B": 8}
    results = [
        _ok(w, {"id": ids[_body(w)["name"]], **_body(w)}, ids[_body(w)["name"]]) for w in creates
    ]
    assert _apply(f, creates, results).linked == 1
    assert ("location", a) in _intents(pg_pool)
    assert ("Shelf B", None, 8) in _places(pg_pool)


# -- the pull's links record the instance (Decision 12, review I2) -------------------------


def test_the_pulls_row_links_record_the_instance(pg_pool: PgPool) -> None:
    """Review I2: the pull links a place against Bambuddy A, then the URL points at B: the
    plan answers ``instance_changed`` and writes nothing with A's ids."""
    f = _filament(pg_pool)
    f.locations.create(LocationCreate(name="Shelf A", kind_id=_kind(f, "Shelf")))
    a = [BbLocation(id=4, name="Shelf A")]
    pull(f, [], now=datetime.now(UTC), pulls=_PULL_ROWS, locations=a, instance=INSTANCE)
    assert _instance(pg_pool) == INSTANCE

    other = _snap([], locations=[]).model_copy(update={"instance": "bbbbbbbbbbbb"})
    planned = _plan(f, other, TWO_WAY_LOCATIONS)
    assert planned.instance_changed is True
    assert planned.writes == []


def test_the_pulls_spool_links_record_the_instance(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    pull(f, [BbSpool.model_validate(_answer(50))], now=datetime.now(UTC), instance=INSTANCE)
    assert _instance(pg_pool) == INSTANCE


def test_a_pull_without_an_instance_records_none(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    pull(f, [BbSpool.model_validate(_answer(50))], now=datetime.now(UTC))
    assert _instance(pg_pool) is None


# -- the boot's adoption of links made before the state row (Decision 12, Task 8) ---------


def test_boot_records_the_instance_of_existing_links(pg_pool: PgPool) -> None:
    """Links #6261's pull made before the state row belong to the configured Bambuddy."""
    f = _filament(pg_pool)
    _link(pg_pool, _spool(f, _type(f)), 50)
    adopt_instance(f, INSTANCE)
    assert _instance(pg_pool) == INSTANCE


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("filament_suppliers", "bb_supplier_id"),
        ("filament_locations", "bb_location_id"),
        ("filament_insert_types", "bb_catalog_id"),
        ("filament_spool_types", "bb_filament_id"),
    ],
)
def test_boot_records_the_instance_of_existing_row_links(
    pg_pool: PgPool, table: str, column: str
) -> None:
    """Review I2: a row link the pull made counts as a link too."""
    f = _filament(pg_pool)
    f.suppliers.create(SupplierCreate(name="Micro Center"))
    f.locations.create(LocationCreate(name="Shelf A", kind_id=_kind(f, "Shelf")))
    f.catalog.create("insert-types", InsertTypeCreate(name="Cardboard", empty_weight_g=200))
    _type(f)
    with pg_pool.connection() as conn:
        assert conn.execute(f"SELECT 1 FROM {table}").fetchone() is not None, table
        conn.execute(f"UPDATE {table} SET {column} = 5")
    adopt_instance(f, INSTANCE)
    assert _instance(pg_pool) == INSTANCE


def test_boot_leaves_the_instance_null_without_links(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    _spool(f, _type(f))
    adopt_instance(f, INSTANCE)
    assert _instance(pg_pool) is None


def test_boot_keeps_an_instance_already_recorded(pg_pool: PgPool) -> None:
    """Another Bambuddy configured since the links were made does not take them over."""
    f = _filament(pg_pool)
    _link(pg_pool, _spool(f, _type(f)), 50)
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_bb_sync_state SET instance = 'ffffffffffff'")
    adopt_instance(f, INSTANCE)
    assert _instance(pg_pool) == "ffffffffffff"
