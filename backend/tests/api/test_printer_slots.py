"""#7276 Task 8: a printer's AMS slots over ScadBuddy's own LAN link (plan
``docs/superpowers/plans/2026-11-25-filament-hardware-off-bambuddy.md``). The state checks
read the printer's stored report (R-7276-12); the printer is scripted by replacing
`LanPrinter.request` (P8); Bambuddy is never called (`forbid_bambuddy`)."""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from scadbuddy.api import printer_slots as slot_routes
from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.bambuddy.catalogue import Catalogue, PresetChoice
from scadbuddy.bambuddy.models import PresetRef
from scadbuddy.bambuddy.resolver import PRINTER_MODEL
from scadbuddy.filament.catalog import CatalogRefError
from scadbuddy.filament.component import FILAMENT, Filament
from scadbuddy.filament.models import Material, SpoolCreate, SpoolKValueCreate, SpoolPresetCreate
from scadbuddy.filament.sb_presets import create_on
from scadbuddy.printers.ams_slots import SlotPresetBody
from scadbuddy.printers.component import PRINTERS, Printers
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.slicing.component import Slicing
from scadbuddy.slicing.own_presets import sb_ref
from scadbuddy.workflows import lan_command as lan_command_module
from tests.api.test_send import BASE, configure
from tests.printers.conftest import KEK
from tests.support.lan_script import Scripted, script_printer
from tests.support.no_bambuddy import forbid_bambuddy

pytestmark = pytest.mark.requires_postgres

SERIAL = "01P00A000000071"
RECORDED: dict[str, Any] = json.loads(
    (Path(__file__).parents[1] / "printers" / "fixtures" / "lan-report-h2c-idle.json").read_text(
        encoding="utf-8"
    )
)["print"]
BODY = {"material": "PLA", "colour": "#FFFFFF", "nozzle_temp_min": 190, "nozzle_temp_max": 220}
BASIC = "Bambu PLA Basic @BBL H2C"


def idle(**values: Any) -> dict[str, Any]:
    """The recorded H2C report (two nozzles, the Filament Track Switch, AMS 0 on inlet A:
    the left extruder, a 0.4 nozzle; AMS 1 on inlet B: the right, 0.6; AMS-HT 128; two
    empty external holders), idle unless ``values`` say otherwise."""
    report: dict[str, Any] = copy.deepcopy(RECORDED)
    report.update({"gcode_state": "IDLE", "ams_status": 0})
    report["ams"]["tray_now"] = "255"
    report.update(values)
    return report


#: ``snow`` when an extruder holds nothing; a slot is ``ams_id << 8 | slot`` (P40).
EMPTY = 0xFFFF
HT_A = 128 << 8


def feeding(
    report: dict[str, Any], right: int = EMPTY, left: int = EMPTY, *, tray_now: int = 255
) -> dict[str, Any]:
    """``report`` with each extruder's ``snow`` (0 the right, 1 the left) and ``tray_now``,
    which names only the active extruder's slot."""
    info = report["device"]["extruder"]["info"]
    info[0]["snow"], info[1]["snow"] = right, left
    report["ams"]["tray_now"] = str(tray_now)
    return report


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> Scripted:
    return script_printer(monkeypatch)


def printers_of(client: TestClient) -> Printers:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state.components.get(PRINTERS)


def filament(client: TestClient) -> Filament:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state.components.get(FILAMENT)


def lan_printer(
    client: TestClient, report: dict[str, Any] | None = None, *, age: timedelta | None = None
) -> str:
    printers = printers_of(client)
    printers.store.kek = KEK
    row = printers.store.create(PrinterCreate(serial=SERIAL, name="H2C"))
    printers.store.link_lan(
        row.id, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256="ab" * 32
    )
    printers.reports.put(row.id, report or idle(), datetime.now(UTC) - (age or timedelta()))
    return row.id


@pytest.fixture
def pid(client: TestClient) -> str:
    return lan_printer(client)


def slot(pid: str, ams: int = 0, tray: int = 1) -> str:
    return f"/api/v1/printers/{pid}/trays/{ams}/{tray}"


def names(script: Scripted) -> list[str]:
    return [p["command"] for p in script.published]


def test_the_view_reads_the_stored_report_and_sends_nothing(
    client: TestClient, scripted: Scripted
) -> None:
    pid = lan_printer(client, feeding(idle(), 1, HT_A, tray_now=1))
    printers_of(client).command_data.set_slot_preset(
        UUID(pid),
        0,
        1,
        SlotPresetBody(preset_id="GFSG00", preset_name="Bambu PETG Basic", tray_info_idx="GFG00"),
    )
    filament(client).locations.name_ams_unit(UUID(pid), 0, "Left rack")
    filament(client).locations.name_ams_unit(UUID(pid), 1, None)

    response = client.get(f"/api/v1/printers/{pid}/ams")

    assert response.status_code == 200, response.text
    view = response.json()
    assert (view["connected"], view["printing"], view["ams_busy"], view["tray_now"]) == (
        True,
        False,
        False,
        1,
    )
    units = {u["ams_id"]: u for u in view["units"]}
    assert list(units) == [0, 1, 2, 3, 128, 255]
    first = units[0]
    assert (first["unit"], first["label"], first["place_name"]) == (
        "AMS-A",
        "Left rack",
        "Left rack",
    )
    # A place still named by default is no label (R-7276-13).
    assert (units[1]["label"], units[1]["place_name"]) == (None, "H2C AMS-B")
    tray = first["slots"][1]
    assert tray["loaded"] is True and tray["filled"] is True
    # HT-A feeds the other extruder, which tray_now does not name (P40).
    assert units[128]["slots"][0]["loaded"] is True
    assert units[1]["slots"][1]["loaded"] is False
    assert tray["saved_preset"]["preset_id"] == "GFSG00"
    assert units[255]["slots"][0]["refreshable"] is False
    assert scripted.published == []


def test_a_printer_with_no_report_is_offline_with_no_units(
    client: TestClient, scripted: Scripted
) -> None:
    printers = printers_of(client)
    printers.store.kek = KEK
    pid = printers.store.create(PrinterCreate(serial=SERIAL, name="H2C")).id
    printers.store.link_lan(
        pid, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256="ab" * 32
    )
    view = client.get(f"/api/v1/printers/{pid}/ams").json()
    assert (view["connected"], view["units"]) == (False, [])


def test_a_stale_report_refuses_every_write_printer_offline(
    client: TestClient, scripted: Scripted
) -> None:
    pid = lan_printer(client, age=timedelta(minutes=5))
    for action in ("configure", "reset", "refresh", "load", "unload"):
        r = client.post(f"{slot(pid)}/{action}", json=BODY if action == "configure" else None)
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "printer_offline"
        assert "no fresh report" in r.json()["detail"]
    assert scripted.published == []


@pytest.mark.parametrize("action", ["configure", "reset", "refresh", "load", "unload"])
@pytest.mark.parametrize(
    ("values", "code"),
    [
        ({"gcode_state": "RUNNING"}, "printer_busy"),
        ({"gcode_state": "PAUSE"}, "printer_busy"),
        ({"ams_status": 0x100}, "ams_busy"),
        ({"ams_status": 0x200}, "ams_busy"),
        ({"ams_status": 0x700}, "ams_busy"),
    ],
)
def test_every_printer_write_refuses_an_unsafe_state(
    client: TestClient, scripted: Scripted, action: str, values: dict[str, Any], code: str
) -> None:
    pid = lan_printer(client, idle(**values))
    response = client.post(f"{slot(pid)}/{action}", json=BODY if action == "configure" else None)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == code
    assert scripted.published == []


def test_the_ams_assist_state_is_not_busy(client: TestClient, scripted: Scripted) -> None:
    pid = lan_printer(client, idle(ams_status=0x300))
    assert client.post(f"{slot(pid)}/load").status_code == 200
    assert client.get(f"/api/v1/printers/{pid}/ams").json()["ams_busy"] is False


def test_configure_sends_setting_then_cali_sel(client: TestClient, scripted: Scripted) -> None:
    pid = lan_printer(client)
    body = {
        "material": "PETG",
        "colour": "#00ae42",
        "preset_id": "GFG99",
        "preset_name": "Generic PETG",
        "nozzle_temp_min": 230,
        "nozzle_temp_max": 260,
    }

    response = client.post(f"{slot(pid)}/configure", json=body)

    assert response.status_code == 200, response.text
    answer = response.json()
    assert (answer["colour"], answer["nozzle_diameter"]) == ("#00AE42FF", "0.4")
    assert (answer["preset_id"], answer["preset_source"], answer["message"]) == (
        "GFG99",
        None,
        "sent",
    )
    assert names(scripted) == ["ams_filament_setting", "extrusion_cali_sel"]
    setting, sel = scripted.published
    assert (setting["ams_id"], setting["tray_id"], setting["slot_id"]) == (0, 1, 1)
    assert setting["tray_info_idx"] == "GFG99"
    # #2604's back-fill: a filament id with no setting id half-configures the slot.
    assert setting["setting_id"] == "GFSG99"
    assert (setting["tray_color"], setting["tray_sub_brands"]) == ("00AE42FF", "Generic PETG")
    assert (setting["nozzle_temp_min"], setting["nozzle_temp_max"]) == (230, 260)
    assert (sel["ams_id"], sel["tray_id"], sel["slot_id"], sel["cali_idx"]) == (0, 1, 1, -1)
    assert (sel["filament_id"], sel["nozzle_diameter"]) == ("GFG99", "0.4")
    assert "setting_id" not in sel


def test_configure_with_no_preset_takes_the_materials_generic(
    client: TestClient, scripted: Scripted
) -> None:
    pid = lan_printer(client)
    response = client.post(f"{slot(pid)}/configure", json={**BODY, "material": "PETG"})
    assert response.status_code == 200, response.text
    assert response.json()["preset_id"] == "GFG99"
    setting = scripted.sent("ams_filament_setting")[0]
    assert (setting["tray_info_idx"], setting["setting_id"]) == ("GFG99", "GFSG99")


def test_configure_with_a_new_k_sends_cali_set_too(client: TestClient, scripted: Scripted) -> None:
    pid = lan_printer(client)
    body = {**BODY, "k_value": 0.031, "setting_id": "PFK1"}
    response = client.post(f"{slot(pid, 1, 2)}/configure", json=body)
    assert response.status_code == 200, response.text
    assert names(scripted) == ["ams_filament_setting", "extrusion_cali_sel", "extrusion_cali_set"]
    setting = scripted.sent("ams_filament_setting")[0]
    # The K profile's setting id is the K set's, never the slot's (P3).
    assert setting["setting_id"] == "GFSL99"
    [entry] = scripted.sent("extrusion_cali_set")[0]["filaments"]
    assert (entry["tray_id"], entry["k_value"], entry["setting_id"]) == (6, "0.031000", "PFK1")
    assert entry["nozzle_diameter"] == "0.6"


def test_a_selected_k_profile_sends_no_cali_set(client: TestClient, scripted: Scripted) -> None:
    pid = lan_printer(client)
    response = client.post(f"{slot(pid)}/configure", json={**BODY, "cali_idx": 4, "k_value": 0.02})
    assert response.status_code == 200, response.text
    assert names(scripted) == ["ams_filament_setting", "extrusion_cali_sel"]
    assert scripted.sent("extrusion_cali_sel")[0]["cali_idx"] == 4


@pytest.mark.parametrize(("ams", "tray", "flat"), [(128, 0, 0), (255, 0, 0), (255, 1, 1)])
def test_a_k_set_on_an_ht_unit_or_external_holder_sends_its_tray_index(
    client: TestClient, scripted: Scripted, ams: int, tray: int, flat: int
) -> None:
    """P33: the payload sent, never the fake's report."""
    pid = lan_printer(client)
    body = {**BODY, "k_value": 0.02, "nozzle_diameter": "0.4"}
    response = client.post(f"{slot(pid, ams, tray)}/configure", json=body)
    assert response.status_code == 200, response.text
    [entry] = scripted.sent("extrusion_cali_set")[0]["filaments"]
    assert entry["tray_id"] == flat


def test_a_k_set_on_an_ams_past_3_is_unsupported(client: TestClient, scripted: Scripted) -> None:
    report = idle()
    unit = copy.deepcopy(report["ams"]["ams"][0])
    unit["id"] = "4"
    unit["tray"] = [unit["tray"][0]]
    report["ams"]["ams"].append(unit)
    pid = lan_printer(client, report)
    body = {**BODY, "k_value": 0.02, "nozzle_diameter": "0.4"}
    response = client.post(f"{slot(pid, 4, 0)}/configure", json=body)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "unsupported_unit"
    assert scripted.published == []


def pla(store: Filament) -> UUID:
    return next(
        r.id for r in store.catalog.list("materials") if isinstance(r, Material) and r.name == "PLA"
    )


def preset_spool(client: TestClient, ref: str) -> UUID:
    """A PLA spool whose own preset for the H2C's 0.4 nozzle is ``ref``."""
    store_ = filament(client)
    spool = store_.spools.create(
        SpoolCreate(
            material_id=pla(store_), rgba="#112233FF", nozzle_temp_min=190, nozzle_temp_max=220
        ),
        actor="ann",
    )
    store_.spools.set_presets(
        spool.id,
        [
            SpoolPresetCreate(
                printer_model=PRINTER_MODEL,
                nozzle_diameter="0.4",
                slicer_filament=ref,
                slicer_filament_name="My PLA",
            )
        ],
        actor="ann",
    )
    return spool.id


def own_preset(client: TestClient) -> str:
    with filament(client).spools.transaction() as conn:
        own = create_on(
            conn,
            name="Mirrored",
            source="manual",
            key=f"attachment-{1:064d}",
            sha256=f"{1:064d}",
            doc={"name": "Mirrored"},
            made_by="test",
            bb_preset_id=141,
        )
    return sb_ref(own)


@pytest.fixture
def catalogue(monkeypatch: pytest.MonkeyPatch) -> list[PresetChoice]:
    """The native catalogue the configure reads: `BASIC` (``GFA00``, setting id
    ``GFSA00_02``) and any rows a test adds."""
    rows = [
        PresetChoice(ref=PresetRef(source="standard", id=BASIC), name=BASIC, filament_id="GFA00")
    ]

    async def read(slicing: Slicing) -> Catalogue:
        return Catalogue(
            printer=[],
            process=[],
            filament=rows,
            native_setting_ids={"printer": {}, "process": {}, "filament": {"GFSA00_02": BASIC}},
        )

    monkeypatch.setattr(slot_routes, "_catalogue", read)
    return rows


def test_configure_from_a_spool_uses_the_presets_filament_id(
    client: TestClient, scripted: Scripted, catalogue: list[PresetChoice]
) -> None:
    pid = lan_printer(client)
    own = own_preset(client)
    catalogue.append(
        PresetChoice(ref=PresetRef(source="local", id=own), name="Mirrored", filament_id="GFL99")
    )
    cases = [
        (BASIC, "GFA00", "GFSA00_02"),
        ("GFSA00_02", "GFA00", "GFSA00_02"),
        (own, "GFL99", "GFSL99"),
    ]
    for ref, filament_id, setting_id in cases:
        spool = preset_spool(client, ref)
        response = client.post(f"{slot(pid)}/configure", json={"spool_id": str(spool)})
        assert response.status_code == 200, response.text
        answer = response.json()
        assert (answer["spool_id"], answer["preset_id"], answer["preset_source"]) == (
            str(spool),
            filament_id,
            None,
        )
        setting = scripted.sent("ams_filament_setting")[-1]
        # Never the Bambuddy mirror's id (141) for a ScadBuddy preset (R-7276-15).
        assert (setting["tray_info_idx"], setting["setting_id"]) == (filament_id, setting_id)
        assert (setting["tray_type"], setting["tray_color"]) == ("PLA", "112233FF")
        assert setting["tray_sub_brands"] == "My PLA"


def test_a_spool_preset_with_no_filament_id_is_no_preset(
    client: TestClient, scripted: Scripted, catalogue: list[PresetChoice]
) -> None:
    pid = lan_printer(client)
    for ref in ("57", "Nothing @BBL H2C"):
        spool = preset_spool(client, ref)
        response = client.post(f"{slot(pid)}/configure", json={"spool_id": str(spool)})
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "no_preset"
    # A preset id sent with it stands in for the spool's.
    typed = client.post(
        f"{slot(pid)}/configure", json={"spool_id": str(spool), "preset_id": "GFL99"}
    )
    assert typed.status_code == 200, typed.text
    assert scripted.sent("ams_filament_setting")[0]["tray_info_idx"] == "GFL99"


def test_a_spool_preset_with_no_slicer_profiles_is_503(
    client: TestClient, scripted: Scripted, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def none(slicing: Slicing) -> None:
        return None

    monkeypatch.setattr(slot_routes, "_catalogue", none)
    pid = lan_printer(client)
    spool = preset_spool(client, BASIC)
    response = client.post(f"{slot(pid)}/configure", json={"spool_id": str(spool)})
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "presets_unavailable"
    assert scripted.published == []


def test_configure_from_any_spool_takes_its_values(client: TestClient, scripted: Scripted) -> None:
    pid = lan_printer(client)
    store_ = filament(client)
    spool = store_.spools.create(
        SpoolCreate(
            material_id=pla(store_), rgba="#112233FF", nozzle_temp_min=190, nozzle_temp_max=220
        ),
        actor="ann",
    )
    response = client.post(
        f"{slot(pid)}/configure", json={"spool_id": str(spool.id), "nozzle_diameter": "0.6"}
    )
    assert response.status_code == 200, response.text
    setting = scripted.sent("ams_filament_setting")[0]
    assert (setting["tray_type"], setting["tray_color"]) == ("PLA", "112233FF")
    assert (setting["nozzle_temp_min"], setting["nozzle_temp_max"]) == (190, 220)
    assert scripted.sent("extrusion_cali_sel")[0]["nozzle_diameter"] == "0.6"


def k_spool(client: TestClient, pid: str, setting_id: str | None) -> UUID:
    """A PLA spool with a stored K profile (slot 7) for the H2C's left extruder at 0.4."""
    store_ = filament(client)
    spool = store_.spools.create(
        SpoolCreate(
            material_id=pla(store_), rgba="#112233FF", nozzle_temp_min=190, nozzle_temp_max=220
        ),
        actor="ann",
    )
    store_.spools.set_k_values(
        spool.id,
        [
            SpoolKValueCreate(
                printer_id=UUID(pid),
                extruder=1,
                nozzle_diameter="0.4",
                k_value=Decimal("0.031"),
                cali_idx=7,
                setting_id=setting_id,
            )
        ],
        actor="ann",
    )
    return spool.id


@pytest.mark.parametrize("setting_id", ["GFSL99", None])
def test_a_k_profile_of_another_filament_is_sent_as_a_new_k(
    client: TestClient, scripted: Scripted, setting_id: str | None
) -> None:
    """P42: a K row made for GFL99 (or for no filament it names) on a slot configured as
    GFA00 selects no profile (-1) and sets the slot's K instead."""
    pid = lan_printer(client)
    spool = k_spool(client, pid, setting_id)
    response = client.post(
        f"{slot(pid)}/configure", json={"spool_id": str(spool), "preset_id": "GFA00"}
    )
    assert response.status_code == 200, response.text
    assert (response.json()["cali_idx"], response.json()["k_value"]) == (-1, 0.031)
    assert names(scripted) == ["ams_filament_setting", "extrusion_cali_sel", "extrusion_cali_set"]
    assert scripted.sent("extrusion_cali_sel")[0]["cali_idx"] == -1
    [entry] = scripted.sent("extrusion_cali_set")[0]["filaments"]
    assert (entry["k_value"], entry["filament_id"]) == ("0.031000", "GFA00")


def test_a_k_profile_of_the_slots_filament_is_selected(
    client: TestClient, scripted: Scripted
) -> None:
    pid = lan_printer(client)
    spool = k_spool(client, pid, "GFSA00")
    response = client.post(
        f"{slot(pid)}/configure", json={"spool_id": str(spool), "preset_id": "GFA00"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["cali_idx"] == 7
    assert names(scripted) == ["ams_filament_setting", "extrusion_cali_sel"]
    assert scripted.sent("extrusion_cali_sel")[0]["cali_idx"] == 7


def test_configure_needs_a_nozzle_the_report_names(client: TestClient, scripted: Scripted) -> None:
    report = idle()
    # Two nozzles, no Filament Track Switch, and no extruder in AMS 0's info.
    del report["device"]["fila_switch"]
    pid = lan_printer(client, report)
    response = client.post(f"{slot(pid)}/configure", json=BODY)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "no_nozzle"
    assert scripted.published == []


def test_configure_names_what_is_missing(client: TestClient, scripted: Scripted) -> None:
    report = idle()
    report["ams"]["ams"][0]["tray"][1].update({"nozzle_temp_min": "0", "nozzle_temp_max": "0"})
    pid = lan_printer(client, report)
    response = client.post(f"{slot(pid)}/configure", json={"material": "PLA"})
    assert response.status_code == 422, response.text
    assert "colour, nozzle_temp_min, nozzle_temp_max" in response.json()["detail"]
    assert scripted.published == []


def test_configure_refuses_an_unknown_spool(
    client: TestClient, scripted: Scripted, pid: str
) -> None:
    missing = "0b7a3a52-0000-4000-8000-000000000000"
    response = client.post(f"{slot(pid)}/configure", json={"spool_id": missing})
    assert response.status_code == 404, response.text


def test_reset_clears_the_saved_preset(client: TestClient, scripted: Scripted, pid: str) -> None:
    data = printers_of(client).command_data
    data.set_slot_preset(
        UUID(pid),
        0,
        1,
        SlotPresetBody(preset_id="GFSA00", preset_name="Basic", tray_info_idx="GFA00"),
    )
    response = client.post(f"{slot(pid)}/reset")
    assert response.status_code == 200, response.text
    assert response.json() == {"ams_id": 0, "tray_id": 1, "action": "reset", "message": "sent"}
    [setting] = scripted.published
    assert (setting["command"], setting["tray_type"], setting["tray_color"]) == (
        "ams_filament_setting",
        "",
        "00000000",
    )
    assert data.slot_presets(UUID(pid)) == {}


def test_refresh_sends_ams_get_rfid_and_no_gcode(
    client: TestClient, scripted: Scripted, pid: str
) -> None:
    response = client.post(f"{slot(pid, 1, 2)}/refresh")
    assert response.status_code == 200, response.text
    assert scripted.published == [
        {
            "command": "ams_get_rfid",
            "ams_id": 1,
            "slot_id": 2,
            "sequence_id": scripted.published[0]["sequence_id"],
        }
    ]


def test_refresh_refuses_while_filament_is_loaded(client: TestClient, scripted: Scripted) -> None:
    report = idle()
    report["ams"]["tray_now"] = "6"
    pid = lan_printer(client, report)
    response = client.post(f"{slot(pid)}/refresh")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "filament_loaded"


def test_load_names_the_global_tray_and_the_hotend(
    client: TestClient, scripted: Scripted, pid: str
) -> None:
    response = client.post(f"{slot(pid)}/load")
    assert response.status_code == 200, response.text
    assert response.json() == {"ams_id": 0, "tray_id": 1, "action": "load", "message": "sent"}
    [load] = scripted.published
    # AMS 0 slot 1 is global tray 1; inlet A is the left extruder, 1.
    assert (load["command"], load["ams_id"], load["slot_id"], load["target"]) == (
        "ams_change_filament",
        0,
        1,
        1,
    )
    assert load["extruder_id"] == 1


def test_a_load_with_no_switch_names_no_hotend(client: TestClient, scripted: Scripted) -> None:
    report = idle()
    del report["device"]["fila_switch"]
    pid = lan_printer(client, report)
    assert client.post(f"{slot(pid)}/load").status_code == 200
    assert "extruder_id" not in scripted.published[0]


def test_a_load_whose_hotend_is_unknown_is_refused(client: TestClient, scripted: Scripted) -> None:
    report = idle()
    report["ams"]["ams"][0]["info"] = "12002E03"
    pid = lan_printer(client, report)
    response = client.post(f"{slot(pid)}/load")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "extruder_unknown"
    assert scripted.published == []


def test_a_slot_with_no_spool_is_not_loaded(client: TestClient, scripted: Scripted) -> None:
    report = idle()
    report["ams"]["tray_exist_bits"] = "1fffd"
    report["ams"]["ams"][0]["tray"][1].update({"tray_type": "", "state": 9})
    pid = lan_printer(client, report)
    response = client.post(f"{slot(pid)}/load")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "tray_empty"


def test_a_loaded_slot_is_not_loaded_again(client: TestClient, scripted: Scripted) -> None:
    pid = lan_printer(client, feeding(idle(), 1, tray_now=1))
    response = client.post(f"{slot(pid)}/load")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "already_loaded"


def test_unload_of_a_slot_not_loaded_is_409(
    client: TestClient, scripted: Scripted, pid: str
) -> None:
    response = client.post(f"{slot(pid, 128, 0)}/unload")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "not_loaded"
    assert scripted.published == []


def test_unload_pulls_back_the_loaded_slot(client: TestClient, scripted: Scripted) -> None:
    pid = lan_printer(client, feeding(idle(), HT_A, tray_now=128))
    response = client.post(f"{slot(pid, 128, 0)}/unload")
    assert response.status_code == 200, response.text
    [unload] = scripted.published
    assert (unload["ams_id"], unload["slot_id"], unload["target"]) == (128, 255, 255)


def h2c_both_loaded() -> dict[str, Any]:
    """Both extruders loaded, as ``lan-report-h2c.json`` reports them: ``snow`` 1 (AMS-A
    slot 1, which ``tray_now`` names) and 32768 (HT-A, on the inactive extruder)."""
    return feeding(idle(), 1, HT_A, tray_now=1)


def test_an_ht_unit_on_the_inactive_extruder_is_unloaded(
    client: TestClient, scripted: Scripted
) -> None:
    """P40: ``tray_now`` names only the active extruder's slot."""
    pid = lan_printer(client, h2c_both_loaded())
    response = client.post(f"{slot(pid, 128, 0)}/unload")
    assert response.status_code == 200, response.text
    [unload] = scripted.published
    assert (unload["command"], unload["ams_id"], unload["target"]) == (
        "ams_change_filament",
        128,
        255,
    )


def test_an_ht_unit_on_the_inactive_extruder_is_already_loaded(
    client: TestClient, scripted: Scripted
) -> None:
    pid = lan_printer(client, h2c_both_loaded())
    response = client.post(f"{slot(pid, 128, 0)}/load")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "already_loaded"
    assert scripted.published == []


@pytest.mark.parametrize(("right", "left"), [(HT_A, EMPTY), (EMPTY, HT_A)])
def test_refresh_refuses_a_slot_loaded_on_either_extruder(
    client: TestClient, scripted: Scripted, right: int, left: int
) -> None:
    """``tray_now`` says nothing is loaded; an extruder's ``snow`` says HT-A is."""
    pid = lan_printer(client, feeding(idle(), right, left))
    response = client.post(f"{slot(pid, 1, 2)}/refresh")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "filament_loaded"
    assert scripted.published == []


def test_an_external_holder_keeps_tray_now(client: TestClient, scripted: Scripted) -> None:
    """No cited source decodes an external holder's ``snow`` (the idle H2C reports 0xFEFF
    with nothing loaded), so Ext-L is loaded only when ``tray_now`` names it."""
    pid = lan_printer(client, feeding(idle(), EMPTY, 0xFEFF, tray_now=254))
    view = client.get(f"/api/v1/printers/{pid}/ams").json()
    external = next(u for u in view["units"] if u["ams_id"] == 255)
    assert [s["loaded"] for s in external["slots"]] == [True, False]


def test_a_slot_the_report_does_not_show_is_refused(client: TestClient, scripted: Scripted) -> None:
    report = idle()
    report["ams"]["ams"] = report["ams"]["ams"][:3]
    pid = lan_printer(client, report)
    response = client.post(f"{slot(pid, 3, 0)}/reset")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "no_slot"


def test_slots_no_printer_has_are_422(client: TestClient, scripted: Scripted, pid: str) -> None:
    assert client.post(f"{slot(pid, 255, 0)}/refresh").status_code == 422
    assert client.post(f"{slot(pid, 5, 0)}/load").status_code == 422
    assert client.post(f"{slot(pid, 128, 1)}/reset").status_code == 422
    assert client.post(f"{slot(pid, 0, 4)}/reset").status_code == 422
    assert scripted.published == []


def test_an_unacknowledged_command_says_so(
    client: TestClient, scripted: Scripted, pid: str
) -> None:
    scripted.silent("ams_get_rfid")
    response = client.post(f"{slot(pid)}/refresh")
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "sent; the printer did not acknowledge it in time"


def test_a_refused_command_is_409_printer_refused(
    client: TestClient, scripted: Scripted, pid: str
) -> None:
    scripted.refuse("ams_filament_setting", "busy")
    response = client.post(f"{slot(pid)}/configure", json=BODY)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "printer_refused"


def follow(client: TestClient, pid: str, pending: httpx.Response) -> httpx.Response:
    assert pending.status_code == 202, pending.text
    url = f"/api/v1/printers/{pid}/commands/{pending.json()['workflow_id']}"
    for _ in range(100):
        response: httpx.Response = client.get(url)
        if response.status_code != 202:
            return response
        time.sleep(0.1)
    raise AssertionError(f"{url} is still running")


@pytest.mark.parametrize(
    ("action", "body"),
    [
        ("configure", {**BODY, "k_value": 0.02}),
        ("reset", None),
        ("refresh", None),
        ("load", None),
    ],
)
def test_a_followed_command_answers_as_its_route(
    client: TestClient,
    scripted: Scripted,
    pid: str,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    body: dict[str, Any] | None,
) -> None:
    """P37: a 202 followed to its end answers the route's own model, a configure of
    three commands included."""
    direct = client.post(
        f"{slot(pid)}/{action}", json=body, headers={"Idempotency-Key": f"direct-{action}"}
    )
    assert direct.status_code == 200, direct.text
    monkeypatch.setattr(lan_command_module, "LAN_ROUTE_WAIT_S", 0)
    pending = client.post(
        f"{slot(pid)}/{action}", json=body, headers={"Idempotency-Key": f"slow-{action}"}
    )
    monkeypatch.setattr(lan_command_module, "LAN_ROUTE_WAIT_S", 10.0)
    followed = follow(client, pid, pending)
    assert followed.status_code == 200, followed.text
    assert followed.json() == direct.json()
    expected = {"ams_id": 0, "tray_id": 1, "message": "sent"}
    if action != "configure":
        expected["action"] = action
    assert followed.json().items() >= expected.items()


class Later(datetime):
    """Ten minutes on: the stored report is stale by then."""

    @classmethod
    def now(cls, tz: Any = None) -> Later:
        return cls.fromtimestamp((datetime.now(tz) + timedelta(minutes=10)).timestamp(), tz)


def test_a_resent_key_answers_after_the_printer_drops_off(
    client: TestClient, scripted: Scripted, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P38: a key whose command exists answers its result, whatever the report says now."""
    first = client.post(f"{slot(pid)}/configure", json=BODY, headers={"Idempotency-Key": "k1"})
    assert first.status_code == 200, first.text
    monkeypatch.setattr(slot_routes, "datetime", Later)
    again = client.post(f"{slot(pid)}/configure", json=BODY, headers={"Idempotency-Key": "k1"})
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert len(scripted.sent("ams_filament_setting")) == 1
    other = client.post(f"{slot(pid)}/configure", json=BODY, headers={"Idempotency-Key": "k2"})
    assert other.status_code == 409, other.text
    assert other.json()["code"] == "printer_offline"


def test_a_resent_load_answers_the_first_and_sends_nothing(
    client: TestClient, scripted: Scripted, pid: str
) -> None:
    """P41: a key that started the command skips every check, the slot now being loaded
    included, and the printer gets nothing again."""
    first = client.post(f"{slot(pid)}/load", headers={"Idempotency-Key": "load-k"})
    assert first.status_code == 200, first.text
    printers_of(client).reports.put(pid, feeding(idle(), 1, tray_now=1), datetime.now(UTC))
    again = client.post(f"{slot(pid)}/load", headers={"Idempotency-Key": "load-k"})
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert len(scripted.published) == 1


def test_a_key_reused_for_unload_never_answers_the_load(
    client: TestClient, scripted: Scripted, pid: str
) -> None:
    """P9: load and unload send the same command, so the route's action is in the run's
    workflow id: one key reused for the unload is a run of its own, not the load's answer."""
    first = client.post(f"{slot(pid)}/load", headers={"Idempotency-Key": "both-k"})
    assert first.status_code == 200, first.text
    assert first.json()["action"] == "load"
    printers_of(client).reports.put(pid, feeding(idle(), left=1, tray_now=1), datetime.now(UTC))
    then = client.post(f"{slot(pid)}/unload", headers={"Idempotency-Key": "both-k"})
    assert then.status_code == 200, then.text
    assert then.json()["action"] == "unload"
    assert len(scripted.published) == 2


def test_a_resent_configure_answers_the_first_bodys_values(
    client: TestClient, scripted: Scripted, pid: str
) -> None:
    """P41: the answer is the started run's, never the resend's body."""
    first = client.post(f"{slot(pid)}/configure", json=BODY, headers={"Idempotency-Key": "cfg-k"})
    assert first.status_code == 200, first.text
    other = {**BODY, "colour": "#FF0000", "material": "PETG", "k_value": 0.02}
    again = client.post(f"{slot(pid)}/configure", json=other, headers={"Idempotency-Key": "cfg-k"})
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert (again.json()["colour"], again.json()["material"]) == ("#FFFFFFFF", "PLA")
    assert names(scripted) == ["ams_filament_setting", "extrusion_cali_sel"]


def test_a_resent_reset_clears_no_newer_preset(
    client: TestClient, scripted: Scripted, pid: str
) -> None:
    """P43: the first reset clears the saved preset; one saved after it survives a resend."""
    data = printers_of(client).command_data
    first = client.post(f"{slot(pid)}/reset", headers={"Idempotency-Key": "reset-k"})
    assert first.status_code == 200, first.text
    newer = SlotPresetBody(preset_id="GFSG00", preset_name="PETG", tray_info_idx="GFG00")
    data.set_slot_preset(UUID(pid), 0, 1, newer)
    again = client.post(f"{slot(pid)}/reset", headers={"Idempotency-Key": "reset-k"})
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert data.slot_presets(UUID(pid))[0, 1].preset_id == "GFSG00"
    assert len(scripted.published) == 1


def test_a_reset_still_running_keeps_the_saved_preset(
    client: TestClient, scripted: Scripted, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 202 is no acknowledged reset: the saved preset stays."""
    data = printers_of(client).command_data
    saved = SlotPresetBody(preset_id="GFSA00", preset_name="Basic", tray_info_idx="GFA00")
    data.set_slot_preset(UUID(pid), 0, 1, saved)
    monkeypatch.setattr(lan_command_module, "LAN_ROUTE_WAIT_S", 0)
    pending = client.post(f"{slot(pid)}/reset", headers={"Idempotency-Key": "slow-reset"})
    monkeypatch.setattr(lan_command_module, "LAN_ROUTE_WAIT_S", 10.0)
    assert pending.status_code == 202, pending.text
    assert data.slot_presets(UUID(pid))[0, 1].preset_id == "GFSA00"
    assert follow(client, pid, pending).status_code == 200


def test_an_unacknowledged_reset_keeps_the_saved_preset(
    client: TestClient, scripted: Scripted, pid: str
) -> None:
    data = printers_of(client).command_data
    saved = SlotPresetBody(preset_id="GFSA00", preset_name="Basic", tray_info_idx="GFA00")
    data.set_slot_preset(UUID(pid), 0, 1, saved)
    scripted.silent("ams_filament_setting")
    response = client.post(f"{slot(pid)}/reset")
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "sent; the printer did not acknowledge it in time"
    assert data.slot_presets(UUID(pid))[0, 1].preset_id == "GFSA00"


def test_a_key_with_a_slash_can_be_followed(
    client: TestClient, scripted: Scripted, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key is hashed into the command id, so the follow route takes it."""
    monkeypatch.setattr(lan_command_module, "LAN_ROUTE_WAIT_S", 0)
    pending = client.post(f"{slot(pid)}/refresh", headers={"Idempotency-Key": "a/b c"})
    monkeypatch.setattr(lan_command_module, "LAN_ROUTE_WAIT_S", 10.0)
    followed = follow(client, pid, pending)
    assert followed.status_code == 200, followed.text
    assert followed.json()["action"] == "refresh"


def test_two_first_sends_of_one_key_answer_the_started_run(
    client: TestClient, scripted: Scripted, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two first sends of one key at once both miss `_resent`; Temporal joins the second to
    the first's run, and the second answers that run's values, never its own body's."""

    async def missed(*_: Any) -> None:
        return None

    monkeypatch.setattr(slot_routes, "_resent", missed)
    first = client.post(f"{slot(pid)}/configure", json=BODY, headers={"Idempotency-Key": "race"})
    assert first.status_code == 200, first.text
    other = {**BODY, "colour": "#FF0000"}
    again = client.post(f"{slot(pid)}/configure", json=other, headers={"Idempotency-Key": "race"})
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert again.json()["colour"] == "#FFFFFFFF"
    assert len(scripted.sent("ams_filament_setting")) == 1


def test_a_slot_preset_is_saved_and_cleared(
    client: TestClient, scripted: Scripted, pid: str
) -> None:
    saved = client.put(
        f"{slot(pid)}/preset",
        json={"preset_id": "G" * 64, "preset_name": "Long", "tray_info_idx": "G" * 64},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"preset_id": "G" * 64, "preset_name": "Long", "tray_info_idx": "G" * 64}
    presets = printers_of(client).command_data.slot_presets(UUID(pid))
    assert presets[(0, 1)].preset_name == "Long"
    assert client.delete(f"{slot(pid)}/preset").status_code == 204
    assert printers_of(client).command_data.slot_presets(UUID(pid)) == {}
    assert scripted.published == []


@pytest.mark.parametrize(
    ("ams", "tray", "body"),
    [
        (16, 0, None),
        (0, 4, None),
        (128, 1, None),
        (255, 2, None),
        (300, 0, None),
        (0, 0, {"preset_id": "", "preset_name": "x"}),
        (0, 0, {"preset_id": "a b", "preset_name": "x"}),
        (0, 0, {"preset_id": "G" * 65, "preset_name": "x"}),
        (0, 0, {"preset_id": "GFA00", "preset_name": ""}),
        (0, 0, {"preset_id": "GFA00", "preset_name": "a\x00b"}),
        (0, 0, {"preset_id": "GFA00", "preset_name": "x" * 129}),
        (0, 0, {"preset_id": "GFA00", "preset_name": "x", "tray_info_idx": "a/b"}),
    ],
    ids=[
        "ams-16",
        "tray-4",
        "ht-tray-1",
        "external-tray-2",
        "ams-300",
        "empty-id",
        "id-space",
        "long-id",
        "empty-name",
        "nul-name",
        "long-name",
        "bad-filament-id",
    ],
)
def test_a_bad_slot_preset_is_422_not_500(
    client: TestClient, pid: str, ams: int, tray: int, body: dict[str, Any] | None
) -> None:
    """P35: refused before the store, never a database error."""
    good = {"preset_id": "GFA00", "preset_name": "Basic"}
    response = client.put(f"{slot(pid, ams, tray)}/preset", json=body or good)
    assert response.status_code == 422, response.text
    if body is None:
        assert client.delete(f"{slot(pid, ams, tray)}/preset").status_code == 422


def test_a_label_names_the_place_only(client: TestClient, scripted: Scripted, pid: str) -> None:
    response = client.put(f"/api/v1/printers/{pid}/ams/0/label", json={"label": "Left rack"})
    assert response.status_code == 200, response.text
    assert response.json()["place"]["name"] == "Left rack"
    assert filament(client).locations.ams_units(UUID(pid))[0].name == "Left rack"
    assert client.get(f"/api/v1/printers/{pid}/ams").json()["units"][0]["label"] == "Left rack"
    cleared = client.delete(f"/api/v1/printers/{pid}/ams/0/label")
    assert cleared.status_code == 200, cleared.text
    assert (cleared.json()["label"], cleared.json()["place"]["name"]) == (None, "H2C AMS-A")
    assert scripted.published == []


def test_two_units_may_share_a_label(client: TestClient, pid: str) -> None:
    places = filament(client).locations
    places.name_ams_unit(UUID(pid), 1, "Shelf")
    response = client.put(f"/api/v1/printers/{pid}/ams/0/label", json={"label": "Shelf"})
    assert response.status_code == 200, response.text
    assert response.json()["place_problem"] is None
    names_ = {k: v.name for k, v in places.ams_units(UUID(pid)).items()}
    assert (names_[0], names_[1]) == ("Shelf", "Shelf")


def test_a_printer_deleted_after_the_label_says_no_printer(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def gone(*_args: object) -> None:
        raise CatalogRefError()

    monkeypatch.setattr(filament(client).locations, "name_ams_unit", gone)
    response = client.put(f"/api/v1/printers/{pid}/ams/0/label", json={"label": "Shelf"})
    assert response.status_code == 200, response.text
    assert (response.json()["place"], response.json()["place_problem"]) == (None, "no_printer")


def test_a_bad_label_is_a_422(client: TestClient, pid: str) -> None:
    for body in ({"label": ""}, {"label": "x" * 101}, {"label": "a\nb"}):
        assert client.put(f"/api/v1/printers/{pid}/ams/0/label", json=body).status_code == 422
    assert (
        client.put(f"/api/v1/printers/{pid}/ams/16/label", json={"label": "x"}).status_code == 422
    )


def test_an_unknown_printer_is_a_404(client: TestClient, scripted: Scripted) -> None:
    missing = "0b7a3a52-0000-4000-8000-000000000000"
    assert client.post(f"{slot(missing)}/reset").status_code == 404
    assert client.get(f"/api/v1/printers/{missing}/ams").status_code == 404
    assert (
        client.put(
            f"{slot(missing)}/preset", json={"preset_id": "G", "preset_name": "x"}
        ).status_code
        == 404
    )
    assert (
        client.put(f"/api/v1/printers/{missing}/ams/0/label", json={"label": "x"}).status_code
        == 404
    )


@pytest.fixture
def bb_only(client: TestClient) -> Iterator[str]:
    printers = printers_of(client)
    row = printers.store.create(PrinterCreate(serial=SERIAL, name="Bare"))
    printers.store.link_bambuddy(row.id, 3)
    yield row.id


def test_a_bambuddy_only_printer_is_not_lan_linked(
    client: TestClient, scripted: Scripted, bb_only: str
) -> None:
    for response in (
        client.get(f"/api/v1/printers/{bb_only}/ams"),
        client.post(f"{slot(bb_only)}/configure", json=BODY),
        client.post(f"{slot(bb_only)}/load"),
    ):
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "not_lan_linked"
    assert scripted.published == []


def test_a_printer_linked_both_ways_never_calls_bambuddy(
    client: TestClient, scripted: Scripted, spans: InMemorySpanExporter
) -> None:
    """Bambuddy has a URL and the row its id, and still no route calls it."""
    configure(client)
    pid = lan_printer(client, feeding(idle(), 1 << 8 | 1, tray_now=5))
    printers_of(client).store.link_bambuddy(pid, 3)
    preset = {"preset_id": "GFSA00", "preset_name": "Basic"}
    with forbid_bambuddy(spans, BASE):
        answers = [
            client.get(f"/api/v1/printers/{pid}/ams"),
            client.post(f"{slot(pid)}/configure", json=BODY),
            client.post(f"{slot(pid)}/reset"),
            client.post(f"{slot(pid, 1, 1)}/unload"),
            client.post(f"{slot(pid, 2, 0)}/load"),
            client.put(f"{slot(pid)}/preset", json=preset),
            client.put(f"/api/v1/printers/{pid}/ams/0/label", json={"label": "Left"}),
            client.delete(f"/api/v1/printers/{pid}/ams/0/label"),
        ]
        assert client.delete(f"{slot(pid)}/preset").status_code == 204
    for response in answers:
        assert response.status_code == 200, response.text
    tray_now_reset = idle()
    tray_now_reset["ams"]["tray_now"] = "255"
    printers_of(client).reports.put(pid, tray_now_reset, datetime.now(UTC))
    with forbid_bambuddy(spans, BASE):
        assert client.post(f"{slot(pid)}/refresh").status_code == 200
