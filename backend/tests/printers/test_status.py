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
