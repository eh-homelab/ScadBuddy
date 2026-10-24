"""`PrinterStatus` from a LAN report (#3823, plan 2026-10-16, Task 5).

The fixture is **synthetic**, not recorded: no printer was reachable from this branch
(plan Task 7 needs one). It is built from OpenBambuAPI `mqtt.md`'s ``pushall`` report
(https://github.com/Doridian/OpenBambuAPI/blob/main/mqtt.md, read 2026-10-18) and
ha-bambulab's comments on the H2D's ``device.extruder`` and ``device.nozzle`` blocks
(``pybambu/models.py``, read the same day), with the job of the Bambuddy recording
``printer-status.json``. Re-record it from the Task 4 live probe."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.status import STALE_AFTER, hms_errors, status_from_report

FIXTURE = Path(__file__).parent / "fixtures" / "lan-report-h2c.json"


@pytest.fixture
def report_h2c() -> LanReport:
    return LanReport.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))


def row(**fields: object) -> PrinterRow:
    now = datetime.now(UTC)
    base: dict[str, object] = {
        "id": "p1",
        "serial": "01P00A000000001",
        "name": "H2C",
        "created_at": now,
        "updated_at": now,
    }
    return PrinterRow.model_validate({**base, **fields})


def test_status_maps_trays_and_progress(report_h2c: LanReport) -> None:
    status = status_from_report(row(), report_h2c, now=report_h2c.received_at)
    assert (status.id, status.name, status.connected, status.state) == (0, "H2C", True, "RUNNING")
    assert (status.progress, status.remaining_time) == (55.0, 50)
    assert (status.layer_num, status.total_layers) == (40, 79)
    assert status.subtask_name == "Name_Keychain_Font_Basic_Plate_1"
    assert status.ams_exists is True
    assert [unit.id for unit in status.ams] == [0, 128]
    first = status.ams[0]
    assert (first.humidity, first.temp) == (4, 22.7)
    assert [tray.id for tray in first.tray] == [0, 1]
    tray = first.tray[1]
    assert (tray.tray_color, tray.tray_type, tray.tray_info_idx) == ("000000FF", "PLA", "GFA00")
    assert (tray.tray_sub_brands, tray.remain, tray.state) == ("PLA Basic", 80, 11)
    assert (tray.nozzle_temp_min, tray.nozzle_temp_max) == (190, 240)
    assert status.ams[1].tray[0].remain == -1
    [external] = status.vt_tray
    assert external.id == 254


def test_the_nozzles_and_the_rack(report_h2c: LanReport) -> None:
    status = status_from_report(row(), report_h2c, now=report_h2c.received_at)
    assert [(n.nozzle_type, n.nozzle_diameter) for n in status.nozzles] == [
        ("HS01", "0.4"),
        ("HH01", "0.4"),
    ]
    assert status.active_extruder == 1
    assert [(slot.id, slot.nozzle_diameter, slot.serial_number) for slot in status.nozzle_rack] == [
        (16, "0.2", "REDACTED-16"),
        (17, "0.6", ""),
    ]
    assert status.nozzle_rack[0].wear == 3


def test_the_bambuddy_id_is_the_link_when_there_is_one(report_h2c: LanReport) -> None:
    status = status_from_report(row(bambuddy_printer_id=4), report_h2c, now=report_h2c.received_at)
    assert status.id == 4


def test_stale_report_is_offline(report_h2c: LanReport) -> None:
    """Review Focus 5: past 90 s without a report the printer is offline."""
    assert timedelta(seconds=90) == STALE_AFTER
    fresh = status_from_report(
        row(), report_h2c, now=report_h2c.received_at + timedelta(seconds=89)
    )
    assert fresh.connected is True
    status = status_from_report(
        row(), report_h2c, now=report_h2c.received_at + timedelta(seconds=91)
    )
    assert status.connected is False


def test_hms_code_formatting() -> None:
    [error] = hms_errors([{"attr": 0x07002200, "code": 0x00020025}])
    assert error.code == error.full_code == "0700220000020025"
    assert error.severity == 2 and error.description is None
    assert hms_errors([{"attr": "x"}, "nope", {"attr": 1}]) == []
    assert hms_errors(None) == []


def test_an_empty_report_is_a_status_all_the_same() -> None:
    report = LanReport(serial="s", received_at=datetime.now(UTC), print={})
    status = status_from_report(row(), report, now=report.received_at)
    assert status.connected is True and status.state is None
    assert status.ams == [] and status.vt_tray == [] and status.nozzles == []


def test_drifted_fields_read_as_unknown() -> None:
    report = LanReport(
        serial="s",
        received_at=datetime.now(UTC),
        print={
            "mc_percent": "n/a",
            "ams": {"ams": [{"id": "x"}, {"id": "1", "tray": "nope"}]},
            "vt_tray": [{"id": "254"}],
            "device": {"nozzle": {"info": "nope"}, "extruder": {"state": "x"}},
        },
    )
    status = status_from_report(row(), report, now=report.received_at)
    assert status.progress is None
    assert [unit.id for unit in status.ams] == [1] and status.ams[0].tray == []
    assert [tray.id for tray in status.vt_tray] == [254]
    assert status.nozzles == [] and status.active_extruder == 0


def test_a_rack_slots_mistyped_filament_reads_as_absent() -> None:
    """#4538: ``fila_id`` and ``color_m`` are kept only as text."""
    report = _lan(
        {
            "device": {
                "nozzle": {
                    "info": [
                        {"id": 16, "diameter": "0.4", "type": "HS01", "fila_id": 7, "color_m": {}},
                        {"id": 17, "diameter": "0.4", "type": "HS01", "fila_id": "GFA00"},
                    ]
                }
            }
        }
    )
    status = status_from_report(row(), report, now=report.received_at)
    assert [(s.filament_id, s.filament_colour) for s in status.nozzle_rack] == [
        ("", ""),
        ("GFA00", ""),
    ]


# --- #4336 review I1, I3: presence bits and the H2 external holders ---------------------


def _lan(print_: dict[str, object]) -> LanReport:
    return LanReport(serial="S", received_at=datetime.now(UTC), print=print_)


def _two_nozzles() -> dict[str, object]:
    return {
        "device": {
            "nozzle": {
                "info": [
                    {"id": 0, "diameter": "0.4", "type": "HS01"},
                    {"id": 1, "diameter": "0.4", "type": "HS01"},
                ]
            }
        }
    }


def test_tray_exist_bits_set_each_trays_presence() -> None:
    """Bambuddy's bit layout (``apply_tray_exist_bits``, ``bambu_mqtt.py:216-330`` at
    ``0d427510``): an AMS tray is bit ``ams_id * 4 + tray``, an AMS-HT bit
    ``16 + (ams_id - 128)``. ``0x10002`` is AMS 0 slot 2 and HT-A."""
    report = _lan(
        {
            "ams": {
                "tray_exist_bits": "10002",
                "ams": [
                    {
                        "id": "0",
                        "tray": [
                            {"id": "0", "tray_type": "PLA", "state": 11},
                            {"id": "1", "tray_type": "PLA", "state": 11},
                        ],
                    },
                    {"id": "128", "tray": [{"id": "0", "tray_type": "PETG", "state": 9}]},
                    {"id": "129", "tray": [{"id": "0", "tray_type": "PETG", "state": 9}]},
                ],
            }
        }
    )
    status = status_from_report(row(), report, now=report.received_at)
    exists = {(unit.id, tray.id): tray.exists for unit in status.ams for tray in unit.tray}
    assert exists == {(0, 0): False, (0, 1): True, (128, 0): True, (129, 0): False}


def test_no_presence_bits_leave_presence_unknown() -> None:
    """A report without the mask, or the printer-off pattern (all zeros with
    ``power_on_flag`` false, Bambuddy #765), says nothing about presence."""
    for ams in (
        {"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PLA"}]}]},
        {
            "tray_exist_bits": "0",
            "power_on_flag": False,
            "ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PLA"}]}],
        },
    ):
        report = _lan({"ams": ams})
        status = status_from_report(row(), report, now=report.received_at)
        assert status.ams[0].tray[0].exists is None


def test_vir_slot_is_both_h2_external_holders() -> None:
    """H2-series: ``vt_tray`` is the one active spool and ``vir_slot`` lists both
    holders (Bambuddy ``bambu_mqtt.py:2323-2349``; ha-bambulab ``models.py:3696``)."""
    report = _lan(
        {
            **_two_nozzles(),
            "vt_tray": {"id": "255", "tray_type": "PETG", "tray_color": "00FF00FF"},
            "vir_slot": [
                {"id": "254", "tray_type": "PLA", "tray_color": "FF0000FF"},
                {"id": "255", "tray_type": "PETG", "tray_color": "00FF00FF"},
            ],
        }
    )
    status = status_from_report(row(), report, now=report.received_at)
    assert [(tray.id, tray.tray_type) for tray in status.vt_tray] == [(254, "PLA"), (255, "PETG")]


def test_a_single_vir_slot_255_is_the_one_holder() -> None:
    """A single-nozzle printer's one holder arrives as 255 in ``vir_slot``; Bambuddy
    reads it as 254 (``bambu_mqtt.py:2327-2333``)."""
    report = _lan({"vir_slot": [{"id": "255", "tray_type": "PLA"}]})
    status = status_from_report(row(), report, now=report.received_at)
    assert [tray.id for tray in status.vt_tray] == [254]


def test_a_holder_id_other_than_254_or_255_is_dropped() -> None:
    """A holder is tray ``id - 254``, so any other id would be a bad tray id (#4377)."""
    reports: list[dict[str, object]] = [
        {"vir_slot": [{"id": "254", "tray_type": "PLA"}, {"id": "7", "tray_type": "PLA"}]},
        {"vt_tray": {"id": "256", "tray_type": "PLA"}},
    ]
    for print_ in reports:
        report = _lan(print_)
        status = status_from_report(row(), report, now=report.received_at)
        assert [tray.id for tray in status.vt_tray] == ([254] if "vir_slot" in print_ else [])


def test_a_presence_bit_an_ams_4_shares_with_an_ams_ht_vetoes_neither() -> None:
    """AMS 4 tray 0 and HT-A are both bit 16 (#4377): with both reported, the bit says
    nothing about either, while the unshared bits still apply."""
    report = _lan(
        {
            "ams": {
                "tray_exist_bits": "0",
                "power_on_flag": True,
                "ams": [
                    {
                        "id": "4",
                        "tray": [
                            {"id": "0", "tray_type": "PLA", "state": 11},
                            {"id": "2", "tray_type": "PLA", "state": 11},
                        ],
                    },
                    {"id": "128", "tray": [{"id": "0", "tray_type": "PETG", "state": 11}]},
                ],
            }
        }
    )
    status = status_from_report(row(), report, now=report.received_at)
    exists = {(unit.id, tray.id): tray.exists for unit in status.ams for tray in unit.tray}
    assert exists == {(4, 0): None, (4, 2): False, (128, 0): None}


RECORDED = Path(__file__).parent / "fixtures" / "lan-report-h2c-idle.json"


@pytest.fixture
def recorded_h2c() -> LanReport:
    data = json.loads(RECORDED.read_text(encoding="utf-8"))
    data.pop("firmware")
    return LanReport.model_validate(data)


def test_a_recorded_h2c_report(recorded_h2c: LanReport) -> None:
    """The live H2C (firmware 01.02.00.00, 2026-10-24): four AMS 2 Pro on the Filament
    Track Switch's inlets (``info`` 11002E03 is A, 10002E03 B), an AMS-HT, and the rack."""
    status = status_from_report(row(), recorded_h2c, now=recorded_h2c.received_at)
    assert status.state == "FINISH"
    assert [unit.id for unit in status.ams] == [0, 1, 2, 3, 128]
    assert all(tray.exists for unit in status.ams for tray in unit.tray)
    assert status.ams[1].tray[1].tray_color == "FEC600FF"
    assert [tray.id for tray in status.vt_tray] == [254, 255]
    assert status.fila_switch is not None and status.fila_switch.installed
    assert status.ams_extruder_map == {}
    assert status.ams_switch_inlet == {"0": "A", "1": "B", "2": "B", "3": "A", "128": "A"}
    assert [slot.id for slot in status.nozzle_rack] == [16, 17, 20, 21, 18]
    first = status.nozzle_rack[0]
    assert (first.nozzle_type, first.nozzle_diameter) == ("HS00", "0.2")
    assert (first.filament_id, first.filament_colour) == ("GFA00", "EC008CFF")


def test_a_unit_on_a_fixed_extruder_is_mapped_and_has_no_inlet(recorded_h2c: LanReport) -> None:
    data = recorded_h2c.print
    del data["device"]["fila_switch"]
    data["ams"]["ams"][0]["info"] = "10001003"
    status = status_from_report(row(), recorded_h2c, now=recorded_h2c.received_at)
    assert status.fila_switch is None
    # Unit 1 still says 0xE ("not fixed"), and with no switch that names nothing.
    assert status.ams_extruder_map == {"0": 0}
    assert status.ams_switch_inlet == {}


def test_an_inlet_other_than_a_or_b_is_unknown(recorded_h2c: LanReport) -> None:
    from scadbuddy.bambuddy.extruders import extruder_of

    recorded_h2c.print["ams"]["ams"][0]["info"] = "12002E03"  # inlet nibble 2
    status = status_from_report(row(), recorded_h2c, now=recorded_h2c.received_at)
    assert "0" not in status.ams_switch_inlet and "0" not in status.ams_extruder_map
    assert extruder_of(0, 0, status) is None


def test_an_info_that_is_not_hex_text_reads_as_absent(recorded_h2c: LanReport) -> None:
    data = recorded_h2c.print
    data["ams"]["ams"][0]["info"] = 1003
    data["ams"]["ams"][1]["info"] = "zz"
    status = status_from_report(row(), recorded_h2c, now=recorded_h2c.received_at)
    assert set(status.ams_switch_inlet) == {"2", "3", "128"}


REINSERT = Path(__file__).parent / "fixtures" / "lan-tray-reinsert-h2c.json"


def test_a_spool_pulled_and_pushed_back(recorded_h2c: LanReport) -> None:
    """AMS 1 slot 1 pulled and pushed back on the live H2C: its presence bit (5) follows
    the spool at once, and the tray stays bare until the RFID read finishes ~20 s later."""
    seen: list[tuple[bool | None, str | None]] = []
    for step in json.loads(REINSERT.read_text(encoding="utf-8"))["steps"]:
        data = recorded_h2c.print
        data["ams"]["tray_exist_bits"] = step["tray_exist_bits"]
        data["ams"]["ams"][1]["tray"][1] = step["ams_1_tray_1"]
        tray = status_from_report(row(), recorded_h2c, now=recorded_h2c.received_at).ams[1].tray[1]
        seen.append((tray.exists, tray.tray_type))
    assert seen[0] == (True, "PLA") and seen[1] == (False, None)
    assert (True, None) in seen[2:]
    assert seen[-1] == (True, "PLA")
