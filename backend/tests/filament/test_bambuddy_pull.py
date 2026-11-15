"""Bambuddy's spools pulled into ScadBuddy's own (#6261, spec §8.1, §8.2): a new BB spool
becomes an SB spool, one SB already holds is linked, and a linked one takes what BB
changed since the last pull."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
import respx

from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.filament.bambuddy_pull import pull, read_and_pull, scheduled_pull
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.component import Filament
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.models import (
    InsertTypeCreate,
    Material,
    SpoolCreate,
    SpoolPatch,
    SpoolTypeCreate,
)
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.types import SpoolTypeStore
from scadbuddy.library.settings_store import StoredSettings
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
BB = "https://bambuddy.test"
TRAY = "0123456789ABCDEF0123456789ABCDEF"


def _filament(pool: PgPool) -> Filament:
    return Filament(
        catalog=CatalogStore(pool),
        types=SpoolTypeStore(pool),
        spools=FilamentSpoolStore(pool),
        locations=LocationStore(pool),
    )


def _pla(pool: PgPool) -> UUID:
    rows = CatalogStore(pool).list("materials")
    return next(r.id for r in rows if isinstance(r, Material) and r.name == "PLA")


def _ids(f: Filament, kind: str) -> dict[str, UUID]:
    """A catalog's unarchived rows, id by name."""
    rows = [row.model_dump() for row in f.catalog.list(kind)]
    return {row["name"]: row["id"] for row in rows}


def bb(**over: Any) -> BbSpool:
    body: dict[str, Any] = {
        "id": 7,
        "material": "PLA",
        "subtype": "Basic",
        "brand": "Bambu Lab",
        "color_name": "Red",
        "rgba": "FF0000FF",
        "label_weight": 1000,
        "weight_used": 100.0,
        "tray_uuid": TRAY,
        "tag_uid": "AABBCCDD11223344",
        "updated_at": "2026-11-14T10:00:00",
    } | over
    return BbSpool.model_validate(body)


def _bb_time(hours: int) -> str:
    """A Bambuddy time (naive UTC) ``hours`` from the database's clock."""
    return (datetime.now(UTC) + timedelta(hours=hours)).replace(tzinfo=None).isoformat()


def _bb_events(f: Filament, spool: UUID) -> list[tuple[str, str | None, float | None]]:
    return [(e.kind, e.source, e.grams_after) for e in f.spools.events(spool)]


def test_a_new_bb_spool_becomes_an_sb_spool(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    kind = f.types.create(
        SpoolTypeCreate(
            code="GFA00-RED", material_id=_pla(pg_pool), rgba="#00FF00", label_weight_g=1000
        )
    )
    report = pull(f, [bb(material_number="gfa00-red")], now=NOW)
    assert (report.created, report.linked, report.updated, report.needs_type) == (1, 0, 0, 0)
    (spool,) = f.spools.list()
    assert report.created_ids == [spool.id]
    assert spool.type_id == kind.id
    assert spool.grams_left == 900
    # BB's colour differs from the type's: it is the spool's override.
    assert spool.rgba == "#FF0000FF"
    assert spool.label_weight_g is None
    binding = f.spools.open_binding(spool.id)
    assert binding is not None
    assert _bb_events(f, spool.id) == [
        ("created", "bb", 900),
        ("tag_bound", None, None),
        ("synced_from_bb", None, None),
    ]


def test_an_unmatched_bb_spool_needs_a_type(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    report = pull(
        f,
        [bb(material="PETG-HF", brand="Acme", subtype=None, rgba="112233", tray_uuid=None)],
        now=NOW,
    )
    assert (report.created, report.needs_type) == (1, 1)
    (spool,) = f.spools.list(needs_type=True)
    assert report.needs_type_ids == [spool.id]
    assert spool.rgba == "#112233FF"
    assert spool.label_weight_g == 1000
    assert spool.material_id == _ids(f, "materials")["PETG-HF"]
    assert spool.brand_id == _ids(f, "brands")["Acme"]


def test_a_bb_spool_matched_by_identity_takes_the_type(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    pull(f, [bb(id=1, tray_uuid=None, tag_uid=None)], now=NOW)
    names = _ids(f, "brands")
    subtypes = _ids(f, "subtypes")
    kind = f.types.create(
        SpoolTypeCreate(
            code="PLA-RED",
            material_id=_pla(pg_pool),
            brand_id=names["Bambu Lab"],
            subtype_id=subtypes["Basic"],
            colour_name="Red",
            rgba="#FF0000",
            label_weight_g=1000,
        )
    )
    report = pull(f, [bb(id=2, tray_uuid=None, tag_uid=None)], now=NOW)
    made = f.spools.get(report.created_ids[0])
    assert made is not None
    assert made.type_id == kind.id
    assert made.overridden == []


def test_a_bb_spool_whose_tag_sb_holds_is_linked_not_duplicated(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    held = f.spools.create(
        SpoolCreate(material_id=_pla(pg_pool), grams_left=500, note="mine"), actor="ann"
    )
    f.spools.bind_tag(held.id, tray_uuid=TRAY, kind="bambu_mifare", actor="ann")
    report = pull(f, [bb()], now=NOW)
    assert (report.created, report.linked) == (0, 1)
    assert report.linked_ids == [held.id]
    (spool,) = f.spools.list()
    # Linking keeps what SB had: its weight and its own fields.
    assert (spool.id, spool.grams_left, spool.note) == (held.id, 500, "mine")


def test_a_bb_spool_whose_record_link_names_an_sb_spool_is_linked(pg_pool: PgPool) -> None:
    """A native spool migrated onto its physical spool's id, which the BB record is
    linked to (`spool_record_links`), is that BB spool, tag or not."""
    f = _filament(pg_pool)
    with pg_pool.connection() as conn:
        physical = conn.execute(
            "INSERT INTO physical_spools DEFAULT VALUES RETURNING id"
        ).fetchone()
        assert physical is not None
        conn.execute(
            "INSERT INTO filament_spools (id, material_id) VALUES (%s, %s)",
            (physical["id"], _pla(pg_pool)),
        )
        conn.execute(
            "INSERT INTO spool_record_links (provider, record_id, physical_spool_id, linked_by)"
            " VALUES ('bambuddy', '7', %s, 'rfid')",
            (physical["id"],),
        )
    report = pull(f, [bb(tray_uuid=None, tag_uid=None)], now=NOW)
    assert (report.created, report.linked) == (0, 1)
    assert report.linked_ids == [physical["id"]]
    assert len(f.spools.list()) == 1


def test_a_second_pull_changes_nothing(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    first = pull(f, [bb(), bb(id=8, tray_uuid=None, tag_uid=None, material="ABS")], now=NOW)
    assert first.created == 2
    again = pull(f, [bb(), bb(id=8, tray_uuid=None, tag_uid=None, material="ABS")], now=NOW)
    assert (again.created, again.linked, again.updated, again.needs_type) == (0, 0, 0, 0)
    assert len(f.spools.list()) == 2


def test_a_bb_weight_change_is_a_bb_reading(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb()], now=NOW).created_ids
    report = pull(f, [bb(weight_used=300.0, updated_at="2026-11-14T11:00:00")], now=NOW)
    assert (report.updated, report.updated_ids) == (1, [made])
    spool = f.spools.get(made)
    assert spool is not None
    assert spool.grams_left == 700
    assert _bb_events(f, made)[-1] == ("synced_from_bb", "bb", 700)


def test_a_locked_spool_takes_no_bb_reading(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb()], now=NOW).created_ids
    f.spools.update(made, SpoolPatch(weight_locked=True), actor="ann")
    report = pull(f, [bb(weight_used=300.0, updated_at="2026-11-14T11:00:00")], now=NOW)
    assert report.updated == 1
    spool = f.spools.get(made)
    assert spool is not None
    assert spool.grams_left == 900
    assert _bb_events(f, made)[-1] == ("synced_from_bb", None, None)


def test_a_bb_change_wins_unless_sb_changed_the_field_after_it(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(updated_at=_bb_time(-2))], now=NOW).created_ids
    f.spools.update(made, SpoolPatch(colour_name="Mine", note="keep"), actor="ann")
    # BB's change is older than SB's edit of colour_name, newer for rgba (SB never
    # edited it); `note` is SB's only and a pull never touches it.
    changed = bb(color_name="Crimson", rgba="AA0000", updated_at=_bb_time(-1))
    report = pull(f, [changed], now=NOW)
    assert report.updated == 1
    spool = f.spools.get(made)
    assert spool is not None
    assert (spool.colour_name, spool.rgba, spool.note) == ("Mine", "#AA0000FF", "keep")
    pull(f, [bb(color_name="Scarlet", rgba="AA0000", updated_at=_bb_time(1))], now=NOW)
    spool = f.spools.get(made)
    assert spool is not None
    assert (spool.colour_name, spool.note) == ("Scarlet", "keep")


def test_a_bb_value_gone_never_clears_sb(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(cost_per_kg=20.0)], now=NOW).created_ids
    pull(f, [bb(cost_per_kg=None, updated_at="2026-11-14T11:00:00")], now=NOW)
    spool = f.spools.get(made)
    assert spool is not None
    assert spool.cost_per_kg == 20


async def test_a_scheduled_pull_with_no_bambuddy_is_skipped(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    assert await scheduled_pull(f, lambda: StoredSettings()) == "skipped"
    assert f.spools.list() == []


async def test_a_scheduled_pull_with_bambuddy_down_is_a_logged_no_op(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    with respx.mock() as router:
        router.get(f"{BB}/api/v1/inventory/spools").respond(503)
        assert await scheduled_pull(f, lambda: settings) == "failed"
    assert "could not pull Bambuddy's spools" in caplog.text
    assert f.spools.list() == []


async def test_a_pull_reads_bambuddy_and_imports(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    with respx.mock() as router:
        router.get(f"{BB}/api/v1/inventory/spools").respond(json=[bb().model_dump(mode="json")])
        report = await read_and_pull(f, settings)
        assert report.created == 1
        assert await scheduled_pull(f, lambda: settings) == "pulled"


def test_a_bb_unlock_never_clears_an_sb_lock(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(weight_locked=True, updated_at=_bb_time(-3))], now=NOW).created_ids
    f.spools.update(made, SpoolPatch(weight_locked=False), actor="ann")
    f.spools.update(made, SpoolPatch(weight_locked=True), actor="ann")
    # BB's unlock is newer than SB's lock, but the lock is SB's own.
    pull(f, [bb(weight_locked=False, updated_at=_bb_time(1))], now=NOW)
    spool = f.spools.get(made)
    assert spool is not None
    assert spool.weight_locked


def test_a_bb_lock_is_cleared_by_a_bb_unlock(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(updated_at=_bb_time(-3))], now=NOW).created_ids
    pull(f, [bb(weight_locked=True, updated_at=_bb_time(-2))], now=NOW)
    locked = f.spools.get(made)
    assert locked is not None
    assert locked.weight_locked
    pull(f, [bb(weight_locked=False, updated_at=_bb_time(-1))], now=NOW)
    unlocked = f.spools.get(made)
    assert unlocked is not None
    assert not unlocked.weight_locked


def test_a_bb_scale_reading_is_the_anchor_when_newer(pg_pool: PgPool) -> None:
    """BB's scale weight is gross; the reading is net of BB's ``core_weight``."""
    f = _filament(pg_pool)
    old = bb(
        core_weight=250,
        last_scale_weight=1200,
        last_weighed_at=_bb_time(-5),
        updated_at=_bb_time(-5),
    )
    (made,) = pull(f, [old], now=NOW).created_ids
    first = f.spools.get(made)
    assert first is not None
    # The weighing is older than the pull's own reading of what is left.
    assert first.grams_left == 900
    assert first.last_weighed_g == 950
    newer = bb(
        core_weight=250, last_scale_weight=900, last_weighed_at=_bb_time(1), updated_at=_bb_time(1)
    )
    assert pull(f, [newer], now=NOW).updated_ids == [made]
    spool = f.spools.get(made)
    assert spool is not None
    assert (spool.grams_left, spool.last_weighed_g) == (650, 650)
    count = len(f.spools.events(made))
    again = pull(f, [newer], now=NOW)
    assert (again.created, again.linked, again.updated) == (0, 0, 0)
    assert len(f.spools.events(made)) == count
    weighed = [e for e in f.spools.events(made) if e.kind == "weighed"]
    assert [(e.source, e.ref_kind, e.grams_after, e.data["bb_gross_g"]) for e in weighed] == [
        ("bb", "bb_scale", 950, 1200),
        ("bb", "bb_scale", 650, 900),
    ]


def test_a_bb_weighing_counts_on_a_locked_spool(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(updated_at=_bb_time(-3))], now=NOW).created_ids
    f.spools.update(made, SpoolPatch(weight_locked=True), actor="ann")
    weighed = bb(
        core_weight=250, last_scale_weight=700, last_weighed_at=_bb_time(1), updated_at=_bb_time(1)
    )
    pull(f, [weighed], now=NOW)
    spool = f.spools.get(made)
    assert spool is not None
    assert (spool.weight_locked, spool.grams_left) == (True, 450)


def test_a_bb_weighing_is_net_of_the_sb_insert_type(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(updated_at=_bb_time(-3))], now=NOW).created_ids
    insert = f.catalog.create(
        "insert-types", InsertTypeCreate(name="Cardboard", empty_weight_g=200)
    ).model_dump()
    f.spools.update(made, SpoolPatch(insert_type_id=insert["id"]), actor="ann")
    weighed = bb(
        core_weight=250, last_scale_weight=700, last_weighed_at=_bb_time(1), updated_at=_bb_time(1)
    )
    pull(f, [weighed], now=NOW)
    spool = f.spools.get(made)
    assert spool is not None
    assert spool.grams_left == 500


def test_a_bb_weighing_with_no_core_weight_books_no_reading(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    weighed = bb(core_weight=0, last_scale_weight=700, last_weighed_at=_bb_time(1))
    (made,) = pull(f, [weighed], now=NOW).created_ids
    spool = f.spools.get(made)
    assert spool is not None
    assert spool.grams_left == 900
    (event,) = [e for e in f.spools.events(made) if e.kind == "weighed"]
    assert (event.grams_after, event.data["bb_gross_g"], event.data["skipped"]) == (
        None,
        700,
        "no_core_weight",
    )


def test_a_bb_drying_is_a_dried_event_once(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    dried = bb(last_dried_at=_bb_time(-1))
    (made,) = pull(f, [dried], now=NOW).created_ids
    pull(f, [dried], now=NOW)
    events = [e for e in f.spools.events(made) if e.kind == "dried"]
    assert [(e.source, e.ref_kind) for e in events] == [("bb", "bb_dried")]
    spool = f.spools.get(made)
    assert spool is not None
    assert spool.last_dried_at == events[0].at
