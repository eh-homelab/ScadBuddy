"""A filament inventory at prod's size for the Bambuddy sync (#6902): about 110 spools
(archived ones too), each with a long ledger, a type with presets and K values, supplier
products with prices, places on shelves and in AMS trays, and every spool linked. Prod held
about 110 spools and 100k ledger events on 2026-11-21 (the issue's evidence).

`build` makes it on a migrated pool and answers the Bambuddy snapshot that matches it, in
which every open spool differs from SB's wish in its note, its K-profiles and its supplier
links (so a full ``two_way`` plan has more than 200 writes), and a third of them also in a
colour name Bambuddy changed since SB last saw it (so the plan asks when SB changed it)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from scadbuddy.bambuddy.models import BbLocation, BbSupplier, CatalogFilament, SpoolCatalogEntry
from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament import ledger
from scadbuddy.filament.bb_sync_models import BbSnapshot, BbWrite, BbWriteResult, seen_row
from scadbuddy.filament.bb_sync_plan import (
    ROW_KINDS,
    BbRefs,
    body_value,
    compare_form,
    desired_spool,
    field_hash,
)
from scadbuddy.filament.component import Filament
from scadbuddy.filament.models import LocationKind, Material, SpoolCreate, SpoolTypeCreate
from scadbuddy.filament.purchasing_models import PricePointCreate, ProductCreate, SupplierCreate
from tests.conftest import PgPool
from tests.filament.test_bb_sync_plan import INSTANCE, _row

#: Every data type ``two_way``: the setting prod had when the passes timed out.
TWO_WAY = FilamentSync(
    spools="two_way",
    weights="two_way",
    tags="two_way",
    presets="two_way",
    suppliers="two_way",
    locations="two_way",
)
SPOOLS = 110
ARCHIVED = 20
TYPES = 25
#: Ledger events per spool: 110 x 900 is about prod's 100k.
EVENTS_PER_SPOOL = 900
_LONG_AGO = datetime(2026, 5, 1, tzinfo=UTC)
#: Bambuddy's ``updated_at`` of every row: before SB made its spools.
_BB_AT = datetime(2026, 10, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Scale:
    snap: BbSnapshot
    spools: list[UUID]


def _kind(f: Filament, name: str) -> UUID:
    rows = f.catalog.list("location-kinds")
    return next(r.id for r in rows if isinstance(r, LocationKind) and r.name == name)


def build(
    pool: PgPool,
    f: Filament,
    *,
    spools: int = SPOOLS,
    archived: int = ARCHIVED,
    events: int = EVENTS_PER_SPOOL,
) -> Scale:
    """The inventory and its snapshot, as the module says; a test that needs no ledger
    of prod's size passes fewer ``spools`` and ``events`` a spool."""
    pla = next(
        r.id for r in f.catalog.list("materials") if isinstance(r, Material) and r.name == "PLA"
    )
    printers = [uuid4(), uuid4()]
    with pool.connection() as conn:
        for n, (printer, model) in enumerate(zip(printers, ("H2C", "P1S"), strict=True)):
            conn.execute(
                "INSERT INTO printers (id, serial, name, model, bambuddy_printer_id)"
                " VALUES (%s, %s, %s, %s, %s)",
                (printer, f"SN{printer.hex[:8]}", f"P{n}", model, n + 1),
            )
        inserts = [uuid4(), uuid4()]
        for n, insert in enumerate(inserts):
            conn.execute(
                "INSERT INTO filament_insert_types (id, name, empty_weight_g, bb_catalog_id)"
                " VALUES (%s, %s, %s, %s)",
                (insert, f"Insert {n}", 200 + n * 50, 500 + n),
            )
    types = []
    for n in range(TYPES):
        made = f.types.create(
            SpoolTypeCreate(
                code=f"GFA{n:02d}-T",
                material_id=pla,
                rgba=f"#{n * 9:02X}{255 - n * 9:02X}40FF",
                colour_name=f"Colour {n}",
                label_weight_g=1000,
                insert_type_id=inserts[n % 2],
                nozzle_temp_min=190,
                nozzle_temp_max=230,
                cost_per_kg=Decimal("19.99"),
            )
        )
        types.append(made.id)
    suppliers = [f.suppliers.create(SupplierCreate(name=f"Shop {n}")).id for n in range(4)]
    for n, type_id in enumerate(types):
        for k, supplier in enumerate(suppliers[: 1 + n % 3]):
            product = f.suppliers.add_product(
                supplier, ProductCreate(type_id=type_id, sku=f"SKU-{n}-{k}")
            )
            assert product is not None
            for price in ("21.99", "19.49"):
                f.suppliers.add_price(product.id, PricePointCreate(price=Decimal(price)))
    with pool.connection() as conn:
        for n, type_id in enumerate(types):
            for model in ("H2C", "P1S"):
                for nozzle in ("0.4", "0.6"):
                    conn.execute(
                        "INSERT INTO filament_spool_type_presets (type_id, printer_model,"
                        " nozzle_diameter, slicer_filament, slicer_filament_name)"
                        " VALUES (%s, %s, %s, %s, %s)",
                        (type_id, model, nozzle, f"GFL{n:02d}", f"Preset {n} {model}"),
                    )
            for printer in printers:
                for extruder in (0, 1):
                    conn.execute(
                        "INSERT INTO filament_spool_type_k_values (type_id, printer_id,"
                        " extruder, nozzle_diameter, k_value, name)"
                        " VALUES (%s, %s, %s, '0.4', %s, 'K')",
                        (type_id, printer, extruder, Decimal("0.02") + Decimal(n) / 1000),
                    )
        shelves = []
        for n in range(5):
            shelf = uuid4()
            conn.execute(
                "INSERT INTO filament_locations (id, name, kind_id, bb_location_id)"
                " VALUES (%s, %s, %s, %s)",
                (shelf, f"Shelf {n}", _kind(f, "Shelf"), 300 + n),
            )
            shelves.append(shelf)
        trays = []
        for n in range(8):
            tray = uuid4()
            conn.execute(
                "INSERT INTO filament_locations (id, name, kind_id, printer_id, ams_id, tray_id)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (tray, f"AMS {n}", _kind(f, "AMS"), printers[n % 2], n // 4, n % 4),
            )
            trays.append(tray)
        for n, supplier in enumerate(suppliers):
            conn.execute(
                "UPDATE filament_suppliers SET bb_supplier_id = %s WHERE id = %s",
                (400 + n, supplier),
            )
        for n, type_id in enumerate(types):
            conn.execute(
                "UPDATE filament_spool_types SET bb_filament_id = %s WHERE id = %s",
                (600 + n, type_id),
            )
    made_spools: list[UUID] = []
    for n in range(spools):
        spool = f.spools.create(
            SpoolCreate(type_id=types[n % TYPES], grams_left=1000.0, note=f"SB note {n}"),
            actor="person",
        ).id
        made_spools.append(spool)
    with pool.connection() as conn, conn.transaction():
        for n, spool in enumerate(made_spools):
            place = trays[n % 8] if n < 8 else shelves[n % 5]
            conn.execute(
                "UPDATE filament_spools SET location_id = %s WHERE id = %s", (place, spool)
            )
            if n % 4 == 0:
                conn.execute(
                    "INSERT INTO filament_spool_k_values (spool_id, printer_id, extruder,"
                    " nozzle_diameter, k_value, name) VALUES (%s, %s, 0, '0.4', 0.031, 'own')",
                    (spool, printers[0]),
                )
            if n % 5 == 0:
                conn.execute(
                    "INSERT INTO filament_spool_presets (spool_id, printer_model,"
                    " nozzle_diameter, slicer_filament, slicer_filament_name)"
                    " VALUES (%s, 'H2C', '0.4', 'OWN', 'Own preset')",
                    (spool,),
                )
        _ledgers(conn, made_spools, printers[0], shelves, events)
        for spool in made_spools:
            ledger.refresh_derived(conn, spool)
        for spool in made_spools[spools - archived :]:
            conn.execute(
                "UPDATE filament_spools SET archived_at = %s WHERE id = %s", (_BB_AT, spool)
            )
    return Scale(snap=_link(pool, made_spools), spools=made_spools)


def _ledgers(
    conn: Any, spools: list[UUID], printer: UUID, shelves: list[UUID], events: int
) -> None:
    """``events`` events per spool over six months: mostly AMS readings, with
    plates (a start and an end with grams), weighings and moves between shelves."""
    conn.execute(
        "INSERT INTO filament_spool_events (spool_id, at, kind, source, printer_id,"
        " grams_after, delta_g, data)"
        " SELECT s.id, %s + (g * interval '4 hours'),"
        " CASE WHEN g %% 10 = 0 THEN 'print_ended' WHEN g %% 10 = 1 THEN 'print_started'"
        "  WHEN g %% 50 = 2 THEN 'weighed' ELSE 'ams_reading' END,"
        " CASE WHEN g %% 10 = 0 THEN 'print_actual' WHEN g %% 10 = 1 THEN 'print_estimate'"
        "  WHEN g %% 50 = 2 THEN 'scale' ELSE 'ams_remain' END,"
        " %s,"
        " CASE WHEN g %% 10 IN (0, 1) THEN NULL ELSE 1000 - (g %% 700) END,"
        " CASE WHEN g %% 10 = 0 THEN -3.5 WHEN g %% 10 = 1 THEN -3.0 ELSE NULL END,"
        " '{}'::jsonb"
        " FROM unnest(%s::uuid[]) AS s(id), generate_series(1, %s) AS g",
        (_LONG_AGO, printer, spools, events - 10),
    )
    conn.execute(
        "INSERT INTO filament_spool_events (spool_id, at, kind, data)"
        " SELECT s.id, %s + (g * interval '13 days'), 'moved',"
        " jsonb_build_object('from', (%s::uuid[])[1 + g %% 5],"
        " 'to', (%s::uuid[])[1 + (g + 1) %% 5])"
        " FROM unnest(%s::uuid[]) AS s(id), generate_series(1, 9) AS g",
        (_LONG_AGO, shelves, shelves, spools),
    )


def _link(pool: PgPool, spools: list[UUID]) -> BbSnapshot:
    """Link every spool to a Bambuddy row and answer the snapshot, as the module says."""
    rows: list[dict[str, Any]] = []
    with pool.connection() as conn, conn.transaction():
        refs = BbRefs.read(conn)
        for n, spool in enumerate(spools):
            desired = desired_spool(conn, spool, refs, TWO_WAY)
            open_ = desired["archived_at"] is None
            body = {
                name: body_value(name, value)
                for name, value in desired.items()
                if name not in ("archived_at", "k_profiles", "filament_presets", "suppliers")
            }
            over: dict[str, Any] = {
                "k_profiles": [],
                "suppliers": [],
                "updated_at": _BB_AT.replace(tzinfo=None).isoformat(),
                "archived_at": None if open_ else _BB_AT.replace(tzinfo=None).isoformat(),
            }
            if open_:
                over["note"] = f"BB note {n}"
            seen_over: dict[str, Any] = {}
            if open_ and n % 3 == 0:
                over["color_name"] = f"BB colour {n}"
                seen_over["color_name"] = f"Old colour {n}"
            row = _row(1000 + n, **(body | over))
            bb = BbSpool.model_validate(row)
            seen = seen_row(bb) | seen_over
            pushed = {
                name: field_hash(compare_form(name, value))
                for name, value in desired.items()
                if name not in ("note", "k_profiles", "suppliers", "color_name")
            } | {"filament_presets": field_hash(compare_form("filament_presets", []))}
            conn.execute(
                "INSERT INTO filament_bb_links (spool_id, bb_spool_id, bb_updated_at,"
                " pushed_fields, bb_seen, bb_seen_at, bb_k_profiles, bb_seeded)"
                " VALUES (%s, %s, %s, %s, %s, %s, '[]', true)",
                (spool, 1000 + n, _BB_AT, Jsonb(pushed), Jsonb(seen), _BB_AT),
            )
            rows.append(row)
        conn.execute("UPDATE filament_bb_sync_state SET instance = %s", (INSTANCE,))
        listings = {kind: ROW_KINDS[kind].items(conn) for kind in ROW_KINDS}
    return BbSnapshot(
        instance=INSTANCE,
        full=True,
        spools=rows,
        spools_complete=True,
        suppliers=[BbSupplier(id=i.bb_id or 0, **i.desired) for i in listings["supplier"]],
        locations=[BbLocation(id=i.bb_id or 0, **i.desired) for i in listings["location"]],
        catalog=[SpoolCatalogEntry(id=i.bb_id or 0, **i.desired) for i in listings["insert_type"]],
        filaments=[
            CatalogFilament(
                id=i.bb_id or 0,
                **{k: v for k, v in i.desired.items() if k in CatalogFilament.model_fields},
            )
            for i in listings["filament"]
        ],
        read_at=datetime.now(UTC) + timedelta(seconds=1),
    )


def ok_results(snap: BbSnapshot, writes: Sequence[BbWrite], at: datetime) -> list[BbWriteResult]:
    """Bambuddy's answer to each of a plan's ``writes`` (updates and list ``PUT``s), as if
    it took them all at ``at``."""
    spools = {row["id"]: row for row in snap.spools}
    listings: dict[str, Sequence[Any]] = {
        "supplier": snap.suppliers or [],
        "location": snap.locations or [],
        "insert_type": snap.catalog or [],
        "filament": snap.filaments or [],
    }
    stamp = at.replace(tzinfo=None).isoformat()
    out = []
    for write in writes:
        answer: dict[str, Any]
        if write.op == "update" and write.kind != "spool":
            row = next(r for r in listings[write.kind] if r.id == write.bb_id)
            answer = row.model_dump(mode="json") | dict(cast("dict[str, Any]", write.body))
        elif write.op == "update":
            body = cast("dict[str, Any]", write.body)
            answer = spools[write.bb_id] | body | {"updated_at": stamp}
        elif write.op in ("k_profiles", "suppliers"):
            answer = {write.op: write.body}
        elif write.op == "presets":
            answer = {}
        else:
            raise AssertionError(f"no answer for {write.op}")
        out.append(BbWriteResult(key=write.key, ok=True, bb_id=write.bb_id, answer=answer))
    return out


@dataclass
class Counts:
    """The statements and the top-level transactions run while `counting` is open."""

    statements: int = 0
    transactions: int = 0


@contextmanager
def counting() -> Iterator[Counts]:
    """Count every ``Connection.execute`` and every transaction a connection opens
    outside one, in this process."""
    counts = Counts()
    execute = psycopg.Connection.execute
    transaction = psycopg.Connection.transaction

    def counted_execute(self: Any, *args: Any, **kwargs: Any) -> Any:
        counts.statements += 1
        return execute(self, *args, **kwargs)

    def counted_transaction(self: Any, *args: Any, **kwargs: Any) -> Any:
        if self.info.transaction_status == psycopg.pq.TransactionStatus.IDLE:
            counts.transactions += 1
        return transaction(self, *args, **kwargs)

    psycopg.Connection.execute = counted_execute  # type: ignore[method-assign]
    psycopg.Connection.transaction = counted_transaction  # type: ignore[method-assign]
    try:
        yield counts
    finally:
        psycopg.Connection.execute = execute  # type: ignore[method-assign]
        psycopg.Connection.transaction = transaction  # type: ignore[method-assign]
