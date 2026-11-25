"""The LAN command payloads (#7276, plan 2026-11-25, Task 1): each builder's shape as
Bambuddy's ``bambu_mqtt.py`` at the pinned commit sends it, with a fresh
``sequence_id``."""

from __future__ import annotations

from dataclasses import astuple
from functools import partial

import pytest

from scadbuddy.printers.lan_commands import (
    BUILDERS,
    RIGHT_DEFAULT,
    UNLOAD_DEFAULT,
    KProfileRow,
    UnsupportedUnitError,
    ams_change_filament_load,
    ams_change_filament_unload,
    ams_filament_setting,
    ams_get_rfid,
    ams_reset,
    cali_set_tray,
    extrusion_cali_del,
    extrusion_cali_get,
    extrusion_cali_sel,
    extrusion_cali_set,
    extrusion_cali_set_slot,
    matches,
    refusal,
    wire_slot,
)

PINNED = "d3be761f3bd0b6bac78045dc53599331d64c3672"


def test_cali_get_shape() -> None:
    assert extrusion_cali_get("0.4", "7") == {
        "print": {
            "command": "extrusion_cali_get",
            "filament_id": "",
            "nozzle_diameter": "0.4",
            "sequence_id": "7",
        }
    }


@pytest.mark.parametrize(
    ("ams_id", "tray_id", "external_count", "local", "cali_sel_ids"),
    [
        (0, 2, 1, (0, 2, 2), (0, 2, 2)),  # AMS 0: local tray 2, global 2
        (1, 3, 1, (1, 3, 3), (1, 7, 3)),  # AMS 1 slot 3: global 7
        (3, 1, 1, (3, 1, 1), (3, 13, 1)),  # AMS 3 slot 1: global 13
        (4, 1, 1, (4, 1, 0), (4, 1, 0)),  # AMS 4: Bambuddy's single-slot branch
        (128, 0, 1, (128, 0, 0), (128, 0, 0)),  # AMS-HT
        (255, 0, 1, (255, 254, 0), (254, 254, 0)),  # single external holder
        (255, 0, 2, (254, 0, 0), (254, 254, 0)),  # Ext-L on a printer with two
        (255, 1, 2, (255, 0, 0), (255, 255, 0)),  # Ext-R on a printer with two
    ],
)
def test_wire_ids(
    ams_id: int,
    tray_id: int,
    external_count: int,
    local: tuple[int, int, int],
    cali_sel_ids: tuple[int, int, int],
) -> None:
    assert astuple(wire_slot(ams_id, tray_id, external_count=external_count)) == local
    setting = ams_filament_setting(
        wire_slot(ams_id, tray_id, external_count=external_count),
        tray_info_idx="GFL05",
        tray_type="PLA",
        tray_sub_brands="PLA Basic",
        tray_color="FF0000FF",
        nozzle_temp_min=190,
        nozzle_temp_max=230,
        setting_id=None,
        seq="2",
    )["print"]
    assert (setting["ams_id"], setting["tray_id"], setting["slot_id"]) == local
    body = extrusion_cali_sel(
        ams_id,
        tray_id,
        external_count=external_count,
        cali_idx=3,
        filament_id="GFL05",
        nozzle_diameter="0.4",
        seq="1",
    )["print"]
    assert (body["ams_id"], body["tray_id"], body["slot_id"]) == cali_sel_ids
    assert "setting_id" not in body
    assert body["sequence_id"] == "1"


@pytest.mark.parametrize(
    ("ams_id", "tray_id", "cali_tray"),
    [
        (0, 0, 0),
        (0, 3, 3),
        (1, 2, 6),
        (2, 1, 9),
        (3, 3, 15),
        (128, 0, 0),  # HT-A: (ams-128)*4
        (130, 0, 8),  # HT-C
        (255, 0, 0),  # Ext-L / the single holder: the raw tray_id
        (255, 1, 1),  # Ext-R: the raw tray_id
    ],
)
def test_cali_set_slot_tray_index(ams_id: int, tray_id: int, cali_tray: int) -> None:
    assert cali_set_tray(ams_id, tray_id) == cali_tray
    body = extrusion_cali_set_slot(
        ams_id,
        tray_id,
        k_value=0.02,
        nozzle_diameter="0.4",
        filament_id="GFL05",
        setting_id="GFSL05_07",
        name="PLA Basic",
        seq="5",
    )
    assert body == {
        "print": {
            "command": "extrusion_cali_set",
            "filaments": [
                {
                    "ams_id": 0,
                    "cali_idx": -1,
                    "extruder_id": 0,
                    "filament_id": "GFL05",
                    "k_value": "0.020000",
                    "n_coef": "1.400000",
                    "name": "PLA Basic",
                    "nozzle_diameter": "0.4",
                    "nozzle_id": "HS00-0.4",
                    "setting_id": "GFSL05_07",
                    "tray_id": cali_tray,
                }
            ],
            "nozzle_diameter": "0.4",
            "sequence_id": "5",
        }
    }


@pytest.mark.parametrize("ams_id", [4, 15])
def test_cali_set_slot_refuses_an_ams_past_3(ams_id: int) -> None:
    with pytest.raises(UnsupportedUnitError):
        cali_set_tray(ams_id, 1)
    with pytest.raises(UnsupportedUnitError):
        extrusion_cali_set_slot(
            ams_id,
            1,
            k_value=0.02,
            nozzle_diameter="0.4",
            filament_id="GFL05",
            setting_id="GFSL05_07",
            name="PLA Basic",
            seq="5",
        )


def test_cali_set_sends_tray_zero_ams_zero_and_no_n_coef() -> None:
    row = KProfileRow(
        cali_idx=-1,
        extruder_id=1,
        nozzle_id="HH00-0.4",
        nozzle_diameter="0.4",
        filament_id="GFL05",
        name="PLA Basic HF",
        k_value="0.025000",
        setting_id="PF12345678901234567",
    )
    body = extrusion_cali_set([row, row], "0.4", "11")
    entry = {
        "ams_id": 0,
        "cali_idx": -1,
        "extruder_id": 1,
        "filament_id": "GFL05",
        "k_value": "0.025000",
        "n_coef": "0.000000",
        "name": "PLA Basic HF",
        "nozzle_diameter": "0.4",
        "nozzle_id": "HH00-0.4",
        "setting_id": "PF12345678901234567",
        "tray_id": 0,
    }
    assert body == {
        "print": {
            "command": "extrusion_cali_set",
            "filaments": [entry, entry],
            "nozzle_diameter": "0.4",
            "sequence_id": "11",
        }
    }


def test_colour_goes_upper_case() -> None:
    body = ams_filament_setting(
        wire_slot(0, 1, external_count=1),
        tray_info_idx="GFL05",
        tray_type="PLA",
        tray_sub_brands="PLA Basic",
        tray_color="#ff00aaff",
        nozzle_temp_min=190,
        nozzle_temp_max=230,
        setting_id="GFSL05_07",
        seq="3",
    )
    assert body == {
        "print": {
            "command": "ams_filament_setting",
            "ams_id": 0,
            "tray_id": 1,
            "slot_id": 1,
            "tray_info_idx": "GFL05",
            "tray_type": "PLA",
            "tray_sub_brands": "PLA Basic",
            "tray_color": "FF00AAFF",
            "nozzle_temp_min": 190,
            "nozzle_temp_max": 230,
            "sequence_id": "3",
            "setting_id": "GFSL05_07",
        }
    }


def test_setting_without_a_setting_id_leaves_it_out() -> None:
    body = ams_filament_setting(
        wire_slot(0, 0, external_count=1),
        tray_info_idx="GFL99",
        tray_type="PLA",
        tray_sub_brands="",
        tray_color="FFFFFFFF",
        nozzle_temp_min=190,
        nozzle_temp_max=230,
        setting_id="",
        seq="3",
    )["print"]
    assert "setting_id" not in body


def test_reset_clears_every_value() -> None:
    assert ams_reset(wire_slot(255, 1, external_count=2), "4") == {
        "print": {
            "command": "ams_filament_setting",
            "ams_id": 255,
            "tray_id": 0,
            "slot_id": 0,
            "tray_info_idx": "",
            "tray_type": "",
            "tray_sub_brands": "",
            "tray_color": "00000000",
            "nozzle_temp_min": 0,
            "nozzle_temp_max": 0,
            "sequence_id": "4",
        }
    }


def test_cali_del_dual_has_no_setting_id_and_single_does() -> None:
    delete = partial(
        extrusion_cali_del,
        cali_idx=4,
        filament_id="GFL05",
        nozzle_id="HS00-0.4",
        nozzle_diameter="0.4",
        extruder_id=1,
        seq="8",
    )
    assert delete(setting_id="PF1", dual_nozzle=True) == {
        "print": {
            "command": "extrusion_cali_del",
            "sequence_id": "8",
            "extruder_id": 1,
            "nozzle_id": "HS00-0.4",
            "filament_id": "GFL05",
            "cali_idx": 4,
            "nozzle_diameter": "0.4",
        }
    }
    assert delete(setting_id=None, dual_nozzle=False) == {
        "print": {
            "command": "extrusion_cali_del",
            "sequence_id": "8",
            "filament_id": "GFL05",
            "cali_idx": 4,
            "setting_id": "",
            "nozzle_diameter": "0.4",
            "nozzle_id": "HS00-0.4",
            "extruder_id": 1,
        }
    }
    single = delete(setting_id="PF1", dual_nozzle=False)["print"]
    assert single["setting_id"] == "PF1"


@pytest.mark.parametrize(
    ("global_tray", "ids"),
    [
        (0, (0, 0, 0)),
        (7, (1, 3, 7)),  # AMS 1 slot 3
        (129, (129, 0, 129)),  # an AMS-HT: its unit id is its tray and target
        (254, (255, 254, 254)),  # the external holder (Ext-L)
    ],
)
def test_load_wire_ids(global_tray: int, ids: tuple[int, int, int]) -> None:
    body = ams_change_filament_load(global_tray, extruder_id=None, right_temp=0, seq="6")
    assert body == {
        "print": {
            "command": "ams_change_filament",
            "sequence_id": "6",
            "ams_id": ids[0],
            "slot_id": ids[1],
            "target": ids[2],
            "curr_temp": -1,
            "tar_temp": -1,
        }
    }


def test_load_of_ext_r_takes_the_right_nozzle_temperature() -> None:
    hot = ams_change_filament_load(255, extruder_id=None, right_temp=230, seq="6")["print"]
    assert (hot["ams_id"], hot["slot_id"], hot["target"]) == (255, 0, 255)
    assert (hot["curr_temp"], hot["tar_temp"]) == (230, 230)
    cold = ams_change_filament_load(255, extruder_id=None, right_temp=30, seq="6")["print"]
    assert (cold["curr_temp"], cold["tar_temp"]) == (RIGHT_DEFAULT, RIGHT_DEFAULT)


def test_load_names_the_extruder_only_when_given() -> None:
    assert (
        "extruder_id"
        not in ams_change_filament_load(2, extruder_id=None, right_temp=0, seq="1")["print"]
    )
    assert (
        ams_change_filament_load(2, extruder_id=1, right_temp=0, seq="1")["print"]["extruder_id"]
        == 1
    )
    assert (
        ams_change_filament_load(2, extruder_id=0, right_temp=0, seq="1")["print"]["extruder_id"]
        == 0
    )


def test_unload_takes_the_floor_temperature() -> None:
    body = ams_change_filament_unload(5, nozzle_temp=25, seq="9")
    assert body == {
        "print": {
            "command": "ams_change_filament",
            "sequence_id": "9",
            "ams_id": 1,
            "slot_id": 255,
            "target": 255,
            "curr_temp": UNLOAD_DEFAULT,
            "tar_temp": UNLOAD_DEFAULT,
        }
    }
    assert ams_change_filament_unload(5, nozzle_temp=240, seq="9")["print"]["curr_temp"] == 240


@pytest.mark.parametrize(
    ("source", "ams_id"), [(3, 0), (13, 3), (131, 131), (254, 255), (255, 255)]
)
def test_unload_addresses_the_source_unit(source: int, ams_id: int) -> None:
    assert ams_change_filament_unload(source, nozzle_temp=220, seq="1")["print"]["ams_id"] == ams_id


def test_rfid_shape() -> None:
    assert ams_get_rfid(128, 0, "12") == {
        "print": {"command": "ams_get_rfid", "ams_id": 128, "slot_id": 0, "sequence_id": "12"}
    }


def test_every_builder_cites_a_pinned_source() -> None:
    assert len(BUILDERS) == 10
    for fn in BUILDERS:
        assert PINNED in (fn.__doc__ or ""), fn.__name__


def test_matches_needs_command_and_sequence() -> None:
    assert matches({"command": "extrusion_cali_set", "sequence_id": "9"}, "extrusion_cali_set", "9")
    assert not matches(
        {"command": "extrusion_cali_set", "sequence_id": "0"}, "extrusion_cali_set", "9"
    )
    assert not matches(
        {"command": "extrusion_cali_sel", "sequence_id": "9"}, "extrusion_cali_set", "9"
    )
    assert not matches({"command": "extrusion_cali_set"}, "extrusion_cali_set", "9")


def test_refusal_reads_fail_case_folded() -> None:
    assert refusal({"result": "FAIL", "reason": "ERROR STATE"}) == "ERROR STATE"
    assert refusal({"result": "fail"}) == "printer reported failure"
    assert refusal({"result": "success"}) is None
    assert refusal({}) is None
