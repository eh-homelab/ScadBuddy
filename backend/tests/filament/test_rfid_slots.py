"""The AMS slot of an override tag (#6264 Task 6, #7276 Task 9): `filament/rfid_slots.py`.
The slot is configured over ScadBuddy's own LAN link: a fake `LanCommandSender` records the
runs, and no printer or Bambuddy is reached. The printer's report is the recorded H2C idle
report (`tests/printers/fixtures/lan-report-h2c-idle.json`) with one tray put in AMS 0,
whose unit rests on inlet A: the left extruder, a 0.4 nozzle."""

from __future__ import annotations

import copy
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import status as http

from scadbuddy.bambuddy.catalogue import Catalogue, PresetChoice
from scadbuddy.bambuddy.models import PresetRef
from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.bambuddy.trays import seen_trays
from scadbuddy.core.problems import ApiError
from scadbuddy.filament import rfid_slots
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
from scadbuddy.filament.rfid_push import try_lock
from scadbuddy.filament.rfid_slots import SLOTS_LOCK, reconcile_slots, reconcile_slots_now
from scadbuddy.filament.sb_presets import create_on
from scadbuddy.filament.sensors import SensorStore
from scadbuddy.filament.shopping import ShoppingStore
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.suppliers import SuppliersStore
from scadbuddy.filament.types import SpoolTypeStore
from scadbuddy.printers.ams_slots import nozzle_of
from scadbuddy.printers.lan_command_models import (
    LanCommandOutcome,
    LanCommandPending,
    LanCommandReply,
    LanCommandRun,
)
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.reports import PrinterReportStore, report_status
from scadbuddy.printers.store import PrinterStore
from scadbuddy.slicing.own_presets import sb_ref
from scadbuddy.workflows.lan_command import LanCommandAnswer
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

TRAY = "0123456789ABCDEF0123456789ABCDEF"
OTHER_TRAY = "FEDCBA9876543210FEDCBA9876543210"
FIXTURES = Path(__file__).parent.parent / "printers" / "fixtures"


def _catalogue(*extra: PresetChoice) -> Catalogue:
    """The native catalogue the slot ids are read on: two BBL generics, and ``extra``."""
    return Catalogue(
        printer=[],
        process=[],
        filament=[
            PresetChoice(
                ref=PresetRef(source="standard", id="Generic PLA @BBL H2C"),
                name="Generic PLA @BBL H2C",
                filament_id="GFL99",
            ),
            PresetChoice(
                ref=PresetRef(source="standard", id="Generic PLA-CF @BBL H2C"),
                name="Generic PLA-CF @BBL H2C",
                filament_id="GFL98",
            ),
            *extra,
        ],
        native_setting_ids={"printer": {}, "process": {}, "filament": {}},
    )


class Sender:
    """A `LanCommandSender` that records each run and answers it: every reply
    ``confirmed`` by default, `LanCommandPending` with ``pending``, or raises the next of
    ``errors``."""

    def __init__(self) -> None:
        self.runs: list[tuple[str, LanCommandRun]] = []
        self.errors: list[ApiError] = []
        self.pending = False
        self.confirmed = True

    async def run(
        self, printer_id: str, run: LanCommandRun, *, key: str | None = None
    ) -> LanCommandAnswer:
        self.runs.append((printer_id, run))
        if self.errors:
            raise self.errors.pop(0)
        if self.pending:
            return LanCommandPending(workflow_id="lan-command-pending")
        return LanCommandOutcome(
            replies=[LanCommandReply(confirmed=self.confirmed) for _ in run.commands]
        )

    def names(self, n: int = -1) -> list[str]:
        return [c.name for c in self.runs[n][1].commands]

    def sent(self, name: str, n: int = -1) -> dict[str, Any]:
        """The ``print`` body of run ``n``'s command ``name``."""
        body: dict[str, Any] = next(
            c.payload["print"] for c in self.runs[n][1].commands if c.name == name
        )
        return body


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


def _report(*more: dict[str, Any], **tray: object) -> dict[str, Any]:
    """The idle H2C report with one tray in AMS 0, tray 1; ``tray`` overrides its keys, and
    ``more`` are further trays of AMS 0."""
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
    data["ams"]["ams"][0]["tray"] = [shown, *more]
    return data


class World:
    def __init__(self, pool: PgPool, *, temps: bool = True) -> None:
        self.pool = pool
        self.filament = _filament(pool)
        self.printers = PrinterStore(pool, None)
        self.reports = PrinterReportStore(pool)
        self.printer = uuid4()
        self.sender = Sender()
        self.catalogue = _catalogue()
        # A LAN-linked printer (host, pinned certificate, access code), no Bambuddy link.
        with pool.connection() as conn:
            conn.execute(
                "INSERT INTO printers (id, serial, name, lan_host, lan_cert_sha256,"
                " access_code_sealed, access_code_dek_sealed, access_code_kek_id)"
                " VALUES (%s, %s, 'Garage', '10.0.0.5', %s,"
                " decode('00', 'hex'), decode('00', 'hex'), 'k1')",
                (self.printer, f"SN{self.printer.hex[:8]}", "ab" * 32),
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

    def bind(self, *, override: bool, spool: UUID | None = None, tray: str = TRAY) -> None:
        self.filament.spools.bind_tag(
            spool or self.spool, tray_uuid=tray, kind="bambu_mifare", override=override, actor="ann"
        )

    def rows(self) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            return list(conn.execute("SELECT * FROM filament_override_slots ORDER BY tray_id"))

    async def run(self) -> SlotReport:
        return await reconcile_slots(
            self.filament,
            self.sender,
            printers=self.printers,
            reports=self.reports,
            catalogue=self.catalogue,
        )


@pytest.mark.parametrize(
    ("fixture", "ams_id", "nozzle"),
    [
        # Units 0 and 3 rest on inlet A (left, extruder 1: 0.4); units 1 and 2 on B (0.6).
        ("lan-report-h2c-idle.json", 0, "0.4"),
        ("lan-report-h2c-idle.json", 1, "0.6"),
        ("lan-report-h2c-idle.json", 9, None),
        ("lan-report-h2c.json", 0, None),  # no unit says where it rests
    ],
)
def test_the_nozzle_is_the_one_of_the_extruder_the_ams_rests_on(
    fixture: str, ams_id: int, nozzle: str | None
) -> None:
    data = json.loads((FIXTURES / fixture).read_text())["print"]
    at = datetime.now(UTC)
    row = PrinterRow(id=str(uuid4()), serial="SN", name="P", created_at=at, updated_at=at)
    status = report_status(row, (data, at), now=at)
    assert nozzle_of(status, ams_id, 0) == nozzle


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


async def test_an_override_slot_is_configured_over_the_lan(pg_pool: PgPool) -> None:
    w = World(pg_pool)

    report = await w.run()

    assert len(w.sender.runs) == 1
    assert w.sender.runs[0][0] == str(w.printer)
    assert w.sender.names() == ["ams_filament_setting", "extrusion_cali_sel"]
    setting = w.sender.sent("ams_filament_setting")
    assert (setting["tray_info_idx"], setting["tray_type"], setting["tray_color"]) == (
        "GFL99",
        "PLA",
        "FF0000FF",
    )
    assert (setting["nozzle_temp_min"], setting["nozzle_temp_max"]) == (190, 240)
    # #2604's back-fill: the filament id's setting id goes with it.
    assert setting["setting_id"] == "GFSL99"
    selected = w.sender.sent("extrusion_cali_sel")
    assert (selected["cali_idx"], selected["nozzle_diameter"]) == (-1, "0.4")
    assert report == SlotReport(configured=1)
    (row,) = w.rows()
    assert row["spool_id"] == w.spool and row["params_hash"] and row["configured_at"]


def _k_rows(w: World, setting_id: str) -> None:
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
                setting_id=setting_id,
            )
            for ext in (0, 1)
        ],
        actor="ann",
    )


async def test_the_k_profile_of_the_extruder_the_ams_feeds_is_selected(pg_pool: PgPool) -> None:
    w = World(pg_pool)
    _k_rows(w, "GFSL99")  # made for the slot's filament

    await w.run()

    # Unit 0 rests on the left: extruder 1, whose profile is cali_idx 8.
    assert w.sender.names() == ["ams_filament_setting", "extrusion_cali_sel"]
    assert w.sender.sent("extrusion_cali_sel")["cali_idx"] == 8


async def test_a_k_profile_of_another_filament_is_sent_as_a_new_k_value(pg_pool: PgPool) -> None:
    """P42: the stored profile's ``cali_idx`` is sent only for a K row made for the slot's
    filament; otherwise ``cali_idx`` -1 and the value through ``extrusion_cali_set``."""
    w = World(pg_pool)
    _k_rows(w, "GFSA00")

    await w.run()

    assert w.sender.names() == ["ams_filament_setting", "extrusion_cali_sel", "extrusion_cali_set"]
    assert w.sender.sent("extrusion_cali_sel")["cali_idx"] == -1
    (k,) = w.sender.sent("extrusion_cali_set")["filaments"]
    assert (k["k_value"], k["filament_id"]) == ("0.030000", "GFL99")


async def test_a_second_reconcile_sends_nothing(pg_pool: PgPool) -> None:
    w = World(pg_pool)
    await w.run()
    w.report(_report(tray_info_idx="GFL99"))  # the AMS now shows what was sent

    report = await w.run()

    assert len(w.sender.runs) == 1
    assert (report.unchanged, report.configured) == (1, 0)


async def test_a_reloaded_tray_is_configured_again(pg_pool: PgPool) -> None:
    w = World(pg_pool)
    await w.run()
    # The hash still matches, but the AMS reloaded the tag: its own idx and material.
    w.report(_report(tray_info_idx="GFA00", tray_type="PLA Basic"))

    report = await w.run()

    assert len(w.sender.runs) == 2
    assert (report.configured, report.unchanged) == (1, 0)


async def test_a_changed_preset_configures_again(pg_pool: PgPool) -> None:
    w = World(pg_pool)
    await w.run()
    w.report(_report(tray_info_idx="GFL99"))
    w.preset("0.4", "GFL98")

    report = await w.run()

    assert len(w.sender.runs) == 2
    assert w.sender.sent("ams_filament_setting")["tray_info_idx"] == "GFL98"
    assert report.configured == 1


async def test_a_plain_binding_is_not_configured(pg_pool: PgPool) -> None:
    w = World(pg_pool)
    w.bind(override=False)

    report = await w.run()

    assert w.sender.runs == []
    assert report == SlotReport()


async def test_an_override_turned_off_deletes_the_row_and_sends_nothing(pg_pool: PgPool) -> None:
    w = World(pg_pool)
    await w.run()
    assert len(w.rows()) == 1
    w.bind(override=False)

    await w.run()

    assert len(w.sender.runs) == 1
    assert w.rows() == []
    w.bind(override=True)  # a later override configures the slot again
    await w.run()
    assert len(w.sender.runs) == 2


async def test_a_pull_keeps_the_override_slot_and_the_next_reconcile_configures_it(
    pg_pool: PgPool,
) -> None:
    w = World(pg_pool)
    await w.run()
    assert len(w.sender.runs) == 1
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

    report = await w.run()

    assert len(w.sender.runs) == 2
    assert report.configured == 1


async def test_a_deleted_spool_cascades_its_row_away(pg_pool: PgPool) -> None:
    w = World(pg_pool)
    await w.run()
    assert len(w.rows()) == 1
    assert w.filament.spools.delete(w.spool)
    assert w.rows() == []


async def test_a_scadbuddy_preset_is_configured_by_its_catalogue_filament_id(
    pg_pool: PgPool,
) -> None:
    """R-7276-15: a ScadBuddy preset's slot ids come from the native catalogue
    (``with_own``), never from its Bambuddy mirror's id."""
    w = World(pg_pool)
    with pg_pool.connection() as conn, conn.transaction():
        own = create_on(
            conn,
            name="Mine",
            source="manual",
            key=f"attachment-{1:064d}",
            sha256=f"{1:064d}",
            doc={"name": "Mine"},
            made_by="test",
            bb_preset_id=141,
        )
    w.catalogue = _catalogue(
        PresetChoice(
            ref=PresetRef(source="local", id=sb_ref(own)), name="Mine", filament_id="GFL95"
        )
    )
    w.preset("0.4", sb_ref(own))

    report = await w.run()

    assert report.configured == 1
    assert w.sender.sent("ams_filament_setting")["tray_info_idx"] == "GFL95"

    w.catalogue = _catalogue()  # the native slicer does not offer it
    w.report(_report(tray_info_idx="GFA00"))
    assert await w.run() == SlotReport(no_preset=1)
    assert len(w.sender.runs) == 1


# A preset the catalogue does not name, and a Bambuddy local id (#7244).
@pytest.mark.parametrize("preset", ["Unknown preset", "141"])
async def test_a_preset_with_no_filament_id_is_no_preset(pg_pool: PgPool, preset: str) -> None:
    w = World(pg_pool)
    w.preset("0.4", preset)

    report = await w.run()

    assert w.sender.runs == []
    assert report == SlotReport(no_preset=1)


async def test_no_preset_for_the_nozzle_sends_nothing(pg_pool: PgPool) -> None:
    w = World(pg_pool)
    w.preset("0.6", "GFL99")

    report = await w.run()

    assert w.sender.runs == []
    assert report == SlotReport(no_preset=1)


async def test_a_tray_whose_nozzle_the_report_does_not_say_sends_nothing(pg_pool: PgPool) -> None:
    w = World(pg_pool)
    data = _report()
    del data["ams"]["ams"][0]["info"]
    w.report(data)

    report = await w.run()

    assert w.sender.runs == []
    assert report == SlotReport(no_nozzle=1)


async def test_a_printer_with_no_lan_link_is_skipped(pg_pool: PgPool) -> None:
    w = World(pg_pool)
    await w.run()
    w.bind(override=False)  # would delete the row, were the printer counted
    with pg_pool.connection() as conn:
        conn.execute("UPDATE printers SET lan_cert_sha256 = NULL, bambuddy_printer_id = 1")

    report = await w.run()

    assert len(w.sender.runs) == 1
    assert report == SlotReport()
    assert len(w.rows()) == 1


@pytest.mark.parametrize("state", [{"gcode_state": "RUNNING"}, {"ams_status": 0x100}])
async def test_a_busy_printer_is_sent_nothing_until_it_is_idle(
    pg_pool: PgPool, state: dict[str, Any]
) -> None:
    """R-7276-12: `guard` refuses the configure while a print runs or the AMS is busy;
    the slot is neither failed nor forgotten, and a later pass configures it."""
    w = World(pg_pool)
    w.report({**_report(), **state})

    report = await w.run()

    assert w.sender.runs == []
    assert report == SlotReport()
    w.report(_report())
    assert (await w.run()) == SlotReport(configured=1)


def _refused(code: str) -> ApiError:
    status = http.HTTP_409_CONFLICT if code == "printer_refused" else http.HTTP_502_BAD_GATEWAY
    return ApiError(status, "the printer said no", code=code)


@pytest.mark.parametrize("code", ["printer_refused", "printer_no_answer", "printer_unreachable"])
async def test_a_refused_configure_is_failed_and_the_next_slot_goes_on(
    pg_pool: PgPool, code: str
) -> None:
    w = World(pg_pool)
    other = w.filament.spools.create(SpoolCreate(type_id=w.type_id), actor="ann").id
    w.bind(override=True, spool=other, tray=OTHER_TRAY)
    second = {
        "id": "2",
        "tray_color": "FF0000FF",
        "tray_type": "PLA",
        "tray_info_idx": "GFA00",
        "tray_uuid": OTHER_TRAY,
        "tag_uid": "0000000000000000",
        "state": 11,
    }
    w.report(_report(second))
    w.sender.errors = [_refused(code)]

    report = await w.run()

    assert len(w.sender.runs) == 2
    assert report == SlotReport(failed=1, configured=1)
    first, then = w.rows()
    assert (first["attempts"], first["params_hash"], first["last_error"]) == (1, None, code)
    assert then["params_hash"] and then["last_error"] is None

    again = await w.run()
    assert again.configured == 2
    assert w.rows()[0]["attempts"] == 0 and w.rows()[0]["last_error"] is None


async def test_a_pending_configure_is_unconfirmed_not_done_or_failed(pg_pool: PgPool) -> None:
    """P36: a run still open when the sender stopped waiting may be on its way: it is
    ``unconfirmed``, never ``configured`` or ``failed``, and sent once in the pass."""
    w = World(pg_pool)
    w.sender.pending = True

    report = await w.run()

    assert len(w.sender.runs) == 1
    assert report == SlotReport(unconfirmed=1)
    (row,) = w.rows()
    assert row["params_hash"] and row["last_error"] is None


async def test_an_unacknowledged_configure_is_unconfirmed(pg_pool: PgPool) -> None:
    w = World(pg_pool)
    w.sender.confirmed = False

    report = await w.run()

    assert report == SlotReport(unconfirmed=1)


async def test_a_spool_without_temperatures_takes_the_trays(pg_pool: PgPool) -> None:
    w = World(pg_pool, temps=False)
    w.report(_report(nozzle_temp_min="190", nozzle_temp_max="240"))

    report = await w.run()

    setting = w.sender.sent("ams_filament_setting")
    assert (setting["nozzle_temp_min"], setting["nozzle_temp_max"]) == (190, 240)
    assert report.configured == 1


@pytest.mark.parametrize(
    "tray",
    [
        {},
        {"nozzle_temp_min": "0", "nozzle_temp_max": "240"},
    ],
)
async def test_no_temperatures_anywhere_sends_nothing(
    pg_pool: PgPool, tray: dict[str, str]
) -> None:
    w = World(pg_pool, temps=False)
    w.report(_report(**tray))

    report = await w.run()

    assert w.sender.runs == []
    assert (report.no_temps, report.no_preset) == (1, 0)
    assert len(w.rows()) == 0


async def test_a_different_spool_in_the_row_configures_again(pg_pool: PgPool) -> None:
    w = World(pg_pool)
    await w.run()
    w.report(_report(tray_info_idx="GFL99"))  # the tray shows what was sent; the hash matches
    other = w.filament.spools.create(SpoolCreate(type_id=w.type_id), actor="ann").id
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_override_slots SET spool_id = %s", (other,))

    report = await w.run()

    assert (len(w.sender.runs), report.configured) == (2, 1)
    assert w.rows()[0]["spool_id"] == w.spool


@pytest.mark.parametrize(
    "tray", [{"tray_color": "FF000080"}, {"tray_color": "00FF00FF"}, {"tray_type": "PLA Basic"}]
)
async def test_a_tray_differing_in_colour_or_material_alone_is_configured_again(
    pg_pool: PgPool, tray: dict[str, str]
) -> None:
    w = World(pg_pool)
    await w.run()
    w.report(_report(tray_info_idx="GFL99", **tray))

    report = await w.run()

    assert (len(w.sender.runs), report.configured, report.unchanged) == (2, 1, 0)


async def test_a_stale_report_deletes_nothing(pg_pool: PgPool) -> None:
    w = World(pg_pool)
    await w.run()
    w.bind(override=False)
    with pg_pool.connection() as conn:
        conn.execute("UPDATE printer_reports SET seen_at = now() - interval '1 hour'")

    report = await w.run()

    assert len(w.rows()) == 1 and len(w.sender.runs) == 1
    assert report == SlotReport()


async def test_repeated_configures_of_unchanged_params_are_counted_and_warned(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    w = World(pg_pool)
    for _ in range(4):  # the AMS never shows what was sent
        await w.run()

    assert w.rows()[0]["attempts"] == 3
    assert [r for r in caplog.records if "same values" in r.message]


def _catalogue_for(catalogue: Catalogue) -> Callable[..., Awaitable[Catalogue]]:
    async def read(client: object, *, native: object) -> Catalogue:
        assert client is None and native is not None
        return catalogue

    return read


async def test_it_runs_with_no_bambuddy_configured(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(pg_pool)
    monkeypatch.setattr(rfid_slots, "catalogue_for", _catalogue_for(w.catalogue))

    report = await reconcile_slots_now(
        w.filament,
        printers=w.printers,
        reports=w.reports,
        commands=w.sender,
        profiles=object(),  # type: ignore[arg-type]
    )

    assert report == SlotReport(configured=1)
    assert len(w.sender.runs) == 1


async def test_with_no_slicer_profiles_a_slot_is_no_preset(pg_pool: PgPool) -> None:
    w = World(pg_pool)

    report = await reconcile_slots_now(
        w.filament, printers=w.printers, reports=w.reports, commands=w.sender, profiles=None
    )

    assert report == SlotReport(no_preset=1)
    assert w.sender.runs == []


async def test_a_held_lock_skips_the_step(pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch) -> None:
    w = World(pg_pool)
    monkeypatch.setattr(rfid_slots, "catalogue_for", _catalogue_for(w.catalogue))
    holder = try_lock(w.filament, SLOTS_LOCK)
    assert holder is not None
    try:
        skipped = await reconcile_slots_now(
            w.filament,
            printers=w.printers,
            reports=w.reports,
            commands=w.sender,
            profiles=object(),  # type: ignore[arg-type]
        )
    finally:
        holder.close()
    assert skipped is None and w.sender.runs == []
