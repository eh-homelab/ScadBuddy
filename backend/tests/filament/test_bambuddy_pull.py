"""Bambuddy's spools pulled into ScadBuddy's own (#6261, spec §8.1, §8.2): a new BB spool
becomes an SB spool, one SB already holds is linked, and a linked one takes what BB
changed since the last pull."""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from psycopg.types.json import Jsonb

from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.bambuddy.models import SpoolCatalogEntry
from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament import ledger, prints
from scadbuddy.filament.bambuddy_pull import (
    _WARNED_DUPLICATES,
    Pulls,
    pull,
    read_assignments,
    try_assignments,
)
from scadbuddy.filament.bb_pull_apply import pull_start
from scadbuddy.filament.bb_pull_models import PullInstanceChangedError, PullOutcome, PullStart
from scadbuddy.filament.bb_pull_run import read_catalog
from scadbuddy.filament.bb_sync_models import seen_row
from scadbuddy.filament.bb_sync_nudge import instance_for
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.component import Filament
from scadbuddy.filament.inventory import FilamentInventory
from scadbuddy.filament.invoices import InvoiceStore
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.metrics_store import MetricsSettings, MetricsStore
from scadbuddy.filament.models import (
    InsertType,
    InsertTypeCreate,
    LocationCreate,
    LocationKind,
    Material,
    SensorIn,
    SpoolCreate,
    SpoolPatch,
    SpoolTypeCreate,
    SpoolTypePatch,
)
from scadbuddy.filament.presets import PoolSpoolPresets
from scadbuddy.filament.print_uses import SlotUse, record_uses, settle_uses
from scadbuddy.filament.rfid_mode import RfidModeStore
from scadbuddy.filament.sensors import SensorStore
from scadbuddy.filament.shopping import ShoppingStore
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.suppliers import SuppliersStore
from scadbuddy.filament.types import SpoolTypeStore
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.operations.kinds import CHECK_ON_BAMBUDDY
from scadbuddy.spools.activities import filament_pull_activity
from tests.conftest import PgPool
from tests.filament.pull_support import read_and_pull, read_ha_sensors, scheduled_pull
from tests.filament.test_bb_sync_plan import SPOOLS, _only, _plan, _snap
from tests.filament.test_bb_sync_plan import _row as _plan_row
from tests.filament.test_bb_sync_plan import _spool as _plan_spool
from tests.filament.test_bb_sync_plan import _type as _plan_type
from tests.filament.test_bb_sync_results import _apply, _bb_now, _created, _ok
from tests.support.pg_waits import wait_for_blocked

pytestmark = pytest.mark.requires_postgres

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
BB = "https://bambuddy.test"
TRAY = "0123456789ABCDEF0123456789ABCDEF"


def _filament(pool: PgPool) -> Filament:
    types = SpoolTypeStore(pool)
    spools = FilamentSpoolStore(pool)
    return Filament(
        catalog=CatalogStore(pool),
        types=types,
        spools=spools,
        locations=LocationStore(pool),
        sensors=SensorStore(pool),
        inventory=FilamentInventory(pool),
        presets=PoolSpoolPresets(pool),
        rfid=RfidModeStore(pool),
        suppliers=SuppliersStore(pool, lambda: "USD"),
        invoices=InvoiceStore(pool, lambda: "USD", spools, types),
        shopping=ShoppingStore(pool),
        metrics=MetricsStore(pool, spools, MetricsSettings),
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


def _link_seen(pool: PgPool, spool: UUID) -> dict[str, Any]:
    """The spool link's ``bb_seen``."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT bb_seen FROM filament_bb_links WHERE spool_id = %s", (spool,)
        ).fetchone()
    assert row is not None
    return dict(row["bb_seen"] or {})


def _forget_seen(pool: PgPool, spool: UUID, field: str) -> None:
    """Take ``field`` out of the spool link's ``bb_seen``, as a link seen before #6265."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE filament_bb_links SET bb_seen = bb_seen - %s WHERE spool_id = %s",
            (field, spool),
        )


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


def test_a_bb_spool_spelled_as_an_alias_takes_the_type(pg_pool: PgPool) -> None:
    """R-6706-19: the identity match resolves each name by key, as the field values do, so
    an alias or another spelling of a name links the type it describes."""
    f = _filament(pg_pool)
    pull(f, [bb(id=1, tray_uuid=None, tag_uid=None)], now=NOW)
    brand = _ids(f, "brands")["Bambu Lab"]
    f.catalog.set_aliases("brands", brand, ["BBL"])
    kind = f.types.create(
        SpoolTypeCreate(
            code="PLA-RED",
            material_id=_pla(pg_pool),
            brand_id=brand,
            subtype_id=_ids(f, "subtypes")["Basic"],
            rgba="#FF0000",
            label_weight_g=1000,
        )
    )
    spellings = [
        bb(id=2, brand="BBL", tray_uuid=None, tag_uid=None),
        bb(
            id=3, brand="BAMBU-LAB", subtype="basic", material="p.l.a", tray_uuid=None, tag_uid=None
        ),
    ]
    report = pull(f, spellings, now=NOW)
    assert (report.created, report.needs_type) == (2, 0)
    for made_id in report.created_ids:
        made = f.spools.get(made_id)
        assert made is not None and made.type_id == kind.id


def test_a_bb_spool_spelled_as_a_merged_loser_takes_the_type(pg_pool: PgPool) -> None:
    """R-6706-19: after the migration merges `PETG-HF` into `PETG HF`, Bambuddy still
    sends the loser's spelling; it links the survivor's type."""
    f = _filament(pg_pool)
    pull(f, [bb(id=1, material="PETG HF", subtype=None, tray_uuid=None, tag_uid=None)], now=NOW)
    kind = f.types.create(
        SpoolTypeCreate(
            code="PETGHF-RED",
            material_id=_ids(f, "materials")["PETG HF"],
            brand_id=_ids(f, "brands")["Bambu Lab"],
            rgba="#FF0000",
            label_weight_g=1000,
        )
    )
    report = pull(
        f, [bb(id=2, material="PETG-HF", subtype=None, tray_uuid=None, tag_uid=None)], now=NOW
    )
    made = f.spools.get(report.created_ids[0])
    assert made is not None and made.type_id == kind.id


def test_a_bb_name_with_no_key_matches_as_none_and_an_unknown_one_matches_nothing(
    pg_pool: PgPool,
) -> None:
    """R-6706-19: a subtype with no letter or digit counts as no subtype (R-6706-18); a
    keyed brand no row holds matches no type."""
    f = _filament(pg_pool)
    pull(f, [bb(id=1, subtype=None, tray_uuid=None, tag_uid=None)], now=NOW)
    kind = f.types.create(
        SpoolTypeCreate(
            code="PLA-RED",
            material_id=_pla(pg_pool),
            brand_id=_ids(f, "brands")["Bambu Lab"],
            rgba="#FF0000",
            label_weight_g=1000,
        )
    )
    with pg_pool.connection() as conn:
        found = f.types.match(
            brand="Bambu Lab",
            material="PLA",
            subtype="---",
            rgba="#FF0000",
            label_weight_g=1000,
            conn=conn,
        )
        assert found is not None and found.id == kind.id
        unknown = f.types.match(
            brand="Nobody",
            material="PLA",
            subtype=None,
            rgba="#FF0000",
            label_weight_g=1000,
            conn=conn,
        )
        assert unknown is None


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
    # No field changed, so it is no update (#6265 concern 4); BB's reading is recorded.
    assert report.updated == 0
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


#: Every data type off: the pull has nothing to do.
NOTHING_PULLS = FilamentSync(spools="off", weights="off", tags="off", presets="off")


async def test_off_skips_the_pull(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    with respx.mock(assert_all_called=False) as router:
        spools = router.get(f"{BB}/api/v1/inventory/spools").respond(
            json=[bb().model_dump(mode="json")]
        )
        assert await scheduled_pull(f, lambda: settings, lambda: NOTHING_PULLS) == "skipped"
        assert not spools.called
    assert f.spools.list() == []


async def test_usage_is_not_booked_with_the_spools_and_weights_off(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    with respx.mock(assert_all_called=False) as router:
        usage = router.get(f"{BB}/api/v1/inventory/usage").respond(json=[])
        router.get(f"{BB}/api/v1/inventory/spools").respond(json=[bb().model_dump(mode="json")])
        router.get(f"{BB}/api/v1/inventory/catalog").respond(json=[])
        assert (
            await scheduled_pull(
                f, lambda: settings, lambda: FilamentSync(spools="off", weights="off")
            )
            == "pulled"
        )
        assert not usage.called
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


class _Execute:
    """`bb_pull_apply.execute_pull` as the spool refresh's activity calls it: records each
    start and answers ``answer`` (or raises it)."""

    def __init__(self, answer: PullOutcome | Exception) -> None:
        self.answer = answer
        self.starts: list[PullStart] = []

    async def __call__(self, start: PullStart) -> PullOutcome:
        self.starts.append(start)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


async def test_the_spool_refresh_activity_reads_no_bambuddy(pg_pool: PgPool) -> None:
    """#6772: the API's activity has the print worker read Bambuddy (``FilamentBbPull``)
    and answers its outcome; the API itself calls no Bambuddy (respx refuses any call)."""
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    execute = _Execute(PullOutcome(outcome="pulled"))
    run = filament_pull_activity(f, lambda: settings, FilamentSync, execute)
    with respx.mock() as router:
        assert await run() == "pulled"
        assert not router.calls
    (start,) = execute.starts
    assert (start.instance, start.pulls) == (instance_for(BB), Pulls())
    assert start.pull_id.startswith("filament-bb-pull-")
    assert (start.ha, start.reconcile, start.strict) == (False, True, False)


async def test_the_spool_refresh_activity_skips_and_fails_as_before(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    idle = _Execute(PullOutcome(outcome="pulled"))
    assert await filament_pull_activity(f, StoredSettings, FilamentSync, idle)() == "skipped"
    nothing = filament_pull_activity(f, lambda: settings, lambda: NOTHING_PULLS, idle)
    assert await nothing() == "skipped"
    assert idle.starts == []
    down = _Execute(RuntimeError("Temporal is out of reach"))
    assert await filament_pull_activity(f, lambda: settings, FilamentSync, down)() == "failed"
    worker = _Execute(PullOutcome(outcome="failed"))
    assert await filament_pull_activity(f, lambda: settings, FilamentSync, worker)() == "failed"


async def test_a_pull_of_another_bambuddy_is_skipped(pg_pool: PgPool) -> None:
    """#6845 R-6845-5: while the links belong to another Bambuddy than the configured one,
    the Schedule's pull reads no spool, and a body the worker read anyway is refused, so
    nothing is written to a linked spool."""
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(updated_at=_bb_time(-3))], now=NOW, instance="0ld0ld0ld0ld").created_ids
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    with respx.mock(assert_all_called=False) as router:
        spools = router.get(f"{BB}/api/v1/inventory/spools").respond(
            json=[bb(note="a stranger's", updated_at=_bb_time(-1)).model_dump(mode="json")]
        )
        router.get(f"{BB}/api/v1/inventory/catalog").respond(json=[])
        assert await scheduled_pull(f, lambda: settings, FilamentSync) == "skipped"
        assert not spools.called
        with pytest.raises(PullInstanceChangedError):
            await read_and_pull(f, settings)
    spool = f.spools.get(made)
    assert spool is not None
    assert spool.note != "a stranger's"


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
        ("scale", "bb_scale", 950, 1200),
        ("scale", "bb_scale", 650, 900),
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


def test_a_lock_after_the_weighing_keeps_the_scale_anchor(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(updated_at=_bb_time(-3))], now=NOW).created_ids
    weighed = bb(
        core_weight=250, last_scale_weight=700, last_weighed_at=_bb_time(1), updated_at=_bb_time(1)
    )
    pull(f, [weighed], now=NOW)
    f.spools.update(made, SpoolPatch(weight_locked=True), actor="ann")
    spool = f.spools.get(made)
    assert spool is not None
    assert (spool.weight_locked, spool.grams_left) == (True, 450)


@pytest.mark.parametrize(("locked", "left"), [(False, 500), (True, 1000)])
def test_a_bb_weighing_is_a_scale_reading(pg_pool: PgPool, locked: bool, left: float) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(updated_at=_bb_time(-3))], now=NOW).created_ids
    if locked:
        f.spools.update(made, SpoolPatch(weight_locked=True), actor="ann")
    weighed = bb(
        core_weight=250,
        last_scale_weight=1250,
        last_weighed_at=_bb_time(1),
        updated_at=_bb_time(1),
    )
    pull(f, [weighed], now=NOW)
    (event,) = [e for e in f.spools.events(made) if e.kind == "weighed"]
    assert (event.source, event.grams_after) == ("scale", 1000)
    # Spec 5.5: the newer AMS reading is the anchor, unless the weight lock is on.
    with pg_pool.connection() as conn:
        ledger.record(
            conn,
            made,
            "ams_reading",
            source="ams_remain",
            absolute_g=500,
            at=datetime.now(UTC) + timedelta(hours=2),
        )
    spool = f.spools.get(made)
    assert spool is not None
    assert spool.grams_left == left


def test_weights_off_books_no_bb_weighing(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    weighed = bb(
        core_weight=250, last_scale_weight=700, last_weighed_at=_bb_time(1), updated_at=_bb_time(1)
    )
    (made,) = pull(f, [weighed], now=NOW, pulls=Pulls(weights=False)).created_ids
    pull(f, [weighed], now=NOW, pulls=Pulls(weights=False))
    assert [e for e in f.spools.events(made) if e.ref_kind == "bb_scale"] == []


def _bb_readings(f: Filament, spool: UUID) -> list[float | None]:
    return [e.grams_after for e in f.spools.events(spool) if e.source == "bb"]


def test_weights_off_books_no_bb_reading_on_an_update(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb()], now=NOW).created_ids
    pull(f, [bb(weight_used=300.0, updated_at=_bb_time(1))], now=NOW, pulls=Pulls(weights=False))
    spool = f.spools.get(made)
    assert spool is not None
    assert spool.grams_left == 900
    assert _bb_readings(f, made) == [900]


def test_weights_off_books_no_bb_reading_on_a_link(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    held = f.spools.create(SpoolCreate(material_id=_pla(pg_pool)), actor="ann")
    f.spools.bind_tag(held.id, tray_uuid=TRAY, kind="bambu_mifare", actor="ann")
    pull(f, [bb()], now=NOW, pulls=Pulls(weights=False))
    spool = f.spools.get(held.id)
    assert spool is not None
    assert spool.grams_left is None
    assert _bb_readings(f, held.id) == []


def test_weights_off_still_books_a_new_spools_first_reading(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb()], now=NOW, pulls=Pulls(weights=False)).created_ids
    spool = f.spools.get(made)
    assert spool is not None
    assert spool.grams_left == 900
    assert _bb_readings(f, made) == [900]


def test_weights_bb_to_sb_books_a_bb_reading_on_an_update(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb()], now=NOW).created_ids
    pull(f, [bb(weight_used=300.0, updated_at=_bb_time(1))], now=NOW, pulls=Pulls(weights=True))
    assert _bb_readings(f, made) == [900, 700]


def test_weights_bb_to_sb_books_the_bb_weighing(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    weighed = bb(
        core_weight=250, last_scale_weight=700, last_weighed_at=_bb_time(1), updated_at=_bb_time(1)
    )
    (made,) = pull(f, [weighed], now=NOW, pulls=Pulls(weights=True)).created_ids
    assert [e.ref_kind for e in f.spools.events(made) if e.kind == "weighed"] == ["bb_scale"]


async def test_a_scheduled_pull_with_weights_off_books_no_bb_weighing(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    weighed = bb(
        core_weight=250, last_scale_weight=700, last_weighed_at=_bb_time(1), updated_at=_bb_time(1)
    )
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    with respx.mock() as router:
        router.get(f"{BB}/api/v1/inventory/spools").respond(json=[weighed.model_dump(mode="json")])
        router.get(f"{BB}/api/v1/inventory/catalog").respond(json=[])
        outcome = await scheduled_pull(f, lambda: settings, lambda: FilamentSync(weights="off"))
    assert outcome == "pulled"
    (made,) = [s.id for s in f.spools.list()]
    assert [e for e in f.spools.events(made) if e.ref_kind == "bb_scale"] == []


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


ARCHIVED_AT = datetime(2026, 11, 12, 8, 0, tzinfo=UTC)


def _archived_bb(**over: Any) -> BbSpool:
    """A BB spool Bambuddy archived before any pull saw it (#6707: BB 125, used up)."""
    return bb(
        **{
            "id": 125,
            "weight_used": 1020.93,
            "archived_at": "2026-11-12T08:00:00",
            "updated_at": "2026-11-12T08:00:00",
        }
        | over
    )


def test_an_archived_unlinked_bb_spool_is_made_archived(pg_pool: PgPool) -> None:
    """#6707: Bambuddy's archived spool comes in archived, at BB's time, linked, with what
    is left as BB says (never below 0), and is never offered for loading."""
    f = _filament(pg_pool)
    report = pull(f, [_archived_bb()], now=NOW)
    (made,) = report.created_ids
    assert (report.created, report.archived_created, report.archived) == (1, 1, 0)
    assert f.spools.list() == []
    spool = f.spools.get(made)
    assert spool is not None
    assert spool.archived_at == ARCHIVED_AT
    assert spool.grams_left == 0
    # An archived spool holds no tag: the tag is free to re-use.
    assert f.spools.open_binding(made) is None
    events = f.spools.events(made)
    assert [e.kind for e in events] == ["created", "synced_from_bb", "archived"]
    assert (events[0].source, events[0].grams_after) == ("bb", 0)
    assert events[-1].at == ARCHIVED_AT
    with pg_pool.connection() as conn:
        link = conn.execute(
            "SELECT bb_spool_id FROM filament_bb_links WHERE spool_id = %s", (made,)
        ).fetchone()
    assert link == {"bb_spool_id": 125}


def test_a_second_pull_of_an_archived_bb_spool_makes_nothing(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [_archived_bb()], now=NOW).created_ids
    count = len(f.spools.events(made))
    again = pull(f, [_archived_bb()], now=NOW)
    assert (again.created, again.archived_created, again.archived_ids) == (0, 0, [made])
    assert len(f.spools.events(made)) == count
    with pg_pool.connection() as conn:
        spools = conn.execute("SELECT count(*) AS n FROM filament_spools").fetchone()
    assert spools == {"n": 1}


def test_an_archived_bb_spool_keeps_what_is_left_and_no_label_is_none_left(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    unlabelled = {"id": 126, "label_weight": 0, "tray_uuid": None, "tag_uid": None}
    left, none = pull(
        f, [_archived_bb(weight_used=250.0), _archived_bb(**unlabelled)], now=NOW
    ).created_ids
    assert [(s.id, s.grams_left) for s in f.spools.list(include_archived=True)] == [
        (left, 750),
        (none, 0),
    ]


def test_an_archived_bb_spool_takes_its_type_or_needs_one(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    kind = f.types.create(
        SpoolTypeCreate(
            code="GFA00-RED", material_id=_pla(pg_pool), rgba="#FF0000FF", label_weight_g=1000
        )
    )
    other = {"id": 126, "brand": "Other", "tray_uuid": None, "tag_uid": None}
    report = pull(f, [_archived_bb(material_number="GFA00-RED"), _archived_bb(**other)], now=NOW)
    typed, untyped = report.created_ids
    assert report.needs_type_ids == [untyped]
    types = {s.id: s.type_id for s in f.spools.list(include_archived=True)}
    assert types == {typed: kind.id, untyped: None}


def test_a_bb_archive_archives_the_sb_spool(pg_pool: PgPool) -> None:
    """#6265 Task 9: a linked spool Bambuddy archives is archived in SB, under
    ``pulls(spools)`` only; Bambuddy's restore after it restores it."""
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(updated_at=_bb_time(-2))], now=NOW).created_ids
    archived = bb(archived_at=_bb_time(-1), updated_at=_bb_time(-1))
    assert pull(f, [archived], now=NOW, pulls=Pulls(spools=False)).updated_ids == []
    spool = f.spools.get(made)
    assert spool is not None and spool.archived_at is None
    report = pull(f, [archived], now=NOW)
    assert (report.created, report.updated_ids, report.archived_created) == (0, [made], 0)
    spool = f.spools.get(made)
    assert spool is not None and spool.archived_at is not None
    assert [e.kind for e in f.spools.events(made)][-1] == "archived"
    # Pulled again, it stays as it is and takes nothing.
    count = len(f.spools.events(made))
    assert pull(f, [archived], now=NOW).archived_ids == [made]
    assert len(f.spools.events(made)) == count
    restored = bb(archived_at=None, updated_at=_bb_time(1))
    assert pull(f, [restored], now=NOW).updated_ids == [made]
    spool = f.spools.get(made)
    assert spool is not None and spool.archived_at is None


def test_an_sb_restore_after_the_bb_archive_wins(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(updated_at=_bb_time(-2))], now=NOW).created_ids
    archived = bb(archived_at=_bb_time(-1), updated_at=_bb_time(-1))
    pull(f, [archived], now=NOW)
    f.spools.archive(made, False, actor="ann")
    report = pull(f, [archived], now=NOW)
    assert report.archived_ids == []
    spool = f.spools.get(made)
    assert spool is not None and spool.archived_at is None


def test_an_sb_archive_is_not_undone_by_a_bb_row_that_was_never_archived(
    pg_pool: PgPool,
) -> None:
    """Bambuddy's row moved, but not its archive state: SB's archive stays."""
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(updated_at=_bb_time(-2))], now=NOW).created_ids
    f.spools.archive(made, True, actor="ann")
    pull(f, [bb(color_name="Blue", updated_at=_bb_time(1))], now=NOW)
    spool = f.spools.get(made)
    assert spool is not None and spool.archived_at is not None


def test_a_link_with_no_seen_archive_state_is_not_archived_and_is_seeded(
    pg_pool: PgPool,
) -> None:
    """R-T9-5: the first pull of a link with no seen archive state never archives or
    restores the SB spool. It seeds the seen archive state with Bambuddy's, so the same
    row pulled again changes nothing."""
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(updated_at=_bb_time(-2))], now=NOW).created_ids
    _forget_seen(pg_pool, made, "archived_at")
    archived = bb(archived_at=_bb_time(-1), updated_at=_bb_time(-1))
    for _ in range(2):
        pull(f, [archived], now=NOW)
        spool = f.spools.get(made)
        assert spool is not None and spool.archived_at is None
        assert _link_seen(pg_pool, made)["archived_at"] is not None
    assert "archived" not in [e.kind for e in f.spools.events(made)]


async def test_an_unlinked_archived_bb_spool_is_made_archived_with_its_usage(
    pg_pool: PgPool,
) -> None:
    """R-T9-0 (#6707 wins over the brief): an unlinked archived BB spool makes an archived
    SB spool with its usage, and it stays archived. R-T9-1: only under ``pulls(spools)``.
    R-T9-2: it takes no weighing, preset or K value."""
    f = _filament(pg_pool)
    printer = UUID(int=0)
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO printers (id, serial, name, model, bambuddy_printer_id)"
            " VALUES (%s, 'SN1', 'P', 'H2C', 1)",
            (printer,),
        )
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    spent = _archived_bb(
        slicer_filament="GFA00",
        slicer_filament_name="Bambu PLA Basic",
        last_scale_weight=400,
        last_weighed_at="2026-11-12T07:00:00",
        k_profiles=[{"printer_id": 1, "extruder": 0, "nozzle_diameter": "0.4", "k_value": 0.02}],
    ).model_dump(mode="json")
    use = {
        "id": 1,
        "spool_id": 125,
        "weight_used": 600.0,
        "status": "completed",
        "created_at": "2026-11-11T08:00:00",
    }
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BB}/api/v1/inventory/spools").respond(json=[spent])
        router.get(f"{BB}/api/v1/inventory/usage").respond(json=[use])
        router.get(f"{BB}/api/v1/inventory/catalog").respond(json=[])
        router.get(f"{BB}/api/v1/archives/").respond(json=[])
        off = await read_and_pull(f, settings, pulls=Pulls(spools=False))
        assert (off.created, f.spools.list(include_archived=True)) == (0, [])
        report = await read_and_pull(f, settings)
        again = await read_and_pull(f, settings)
    (made,) = report.created_ids
    assert (report.archived_created, report.usage_booked) == (1, 1)
    assert (again.created, again.archived_ids) == (0, [made])
    spool = f.spools.get(made)
    assert spool is not None and spool.archived_at == ARCHIVED_AT
    kinds = [e.kind for e in f.spools.events(made)]
    assert "weighed" not in kinds
    assert [e.ref_kind for e in f.spools.events(made) if e.ref_kind == ledger.BB_USAGE] == [
        ledger.BB_USAGE
    ]
    assert _own_presets(pg_pool, made) == []
    with pg_pool.connection() as conn:
        k = conn.execute(
            "SELECT count(*) AS n FROM filament_spool_k_values WHERE spool_id = %s", (made,)
        ).fetchone()
    assert k == {"n": 0}


def test_an_archived_bb_duplicate_of_a_linked_tag_makes_no_spool(pg_pool: PgPool) -> None:
    """A BB spool Bambuddy added for a tag a linked SB spool holds stays skipped when it is
    archived too (#6264): no second spool, archived or not, for one physical spool."""
    f = _filament(pg_pool)
    (made,) = pull(f, [bb()], now=NOW).created_ids
    report = pull(f, [bb(), _archived_bb(id=8, weight_used=0.0)], now=NOW)
    assert (report.created, report.rfid_duplicate_bb_ids) == (0, [8])
    assert [s.id for s in f.spools.list(include_archived=True)] == [made]


def test_the_live_bb_record_of_a_tag_is_linked_before_an_archived_one(pg_pool: PgPool) -> None:
    """R-6707-13: when an archived and a live BB record carry one tag, the live one is
    linked to the SB spool that holds it, and the archived one is the RFID duplicate,
    even when BB lists the archived one first. So the live record's weight reaches SB."""
    f = _filament(pg_pool)
    held = f.spools.create(
        SpoolCreate(material_id=_pla(pg_pool), grams_left=500, note="mine"), actor="ann"
    )
    f.spools.bind_tag(held.id, tray_uuid=TRAY, kind="bambu_mifare", actor="ann")
    report = pull(f, [_archived_bb(id=8), bb(id=9)], now=NOW)
    assert (report.linked_ids, report.rfid_duplicate_bb_ids) == ([held.id], [8])
    assert (report.created, report.archived_created) == (0, 0)
    with pg_pool.connection() as conn:
        link = conn.execute(
            "SELECT bb_spool_id FROM filament_bb_links WHERE spool_id = %s", (held.id,)
        ).fetchone()
    assert link == {"bb_spool_id": 9}
    (spool,) = f.spools.list(include_archived=True)
    assert (spool.id, spool.archived_at) == (held.id, None)


def test_an_archived_bb_record_of_a_live_records_tag_makes_no_spool(pg_pool: PgPool) -> None:
    """R-6707-13: with no SB spool holding the tag, the live record is made and the
    archived copy of its tag is skipped, whatever BB's order."""
    f = _filament(pg_pool)
    report = pull(f, [_archived_bb(id=8), bb(id=9)], now=NOW)
    (made,) = report.created_ids
    assert (report.archived_created, report.rfid_duplicate_bb_ids) == (0, [8])
    assert [(s.id, s.archived_at) for s in f.spools.list(include_archived=True)] == [(made, None)]


async def test_the_pull_reads_bambuddys_archived_spools(pg_pool: PgPool) -> None:
    """#6707 R-6707-1: the import and the Schedule's pull ask for the archived rows."""
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    with respx.mock() as router:
        spools = router.get(
            f"{BB}/api/v1/inventory/spools", params={"include_archived": "true"}
        ).respond(json=[_archived_bb().model_dump(mode="json")])
        router.get(f"{BB}/api/v1/inventory/usage").respond(json=[])
        router.get(f"{BB}/api/v1/inventory/catalog").respond(json=[])
        report = await read_and_pull(f, settings)
        assert (report.created, report.archived_created) == (1, 1)
        assert await scheduled_pull(f, lambda: settings, FilamentSync) == "pulled"
        assert spools.call_count == 2


def test_two_bb_spools_with_one_tag_make_one_spool_and_count_the_second(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    _WARNED_DUPLICATES.clear()
    f = _filament(pg_pool)
    report = pull(f, [bb(id=1), bb(id=2)], now=NOW)
    (first,) = report.created_ids
    assert f.spools.open_binding(first) is not None
    assert (report.rfid_duplicates, report.rfid_duplicate_bb_ids) == (1, [2])
    assert "is left alone" in caplog.text


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


def _report_end(pool: PgPool, subtask: str) -> None:
    """The printer's report that shows plate ``subtask`` finished, now (Task 5)."""
    with pool.connection() as conn, conn.transaction():
        prints.plate_ended(conn, str(uuid4()), subtask, "FINISH", datetime.now(UTC))


def test_a_pull_between_two_plates_counts_each_plate_once(pg_pool: PgPool) -> None:
    """BB deducts plate ``a`` as it ends; a pull then anchors on a value that holds it,
    and the run's settle at its end takes off plate ``b`` only."""
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(updated_at=_bb_time(-3))], now=NOW).created_ids
    _lan_run(pg_pool, "a", "b")
    record_uses(pg_pool, RUN, "a", _use(made, 50))
    # Plate a ends (the printer's report books it), BB takes its 50 g off, and the pull
    # reads that before plate b.
    _report_end(pg_pool, "a")
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
    _report_end(pg_pool, "a")
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
    f.types.update(typed.id, SpoolTypePatch(insert_type_id=inserts[5]), actor="ann")
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

    monkeypatch.setattr("scadbuddy.filament.bb_pull_run.CATALOG_TIMEOUT", 0.2)
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
    events = len(_synced_events(f, made))
    # The reading is booked again with the key, but no field changed: no update (#6265
    # concern 4).
    assert pull(f, [_row(None)], now=NOW).updated_ids == []
    assert len(_synced_events(f, made)) == events + 1
    assert pull(f, [_row(None)], now=NOW).updated_ids == []
    assert len(_synced_events(f, made)) == events + 1


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
    f.types.update(typed.id, SpoolTypePatch(insert_type_id=inserts[5]), actor="ann")
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


@respx.mock
async def test_a_payload_of_another_shape_logs_no_value_from_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#6580: a pydantic `ValidationError`'s text quotes the values Bambuddy sent; the
    log names only the error type."""
    secret = "SECRET-TRAY-VALUE-123"
    respx.get("https://bambuddy.test/api/v1/inventory/assignments").mock(
        return_value=httpx.Response(200, json=[{"id": secret, "spool_id": secret}])
    )
    respx.get("https://bambuddy.test/api/v1/inventory/catalog").mock(
        return_value=httpx.Response(200, json=[{"id": secret, "name": 5, "weight": secret}])
    )
    settings = StoredSettings(bambuddy_url="https://bambuddy.test", bambuddy_api_key="bb_test")
    with caplog.at_level("DEBUG"):
        assert await try_assignments(settings) is None
        assert await read_catalog(settings) is None
    assert caplog.text.count("another shape") == 2
    assert "ValidationError" in caplog.text
    assert secret not in caplog.text


# -- Home Assistant sensors through Bambuddy (#6268) --------------------------------------

ENTITIES = f"{BB}/api/v1/location-ha-sensors/entities"


def _ha_sensor(pool: PgPool, entity: str, **extra: Any) -> UUID:
    kinds = CatalogStore(pool).list("location-kinds")
    kind = next(r.id for r in kinds if isinstance(r, LocationKind) and r.name == "Shelf")
    place = LocationStore(pool).create(LocationCreate(name=f"Box {entity}", kind_id=kind)).id
    body = SensorIn.model_validate(
        {
            "location_id": place,
            "source": "home_assistant",
            "kind": "humidity",
            "entity_id": entity,
            "name": entity,
            **extra,
        }
    )
    return SensorStore(pool).create(body, actor="ann").id


def _sensor_row(pool: PgPool, sensor: UUID) -> tuple[float | None, bool, int]:
    (got,) = [s for s in SensorStore(pool).list(None) if s.id == sensor]
    return got.last_value, got.reachable, len(SensorStore(pool).readings(sensor, NOW - HOUR, 99))


HOUR = timedelta(hours=1)


def _entity(entity: str, state: str | None) -> dict[str, Any]:
    return {"entity_id": entity, "friendly_name": entity, "state": state, "domain": "sensor"}


async def test_ha_states_become_readings(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    a, b = _ha_sensor(pg_pool, "sensor.a"), _ha_sensor(pg_pool, "sensor.b")
    other = _ha_sensor(pg_pool, "sensor.not_listed")
    with respx.mock() as router:
        route = router.get(ENTITIES).respond(
            json=[_entity("sensor.a", "18.4"), _entity("sensor.b", "-3"), _entity("sensor.x", "9")]
        )
        await read_ha_sensors(f, settings, NOW)
        assert route.call_count == 1
    assert _sensor_row(pg_pool, a) == (18.4, True, 1)
    assert _sensor_row(pg_pool, b) == (-3.0, True, 1)
    # An entity Bambuddy does not list cannot be read: unreachable, nothing stored.
    assert _sensor_row(pg_pool, other) == (None, False, 0)
    got = {s.id: s.last_at for s in SensorStore(pg_pool).list(None)}
    assert got[a] == NOW


@pytest.mark.parametrize(
    "state", ["unavailable", "unknown", "45.2 %", "", "nan", "inf", "9" * 400, None]
)
async def test_a_non_numeric_state_marks_unreachable(pg_pool: PgPool, state: str | None) -> None:
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    sensor = _ha_sensor(pg_pool, "sensor.a")
    with respx.mock() as router:
        router.get(ENTITIES).respond(json=[_entity("sensor.a", "20")])
        await read_ha_sensors(f, settings, NOW)
        assert _sensor_row(pg_pool, sensor) == (20.0, True, 1)
    with respx.mock() as router:
        router.get(ENTITIES).respond(json=[_entity("sensor.a", state)])
        await read_ha_sensors(f, settings, NOW + timedelta(minutes=5))
    # Nothing stored, the last value stays, and the sensor is unreachable.
    assert _sensor_row(pg_pool, sensor) == (20.0, False, 1)
    with respx.mock() as router:
        router.get(ENTITIES).respond(json=[_entity("sensor.a", "21")])
        await read_ha_sensors(f, settings, NOW + timedelta(minutes=10))
    assert _sensor_row(pg_pool, sensor) == (21.0, True, 2)


async def test_no_ha_sensor_means_no_read(pg_pool: PgPool) -> None:
    """The API asks the worker for Home Assistant's states only while a sensor is set up
    (#6772: `pull_start` decides, the worker reads)."""
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    assert await pull_start(f, lambda: settings, lambda: NOTHING_PULLS) is None
    start = await pull_start(f, lambda: settings, FilamentSync)
    assert start is not None and start.ha is False
    _ha_sensor(pg_pool, "sensor.a")
    start = await pull_start(f, lambda: settings, lambda: NOTHING_PULLS)
    assert start is not None and (start.ha, start.pulls) == (True, None)


async def test_a_failed_ha_read_never_fails_the_pull(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    sensor = _ha_sensor(pg_pool, "sensor.a")
    with respx.mock() as router:
        router.get(ENTITIES).respond(503)
        router.get(f"{BB}/api/v1/inventory/spools").respond(json=[])
        router.get(f"{BB}/api/v1/inventory/catalog").respond(json=[])
        assert await scheduled_pull(f, lambda: settings, FilamentSync) == "pulled"
    assert "could not read Bambuddy's Home Assistant sensors" in caplog.text
    assert _sensor_row(pg_pool, sensor)[2] == 0
    # Reachable means the last read gave a number: a failed read says no.
    assert _sensor_row(pg_pool, sensor)[1] is False


async def test_the_ha_read_runs_with_the_spools_off(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    sensor = _ha_sensor(pg_pool, "sensor.a")
    with respx.mock(assert_all_called=False) as router:
        spools = router.get(f"{BB}/api/v1/inventory/spools").respond(json=[])
        router.get(ENTITIES).respond(json=[_entity("sensor.a", "33")])
        outcome = await scheduled_pull(f, lambda: settings, lambda: NOTHING_PULLS)
        assert outcome == "skipped"
        assert not spools.called
    assert _sensor_row(pg_pool, sensor)[0] == 33.0


async def test_the_ha_read_needs_bambuddy_configured(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    _ha_sensor(pg_pool, "sensor.a")
    with respx.mock(assert_all_called=False) as router:
        route = router.get(ENTITIES).respond(json=[])
        assert await scheduled_pull(f, lambda: StoredSettings(), FilamentSync) == "skipped"
        assert not route.called


def _bound_kind(pool: PgPool, spool: UUID) -> str:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT t.kind FROM filament_tag_bindings b JOIN filament_tags t ON t.id = b.tag_id"
            " WHERE b.spool_id = %s AND b.unbound_at IS NULL",
            (spool,),
        ).fetchone()
    assert row is not None
    return str(row["kind"])


def test_an_opentag_spool_binds_as_ntag_opentag(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(
        f,
        [bb(tray_uuid=None, tag_uid="04AABB", data_origin="opentag3d")],
        now=NOW,
    ).created_ids
    assert _bound_kind(pg_pool, made) == "ntag_opentag"


def test_a_plain_uid_stays_other(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(tray_uuid=None, tag_uid="04AABB")], now=NOW).created_ids
    assert _bound_kind(pg_pool, made) == "other"


def test_a_tray_uuid_stays_bambu_mifare(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(tag_uid="04AABB", data_origin="opentag3d")], now=NOW).created_ids
    assert _bound_kind(pg_pool, made) == "bambu_mifare"


def _linked_override_holder(f: Filament) -> UUID:
    """SB spool A, linked to BB 11, holding TRAY as an override tag."""
    (a,) = pull(f, [bb(id=11, tray_uuid=None, tag_uid=None)], now=NOW).created_ids
    f.spools.bind_tag(a, tray_uuid=TRAY, kind="bambu_mifare", override=True, actor="ann")
    return a


def test_an_auto_added_bb_spool_for_an_override_tag_makes_no_second_sb_spool(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    _WARNED_DUPLICATES.clear()
    f = _filament(pg_pool)
    a = _linked_override_holder(f)
    before = f.spools.open_binding(a)
    assert before is not None
    report = pull(
        f,
        [bb(id=40, tag_uid=None, tray_uuid=TRAY, data_origin="rfid_auto")],
        now=NOW,
    )
    assert report.created == 0
    assert (report.rfid_duplicates, report.rfid_duplicate_bb_ids) == (1, [40])
    assert [s.id for s in f.spools.list()] == [a]
    after = f.spools.open_binding(a)
    assert after is not None and after.id == before.id
    assert "a Bambuddy spool made for a tag a linked spool holds is left alone" in caplog.text


def test_an_unlinked_sb_spool_with_the_tag_is_still_linked(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    held = f.spools.create(SpoolCreate(material_id=_pla(pg_pool)), actor="ann")
    f.spools.bind_tag(held.id, tray_uuid=TRAY, kind="bambu_mifare", override=True, actor="ann")
    report = pull(f, [bb(id=40, tag_uid=None, tray_uuid=TRAY, data_origin="rfid_auto")], now=NOW)
    assert (report.linked_ids, report.rfid_duplicates, report.created) == ([held.id], 0, 0)


def test_an_auto_added_bb_spool_for_a_plain_binding_makes_no_second_sb_spool(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    (a,) = pull(f, [bb(id=11, tray_uuid=None, tag_uid=None)], now=NOW).created_ids
    f.spools.bind_tag(a, tray_uuid=TRAY, kind="bambu_mifare", actor="ann")
    report = pull(f, [bb(id=40, tag_uid=None, tray_uuid=TRAY, data_origin="rfid_auto")], now=NOW)
    assert (report.created, report.rfid_duplicates, report.rfid_duplicate_bb_ids) == (0, 1, [40])
    assert [s.id for s in f.spools.list()] == [a]


def test_the_bb_spool_the_override_holder_is_linked_to_is_no_duplicate(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    a = _linked_override_holder(f)
    report = pull(
        f,
        [bb(id=11, tag_uid=None, tray_uuid=TRAY, updated_at=_bb_time(1), weight_used=300.0)],
        now=NOW,
    )
    assert (report.rfid_duplicates, report.created, report.updated_ids) == (0, 0, [a])


def test_a_duplicate_is_warned_once_then_debug(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    _WARNED_DUPLICATES.clear()
    f = _filament(pg_pool)
    _linked_override_holder(f)
    rows = [bb(id=40, tag_uid=None, tray_uuid=TRAY, data_origin="rfid_auto")]
    with caplog.at_level("DEBUG"):
        pull(f, rows, now=NOW)
        pull(f, rows, now=NOW)
    levels = [r.levelname for r in caplog.records if "is left alone" in r.getMessage()]
    assert levels == ["WARNING", "DEBUG"]


def test_a_pull_after_the_push_keeps_an_override_binding(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    a = _linked_override_holder(f)
    before = f.spools.open_binding(a)
    assert before is not None and before.override
    # The push linked TRAY on BB 11: its tag keys now differ from the last reading.
    pull(
        f,
        [bb(id=11, tag_uid=None, tray_uuid=TRAY, updated_at=_bb_time(1), weight_used=300.0)],
        now=NOW,
    )
    after = f.spools.open_binding(a)
    assert after is not None and after.id == before.id and after.override
    spool = f.spools.get(a)
    assert spool is not None and spool.weight_locked


def test_a_skipped_duplicate_stays_skipped_after_the_push_cleared_its_tag(
    pg_pool: PgPool,
) -> None:
    _WARNED_DUPLICATES.clear()
    f = _filament(pg_pool)
    a = _linked_override_holder(f)
    dup = bb(id=40, tag_uid=None, tray_uuid=TRAY, data_origin="rfid_auto")
    assert pull(f, [dup], now=NOW).rfid_duplicate_bb_ids == [40]
    # The push linked TRAY on BB 11 and cleared the auto-added BB 40.
    cleared = bb(id=40, tag_uid=None, tray_uuid=None, data_origin="rfid_auto")
    report = pull(f, [cleared], now=NOW)
    assert (report.created, report.rfid_duplicates, report.rfid_duplicate_bb_ids) == (0, 1, [40])
    assert [s.id for s in f.spools.list()] == [a]
    # Still skipped on every later pull.
    assert pull(f, [cleared], now=NOW).created == 0


# -- #6265: a direction per data type ----------------------------------------------------


def test_spools_sb_to_bb_pulls_no_spool_fields(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb()], now=NOW).created_ids
    pushed_only = Pulls.of(FilamentSync(spools="sb_to_bb"))
    report = pull(
        f,
        [
            bb(color_name="Blue", rgba="0000FFFF", updated_at="2026-11-14T11:00:00"),
            bb(id=8, tray_uuid=None, tag_uid=None),
        ],
        now=NOW,
        pulls=pushed_only,
    )
    assert (report.created, report.linked) == (0, 0)
    (spool,) = f.spools.list()
    assert spool.id == made
    assert (spool.colour_name, spool.rgba) == ("Red", "#FF0000FF")


async def test_weights_alone_books_usage_rows(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb()], now=NOW).created_ids
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    row = {
        "id": 457,
        "spool_id": 7,
        "printer_id": 1,
        "print_name": "scadbuddy-ac13ac3c-p1",
        "weight_used": 75.1,
        "percent_used": 7.5,
        "status": "completed",
        "created_at": "2026-11-14T11:00:00",
    }
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BB}/api/v1/inventory/spools").respond(json=[bb().model_dump(mode="json")])
        usage = router.get(f"{BB}/api/v1/inventory/usage").respond(json=[row])
        router.get(f"{BB}/api/v1/inventory/catalog").respond(json=[])
        router.get(f"{BB}/api/v1/archives/").respond(json=[])
        outcome = await scheduled_pull(
            f, lambda: settings, lambda: FilamentSync(spools="sb_to_bb", weights="bb_to_sb")
        )
        assert usage.called
    assert outcome == "pulled"
    assert [e.ref_kind for e in f.spools.events(made) if e.ref_kind == ledger.BB_USAGE] == [
        ledger.BB_USAGE
    ]


async def test_nothing_pulls_skips(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    pushed_only = FilamentSync(
        spools="sb_to_bb",
        weights="off",
        tags="sb_to_bb",
        presets="off",
        suppliers="sb_to_bb",
        locations="off",
    )
    with respx.mock(assert_all_called=False) as router:
        spools = router.get(f"{BB}/api/v1/inventory/spools").respond(json=[])
        assert await scheduled_pull(f, lambda: settings, lambda: pushed_only) == "skipped"
        assert not spools.called


def test_presets_off_leaves_sb_presets_alone(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    no_presets = Pulls.of(FilamentSync(presets="off"))
    report = pull(
        f,
        [bb(id=8, tray_uuid=None, tag_uid=None, slicer_filament="GFA00")],
        now=NOW,
        pulls=no_presets,
    )
    assert _own_presets(pg_pool, report.created_ids[0]) == []
    (made,) = pull(
        f, [bb(slicer_filament="GFA00", slicer_filament_name="Bambu PLA Basic")], now=NOW
    ).created_ids
    before = _own_presets(pg_pool, made)
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
        pulls=no_presets,
    )
    assert _own_presets(pg_pool, made) == before


def test_tags_off_binds_no_bb_tag_on_a_linked_spool(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(tray_uuid=None, tag_uid=None)], now=NOW).created_ids
    no_tags = Pulls.of(FilamentSync(tags="off"))
    report = pull(
        f,
        [
            bb(tray_uuid=None, tag_uid="04AABB", updated_at="2026-11-14T11:00:00"),
            bb(id=8, tray_uuid=TRAY, tag_uid=None),
        ],
        now=NOW,
        pulls=no_tags,
    )
    assert f.spools.open_binding(made) is None
    # A new BB spool still takes its tags: they are what it is (Decision 6).
    (new,) = report.created_ids
    assert f.spools.open_binding(new) is not None


def test_weights_two_way_books_bb_readings(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    both = Pulls.of(FilamentSync(weights="two_way"))
    (made,) = pull(f, [bb()], now=NOW, pulls=both).created_ids
    weighed = bb(
        weight_used=300.0,
        core_weight=250,
        last_scale_weight=700,
        last_weighed_at=_bb_time(1),
        updated_at=_bb_time(1),
    )
    pull(f, [weighed], now=NOW, pulls=both)
    assert _bb_readings(f, made) == [900, 700]
    assert [e.ref_kind for e in f.spools.events(made) if e.kind == "weighed"] == ["bb_scale"]


def test_a_tag_bb_set_while_tags_did_not_pull_is_bound_once_they_do(pg_pool: PgPool) -> None:
    """#6265 concern 3: BB's values are recorded as seen only for the data types that pull,
    so a type switched on later takes what BB changed meanwhile, with BB's row unmoved."""
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(tray_uuid=None, tag_uid=None)], now=NOW).created_ids
    # BB moves a spools field with the tag, so the gated pull records a reading.
    tagged = bb(
        tray_uuid=None, tag_uid="04AABB", color_name="Blue", updated_at="2026-11-14T11:00:00"
    )
    pull(f, [tagged], now=NOW, pulls=Pulls(tags=False))
    assert f.spools.open_binding(made) is None
    report = pull(f, [tagged], now=NOW, pulls=Pulls.of(FilamentSync(tags="bb_to_sb")))
    assert report.updated_ids == [made]
    assert f.spools.open_binding(made) is not None
    (last, *_) = reversed(_synced_events(f, made))
    assert last.data["bb"]["tag_uid"] == "04AABB"
    # Seen now: the next pull reads nothing again.
    events = len(f.spools.events(made))
    assert pull(f, [tagged], now=NOW).updated_ids == []
    assert len(f.spools.events(made)) == events


def test_a_weight_bb_moved_while_weights_did_not_pull_is_booked_once_they_do(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb()], now=NOW).created_ids
    used = bb(weight_used=300.0, updated_at=_bb_time(1))
    pull(f, [used], now=NOW, pulls=Pulls(weights=False))
    assert _bb_readings(f, made) == [900]
    pull(f, [used], now=NOW)
    assert _bb_readings(f, made) == [900, 700]


def _linked_while_weights_off(f: Filament, pool: PgPool, row: BbSpool) -> UUID:
    held = f.spools.create(SpoolCreate(material_id=_pla(pool)), actor="ann")
    f.spools.bind_tag(held.id, tray_uuid=TRAY, kind="bambu_mifare", actor="ann")
    pull(f, [row], now=NOW, pulls=Pulls(weights=False))
    spool = f.spools.get(held.id)
    assert spool is not None
    assert spool.grams_left is None
    assert not spool.weight_locked
    return held.id


def test_a_link_made_while_weights_did_not_pull_takes_the_weight_later(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    row = bb(weight_used=300.0)
    held = _linked_while_weights_off(f, pg_pool, row)
    pull(f, [row], now=NOW)
    spool = f.spools.get(held)
    assert spool is not None
    assert spool.grams_left == 700
    assert _bb_readings(f, held) == [700]


def test_a_link_made_while_weights_did_not_pull_takes_the_lock_later(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    row = bb(weight_used=300.0, weight_locked=True)
    held = _linked_while_weights_off(f, pg_pool, row)
    pull(f, [row], now=NOW)
    spool = f.spools.get(held)
    assert spool is not None
    assert spool.weight_locked


def test_a_link_made_while_presets_did_not_pull_takes_the_preset_later(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    held = f.spools.create(SpoolCreate(material_id=_pla(pg_pool)), actor="ann")
    f.spools.bind_tag(held.id, tray_uuid=TRAY, kind="bambu_mifare", actor="ann")
    row = bb(slicer_filament="GFA05", slicer_filament_name="Bambu PLA Silk")
    pull(f, [row], now=NOW, pulls=Pulls(presets=False))
    assert _own_presets(pg_pool, held.id) == []
    pull(f, [row], now=NOW)
    assert {p[1] for p in _own_presets(pg_pool, held.id)} == {"GFA05"}


def test_a_preset_bb_moved_while_presets_did_not_pull_is_taken_once_they_do(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    (made,) = pull(
        f, [bb(slicer_filament="GFA00", slicer_filament_name="Bambu PLA Basic")], now=NOW
    ).created_ids
    silk = bb(
        slicer_filament="GFA05",
        slicer_filament_name="Bambu PLA Silk",
        updated_at="2026-11-14T11:00:00",
    )
    pull(f, [silk], now=NOW, pulls=Pulls(presets=False))
    assert {p[1] for p in _own_presets(pg_pool, made)} == {"GFA00"}
    pull(f, [silk], now=NOW)
    assert {p[1] for p in _own_presets(pg_pool, made)} == {"GFA05"}


def test_a_moved_bb_row_with_spools_off_counts_no_update(pg_pool: PgPool) -> None:
    """#6265 concern 4: a BB row counts as ``updated`` only when a field of the SB spool
    changed, not because BB moved it."""
    f = _filament(pg_pool)
    (made,) = pull(f, [bb()], now=NOW).created_ids
    before = len(f.spools.events(made))
    report = pull(
        f,
        [bb(color_name="Blue", rgba="0000FFFF", updated_at="2026-11-14T11:00:00")],
        now=NOW,
        pulls=Pulls(spools=False),
    )
    assert (report.updated, report.updated_ids) == (0, [])
    assert len(f.spools.events(made)) == before


def test_weights_gates_the_lock_and_the_drying(pg_pool: PgPool) -> None:
    """#6265 concern 5, Decision 14: ``weight_locked`` and ``last_dried_at`` are
    ``weights`` data: they move with ``weights`` pulling, ``spools`` off or not."""
    f = _filament(pg_pool)
    (made,) = pull(f, [bb()], now=NOW).created_ids
    moved = bb(weight_locked=True, last_dried_at=_bb_time(-1), updated_at=_bb_time(1))
    pull(f, [moved], now=NOW, pulls=Pulls(weights=False))
    spool = f.spools.get(made)
    assert spool is not None
    assert not spool.weight_locked
    assert [e for e in f.spools.events(made) if e.kind == "dried"] == []
    pull(f, [moved], now=NOW, pulls=Pulls(spools=False))
    spool = f.spools.get(made)
    assert spool is not None
    assert spool.weight_locked
    assert [e.kind for e in f.spools.events(made) if e.kind == "dried"] == ["dried"]


# -- #6265 Task 9: "newer change wins", the seen copy, intents and the push ---------------


def test_a_bb_edit_older_than_an_sb_type_edit_is_not_pulled(pg_pool: PgPool) -> None:
    """Decision 8, F12: an inherited field's SB change time counts its type's
    ``updated_at``, on the pull as on the push."""
    f = _filament(pg_pool)
    kind = f.types.create(
        SpoolTypeCreate(
            code="GFA00-RED",
            material_id=_pla(pg_pool),
            rgba="#FF0000FF",
            label_weight_g=1000,
            colour_name="Red",
        )
    )
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_spool_types SET updated_at = now() - interval '5 hours' WHERE id = %s",
            (kind.id,),
        )
    first = bb(material_number="GFA00-RED", updated_at=_bb_time(-3))
    (made,) = pull(f, [first], now=NOW).created_ids
    f.types.update(kind.id, SpoolTypePatch(colour_name="Scarlet"), actor="ann")
    older = bb(material_number="GFA00-RED", color_name="Crimson", updated_at=_bb_time(-1))
    pull(f, [older], now=NOW)
    spool = f.spools.get(made)
    assert spool is not None and spool.effective.colour_name == "Scarlet"
    # A BB edit after the type's is taken.
    newer = bb(material_number="GFA00-RED", color_name="Maroon", updated_at=_bb_time(1))
    pull(f, [newer], now=NOW)
    spool = f.spools.get(made)
    assert spool is not None and spool.effective.colour_name == "Maroon"


def _link_of(pool: PgPool, spool: UUID) -> dict[str, Any]:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT * FROM filament_bb_links WHERE spool_id = %s", (spool,)
        ).fetchone()
    assert row is not None
    return dict(row)


def test_the_pull_writes_bb_seen_as_seen_row(pg_pool: PgPool) -> None:
    """Every pull writes ``bb_seen`` from the row it read, in `seen_row`'s form, merged by
    R-T4-28: a field SB does not hold (BB's note, which the pull never takes) stays out."""
    f = _filament(pg_pool)
    row = bb(note="from bb", updated_at=_bb_time(-2))
    (made,) = pull(f, [row], now=NOW).created_ids
    link = _link_of(pg_pool, made)
    seen = seen_row(row)
    taken = ("material", "subtype", "brand", "color_name", "rgba", "label_weight", "weight_used")
    assert {k: link["bb_seen"][k] for k in taken} == {k: seen[k] for k in taken}
    assert "note" not in link["bb_seen"]
    assert link["bb_seen_at"] == NOW
    blue = bb(note="from bb", color_name="Blue", updated_at=_bb_time(-1))
    pull(f, [blue], now=NOW + timedelta(minutes=1))
    link = _link_of(pg_pool, made)
    assert link["bb_seen"]["color_name"] == "Blue"
    assert link["bb_seen_at"] == NOW + timedelta(minutes=1)


def test_the_pull_leaves_a_spool_an_intent_claims(pg_pool: PgPool) -> None:
    """Decision 10: a BB spool SB's own create made, whose answer was lost, is the push's
    to adopt; the pull makes no second SB spool of it."""
    f = _filament(pg_pool)
    spool = _plan_spool(f, _plan_type(f))
    create = _only(_plan(f, _snap([])).writes, "create")
    assert isinstance(create.body, dict)
    row = _plan_row(60, **create.body) | {"created_at": _bb_time(0)}
    report = pull(f, [BbSpool.model_validate(row)], now=NOW)
    assert (report.claimed, report.created, report.linked) == (1, 0, 0)
    assert [s.id for s in f.spools.list()] == [spool]


@pytest.mark.parametrize("newer", ["sb", "bb"])
def test_push_then_pull_settles(pg_pool: PgPool, newer: str) -> None:
    """One field changed on both sides: after two rounds (plan, results, pull) both hold
    the newer value, and a third round writes nothing on either side."""
    f = _filament(pg_pool)
    spool, _, answer = _created(pg_pool, f)
    two_way = SPOOLS.model_copy(update={"spools": "two_way"})
    if newer == "sb":
        row = answer | {"color_name": "Teal", "updated_at": _bb_now()}
        f.spools.update(spool, SpoolPatch(colour_name="Brick"), actor="person")
    else:
        f.spools.update(spool, SpoolPatch(colour_name="Brick"), actor="person")
        row = answer | {"color_name": "Teal", "updated_at": _bb_now(60)}
    want = "Brick" if newer == "sb" else "Teal"
    minutes = 0

    def one_round(row: dict[str, Any]) -> tuple[dict[str, Any], int, int, int]:
        nonlocal minutes
        writes = _plan(f, _snap([row]), two_way).writes
        results = []
        for write in writes:
            minutes += 1
            row = row | dict(write.body) | {"updated_at": _bb_now(60 + minutes)}
            results.append(_ok(write, row))
        if writes:
            _apply(f, writes, results)
        events = len(f.spools.events(spool))
        report = pull(f, [BbSpool.model_validate(row)], now=datetime.now(UTC))
        return row, len(writes), report.updated, len(f.spools.events(spool)) - events

    for _ in range(2):
        row, *_ = one_round(row)
    sb = f.spools.get(spool)
    assert sb is not None
    assert (sb.colour_name, row["color_name"]) == (want, want)
    assert one_round(row)[1:] == (0, 0, 0)


def test_a_persons_edit_the_push_did_not_send_is_pulled(pg_pool: PgPool) -> None:
    """R-T5-5: the push's echo event holds Bambuddy's whole answer, so a person's
    Bambuddy edit of a field the push did not send is read from ``bb_seen``."""
    f = _filament(pg_pool)
    spool, _, answer = _created(pg_pool, f)
    two_way = SPOOLS.model_copy(update={"spools": "two_way"})
    person = answer | {"color_name": "Teal", "updated_at": _bb_now(1)}
    f.spools.update(spool, SpoolPatch(note="mine"), actor="ann")
    update = _only(_plan(f, _snap([person]), two_way).writes, "update")
    assert update.body == {"note": "mine"}
    after = person | update.body | {"updated_at": _bb_now(2)}
    _apply(f, [update], [_ok(update, after)])
    pull(f, [BbSpool.model_validate(after)], now=datetime.now(UTC))
    sb = f.spools.get(spool)
    assert sb is not None
    assert (sb.colour_name, sb.note) == ("Teal", "mine")


def _link_times(pool: PgPool, spool: UUID) -> tuple[datetime, datetime | None]:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT bb_updated_at, bb_seen_at FROM filament_bb_links WHERE spool_id = %s",
            (spool,),
        ).fetchone()
    assert row is not None
    return row["bb_updated_at"], row["bb_seen_at"]


def test_a_row_older_than_the_link_is_left_alone(pg_pool: PgPool) -> None:
    """Review M1: the push's results recorded a newer Bambuddy row than one this pull
    read before it. The stale row takes nothing, books nothing and moves no link time."""
    f = _filament(pg_pool)
    (spool,) = pull(f, [bb(updated_at=_bb_time(-3))], now=NOW).created_ids
    newer = datetime.now(UTC) - timedelta(hours=1)
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_bb_links SET bb_updated_at = %s WHERE spool_id = %s",
            (newer, spool),
        )
    seen, events = _link_seen(pg_pool, spool), _bb_events(f, spool)

    stale = bb(updated_at=_bb_time(-2), weight_used=400.0, color_name="Blue")
    report = pull(f, [stale], now=NOW + timedelta(hours=1))

    assert report.updated == 0
    assert _link_times(pg_pool, spool) == (newer, NOW)
    assert (_link_seen(pg_pool, spool), _bb_events(f, spool)) == (seen, events)
    sb = f.spools.get(spool)
    assert sb is not None and sb.colour_name == "Red"


def test_a_pull_that_sees_nothing_new_keeps_the_seen_time(pg_pool: PgPool) -> None:
    """Review M2: ``bb_seen_at`` moves only when the seen copy changes, so a running pass
    does not take every linked spool as written after its snapshot."""
    f = _filament(pg_pool)
    row = bb(updated_at=_bb_time(-3))
    (spool,) = pull(f, [row], now=NOW).created_ids
    assert _link_times(pg_pool, spool)[1] == NOW

    pull(f, [row], now=NOW + timedelta(hours=1))
    assert _link_times(pg_pool, spool)[1] == NOW

    pull(f, [bb(updated_at=_bb_time(-1), color_name="Blue")], now=NOW + timedelta(hours=2))
    assert _link_times(pg_pool, spool)[1] == NOW + timedelta(hours=2)
