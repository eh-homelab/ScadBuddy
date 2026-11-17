"""Bambuddy's spools pulled into ScadBuddy's own (#6261, spec §8.1, §8.2): a new BB spool
becomes an SB spool, one SB already holds is linked, and a linked one takes what BB
changed since the last pull."""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from psycopg.types.json import Jsonb

from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.bambuddy.models import SpoolCatalogEntry
from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament.bambuddy_pull import (
    pull,
    read_and_pull,
    read_assignments,
    read_catalog,
    scheduled_pull,
)
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.component import Filament
from scadbuddy.filament.inventory import FilamentInventory
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.models import (
    InsertType,
    InsertTypeCreate,
    Material,
    SpoolCreate,
    SpoolPatch,
    SpoolTypeCreate,
    SpoolTypePatch,
)
from scadbuddy.filament.presets import PoolSpoolPresets
from scadbuddy.filament.print_uses import SlotUse, record_uses, settle_uses
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.types import SpoolTypeStore
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.operations.kinds import CHECK_ON_BAMBUDDY
from tests.conftest import PgPool
from tests.support.pg_waits import wait_for_blocked

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
        inventory=FilamentInventory(pool),
        presets=PoolSpoolPresets(pool),
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
    # BB's state sits at BB's ``updated_at``, before the binding the pull made now.
    assert _bb_events(f, spool.id) == [
        ("created", "bb", 900),
        ("synced_from_bb", None, None),
        ("tag_bound", None, None),
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


def test_a_linked_spool_given_its_first_type_drops_its_migrated_presets(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    kind = f.types.create(
        SpoolTypeCreate(
            code="GFA00-RED", material_id=_pla(pg_pool), rgba="#FF0000", label_weight_g=1000
        )
    )
    held = f.spools.create(SpoolCreate(material_id=_pla(pg_pool)), actor="ann")
    f.spools.bind_tag(held.id, tray_uuid=TRAY, kind="bambu_mifare", actor="ann")
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO filament_spool_presets (spool_id, printer_model, nozzle_diameter,"
            " slicer_filament, migrated) VALUES (%s, 'H2C', '0.4', 'P1', true)",
            (held.id,),
        )
    pull(f, [bb(material_number="gfa00-red")], now=NOW)
    spool = f.spools.get(held.id)
    assert spool is not None and spool.type_id == kind.id
    assert f.spools.presets(held.id) == []
    (synced,) = [e for e in f.spools.events(held.id) if e.kind == "synced_from_bb"]
    before, after = synced.data["changes"]["presets"]
    assert ([r["slicer_filament"] for r in before], after) == (["P1"], [])


def _own_presets(pool: PgPool, spool: UUID) -> list[tuple[str, str, str | None, bool]]:
    with pool.connection() as conn:
        return [
            (r["nozzle_diameter"], r["slicer_filament"], r["slicer_filament_name"], r["migrated"])
            for r in conn.execute(
                "SELECT * FROM filament_spool_presets WHERE spool_id = %s ORDER BY nozzle_diameter",
                (spool,),
            )
        ]


def test_a_bb_spools_preset_is_the_sb_spools_where_it_has_none(pg_pool: PgPool) -> None:
    """#6262 Task 7 ruling R1: Bambuddy's ``slicer_filament`` and its name become the
    SB spool's ``migrated`` H2C rows at every nozzle size, a person's own row kept, and
    a later pull follows Bambuddy's change on the migrated rows only."""
    f = _filament(pg_pool)
    report = pull(f, [bb(slicer_filament="GFA00", slicer_filament_name="Bambu PLA Basic")], now=NOW)
    (spool,) = f.spools.list()
    assert spool.type_id is None and report.needs_type == 1
    assert _own_presets(pg_pool, spool.id) == [
        (size, "GFA00", "Bambu PLA Basic", True) for size in ("0.2", "0.4", "0.6", "0.8")
    ]
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_spool_presets SET slicer_filament = 'MINE',"
            " slicer_filament_name = NULL, migrated = false"
            " WHERE spool_id = %s AND nozzle_diameter = '0.4'",
            (spool.id,),
        )
    pull(
        f,
        [
            bb(
                slicer_filament="GFA05",
                slicer_filament_name="Bambu PLA Silk",
                updated_at="2026-11-14T11:00:00",
            )
        ],
        now=NOW,
    )
    assert _own_presets(pg_pool, spool.id) == [
        ("0.2", "GFA05", "Bambu PLA Silk", True),
        ("0.4", "MINE", None, False),
        ("0.6", "GFA05", "Bambu PLA Silk", True),
        ("0.8", "GFA05", "Bambu PLA Silk", True),
    ]


def _set_row(pool: PgPool, spool: UUID, size: str, value: str, *, migrated: bool) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE filament_spool_presets SET slicer_filament = %s,"
            " slicer_filament_name = %s, migrated = %s"
            " WHERE spool_id = %s AND nozzle_diameter = %s",
            (value, f"{value} name", migrated, spool, size),
        )


def _preset_events(f: Filament, spool: UUID) -> list[Any]:
    return [e for e in f.spools.events(spool) if "presets" in (e.data or {}).get("changes", {})]


def test_a_migrated_row_bambuddy_did_not_write_survives_a_pull(pg_pool: PgPool) -> None:
    """#6262 Task 7 re-review I-1: a ``migrated`` row the native API or the native
    migration wrote holds a person's choice; a pull with another Bambuddy value leaves
    it, and rewrites only the rows that still hold Bambuddy's last value."""
    f = _filament(pg_pool)
    pull(f, [bb(slicer_filament="GFA00", slicer_filament_name="Bambu PLA Basic")], now=NOW)
    (spool,) = f.spools.list()
    _set_row(pg_pool, spool.id, "0.4", "NATIVE", migrated=True)

    pull(
        f,
        [
            bb(
                slicer_filament="GFA05",
                slicer_filament_name="Bambu PLA Silk",
                updated_at=_bb_time(1),
            )
        ],
        now=NOW,
    )

    assert _own_presets(pg_pool, spool.id) == [
        ("0.2", "GFA05", "Bambu PLA Silk", True),
        ("0.4", "NATIVE", "NATIVE name", True),
        ("0.6", "GFA05", "Bambu PLA Silk", True),
        ("0.8", "GFA05", "Bambu PLA Silk", True),
    ]


def test_a_bambuddy_clear_deletes_only_the_rows_holding_its_last_value(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    pull(f, [bb(slicer_filament="GFA00", slicer_filament_name="Bambu PLA Basic")], now=NOW)
    (spool,) = f.spools.list()
    _set_row(pg_pool, spool.id, "0.4", "NATIVE", migrated=True)
    _set_row(pg_pool, spool.id, "0.6", "MINE", migrated=False)

    pull(f, [bb(slicer_filament=None, updated_at=_bb_time(1))], now=NOW)

    assert _own_presets(pg_pool, spool.id) == [
        ("0.4", "NATIVE", "NATIVE name", True),
        ("0.6", "MINE", "MINE name", False),
    ]


def test_an_unchanged_pull_writes_no_preset_and_no_event(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    sent = bb(slicer_filament="GFA00", slicer_filament_name="Bambu PLA Basic")
    pull(f, [sent], now=NOW)
    (spool,) = f.spools.list()

    def written() -> list[tuple[str, str]]:
        with pg_pool.connection() as conn:
            return [
                (r["nozzle_diameter"], r["xmin"])
                for r in conn.execute(
                    "SELECT nozzle_diameter, xmin::text AS xmin FROM filament_spool_presets"
                    " WHERE spool_id = %s ORDER BY nozzle_diameter",
                    (spool.id,),
                )
            ]

    rows, events = written(), len(f.spools.events(spool.id))
    report = pull(f, [sent], now=NOW)

    assert written() == rows
    assert len(f.spools.events(spool.id)) == events
    assert report.updated == 0


def test_a_preset_change_is_an_update_with_one_event(pg_pool: PgPool) -> None:
    """A pull whose only change is Bambuddy's preset counts as an update and records
    one event with the spool's own rows before and after (``changes.presets``)."""
    f = _filament(pg_pool)
    pull(f, [bb(slicer_filament="GFA00", slicer_filament_name="Bambu PLA Basic")], now=NOW)
    (spool,) = f.spools.list()
    first = _preset_events(f, spool.id)

    report = pull(
        f,
        [
            bb(
                slicer_filament="GFA05",
                slicer_filament_name="Bambu PLA Silk",
                updated_at=_bb_time(1),
            )
        ],
        now=NOW,
    )

    assert report.updated_ids == [spool.id]
    [event] = _preset_events(f, spool.id)[len(first) :]
    before, after = event.data["changes"]["presets"]
    assert {row["slicer_filament"] for row in before} == {"GFA00"}
    assert {row["slicer_filament"] for row in after} == {"GFA05"}


def test_the_preset_sync_waits_for_a_persons_edit_and_keeps_it(pg_pool: PgPool) -> None:
    """#6262 Task 7 re-review 2 I-A: the pull locks the spool before it reads its rows,
    so a person's edit in flight (`set_presets` holds the spool's lock) is waited for,
    then seen and kept. Bambuddy's ``updated_at`` is unchanged, the common pull, where
    nothing else takes the spool's lock."""
    f = _filament(pg_pool)
    pull(f, [bb(slicer_filament="GFA00", slicer_filament_name="Bambu PLA Basic")], now=NOW)
    (spool,) = f.spools.list()
    workers = ThreadPoolExecutor(1)
    try:
        with pg_pool.connection() as conn, conn.transaction():
            conn.execute("SELECT 1 FROM filament_spools WHERE id = %s FOR UPDATE", (spool.id,))
            conn.execute(
                "DELETE FROM filament_spool_presets WHERE spool_id = %s"
                " AND nozzle_diameter = '0.4'",
                (spool.id,),
            )
            conn.execute(
                "INSERT INTO filament_spool_presets (spool_id, printer_model, nozzle_diameter,"
                " slicer_filament, slicer_filament_name, migrated)"
                " VALUES (%s, 'H2C', '0.4', 'MINE', 'MINE name', false)",
                (spool.id,),
            )
            pulled = workers.submit(
                pull,
                f,
                [bb(slicer_filament="GFA05", slicer_filament_name="Bambu PLA Silk")],
                now=NOW,
            )
            wait_for_blocked(pg_pool)  # the pull is waiting on the spool's lock
            assert not pulled.done()
        pulled.result(timeout=60)
    finally:
        workers.shutdown(wait=True)

    assert _own_presets(pg_pool, spool.id) == [
        ("0.2", "GFA05", "Bambu PLA Silk", True),
        ("0.4", "MINE", "MINE name", False),
        ("0.6", "GFA05", "Bambu PLA Silk", True),
        ("0.8", "GFA05", "Bambu PLA Silk", True),
    ]


def test_a_preset_a_person_deletes_stays_deleted(pg_pool: PgPool) -> None:
    """Re-review 2 I-B: a key with no row is filled only when Bambuddy's value changed
    since it last sent one, so a person's deletion survives an unchanged pull (no
    event, not an update); Bambuddy's next change fills it again."""
    f = _filament(pg_pool)
    pull(f, [bb(slicer_filament="GFA00", slicer_filament_name="Bambu PLA Basic")], now=NOW)
    (spool,) = f.spools.list()
    with pg_pool.connection() as conn:
        conn.execute(
            "DELETE FROM filament_spool_presets WHERE spool_id = %s AND nozzle_diameter = '0.4'",
            (spool.id,),
        )
    events = len(_preset_events(f, spool.id))

    report = pull(
        f,
        [
            bb(
                slicer_filament="GFA00",
                slicer_filament_name="Bambu PLA Basic",
                updated_at=_bb_time(1),
            )
        ],
        now=NOW,
    )

    assert [size for size, *_ in _own_presets(pg_pool, spool.id)] == ["0.2", "0.6", "0.8"]
    assert len(_preset_events(f, spool.id)) == events
    assert report.updated == 0

    pull(
        f,
        [
            bb(
                slicer_filament="GFA05",
                slicer_filament_name="Bambu PLA Silk",
                updated_at=_bb_time(2),
            )
        ],
        now=NOW,
    )
    assert _own_presets(pg_pool, spool.id) == [
        (size, "GFA05", "Bambu PLA Silk", True) for size in ("0.2", "0.4", "0.6", "0.8")
    ]


def test_a_link_with_no_last_value_fills_only_its_empty_keys(pg_pool: PgPool) -> None:
    """Re-review 2 M-1: a link from before the last value was kept (NULL), on a spool
    with ``migrated`` rows from the native migration or the native API: its empty keys
    are filled once, and no row is rewritten."""
    f = _filament(pg_pool)
    pull(f, [bb()], now=NOW)
    (spool,) = f.spools.list()
    with pg_pool.connection() as conn:
        for size in ("0.4", "0.6"):
            conn.execute(
                "INSERT INTO filament_spool_presets (spool_id, printer_model, nozzle_diameter,"
                " slicer_filament, slicer_filament_name, migrated)"
                " VALUES (%s, 'H2C', %s, 'NATIVE', 'NATIVE name', true)",
                (spool.id, size),
            )

    pull(
        f,
        [
            bb(
                slicer_filament="GFA00",
                slicer_filament_name="Bambu PLA Basic",
                updated_at=_bb_time(1),
            )
        ],
        now=NOW,
    )

    assert _own_presets(pg_pool, spool.id) == [
        ("0.2", "GFA00", "Bambu PLA Basic", True),
        ("0.4", "NATIVE", "NATIVE name", True),
        ("0.6", "NATIVE", "NATIVE name", True),
        ("0.8", "GFA00", "Bambu PLA Basic", True),
    ]


def test_a_typed_spool_takes_no_preset_from_bambuddy(pg_pool: PgPool) -> None:
    """R1 with F27: a spool's type gives its presets; a pull adds no migrated row over
    it (one the type's first assignment would have dropped)."""
    f = _filament(pg_pool)
    f.types.create(
        SpoolTypeCreate(
            code="GFA00-RED", material_id=_pla(pg_pool), rgba="#FF0000", label_weight_g=1000
        )
    )
    pull(f, [bb(material_number="gfa00-red", slicer_filament="GFA00")], now=NOW)
    (spool,) = f.spools.list()
    assert spool.type_id is not None
    assert _own_presets(pg_pool, spool.id) == []


def test_a_retired_record_link_is_not_read(pg_pool: PgPool) -> None:
    """#6262 Task 12 (F18): the overlay's record links are no longer read. A migration
    carried each Bambuddy one onto `filament_bb_links`
    (`20261116T0034Z_filament_plan_spool_numbers.sql`), so a record link alone, with no
    tag, names no spool: the BB spool is a new one."""
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
    assert (report.created, report.linked) == (1, 0)
    assert report.created_ids != [physical["id"]]
    assert len(f.spools.list()) == 2


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
    report = pull(f, [bb(weight_used=300.0, updated_at=_bb_time(1))], now=NOW)
    assert (report.updated, report.updated_ids) == (1, [made])
    spool = f.spools.get(made)
    assert spool is not None
    assert spool.grams_left == 700
    assert _bb_events(f, made)[-1] == ("synced_from_bb", "bb", 700)


def test_a_locked_spool_takes_no_bb_reading(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb()], now=NOW).created_ids
    f.spools.update(made, SpoolPatch(weight_locked=True), actor="ann")
    report = pull(f, [bb(weight_used=300.0, updated_at=_bb_time(1))], now=NOW)
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
    assert await scheduled_pull(f, lambda: StoredSettings(), FilamentSync) == "skipped"
    assert f.spools.list() == []


async def test_off_skips_the_pull(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    with respx.mock(assert_all_called=False) as router:
        spools = router.get(f"{BB}/api/v1/inventory/spools").respond(
            json=[bb().model_dump(mode="json")]
        )
        assert (
            await scheduled_pull(f, lambda: settings, lambda: FilamentSync(spools="off"))
            == "skipped"
        )
        assert not spools.called
    assert f.spools.list() == []


async def test_a_scheduled_pull_with_bambuddy_down_is_a_logged_no_op(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    with respx.mock() as router:
        router.get(f"{BB}/api/v1/inventory/spools").respond(503)
        assert await scheduled_pull(f, lambda: settings, FilamentSync) == "failed"
    assert "could not pull Bambuddy's spools" in caplog.text
    assert f.spools.list() == []


async def test_a_pull_reads_bambuddy_and_imports(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    with respx.mock() as router:
        router.get(f"{BB}/api/v1/inventory/spools").respond(json=[bb().model_dump(mode="json")])
        router.get(f"{BB}/api/v1/inventory/catalog").respond(json=[])
        report = await read_and_pull(f, settings)
        assert report.created == 1
        assert await scheduled_pull(f, lambda: settings, FilamentSync) == "pulled"


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


def test_an_unchanged_bb_row_pulled_twice_books_one_weighing_and_one_drying(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    row = bb(
        core_weight=250,
        last_scale_weight=1200,
        last_weighed_at=_bb_time(-5),
        last_dried_at=_bb_time(-4),
        updated_at=_bb_time(-3),
    )
    (made,) = pull(f, [row], now=NOW).created_ids
    left = f.spools.get(made)
    assert left is not None
    pull(f, [row], now=NOW)
    events = f.spools.events(made)
    assert [e.kind for e in events].count("weighed") == 1
    assert [e.kind for e in events].count("dried") == 1
    again = f.spools.get(made)
    assert again is not None and again.grams_left == left.grams_left


def test_a_re_used_tag_of_an_archived_spool_makes_a_new_spool(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    old = f.spools.create(SpoolCreate(material_id=_pla(pg_pool)), actor="ann")
    f.spools.bind_tag(old.id, tray_uuid=TRAY, kind="bambu_mifare", actor="ann")
    f.spools.archive(old.id, True, actor="ann")
    report = pull(f, [bb()], now=NOW)
    assert (report.created, report.linked) == (1, 0)
    (made,) = report.created_ids
    binding = f.spools.open_binding(made)
    assert binding is not None


def test_an_archived_holder_with_an_open_binding_gives_up_its_tag(pg_pool: PgPool) -> None:
    """A binding left open on an archived spool (written before archives closed them)."""
    f = _filament(pg_pool)
    old = f.spools.create(SpoolCreate(material_id=_pla(pg_pool)), actor="ann")
    f.spools.bind_tag(old.id, tray_uuid=TRAY, kind="bambu_mifare", actor="ann")
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_spools SET archived_at = now() WHERE id = %s", (old.id,))
    (made,) = pull(f, [bb()], now=NOW).created_ids
    assert f.spools.open_binding(made) is not None
    assert f.spools.open_binding(old.id) is None


def test_a_linked_spool_archived_in_sb_takes_nothing(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(updated_at=_bb_time(-2))], now=NOW).created_ids
    f.spools.archive(made, True, actor="ann")
    count = len(f.spools.events(made))
    report = pull(f, [bb(weight_used=500.0, updated_at=_bb_time(-1))], now=NOW)
    assert (report.updated, report.archived_ids) == (0, [made])
    assert len(f.spools.events(made)) == count


def test_two_bb_spools_with_one_tag_give_the_second_none(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    f = _filament(pg_pool)
    report = pull(f, [bb(id=1), bb(id=2)], now=NOW)
    first, second = report.created_ids
    assert f.spools.open_binding(first) is not None
    assert f.spools.open_binding(second) is None
    assert "tag is on another spool; not moved" in caplog.text


def test_a_failing_spool_is_listed_and_the_rest_go_on(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    f = _filament(pg_pool)
    backwards = bb(id=3, tray_uuid=None, tag_uid=None, nozzle_temp_min=250, nozzle_temp_max=200)
    report = pull(f, [backwards, bb(id=4, tray_uuid=None, tag_uid=None)], now=NOW)
    assert (report.failed_bb_ids, report.created) == ([3], 1)
    assert len(f.spools.list()) == 1
    assert "TypeTempError" in caplog.text


def test_keys_bound_to_two_spools_keep_the_oldest_binding(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    f = _filament(pg_pool)
    older = f.spools.create(SpoolCreate(material_id=_pla(pg_pool)), actor="ann")
    newer = f.spools.create(SpoolCreate(material_id=_pla(pg_pool)), actor="ann")
    f.spools.bind_tag(older.id, tag_uid="AABBCCDD11223344", kind="bambu_mifare", actor="ann")
    f.spools.bind_tag(newer.id, tray_uuid=TRAY, kind="bambu_mifare", actor="ann")
    report = pull(f, [bb()], now=NOW)
    assert report.linked_ids == [older.id]
    assert "bound to two spools" in caplog.text
    assert f.spools.open_binding(older.id) is not None
    assert f.spools.open_binding(newer.id) is not None


def test_the_pull_names_its_actor(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (scheduled,) = pull(f, [bb(id=1, tray_uuid=None, tag_uid=None)], now=NOW).created_ids
    (person,) = pull(f, [bb(id=2, tray_uuid=None, tag_uid=None)], now=NOW, actor="ann").created_ids
    assert {e.actor for e in f.spools.events(scheduled)} == {"bambuddy"}
    assert {e.actor for e in f.spools.events(person)} == {"ann"}


async def test_a_malformed_bb_row_is_skipped_and_counted(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    rows = [bb().model_dump(mode="json"), {"id": 99, "material": None}, "nonsense"]
    with respx.mock() as router:
        router.get(f"{BB}/api/v1/inventory/spools").respond(json=rows)
        router.get(f"{BB}/api/v1/inventory/catalog").respond(json=[])
        report = await read_and_pull(f, settings)
    assert (report.created, report.invalid, report.invalid_bb_ids) == (1, 2, [99])
    assert "is not a spool; skipped" in caplog.text


# -- a print SB and BB both book is counted once (§8.2) ------------------------------

RUN = "run-6261"


def _lan_run(pool: PgPool, *names: str) -> None:
    starts = [{"subtask_name": n, "started_at": "2026-11-14T12:00:00Z"} for n in names]
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO print_runs (id, output_id, idempotency_key, status, result)"
            " VALUES (%s, 'out', 'k', 'succeeded', %s)",
            (RUN, Jsonb({"lan": starts})),
        )


def _use(spool_id: UUID, grams: float) -> list[SlotUse]:
    """Slot 1's use of ``spool_id``, its plate started now."""
    return [SlotUse(slot_id=1, spool_id=spool_id, grams=grams, plate_started_at=datetime.now(UTC))]


def _now_in_bb() -> str:
    """Bambuddy's time for now (naive UTC), after every event booked so far."""
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def test_a_pull_between_two_plates_counts_each_plate_once(pg_pool: PgPool) -> None:
    """BB deducts plate ``a`` as it ends; a pull then anchors on a value that holds it,
    and the run's settle at its end takes off plate ``b`` only."""
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(updated_at=_bb_time(-3))], now=NOW).created_ids
    _lan_run(pg_pool, "a", "b")
    record_uses(pg_pool, RUN, "a", _use(made, 50))
    # Plate a ends, BB takes its 50 g off, and the pull reads that before plate b.
    pull(f, [bb(weight_used=150.0, updated_at=_now_in_bb())], now=NOW)
    record_uses(pg_pool, RUN, "b", _use(made, 30))
    settle_uses(pg_pool, RUN, ["done", "done"])
    spool = f.spools.get(made)
    assert spool is not None
    assert spool.grams_left == 1000 - 150 - 30


def test_a_bb_update_before_the_settle_counts_the_plate_once(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(updated_at=_bb_time(-3))], now=NOW).created_ids
    _lan_run(pg_pool, "a")
    record_uses(pg_pool, RUN, "a", _use(made, 50))
    pull(f, [bb(weight_used=150.0, updated_at=_now_in_bb())], now=NOW)
    settle_uses(pg_pool, RUN, ["done"])
    spool = f.spools.get(made)
    assert spool is not None
    assert spool.grams_left == 850
    # BB's next write, after a later plate, still anchors.
    pull(f, [bb(weight_used=200.0, updated_at=_now_in_bb())], now=NOW)
    assert f.spools.get(made).grams_left == 800  # type: ignore[union-attr]


def test_a_persons_row_matching_bambuddys_value_is_kept(pg_pool: PgPool) -> None:
    """Task 7 re-review 3: a person's row (not migrated) is never Bambuddy's, even when
    it holds Bambuddy's last value, so Bambuddy's next change leaves it alone."""
    f = _filament(pg_pool)
    pull(f, [bb(slicer_filament="GFA00", slicer_filament_name="Bambu PLA Basic")], now=NOW)
    (spool,) = f.spools.list()
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_spool_presets SET migrated = false"
            " WHERE spool_id = %s AND nozzle_diameter = '0.4'",
            (spool.id,),
        )

    pull(
        f,
        [
            bb(
                slicer_filament="GFA05",
                slicer_filament_name="Bambu PLA Silk",
                updated_at=_bb_time(1),
            )
        ],
        now=NOW,
    )

    assert _own_presets(pg_pool, spool.id) == [
        ("0.2", "GFA05", "Bambu PLA Silk", True),
        ("0.4", "GFA00", "Bambu PLA Basic", False),
        ("0.6", "GFA05", "Bambu PLA Silk", True),
        ("0.8", "GFA05", "Bambu PLA Silk", True),
    ]


@respx.mock
async def test_the_assignment_read_names_bambuddy_only_while_it_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 9 fix round 3: `read_assignments` is a `waiting_on_bambuddy` wait. Its own
    `ASSIGNMENTS_TIMEOUT` ends the wait (no assignments, the flag cleared), so a check
    that goes on is not blamed on Bambuddy later; a cancel inside it (the check's budget
    running out) leaves the flag set, so that timeout names Bambuddy."""

    monkeypatch.setattr("scadbuddy.filament.bambuddy_pull.ASSIGNMENTS_TIMEOUT", 0.5)

    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, json=[])

    respx.get("https://bambuddy.test/api/v1/inventory/assignments").mock(side_effect=hang)
    settings = StoredSettings(bambuddy_url="https://bambuddy.test", bambuddy_api_key="bb_test")
    waiting = [False]
    token = CHECK_ON_BAMBUDDY.set(waiting)
    try:
        started = time.monotonic()
        assert await read_assignments(settings) == []
        assert time.monotonic() - started < 10  # far under the 30 s hang
        assert waiting == [False]

        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2):
                await read_assignments(settings)
        assert waiting == [True]
    finally:
        CHECK_ON_BAMBUDDY.reset(token)


def _entry(entry_id: int = 5, name: str = "Bambu reusable", weight: int = 250) -> SpoolCatalogEntry:
    return SpoolCatalogEntry(id=entry_id, name=name, weight=weight)


def _insert_types(f: Filament) -> list[InsertType]:
    return [r for r in f.catalog.list("insert-types") if isinstance(r, InsertType)]


def test_a_bb_catalog_entry_becomes_an_insert_type(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    pull(f, [], now=NOW, catalog=[_entry()])
    (kind,) = _insert_types(f)
    assert (kind.name, kind.empty_weight_g, kind.bb_catalog_id) == ("Bambu reusable", 250, 5)
    assert kind.insert_material_id is None


def test_a_second_pull_reuses_it(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    pull(f, [], now=NOW, catalog=[_entry()])
    pull(f, [], now=NOW, catalog=[_entry(name="Bambu reusable v2", weight=260)])
    (kind,) = _insert_types(f)
    assert kind.bb_catalog_id == 5
    # The pull creates and links; it never rewrites a type a person may have edited.
    assert (kind.name, kind.empty_weight_g) == ("Bambu reusable", 250)


def test_an_unlinked_insert_type_of_the_same_name_is_linked(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    own = f.catalog.create(
        "insert-types", InsertTypeCreate(name="bambu REUSABLE", empty_weight_g=240)
    ).model_dump()
    pull(f, [], now=NOW, catalog=[_entry()])
    (kind,) = _insert_types(f)
    assert (kind.id, kind.bb_catalog_id, kind.empty_weight_g) == (own["id"], 5, 240)


def test_a_spool_takes_its_core_weight_insert_type(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    typed = f.types.create(
        SpoolTypeCreate(
            code="GFA00-RED", material_id=_pla(pg_pool), rgba="#FF0000FF", label_weight_g=1000
        )
    )
    catalog = [_entry(5, "Bambu reusable", 250), _entry(6, "Bambu cardboard", 212)]
    none: dict[str, Any] = {"tag_uid": None, "tray_uuid": None}
    report = pull(
        f,
        [
            bb(id=1, core_weight_catalog_id=5, **none),
            bb(id=2, material_number="GFA00-RED", core_weight_catalog_id=6, **none),
        ],
        now=NOW,
        catalog=catalog,
    )
    inserts = {k.bb_catalog_id: k.id for k in _insert_types(f)}
    by_id = {s.id: s for s in f.spools.list()}
    typeless, with_type = (by_id[i] for i in report.created_ids)
    assert typeless.type_id is None and typeless.insert_type_id == inserts[5]
    assert with_type.type_id == typed.id and with_type.insert_type_id == inserts[6]
    # The type's own insert type is no override.
    f.types.update(typed.id, SpoolTypePatch(insert_type_id=inserts[5]))
    same = pull(
        f,
        [bb(id=3, material_number="GFA00-RED", core_weight_catalog_id=5, **none)],
        now=NOW,
        catalog=catalog,
    )
    spool = f.spools.get(same.created_ids[0])
    assert spool is not None
    assert (spool.type_id, spool.insert_type_id) == (typed.id, None)


@respx.mock
async def test_a_failed_catalog_read_pulls_the_spools_anyway(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    f = _filament(pg_pool)
    pull(f, [], now=NOW, catalog=[_entry()])
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    respx.get(f"{BB}/api/v1/inventory/spools").respond(
        json=[bb(core_weight_catalog_id=5).model_dump(mode="json")]
    )
    route = respx.get(f"{BB}/api/v1/inventory/catalog")
    route.respond(503)
    report = await read_and_pull(f, settings)
    assert (report.catalog_failed, report.created) == (True, 1)
    assert "no spool catalog from Bambuddy" in caplog.text
    # An answer of another shape is a failed read too, and a good read says so.
    route.respond(200, json={"not": "a list"})
    assert (await read_and_pull(f, settings)).catalog_failed is True
    route.respond(200, json=[{"id": 5, "name": "Bambu reusable", "weight": 250}])
    assert (await read_and_pull(f, settings)).catalog_failed is False
    (kind,) = _insert_types(f)
    assert kind.bb_catalog_id == 5


# -- fix round 1: linked spools, a fractional weight, skipped entries, read_catalog -----

_NO_TAGS: dict[str, Any] = {"tag_uid": None, "tray_uuid": None}


def _linked_untyped(f: Filament, **over: Any) -> UUID:
    """A BB spool pulled before any catalog was known: linked, with no insert type."""
    (made,) = pull(
        f, [bb(id=1, updated_at="2026-11-14T10:00:00", **_NO_TAGS | over)], now=NOW
    ).created_ids
    return made


def _own_insert(f: Filament, spool: UUID) -> UUID | None:
    got = f.spools.get(spool)
    assert got is not None
    return got.insert_type_id


def test_a_linked_spool_takes_bbs_insert_type_on_the_next_pull(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    made = _linked_untyped(f, core_weight_catalog_id=5)
    assert _own_insert(f, made) is None
    # The spool was read with entry 5 while no catalog was mapped; BB's row did not move (same
    # updated_at). Mapped now, the entry reaches the spool.
    report = pull(
        f,
        [bb(id=1, updated_at="2026-11-14T10:00:00", core_weight_catalog_id=5, **_NO_TAGS)],
        now=NOW,
        catalog=[_entry()],
    )
    (kind,) = _insert_types(f)
    assert _own_insert(f, made) == kind.id
    assert report.updated_ids == [made]
    # Nothing moves again.
    again = pull(
        f,
        [bb(id=1, updated_at="2026-11-14T10:00:00", core_weight_catalog_id=5, **_NO_TAGS)],
        now=NOW,
        catalog=[_entry()],
    )
    assert again.updated_ids == []


def test_a_bb_catalog_change_reaches_a_linked_spool(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    catalog = [_entry(5, "Bambu reusable", 250), _entry(6, "Bambu cardboard", 212)]
    made = _linked_untyped(f, core_weight_catalog_id=5)
    pull(
        f,
        [bb(id=1, updated_at="2026-11-14T10:00:00", core_weight_catalog_id=5, **_NO_TAGS)],
        now=NOW,
        catalog=catalog,
    )
    inserts = {k.bb_catalog_id: k.id for k in _insert_types(f)}
    assert _own_insert(f, made) == inserts[5]
    pull(
        f,
        [bb(id=1, updated_at="2026-11-14T11:00:00", core_weight_catalog_id=6, **_NO_TAGS)],
        now=NOW,
        catalog=catalog,
    )
    assert _own_insert(f, made) == inserts[6]


def test_a_persons_insert_type_edit_is_kept(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    catalog = [_entry(5, "Bambu reusable", 250), _entry(6, "Bambu cardboard", 212)]
    made = _linked_untyped(f, core_weight_catalog_id=5)
    pull(
        f,
        [bb(id=1, updated_at="2026-11-14T10:00:00", core_weight_catalog_id=5, **_NO_TAGS)],
        now=NOW,
        catalog=catalog,
    )
    own = f.catalog.create(
        "insert-types", InsertTypeCreate(name="Mine", empty_weight_g=100)
    ).model_dump()
    # The person's edit is after BB's change.
    f.spools.update(made, SpoolPatch(insert_type_id=own["id"]), actor="ann")
    pull(
        f,
        [bb(id=1, updated_at="2026-11-14T10:30:00", core_weight_catalog_id=6, **_NO_TAGS)],
        now=NOW,
        catalog=catalog,
    )
    assert _own_insert(f, made) == own["id"]


def test_a_persons_insert_type_survives_a_later_catalog_mapping(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    made = _linked_untyped(f, core_weight_catalog_id=5)
    own = f.catalog.create(
        "insert-types", InsertTypeCreate(name="Mine", empty_weight_g=100)
    ).model_dump()
    f.spools.update(made, SpoolPatch(insert_type_id=own["id"]), actor="ann")
    pull(
        f,
        [bb(id=1, updated_at="2026-11-14T10:00:00", core_weight_catalog_id=5, **_NO_TAGS)],
        now=NOW,
        catalog=[_entry()],
    )
    assert _own_insert(f, made) == own["id"]


def test_a_spool_linked_by_tag_takes_bbs_insert_type(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    mine = f.spools.create(SpoolCreate(material_id=_pla(pg_pool)), actor="ann")
    f.spools.bind_tag(mine.id, tray_uuid=TRAY, kind="bambu_mifare", actor="ann")
    report = pull(f, [bb(core_weight_catalog_id=5)], now=NOW, catalog=[_entry()])
    assert report.linked_ids == [mine.id]
    (kind,) = _insert_types(f)
    assert _own_insert(f, mine.id) == kind.id


def test_a_fractional_catalog_weight_is_rounded_not_refused(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    entry = SpoolCatalogEntry.model_validate({"id": 5, "name": "Half", "weight": 212.5})
    pull(f, [], now=NOW, catalog=[entry])
    (kind,) = _insert_types(f)
    assert kind.empty_weight_g == 213


def test_a_name_clash_and_a_weightless_entry_are_skipped_and_counted(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    catalog = [_entry(5, "Bambu reusable", 250), _entry(6, "BAMBU reusable", 212)]
    report = pull(f, [], now=NOW, catalog=[*catalog, _entry(7, "Nothing", 0)])
    assert report.catalog_skipped == 2
    (kind,) = _insert_types(f)
    assert kind.bb_catalog_id == 5
    # A spool naming the skipped entry gets no insert type.
    (made,) = pull(
        f, [bb(id=2, core_weight_catalog_id=6, **_NO_TAGS)], now=NOW, catalog=catalog
    ).created_ids
    assert _own_insert(f, made) is None


def test_an_archived_insert_type_is_no_spools_insert_type(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    pull(f, [], now=NOW, catalog=[_entry()])
    (kind,) = _insert_types(f)
    f.catalog.archive("insert-types", kind.id, True)
    (made,) = pull(
        f, [bb(id=3, core_weight_catalog_id=5, **_NO_TAGS)], now=NOW, catalog=[_entry()]
    ).created_ids
    assert _own_insert(f, made) is None
    # And the entry is not made again beside the archived one.
    assert f.catalog.list("insert-types") == []


def test_an_empty_catalog_maps_nothing_and_skips_nothing(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    assert pull(f, [], now=NOW, catalog=[]).catalog_skipped == 0
    assert _insert_types(f) == []


@pytest.mark.parametrize(
    ("answer", "text"),
    [
        (httpx.Response(200, text="not json"), "another shape"),
        (httpx.Response(200, json=[{"id": "x"}]), "another shape"),
    ],
)
@respx.mock
async def test_a_garbled_catalog_read_is_none_and_logged(
    answer: httpx.Response, text: str, caplog: pytest.LogCaptureFixture
) -> None:
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    respx.get(f"{BB}/api/v1/inventory/catalog").mock(return_value=answer)
    assert await read_catalog(settings) is None
    assert text in caplog.text


@respx.mock
async def test_a_slow_catalog_read_is_none_within_its_timeout(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, json=[])

    monkeypatch.setattr("scadbuddy.filament.bambuddy_pull.CATALOG_TIMEOUT", 0.2)
    respx.get(f"{BB}/api/v1/inventory/catalog").mock(side_effect=hang)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    assert await read_catalog(settings) is None
    assert "no answer within" in caplog.text


# -- fix round 2 -----------------------------------------------------------------------

_SAME = "2026-11-14T10:00:00"


def _strip_key(f: Filament, spool: UUID) -> None:
    """Make the spool's readings those of a pull before the catalog id was kept."""
    with f.spools.transaction() as conn:
        conn.execute(
            "UPDATE filament_spool_events SET data = data #- '{bb,core_weight_catalog_id}'"
            " WHERE spool_id = %s AND kind = 'synced_from_bb'",
            (spool,),
        )


def _synced_events(f: Filament, spool: UUID) -> list[Any]:
    return [e for e in f.spools.events(spool) if e.kind == "synced_from_bb"]


def _row(core: int | None, stamp: str = _SAME) -> BbSpool:
    return bb(id=1, updated_at=stamp, core_weight_catalog_id=core, **_NO_TAGS)


def test_a_reading_with_no_catalog_id_is_read_once_and_the_event_is_booked(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    made = _linked_untyped(f, core_weight_catalog_id=5)
    _strip_key(f, made)
    events = len(_synced_events(f, made))
    report = pull(f, [_row(5)], now=NOW, catalog=[_entry()])
    (kind,) = _insert_types(f)
    assert report.updated_ids == [made]
    assert _own_insert(f, made) == kind.id
    booked = _synced_events(f, made)
    assert len(booked) == events + 1
    assert "core_weight_catalog_id" in booked[-1].data["bb"]
    assert booked[-1].data["changes"] == {"insert_type_id": [None, str(kind.id)]}
    # The next pull sees the key: nothing is looked at again.
    assert pull(f, [_row(5)], now=NOW, catalog=[_entry()]).updated_ids == []
    assert len(_synced_events(f, made)) == events + 1


def test_a_reading_with_no_catalog_id_is_read_once_with_no_catalog_at_all(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    made = _linked_untyped(f)
    _strip_key(f, made)
    assert pull(f, [_row(None)], now=NOW).updated_ids == [made]
    assert pull(f, [_row(None)], now=NOW).updated_ids == []


def test_a_reading_with_no_catalog_id_keeps_a_persons_insert_type(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    made = _linked_untyped(f, core_weight_catalog_id=5)
    own = f.catalog.create(
        "insert-types", InsertTypeCreate(name="Mine", empty_weight_g=100)
    ).model_dump()
    f.spools.update(made, SpoolPatch(insert_type_id=own["id"]), actor="ann")
    _strip_key(f, made)
    events = len(_synced_events(f, made))
    pull(f, [_row(5)], now=NOW, catalog=[_entry()])
    assert _own_insert(f, made) == own["id"]
    assert len(_synced_events(f, made)) == events + 1
    assert pull(f, [_row(5)], now=NOW, catalog=[_entry()]).updated_ids == []


def test_a_persons_cleared_insert_type_stays_cleared(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    made = _linked_untyped(f, core_weight_catalog_id=5)
    pull(f, [_row(5)], now=NOW, catalog=[_entry()])
    assert _own_insert(f, made) is not None
    f.spools.update(made, SpoolPatch(insert_type_id=None), actor="ann")
    assert _own_insert(f, made) is None
    assert pull(f, [_row(5)], now=NOW, catalog=[_entry()]).updated_ids == []
    assert _own_insert(f, made) is None


def test_bb_moving_to_the_types_own_insert_type_clears_the_override(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    typed = f.types.create(
        SpoolTypeCreate(
            code="GFA00-RED", material_id=_pla(pg_pool), rgba="#FF0000FF", label_weight_g=1000
        )
    )
    catalog = [_entry(5, "Bambu reusable", 250), _entry(6, "Bambu cardboard", 212)]
    row = {"material_number": "GFA00-RED", "id": 1, **_NO_TAGS}
    (made,) = pull(
        f,
        [bb(updated_at=_SAME, core_weight_catalog_id=6, **row)],
        now=NOW,
        catalog=catalog,
    ).created_ids
    inserts = {k.bb_catalog_id: k.id for k in _insert_types(f)}
    assert _own_insert(f, made) == inserts[6]
    f.types.update(typed.id, SpoolTypePatch(insert_type_id=inserts[5]))
    pull(
        f,
        [bb(updated_at="2026-11-14T11:00:00", core_weight_catalog_id=5, **row)],
        now=NOW,
        catalog=catalog,
    )
    assert _own_insert(f, made) is None


def test_an_unmapped_catalog_entry_is_applied_once_it_is_mapped(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    catalog = [_entry(5, "Bambu reusable", 250), _entry(6, "Bambu cardboard", 212)]
    made = _linked_untyped(f, core_weight_catalog_id=5)
    pull(f, [_row(5)], now=NOW, catalog=[catalog[0]])
    inserts = {k.bb_catalog_id: k.id for k in _insert_types(f)}
    assert _own_insert(f, made) == inserts[5]
    # BB moves to 6 while its entry is not mapped (a failed catalog read).
    pull(f, [_row(6, "2026-11-14T11:00:00")], now=NOW)
    assert _own_insert(f, made) == inserts[5]
    # The entry is mapped on the next pull, BB's row unmoved: BB's value is applied.
    pull(f, [_row(6, "2026-11-14T11:00:00")], now=NOW, catalog=catalog)
    inserts = {k.bb_catalog_id: k.id for k in _insert_types(f)}
    assert _own_insert(f, made) == inserts[6]
    assert pull(f, [_row(6, "2026-11-14T11:00:00")], now=NOW, catalog=catalog).updated_ids == []


def test_a_skipped_entry_warns_exactly_once(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scadbuddy.filament.bambuddy_pull._WARNED_SKIPS", set())
    f = _filament(pg_pool)
    catalog = [_entry(5, "Bambu reusable", 250), _entry(6, "Bambu REUSABLE", 212)]
    with caplog.at_level("DEBUG"):
        pull(f, [], now=NOW, catalog=catalog)
        pull(f, [], now=NOW, catalog=catalog)
    warned = [r for r in caplog.records if r.levelname == "WARNING" and "skipped" in r.message]
    assert len(warned) == 1
    assert "get no insert type" in caplog.text


# -- fix round 3 -----------------------------------------------------------------------


def _mine(f: Filament) -> UUID:
    made = f.catalog.create(
        "insert-types", InsertTypeCreate(name="Mine", empty_weight_g=100)
    ).model_dump()
    return UUID(str(made["id"]))


def test_a_recorded_none_moving_to_a_mapped_id_replaces_an_older_person_choice(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    made = _linked_untyped(f)
    f.spools.update(made, SpoolPatch(insert_type_id=_mine(f)), actor="ann")
    # BB's change is after the person's edit (BB's clock is ahead of the database's).
    pull(f, [_row(6, _bb_time(1))], now=NOW, catalog=[_entry(6, "Bambu cardboard", 212)])
    (kind,) = [k for k in _insert_types(f) if k.bb_catalog_id == 6]
    assert _own_insert(f, made) == kind.id


def test_a_recorded_none_moving_to_a_mapped_id_keeps_a_later_person_choice(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    made = _linked_untyped(f)
    mine = _mine(f)
    f.spools.update(made, SpoolPatch(insert_type_id=mine), actor="ann")
    # BB's change is before the person's edit.
    pull(f, [_row(6, "2026-11-14T11:00:00")], now=NOW, catalog=[_entry(6, "Bambu cardboard", 212)])
    assert _own_insert(f, made) == mine


def test_an_unmapped_entry_with_no_last_id_leaves_the_key_absent(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    made = _linked_untyped(f, core_weight_catalog_id=5)
    _strip_key(f, made)
    events = len(_synced_events(f, made))
    for _ in range(2):
        assert pull(f, [_row(5)], now=NOW).updated_ids == []
    assert len(_synced_events(f, made)) == events
    assert "core_weight_catalog_id" not in _synced_events(f, made)[-1].data["bb"]
    # Mapped later, BB's row unmoved: the first-read rule fills the spool.
    pull(f, [_row(5)], now=NOW, catalog=[_entry()])
    (kind,) = _insert_types(f)
    assert _own_insert(f, made) == kind.id
    assert pull(f, [_row(5)], now=NOW, catalog=[_entry()]).updated_ids == []


def test_a_first_read_keeps_a_persons_clear(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    made = _linked_untyped(f, core_weight_catalog_id=5)
    f.spools.update(made, SpoolPatch(insert_type_id=_mine(f)), actor="ann")
    f.spools.update(made, SpoolPatch(insert_type_id=None), actor="ann")
    _strip_key(f, made)
    pull(f, [_row(5)], now=NOW, catalog=[_entry()])
    assert _own_insert(f, made) is None
