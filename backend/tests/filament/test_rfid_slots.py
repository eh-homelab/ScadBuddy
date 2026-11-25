"""The AMS slot of an override tag (#6264 Task 6, #7276 Task 9): `filament/rfid_slots.py`.
The slot is configured over ScadBuddy's own LAN link: a fake `LanCommandSender` records the
runs, and no printer or Bambuddy is reached. The printer's report is the recorded H2C idle
report (`tests/printers/fixtures/lan-report-h2c-idle.json`) with one tray put in AMS 0,
whose unit rests on inlet A: the left extruder, a 0.4 nozzle."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import status as http

from scadbuddy.bambuddy.catalogue import Catalogue, PresetChoice
from scadbuddy.bambuddy.models import PresetRef
from scadbuddy.bambuddy.models import Spool as BbSpool
from scadbuddy.bambuddy.trays import seen_trays
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import FilamentSync
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
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.printers import ams_slots
from scadbuddy.printers.ams_slots import nozzle_of
from scadbuddy.printers.component import Printers
from scadbuddy.printers.lan_command_models import (
    LanCommandOutcome,
    LanCommandPending,
    LanCommandReply,
    LanCommandRun,
)
from scadbuddy.printers.lan_commands import UnsupportedUnitError
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.reports import PrinterReportStore, report_status
from scadbuddy.printers.store import PrinterStore
from scadbuddy.slicing.own_presets import sb_ref
from scadbuddy.slicing.profiles import Profiles
from scadbuddy.spools.activities import filament_rfid_activity
from scadbuddy.workflows.lan_command import LanCommandAnswer
from scadbuddy.workflows.print_models import LAN_ROUTE_WAIT_S
from tests.conftest import PgPool
from tests.slicing.conftest import PROFILES

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
    ``errors``. As the ``LanCommand`` workflow does, a key sent again while its run is open
    joins it: ``published`` lists the runs actually started. With ``hold`` set, each answer
    waits until the event is set; ``open`` and ``most`` are the runs open now and at most,
    and ``most_by`` the most open at once per printer."""

    def __init__(self) -> None:
        self.runs: list[tuple[str, LanCommandRun]] = []
        self.keys: list[str | None] = []
        self.published: list[str | None] = []
        self.errors: list[Exception] = []
        self.pending = False
        self.confirmed = True
        self.hold: asyncio.Event | None = None
        self.open = 0
        self.most = 0
        self.open_by: dict[str, int] = {}
        self.most_by: dict[str, int] = {}

    async def run(
        self, printer_id: str, run: LanCommandRun, *, key: str | None = None
    ) -> LanCommandAnswer:
        self.runs.append((printer_id, run))
        self.keys.append(key)
        if key is None or key not in self.published:
            self.published.append(key)
        self.open += 1
        self.most = max(self.most, self.open)
        self.open_by[printer_id] = self.open_by.get(printer_id, 0) + 1
        self.most_by[printer_id] = max(self.most_by.get(printer_id, 0), self.open_by[printer_id])
        try:
            if self.hold is not None:
                await self.hold.wait()
            else:
                await asyncio.sleep(0)
        finally:
            self.open -= 1
            self.open_by[printer_id] -= 1
        if self.errors:
            raise self.errors.pop(0)
        if self.pending:
            return LanCommandPending(workflow_id=f"lan-command-{printer_id}-x-{key}")
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
        self.built = 0
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

    def another_printer(self, *tray_ids: int) -> str:
        """A second LAN-linked printer whose AMS 0 holds an override tag of a spool of its
        own in each of ``tray_ids``; its id."""
        printer = uuid4()
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO printers (id, serial, name, lan_host, lan_cert_sha256,"
                " access_code_sealed, access_code_dek_sealed, access_code_kek_id)"
                " VALUES (%s, %s, 'Shed', '10.0.0.6', %s,"
                " decode('00', 'hex'), decode('00', 'hex'), 'k1')",
                (printer, f"SN{printer.hex[:8]}", "cd" * 32),
            )
        trays = []
        for tray_id in tray_ids:
            tag = uuid4().hex.upper()
            spool = self.filament.spools.create(SpoolCreate(type_id=self.type_id), actor="ann").id
            self.bind(override=True, spool=spool, tray=tag)
            trays.append(_tray(tray_id, tag))
        data = _report()
        data["ams"]["ams"][0]["tray"] = trays
        self.reports.put(str(printer), data, datetime.now(UTC))
        return str(printer)

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

    async def native(self) -> Catalogue | None:
        self.built += 1
        return self.catalogue

    async def run(self, deadline: float | None = None) -> SlotReport:
        return await reconcile_slots(
            self.filament,
            self.sender,
            printers=self.printers,
            reports=self.reports,
            catalogue=self.native,
            deadline=time.monotonic() + 60 if deadline is None else deadline,
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
    assert w.built == 0  # no slot needed the catalogue


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
    assert report == SlotReport(waiting=1)  # P49
    assert w.rows() == []
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
    assert row["params_hash"] and row["last_error"] == "unconfirmed"
    assert row["run_key"] == w.sender.keys[0]


async def test_an_unacknowledged_configure_is_unconfirmed_and_not_a_repeat(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    """P48: the row says ``unconfirmed``, a warning of its own says so, and the repeat
    counter does not read an unacknowledged run as an AMS that does not echo."""
    w = World(pg_pool)
    w.sender.confirmed = False

    with caplog.at_level(logging.WARNING, logger=rfid_slots.__name__):
        for _ in range(4):
            assert await w.run() == SlotReport(unconfirmed=1)

    (row,) = w.rows()
    assert (row["last_error"], row["attempts"], row["run_key"]) == ("unconfirmed", 0, None)
    assert [r for r in caplog.records if "sent, not acknowledged" in r.message]
    assert not [r for r in caplog.records if "same values" in r.message]
    # Four closed runs: each pass sent its own (no key joins a closed run).
    assert len(set(w.sender.published)) == 4

    # N1: the first acknowledged configure after unacknowledged ones is not a repeat.
    w.sender.confirmed = True
    assert await w.run() == SlotReport(configured=1)
    (row,) = w.rows()
    assert (row["last_error"], row["attempts"]) == (None, 0)


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
        deadline=time.monotonic() + 60,
    )

    assert report == SlotReport(configured=1)
    assert len(w.sender.runs) == 1


async def test_with_no_slicer_profiles_a_slot_is_no_preset(pg_pool: PgPool) -> None:
    w = World(pg_pool)

    report = await reconcile_slots_now(
        w.filament,
        printers=w.printers,
        reports=w.reports,
        commands=w.sender,
        profiles=None,
        deadline=time.monotonic() + 60,
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
            deadline=time.monotonic() + 60,
        )
    finally:
        holder.close()
    assert skipped is None and w.sender.runs == []


def _tray(tray_id: int, uuid: str) -> dict[str, Any]:
    return {
        "id": str(tray_id),
        "tray_color": "FF0000FF",
        "tray_type": "PLA",
        "tray_info_idx": "GFA00",
        "tray_uuid": uuid,
        "tag_uid": "0000000000000000",
        "state": 11,
    }


def _four_slots(w: World) -> None:
    """AMS 0's four trays, each an override tag of its own spool."""
    more = []
    for tray_id in (0, 2, 3):
        uuid = f"{tray_id + 16:032X}"
        spool = w.filament.spools.create(SpoolCreate(type_id=w.type_id), actor="ann").id
        w.bind(override=True, spool=spool, tray=uuid)
        more.append(_tray(tray_id, uuid))
    w.report(_report(*more))


async def _until(done: Callable[[], bool]) -> None:
    """Yield to the loop until ``done`` holds (a test's latch, never a wall-time wait)."""

    async def spin() -> None:
        while not done():
            await asyncio.sleep(0)

    await asyncio.wait_for(spin(), timeout=10)


async def _settle() -> None:
    """Give every ready task a turn, so one that could start a run has started it."""
    for _ in range(50):
        await asyncio.sleep(0)


async def test_two_printers_run_at_once_but_one_printers_slots_one_at_a_time(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P46 and P58: printers' runs are sent together, so a pass waits about one run's wait
    per slot of its busiest printer; each run opens its own MQTT connection, so a printer
    has at most one of the pass's runs open at once."""
    w = World(pg_pool)
    _four_slots(w)
    other = w.another_printer(0, 1)
    w.sender.hold = asyncio.Event()
    # Count the slots that entered `_send` (on the loop) and the `_started` writes that
    # finished (in their threads), so the check below waits until every slot let through
    # has written its row and sent its run. With a broken per-printer lock, all six enter.
    entered, wrote = [0], [0]
    send, started = rfid_slots._send, rfid_slots._started

    async def counted_send(*args: Any) -> None:
        entered[0] += 1
        await send(*args)

    def counted_started(*args: Any) -> None:
        started(*args)
        wrote[0] += 1

    monkeypatch.setattr(rfid_slots, "_send", counted_send)
    monkeypatch.setattr(rfid_slots, "_started", counted_started)

    task = asyncio.create_task(w.run())
    # One run of each printer, at once, and no other slot between `_started` and its run.
    await _until(lambda: w.sender.open >= 2 and entered[0] == wrote[0] == len(w.sender.runs))
    assert w.sender.open == 2
    w.sender.hold.set()
    report = await asyncio.wait_for(task, timeout=10)

    assert report == SlotReport(configured=6)
    assert w.sender.most_by == {str(w.printer): 1, other: 1}
    assert w.sender.most == 2


async def test_at_most_so_many_runs_are_open_at_once(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(pg_pool)
    w.another_printer(1)
    w.another_printer(1)
    w.sender.hold = asyncio.Event()
    monkeypatch.setattr(rfid_slots, "MAX_CONCURRENT_RUNS", 2)

    task = asyncio.create_task(w.run())
    await _until(lambda: w.sender.open >= 2)
    await _settle()
    assert w.sender.open == 2
    w.sender.hold.set()

    assert await asyncio.wait_for(task, timeout=10) == SlotReport(configured=3)
    assert w.sender.most == 2


async def test_a_run_that_raises_is_failed_and_the_other_slots_go_on(pg_pool: PgPool) -> None:
    """I1: an error other than a problem (a Temporal RPC error, say) fails its slot only.
    Its run may still be open, so the row keeps its key and the next pass joins it."""
    w = World(pg_pool)
    _four_slots(w)
    w.sender.errors = [RuntimeError("the RPC broke")]

    report = await w.run()

    assert report == SlotReport(failed=1, configured=3)
    broke = w.sender.keys[0]
    (row,) = [r for r in w.rows() if r["run_key"] is not None]
    assert (row["run_key"], row["last_error"], row["attempts"]) == (broke, "internal", 1)

    await w.run()
    assert broke in w.sender.keys[4:]
    assert w.sender.published.count(broke) == 1


async def test_a_cancelled_pass_leaves_each_runs_key_for_the_next_pass(pg_pool: PgPool) -> None:
    """I1: the row takes its run's key before the run is sent, so a pass cancelled while it
    waits for the answer (a deploy) leaves a key the next pass joins: nothing is sent twice."""
    w = World(pg_pool)
    w.sender.hold = asyncio.Event()

    task = asyncio.create_task(w.run())
    await _until(lambda: w.sender.open == 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    (row,) = w.rows()
    assert row["run_key"] == w.sender.keys[0] and row["params_hash"]
    w.sender.hold = None
    assert await w.run() == SlotReport(configured=1)
    assert w.sender.keys[1] == w.sender.keys[0]
    assert len(w.sender.published) == 1
    # N1: the joined run is the slot's first acknowledged configure, not a repeat.
    assert w.rows()[0]["attempts"] == 0


async def test_failed_passes_do_not_count_as_repeats(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    """N1: only acknowledged configures are repeats. Two passes that fail ``internal`` keep
    the hash, but the configure that then lands is the slot's first."""
    w = World(pg_pool)
    w.sender.errors = [RuntimeError("the RPC broke"), RuntimeError("the RPC broke")]

    assert await w.run() == SlotReport(failed=1)
    assert await w.run() == SlotReport(failed=1)
    assert w.rows()[0]["attempts"] == 2

    with caplog.at_level(logging.WARNING, logger=rfid_slots.__name__):
        assert await w.run() == SlotReport(configured=1)

    assert w.rows()[0]["attempts"] == 0
    assert not [r for r in caplog.records if "same values" in r.message]


async def test_a_run_started_past_the_deadline_waits_and_keeps_its_key(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N2: the row's write (`_started`) can take the pass past its deadline. The run is then
    not sent: the slot counts ``waiting``, and the next pass starts it under the stored key."""
    w = World(pg_pool)
    clock = [0.0]
    monkeypatch.setattr(rfid_slots, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    started = rfid_slots._started

    def slow(*args: Any) -> None:
        started(*args)
        clock[0] += 10

    monkeypatch.setattr(rfid_slots, "_started", slow)

    assert await w.run(deadline=5.0) == SlotReport(waiting=1)
    assert w.sender.runs == []
    (row,) = w.rows()
    assert row["run_key"] and row["params_hash"]
    key = row["run_key"]

    monkeypatch.setattr(rfid_slots, "_started", started)
    assert await w.run(deadline=clock[0] + 5.0) == SlotReport(configured=1)
    assert w.sender.keys == [key]


async def test_a_deadline_already_past_leaves_the_slots_waiting(pg_pool: PgPool) -> None:
    w = World(pg_pool)
    _four_slots(w)

    report = await w.run(deadline=time.monotonic() - 1)

    assert report == SlotReport(waiting=4)
    assert w.sender.runs == [] and w.rows() == []
    assert await w.run() == SlotReport(configured=4)


async def test_a_slot_not_started_by_the_deadline_waits(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each run takes ten seconds of a fake clock; one at a time, only the first starts
    before the deadline."""
    w = World(pg_pool)
    _four_slots(w)
    clock = [0.0]
    monkeypatch.setattr(rfid_slots, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(rfid_slots, "MAX_CONCURRENT_RUNS", 1)
    answer = w.sender.run

    async def slow(printer_id: str, run: LanCommandRun, *, key: str | None = None) -> Any:
        clock[0] += 10
        return await answer(printer_id, run, key=key)

    monkeypatch.setattr(w.sender, "run", slow)

    assert await w.run(deadline=5.0) == SlotReport(configured=1, waiting=3)


def test_the_start_deadline_leaves_room_for_one_wait() -> None:
    assert rfid_slots.start_deadline(100.0, rfid_slots.ROUTE_BUDGET_S) == pytest.approx(
        100.0 + rfid_slots.ROUTE_BUDGET_S - LAN_ROUTE_WAIT_S
    )


async def test_a_pending_run_is_joined_by_the_next_pass_not_sent_again(pg_pool: PgPool) -> None:
    """P47: the slot's key is kept while its run is open, so the next pass joins it."""
    w = World(pg_pool)
    w.sender.pending = True

    assert await w.run() == SlotReport(unconfirmed=1)
    assert await w.run() == SlotReport(unconfirmed=1)

    assert len(w.sender.runs) == 2 and len(w.sender.published) == 1
    assert w.sender.keys[0] == w.sender.keys[1]
    key = w.sender.keys[0]
    assert key is not None and key.startswith("slot-0-1-")

    w.sender.pending = False  # the run closed, acknowledged
    assert await w.run() == SlotReport(configured=1)
    assert w.sender.keys[2] == key and len(w.sender.published) == 1
    (row,) = w.rows()
    assert (row["last_error"], row["run_key"]) == (None, None)

    # The AMS never shows it: the next configure is a new run, not the closed one's outcome.
    assert await w.run() == SlotReport(configured=1)
    assert len(w.sender.published) == 2 and w.sender.keys[3] != key


async def test_a_tray_showing_an_unconfirmed_configure_settles_it(pg_pool: PgPool) -> None:
    w = World(pg_pool)
    w.sender.pending = True
    await w.run()
    w.report(_report(tray_info_idx="GFL99"))  # it applied

    assert await w.run() == SlotReport(unchanged=1)
    (row,) = w.rows()
    assert (row["last_error"], row["run_key"]) == (None, None)


async def test_no_override_slot_builds_no_catalogue(pg_pool: PgPool) -> None:
    w = World(pg_pool)
    w.bind(override=False)

    async def boom() -> Catalogue | None:
        raise AssertionError("the catalogue is built only for a slot that needs it")

    report = await reconcile_slots(
        w.filament,
        w.sender,
        printers=w.printers,
        reports=w.reports,
        catalogue=boom,
        deadline=time.monotonic() + 60,
    )

    assert report == SlotReport()


async def test_the_catalogue_is_built_once_a_pass(pg_pool: PgPool) -> None:
    w = World(pg_pool)
    _four_slots(w)

    await w.run()

    assert w.built == 1


async def test_an_unsupported_unit_is_recorded_once_and_never_sent(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """P22: a unit the LAN commands cannot address counts under its label each pass; its
    row is written once while the params' hash is unchanged."""
    w = World(pg_pool)

    def unsupported(*_args: object, **_kwargs: object) -> object:
        raise UnsupportedUnitError("no known wire ids for unit 0")

    monkeypatch.setattr(ams_slots, "configure_commands", unsupported)

    with caplog.at_level(logging.WARNING, logger=rfid_slots.__name__):
        assert await w.run() == SlotReport(unsupported_unit=1)
        assert await w.run() == SlotReport(unsupported_unit=1)

    assert w.sender.runs == []
    (row,) = w.rows()
    assert (row["last_error"], row["attempts"]) == ("unsupported_unit", 1)
    assert row["params_hash"]
    assert len([r for r in caplog.records if "cannot configure" in r.message]) == 1

    w.preset("0.4", "GFL98")  # new params: recorded again
    assert await w.run() == SlotReport(unsupported_unit=1)
    assert w.rows()[0]["attempts"] == 2


async def test_a_minimum_above_the_maximum_is_invalid_temps(pg_pool: PgPool) -> None:
    w = World(pg_pool, temps=False)
    w.report(_report(nozzle_temp_min="240", nozzle_temp_max="190"))

    report = await w.run()

    assert w.sender.runs == []
    assert report == SlotReport(invalid_temps=1)


async def test_the_real_catalogue_configures_with_no_bambuddy(pg_pool: PgPool) -> None:
    """The native catalogue of the test profiles, no `catalogue_for` stand-in."""
    w = World(pg_pool)
    w.preset("0.4", "Bambu PLA Basic @BBL H2C")

    report = await reconcile_slots_now(
        w.filament,
        printers=w.printers,
        reports=w.reports,
        commands=w.sender,
        profiles=Profiles(PROFILES),
        deadline=time.monotonic() + 60,
    )

    assert report == SlotReport(configured=1)
    assert w.sender.sent("ams_filament_setting")["tray_info_idx"] == "GFA00"


async def test_an_unconfirmed_slot_does_not_fail_the_activity(pg_pool: PgPool) -> None:
    w = World(pg_pool)
    w.preset("0.4", "Bambu PLA Basic @BBL H2C")
    w.sender.pending = True
    printers = cast(Printers, SimpleNamespace(store=w.printers, reports=w.reports))
    run = filament_rfid_activity(
        printers,
        w.filament,
        StoredSettings,  # no Bambuddy: the mode is pending, not failed
        lambda: FilamentSync(tags="off"),
        w.sender,
        lambda: Profiles(PROFILES),
    )

    assert await run() == "done"
    assert len(w.sender.runs) == 1
    (row,) = w.rows()
    assert row["last_error"] == "unconfirmed"
