"""The pure parts of the AMS slot routes over the LAN (#7276 Task 8)."""

from __future__ import annotations

import pytest

from scadbuddy.bambuddy.catalogue import Catalogue, PresetChoice
from scadbuddy.bambuddy.models import AmsTray, AmsUnit, PresetRef, PrinterStatus
from scadbuddy.core.problems import ApiError
from scadbuddy.printers import ams_slots as s
from scadbuddy.printers.lan_command_models import (
    LanCommandOutcome,
    LanCommandReply,
    LanCommandRun,
)
from scadbuddy.printers.lan_commands import SlotParams, UnsupportedUnitError
from scadbuddy.printers.native import command_view
from scadbuddy.workflows.print_models import LAN_REPLY_WAIT_AMS

BASIC = "Bambu PLA Basic @BBL H2C"
OWN = "sb-12"


def catalogue() -> Catalogue:
    return Catalogue(
        printer=[],
        process=[],
        filament=[
            PresetChoice(
                ref=PresetRef(source="standard", id=BASIC), name=BASIC, filament_id="GFA00"
            ),
            PresetChoice(ref=PresetRef(source="standard", id="Bare"), name="Bare"),
            PresetChoice(ref=PresetRef(source="local", id=OWN), name="Mine", filament_id="GFL99"),
        ],
        native_setting_ids={"printer": {}, "process": {}, "filament": {"GFSA00_02": BASIC}},
        local_aliases={"141": OWN},
    )


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        (BASIC, s.SlotIds("GFA00", "GFSA00_02")),
        ("GFSA00_02", s.SlotIds("GFA00", "GFSA00_02")),
        ("GFA00", s.SlotIds("GFA00", None)),
        (OWN, s.SlotIds("GFL99", None)),
        ("Bare", None),
        ("141", None),
        ("sb-99", None),
        ("Nothing", None),
    ],
)
def test_slot_ids_read_the_native_catalogue(ref: str, expected: s.SlotIds | None) -> None:
    assert s.slot_ids(catalogue(), ref) == expected


@pytest.mark.parametrize(
    ("material", "generic"),
    [
        ("PLA", "GFL99"),
        ("petg", "GFG99"),
        ("PETG HF", "GFG96"),
        ("PLA Matte", "GFL99"),
        ("PA-CF", "GFN98"),
        ("Wood", ""),
    ],
)
def test_a_material_takes_its_generic_filament_id(material: str, generic: str) -> None:
    assert s.generic_filament_id(material) == generic


@pytest.mark.parametrize(
    ("filament_id", "setting_id"),
    [
        ("GFL99", "GFSL99"),
        ("GFS99", "GFSS99"),
        ("GFSL05", "GFSL05"),
        ("GFSNL03", "GFSNLS03"),
        ("P4d64437", "P4d64437"),
        ("", ""),
    ],
)
def test_a_filament_id_back_fills_its_setting_id(filament_id: str, setting_id: str) -> None:
    assert s.filament_id_to_setting_id(filament_id) == setting_id


def params(**values: object) -> SlotParams:
    base: dict[str, object] = {
        "tray_info_idx": "GFA00",
        "tray_type": "PLA",
        "tray_sub_brands": "Basic",
        "tray_color": "112233FF",
        "nozzle_temp_min": 190,
        "nozzle_temp_max": 220,
        "nozzle_diameter": "0.4",
        "preset_setting_id": "GFSA00_02",
    }
    return SlotParams.model_validate({**base, **values})


def test_configure_commands_are_setting_then_selection() -> None:
    commands = s.configure_commands(params(), 0, 2, external_count=2, printer_id="p")
    assert [c.name for c in commands] == ["ams_filament_setting", "extrusion_cali_sel"]
    assert all(c.kind == "write" and c.wait_s == LAN_REPLY_WAIT_AMS for c in commands)
    setting = commands[0].payload["print"]
    assert (setting["slot_id"], setting["setting_id"]) == (2, "GFSA00_02")
    sel = commands[1].payload["print"]
    assert (sel["tray_id"], sel["cali_idx"], sel["filament_id"]) == (2, -1, "GFA00")


def test_a_new_k_value_is_set_on_the_slot() -> None:
    commands = s.configure_commands(
        params(cali_idx=-1, k_value=0.025, k_setting_id="PFK"),
        1,
        3,
        external_count=2,
        printer_id="p",
    )
    assert [c.name for c in commands][-1] == "extrusion_cali_set"
    [entry] = commands[-1].payload["print"]["filaments"]
    assert (entry["tray_id"], entry["setting_id"], entry["name"]) == (7, "PFK", "Basic")


def test_a_selected_profile_or_no_k_sets_nothing() -> None:
    for values in ({"cali_idx": 3, "k_value": 0.02}, {"k_value": 0.0}, {}):
        commands = s.configure_commands(params(**values), 0, 0, external_count=2, printer_id="p")
        assert len(commands) == 2


def test_a_k_set_on_an_ams_past_3_is_unsupported() -> None:
    with pytest.raises(UnsupportedUnitError):
        s.configure_commands(params(k_value=0.02), 4, 0, external_count=2, printer_id="p")


def status(**values: object) -> PrinterStatus:
    base = PrinterStatus(
        id=0,
        name="H2C",
        connected=True,
        state="IDLE",
        ams=[AmsUnit(id=0, tray=[AmsTray(id=0, tray_type="PLA", state=11)])],
        tray_now=255,
    )
    return base.model_copy(update=values)


def test_a_stale_report_is_printer_offline() -> None:
    with pytest.raises(ApiError) as caught:
        s.guard(status(connected=False), "reset", 0, 0)
    assert caught.value.extensions["code"] == "printer_offline"
    assert caught.value.detail == "ScadBuddy has no fresh report from H2C"


def test_an_unload_of_another_slot_is_not_loaded() -> None:
    with pytest.raises(ApiError) as caught:
        s.guard(status(tray_now=3), "unload", 0, 0)
    assert caught.value.extensions["code"] == "not_loaded"
    s.guard(status(tray_now=0), "unload", 0, 0)


def test_snow_overrules_tray_now_for_an_ams_slot() -> None:
    """P40: with each extruder's slot reported, ``tray_now`` (the active one's) is not read
    for an AMS or AMS-HT slot."""
    both = status(tray_now=0, extruder_loaded=[(0, 0), (128, 0)])
    assert s.is_loaded(both, 128, 0) and s.is_loaded(both, 0, 0)
    stale = status(tray_now=0, extruder_loaded=[None, None])
    assert not s.is_loaded(stale, 0, 0)
    assert s.any_loaded(stale)
    assert s.is_loaded(status(tray_now=0), 0, 0)


def test_an_external_holder_reads_tray_now_only() -> None:
    assert s.is_loaded(status(tray_now=254, extruder_loaded=[None, None]), 255, 0)
    assert not s.is_loaded(status(tray_now=255, extruder_loaded=[None, None]), 255, 1)


@pytest.mark.parametrize(
    ("report", "temps"),
    [
        ({"nozzle_temper": 245.0}, (245, 0)),
        (
            {
                "nozzle_temper": 30.0,
                "device": {
                    "extruder": {
                        "info": [{"id": 0, "temp": 220 * 65536 + 218}, {"id": 1, "temp": 40}]
                    }
                },
            },
            (40, 218),
        ),
        ({}, (0, 0)),
    ],
)
def test_nozzle_temps_read_the_report(report: dict[str, object], temps: tuple[int, int]) -> None:
    assert s.nozzle_temps(report) == temps


def run_of(answer: dict[str, object], *names: str) -> LanCommandRun:
    return LanCommandRun.model_validate(
        {
            "commands": [
                {"printer_id": "p", "name": n, "payload": {}, "wait_s": 1, "kind": "write"}
                for n in names
            ],
            "answer": answer,
        }
    )


def test_a_slot_run_answers_its_routes_model() -> None:
    done = LanCommandOutcome(replies=[LanCommandReply(confirmed=True)])
    command = run_of({"ams_id": 0, "tray_id": 1, "action": "refresh"}, "ams_get_rfid")
    assert command_view(command, done) == s.SlotCommandSent(
        ams_id=0, tray_id=1, action="refresh", message="sent"
    )
    configured = s.SlotConfigured(
        ams_id=0,
        tray_id=1,
        spool_id=None,
        material="PLA",
        colour="#112233FF",
        preset_id="GFL99",
        preset_name="PLA",
        nozzle_temp_min=190,
        nozzle_temp_max=220,
        nozzle_diameter="0.4",
        cali_idx=-1,
        k_value=0.02,
    )
    run = run_of(
        configured.model_dump(mode="json"),
        "ams_filament_setting",
        "extrusion_cali_sel",
        "extrusion_cali_set",
    )
    unsure = LanCommandOutcome(
        replies=[
            LanCommandReply(confirmed=True),
            LanCommandReply(confirmed=False),
            LanCommandReply(confirmed=True),
        ]
    )
    assert command_view(run, unsure) == configured.model_copy(
        update={"message": "sent; the printer did not acknowledge it in time"}
    )
