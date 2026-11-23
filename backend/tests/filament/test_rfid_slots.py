"""The AMS slot of an override tag (#6264 Task 6): `filament/rfid_slots.py`. Bambuddy is
respx; the route is the one the recording names
(`tests/bambuddy/recordings/openapi/rfid-routes.json`). The printer's report is the
recorded H2C idle report (`tests/printers/fixtures/lan-report-h2c-idle.json`) with one tray
put in AMS 0, whose unit rests on inlet A: the left extruder, a 0.4 nozzle."""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.models import SlotConfigure
from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.bambuddy.trays import seen_trays
from scadbuddy.core.problems import ApiError
from scadbuddy.filament.bambuddy_pull import pull
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.component import Filament
from scadbuddy.filament.inventory import FilamentInventory
from scadbuddy.filament.invoices import InvoiceStore
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.metrics_store import MetricsSettings, MetricsStore
from scadbuddy.filament.models import (
    Material,
    SlotReport,
    SpoolCreate,
    SpoolTypeCreate,
    TypeKValue,
    TypePreset,
)
from scadbuddy.filament.presets import PoolSpoolPresets
from scadbuddy.filament.rfid_mode import RfidModeStore
from scadbuddy.filament.rfid_push import error_label, try_lock
from scadbuddy.filament.rfid_slots import (
    SLOTS_LOCK,
    installed_nozzle,
    reconcile_slots,
    reconcile_slots_now,
)
from scadbuddy.filament.sb_presets import create_on
from scadbuddy.filament.sensors import SensorStore
from scadbuddy.filament.shopping import ShoppingStore
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.suppliers import SuppliersStore
from scadbuddy.filament.types import SpoolTypeStore
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.reports import PrinterReportStore, report_status
from scadbuddy.printers.store import PrinterStore
from scadbuddy.slicing.own_presets import sb_ref
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

BASE = "https://bambuddy.test"
API = f"{BASE}/api/v1"
CONFIGURE = f"{API}/printers/1/slots/0/1/configure"
TRAY = "0123456789ABCDEF0123456789ABCDEF"
FIXTURES = Path(__file__).parent.parent / "printers" / "fixtures"


def _filament(pool: PgPool) -> Filament:
    types = SpoolTypeStore(pool)
    spools = FilamentSpoolStore(pool)
    return Filament(
        catalog=CatalogStore(pool),
        types=types,
        spools=spools,
        locations=LocationStore(pool),
        inventory=FilamentInventory(pool),
        presets=PoolSpoolPresets(pool),
        sensors=SensorStore(pool),
        rfid=RfidModeStore(pool),
        suppliers=SuppliersStore(pool, lambda: "USD"),
        invoices=InvoiceStore(pool, lambda: "USD", spools, types),
        shopping=ShoppingStore(pool),
        metrics=MetricsStore(pool, spools, MetricsSettings),
    )


@pytest.fixture
async def client() -> AsyncIterator[BambuddyClient]:
    async with BambuddyClient(BambuddyConfig(base_url=BASE, api_key="s3cret")) as bambuddy:
        yield bambuddy


def _report(**tray: object) -> dict[str, Any]:
    """The idle H2C report with one tray in AMS 0, tray 1; ``tray`` overrides its keys."""
    data: dict[str, Any] = json.loads((FIXTURES / "lan-report-h2c-idle.json").read_text())["print"]
    shown = {
        "id": "1",
        "tray_color": "FF0000FF",
        "tray_type": "PLA",
        "tray_info_idx": "GFA00",
        "tray_uuid": TRAY,
        "tag_uid": "0000000000000000",
        "state": 11,
        **tray,
    }
    data = copy.deepcopy(data)
    data["ams"]["ams"][0]["tray"] = [shown]
    return data


class World:
    def __init__(self, pool: PgPool, *, temps: bool = True) -> None:
        self.pool = pool
        self.filament = _filament(pool)
        self.printers = PrinterStore(pool, None)
        self.reports = PrinterReportStore(pool)
        self.printer = uuid4()
        with pool.connection() as conn:
            conn.execute(
                "INSERT INTO printers (id, serial, name, bambuddy_printer_id, lan_host)"
                " VALUES (%s, %s, 'Garage', 1, '10.0.0.5')",
                (self.printer, f"SN{self.printer.hex[:8]}"),
            )
        self.report(_report())
        material = next(
            r.id
            for r in CatalogStore(pool).list("materials")
            if isinstance(r, Material) and r.name == "PLA"
        )
        self.type_id = self.filament.types.create(
            SpoolTypeCreate(
                code="ACME-PLA",
                material_id=material,
                label_weight_g=1000,
                rgba="#FF0000FF",
                nozzle_temp_min=190 if temps else None,
                nozzle_temp_max=240 if temps else None,
            )
        ).id
        self.preset("0.4", "GFL99")
        self.spool = self.filament.spools.create(SpoolCreate(type_id=self.type_id), actor="ann").id
        self.bind(override=True)

    def report(self, data: dict[str, Any]) -> None:
        self.reports.put(str(self.printer), data, datetime.now(UTC))

    def preset(self, nozzle: str, filament: str) -> None:
        self.filament.types.set_presets(
            self.type_id,
            [
                TypePreset(
                    type_id=self.type_id,
                    printer_model="H2C",
                    nozzle_diameter=nozzle,
                    slicer_filament=filament,
                    slicer_filament_name=None,
                )
            ],
            actor="ann",
        )

    def bind(self, *, override: bool) -> None:
        self.filament.spools.bind_tag(
            self.spool, tray_uuid=TRAY, kind="bambu_mifare", override=override, actor="ann"
        )

    def rows(self) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            return list(conn.execute("SELECT * FROM filament_override_slots"))

    async def run(self, client: BambuddyClient) -> SlotReport:
        return await reconcile_slots(
            self.filament, client, printers=self.printers, reports=self.reports
        )


def _configure(status: int = 200) -> respx.Route:
    return respx.post(CONFIGURE).mock(return_value=httpx.Response(status, json={}))


def _status(fixture: str) -> tuple[Any, Any]:
    data = json.loads((FIXTURES / fixture).read_text())["print"]
    at = datetime.now(UTC)
    row = PrinterRow(id=str(uuid4()), serial="SN", name="P", created_at=at, updated_at=at)
    return report_status(row, (data, datetime.now(UTC)), now=datetime.now(UTC)), data


def test_the_installed_nozzle_is_the_one_of_the_extruder_the_ams_rests_on() -> None:
    status, idle = _status("lan-report-h2c-idle.json")
    # Units 0 and 3 rest on inlet A (left, extruder 1: 0.4); units 1 and 2 on B (right: 0.6).
    assert installed_nozzle(status, idle, 0) == "0.4"
    assert installed_nozzle(status, idle, 1) == "0.6"
    assert installed_nozzle(status, idle, 9) is None
    busy_status, busy = _status("lan-report-h2c.json")
    assert installed_nozzle(busy_status, busy, 0) is None


def test_the_nozzle_is_read_by_extruder_id_not_by_position() -> None:
    status, idle = _status("lan-report-h2c-idle.json")
    idle["device"]["nozzle"]["info"] = [
        e for e in idle["device"]["nozzle"]["info"] if e["id"] != 0
    ]  # only id 1 (0.4) is listed: extruder 0 has no nozzle, and unit 1 rests on extruder 0
    assert installed_nozzle(status, idle, 1) is None
    assert installed_nozzle(status, idle, 0) == "0.4"


async def test_an_override_tag_resolves_through_trays(pg_pool: PgPool) -> None:
    w = World(pg_pool)
    row = w.printers.require(str(w.printer))
    stored = w.reports.get(str(w.printer))
    assert stored is not None

    seen = seen_trays(report_status(row, stored, now=datetime.now(UTC)))
    view = w.filament.inventory.trays(w.printer, seen)

    holder = next(t for t in view.trays if (t.ams_id, t.tray_id) == (0, 1))
    assert holder.spool is not None and holder.spool.id == w.spool
    assert holder.found_by == "tag"


@respx.mock
async def test_an_override_slot_is_configured_with_the_types_preset(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    w = World(pg_pool)
    route = _configure()

    report = await w.run(client)

    assert route.call_count == 1
    query = dict(route.calls[0].request.url.params)
    assert query["tray_info_idx"] == "GFL99"
    assert query["tray_color"] == "FF0000FF"
    assert query["nozzle_diameter"] == "0.4"
    assert query["tray_type"] == "PLA"
    assert query["cali_idx"] == "-1"
    assert report.configured == 1
    (row,) = w.rows()
    assert row["spool_id"] == w.spool and row["params_hash"] and row["configured_at"]


@respx.mock
async def test_the_k_value_of_the_extruder_the_ams_feeds_is_sent(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    w = World(pg_pool)
    w.filament.types.set_k_values(
        w.type_id,
        [
            TypeKValue(
                type_id=w.type_id,
                printer_id=w.printer,
                extruder=ext,
                nozzle_diameter="0.4",
                nozzle_type=None,
                k_value=Decimal(f"0.0{ext + 2}"),
                name=None,
                cali_idx=ext + 7,
                setting_id=f"S{ext}",
            )
            for ext in (0, 1)
        ],
        actor="ann",
    )
    route = _configure()

    await w.run(client)

    query = dict(route.calls[0].request.url.params)  # unit 0 rests on the left: extruder 1
    assert (query["cali_idx"], query["setting_id"], query["k_value"]) == ("8", "S1", "0.03")


@respx.mock
async def test_a_second_reconcile_makes_no_call(pg_pool: PgPool, client: BambuddyClient) -> None:
    w = World(pg_pool)
    route = _configure()
    await w.run(client)
    w.report(_report(tray_info_idx="GFL99"))  # the AMS now shows what was sent

    report = await w.run(client)

    assert route.call_count == 1
    assert (report.unchanged, report.configured) == (1, 0)


@respx.mock
async def test_a_reloaded_tray_is_configured_again(pg_pool: PgPool, client: BambuddyClient) -> None:
    w = World(pg_pool)
    route = _configure()
    await w.run(client)
    # The hash still matches, but the AMS reloaded the tag: its own idx and material.
    w.report(_report(tray_info_idx="GFA00", tray_type="PLA Basic"))

    report = await w.run(client)

    assert route.call_count == 2
    assert (report.configured, report.unchanged) == (1, 0)


@respx.mock
async def test_a_changed_preset_configures_again(pg_pool: PgPool, client: BambuddyClient) -> None:
    w = World(pg_pool)
    route = _configure()
    await w.run(client)
    w.report(_report(tray_info_idx="GFL99"))
    w.preset("0.4", "GFL98")

    report = await w.run(client)

    assert route.call_count == 2
    assert dict(route.calls[1].request.url.params)["tray_info_idx"] == "GFL98"
    assert report.configured == 1


@respx.mock
async def test_a_plain_binding_is_not_configured(pg_pool: PgPool, client: BambuddyClient) -> None:
    w = World(pg_pool)
    w.bind(override=False)
    route = _configure()

    report = await w.run(client)

    assert route.call_count == 0
    assert report == SlotReport()


@respx.mock
async def test_an_override_turned_off_deletes_the_row_and_sends_nothing(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    w = World(pg_pool)
    route = _configure()
    await w.run(client)
    assert len(w.rows()) == 1
    w.bind(override=False)

    await w.run(client)

    assert route.call_count == 1
    assert w.rows() == []
    w.bind(override=True)  # a later override configures the slot again
    await w.run(client)
    assert route.call_count == 2


@respx.mock
async def test_a_pull_keeps_the_override_slot_and_the_next_reconcile_configures_it(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    w = World(pg_pool)
    route = _configure()
    await w.run(client)
    assert route.call_count == 1
    (before,) = w.rows()
    # The AMS reloaded the tag, so the slot needs configuring again.
    w.report(_report(tray_info_idx="GFA00", tray_type="PLA Basic"))
    now = datetime.now(UTC)
    # A changed Bambuddy spool linked to the holder is pulled: its tag keys differ.
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO filament_bb_links (spool_id, bb_spool_id, bb_updated_at, linked_at)"
            " VALUES (%s, 11, %s, %s)",
            (w.spool, now, now),
        )
    pull(
        w.filament,
        [
            BbSpool.model_validate(
                {
                    "id": 11,
                    "material": "PLA",
                    "tray_uuid": TRAY,
                    "tag_uid": None,
                    "updated_at": "2099-01-01T00:00:00",
                    "weight_used": 300.0,
                }
            )
        ],
        now=now,
    )

    binding = w.filament.spools.open_binding(w.spool)
    assert binding is not None and binding.override
    (kept,) = w.rows()
    assert kept["spool_id"] == before["spool_id"]

    report = await w.run(client)

    assert route.call_count == 2
    assert report.configured == 1


@respx.mock
async def test_a_deleted_spool_cascades_its_row_away(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    w = World(pg_pool)
    _configure()
    await w.run(client)
    assert len(w.rows()) == 1
    assert w.filament.spools.delete(w.spool)
    assert w.rows() == []


@respx.mock
async def test_a_scadbuddy_preset_is_configured_by_its_mirrors_id(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    """#7176: Bambuddy knows a ScadBuddy preset only by its mirror's local id; one with
    no mirror is no preset Bambuddy can set."""
    w = World(pg_pool)
    with pg_pool.connection() as conn, conn.transaction():
        mirrored, bare = (
            create_on(
                conn,
                name=name,
                source="manual",
                key=f"attachment-{n:064d}",
                sha256=f"{n:064d}",
                doc={"name": name},
                made_by="test",
                bb_preset_id=bb_id,
            )
            for n, name, bb_id in ((1, "Mirrored", 141), (2, "Bare", None))
        )
    route = _configure()
    w.preset("0.4", sb_ref(mirrored))
    report = await w.run(client)
    assert route.call_count == 1 and report.configured == 1
    assert dict(route.calls[0].request.url.params)["tray_info_idx"] == "141"

    w.preset("0.4", sb_ref(bare))
    report = await w.run(client)
    assert route.call_count == 1 and report.no_preset == 1


@respx.mock
async def test_no_preset_for_the_nozzle_calls_nothing(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    w = World(pg_pool)
    w.preset("0.6", "GFL99")
    route = _configure()

    report = await w.run(client)

    assert route.call_count == 0
    assert report.no_preset == 1


@respx.mock
async def test_a_tray_whose_nozzle_the_report_does_not_say_calls_nothing(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    w = World(pg_pool)
    data = _report()
    del data["ams"]["ams"][0]["info"]
    w.report(data)
    route = _configure()

    report = await w.run(client)

    assert route.call_count == 0
    assert report.no_nozzle == 1


@respx.mock
async def test_a_failed_configure_is_retried_next_time(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    w = World(pg_pool)
    route = _configure(500)

    report = await w.run(client)

    assert report.failed == 1
    (row,) = w.rows()
    assert row["attempts"] == 1 and row["params_hash"] is None
    probe = SlotConfigure(
        tray_info_idx="X",
        tray_type="PLA",
        tray_sub_brands="PLA",
        tray_color="FF0000FF",
        nozzle_temp_min=190,
        nozzle_temp_max=240,
        nozzle_diameter="0.4",
    )
    with pytest.raises(ApiError) as caught:
        await client.configure_slot(1, 0, 1, probe)
    assert row["last_error"] == error_label(caught.value)

    route.mock(return_value=httpx.Response(200, json={}))
    again = await w.run(client)
    assert again.configured == 1
    assert w.rows()[0]["attempts"] == 0 and w.rows()[0]["last_error"] is None


@respx.mock
async def test_a_spool_without_temperatures_takes_the_trays(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    w = World(pg_pool, temps=False)
    w.report(_report(nozzle_temp_min="190", nozzle_temp_max="240"))
    route = _configure()

    report = await w.run(client)

    query = dict(route.calls[0].request.url.params)
    assert (query["nozzle_temp_min"], query["nozzle_temp_max"]) == ("190", "240")
    assert report.configured == 1


@respx.mock
@pytest.mark.parametrize(
    "tray",
    [
        {},
        {"nozzle_temp_min": "0", "nozzle_temp_max": "240"},
    ],
)
async def test_no_temperatures_anywhere_calls_nothing(
    pg_pool: PgPool, client: BambuddyClient, tray: dict[str, str]
) -> None:
    w = World(pg_pool, temps=False)
    w.report(_report(**tray))
    route = _configure()

    report = await w.run(client)

    assert route.call_count == 0
    assert (report.no_temps, report.no_preset) == (1, 0)
    assert len(w.rows()) == 0


@respx.mock
async def test_a_different_spool_in_the_row_configures_again(
    pg_pool: PgPool, client: BambuddyClient
) -> None:
    w = World(pg_pool)
    route = _configure()
    await w.run(client)
    w.report(_report(tray_info_idx="GFL99"))  # the tray shows what was sent; the hash matches
    other = w.filament.spools.create(SpoolCreate(type_id=w.type_id), actor="ann").id
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_override_slots SET spool_id = %s", (other,))

    report = await w.run(client)

    assert (route.call_count, report.configured) == (2, 1)
    assert w.rows()[0]["spool_id"] == w.spool


@respx.mock
@pytest.mark.parametrize(
    "tray", [{"tray_color": "FF000080"}, {"tray_color": "00FF00FF"}, {"tray_type": "PLA Basic"}]
)
async def test_a_tray_differing_in_colour_or_material_alone_is_configured_again(
    pg_pool: PgPool, client: BambuddyClient, tray: dict[str, str]
) -> None:
    w = World(pg_pool)
    route = _configure()
    await w.run(client)
    w.report(_report(tray_info_idx="GFL99", **tray))

    report = await w.run(client)

    assert (route.call_count, report.configured, report.unchanged) == (2, 1, 0)


@respx.mock
async def test_a_stale_report_deletes_nothing(pg_pool: PgPool, client: BambuddyClient) -> None:
    w = World(pg_pool)
    route = _configure()
    await w.run(client)
    w.bind(override=False)
    with pg_pool.connection() as conn:
        conn.execute("UPDATE printer_reports SET seen_at = now() - interval '1 hour'")

    report = await w.run(client)

    assert len(w.rows()) == 1 and route.call_count == 1
    assert report == SlotReport()


@respx.mock
async def test_repeated_configures_of_unchanged_params_are_counted_and_warned(
    pg_pool: PgPool, client: BambuddyClient, caplog: pytest.LogCaptureFixture
) -> None:
    w = World(pg_pool)
    _configure()
    for _ in range(4):  # the AMS never shows what was sent
        await w.run(client)

    assert w.rows()[0]["attempts"] == 3
    assert [r for r in caplog.records if "same values" in r.message]


@respx.mock
async def test_a_held_lock_skips_the_step(pg_pool: PgPool) -> None:
    w = World(pg_pool)
    route = _configure()
    holder = try_lock(w.filament, SLOTS_LOCK)
    assert holder is not None
    try:
        skipped = await reconcile_slots_now(
            w.filament,
            lambda: StoredSettings(bambuddy_url=BASE, bambuddy_api_key="s3cret"),
            printers=w.printers,
            reports=w.reports,
        )
    finally:
        holder.close()
    assert skipped is None and route.call_count == 0
