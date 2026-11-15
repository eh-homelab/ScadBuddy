"""A printer merge moves the filament inventory's references (#6261): the AMS tray
places and the K values of the gone printer go to the kept one; where both have one,
the kept printer's stands."""

from __future__ import annotations

import uuid
from uuid import UUID

import pytest

from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.locations import MERGE_ACTOR, LocationStore
from scadbuddy.filament.models import Material, SpoolCreate
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.printers.models import PrinterCreate
from scadbuddy.printers.store import PrinterStore
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

SERIAL = "01P00A000006261"


def _merge(printers: PrinterStore) -> tuple[UUID, UUID]:
    """A synthetic Bambuddy row and a row holding the serial; learning the serial merges
    the first into the second. Both ids, gone first."""
    synthetic = printers.ensure_bambuddy(7, serial=None, name="H2C", model="H2C")
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))
    return uuid.UUID(synthetic.id), uuid.UUID(mine.id)


def _material(pool: PgPool) -> UUID:
    rows = CatalogStore(pool).list("materials")
    return next(r.id for r in rows if isinstance(r, Material) and r.name == "PLA")


def test_a_tray_both_printers_have_keeps_the_kept_place_and_takes_the_spools(
    printers: PrinterStore, pg_pool: PgPool
) -> None:
    gone, kept = _merge(printers)
    places, spools = LocationStore(pg_pool), FilamentSpoolStore(pg_pool)
    clash_gone = places.ensure_ams(gone, 0, 1, label="gone A1")
    clash_kept = places.ensure_ams(kept, 0, 1, label="kept A1")
    only_gone = places.ensure_ams(gone, 0, 2, label="gone A2")
    spool = spools.create(SpoolCreate(material_id=_material(pg_pool)), actor="t")
    places.move(spool.id, clash_gone.id, actor="t")

    printers.ensure_bambuddy(7, serial=SERIAL, name="H2C", model="H2C")

    moved = spools.get(spool.id)
    assert moved is not None and moved.location_id == clash_kept.id
    last = spools.events(spool.id)[-1]
    assert (last.kind, last.actor) == ("moved", MERGE_ACTOR)
    assert last.data["from"] == str(clash_gone.id) and last.data["to"] == str(clash_kept.id)
    archived = places.get(clash_gone.id)
    assert archived is not None and archived.archived_at is not None
    assert archived.printer_id == kept
    stays = places.get(clash_kept.id)
    assert stays is not None and stays.archived_at is None and stays.name == "kept A1"
    taken = places.get(only_gone.id)
    assert taken is not None and taken.printer_id == kept and taken.archived_at is None


def test_a_k_value_both_printers_have_keeps_the_kept_one(
    printers: PrinterStore, pg_pool: PgPool
) -> None:
    gone, kept = _merge(printers)
    spool = FilamentSpoolStore(pg_pool).create(
        SpoolCreate(material_id=_material(pg_pool)), actor="t"
    )
    with pg_pool.connection() as conn:
        kind = conn.execute(
            "INSERT INTO filament_spool_types (code, material_id, rgba, label_weight_g)"
            " VALUES ('T6261', %s, '#FFFFFFFF', 1000) RETURNING id",
            (_material(pg_pool),),
        ).fetchone()
        assert kind is not None
        for table, owner, who in (
            ("filament_spool_type_k_values", "type_id", kind["id"]),
            ("filament_spool_k_values", "spool_id", spool.id),
        ):
            for printer, nozzle, k in (
                (gone, "0.4", 0.02),
                (kept, "0.4", 0.03),
                (gone, "0.6", 0.04),
            ):
                conn.execute(
                    f"INSERT INTO {table} ({owner}, printer_id, extruder, nozzle_diameter,"
                    " k_value) VALUES (%s, %s, 0, %s, %s)",
                    (who, printer, nozzle, k),
                )

    printers.ensure_bambuddy(7, serial=SERIAL, name="H2C", model="H2C")

    with pg_pool.connection() as conn:
        for table in ("filament_spool_type_k_values", "filament_spool_k_values"):
            rows = conn.execute(
                f"SELECT printer_id, nozzle_diameter, k_value::float AS k FROM {table}"
                " ORDER BY nozzle_diameter"
            ).fetchall()
            assert [(r["printer_id"], r["nozzle_diameter"], r["k"]) for r in rows] == [
                (kept, "0.4", 0.03),
                (kept, "0.6", 0.04),
            ], table
