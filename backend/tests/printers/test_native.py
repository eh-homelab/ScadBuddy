"""The LAN-link gate, the stored-report status and the command answer (#7276, plan
2026-11-25 Task 6, R-7276-4, P14, P27, P30). No printer is contacted."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.core.problems import ApiError
from scadbuddy.printers.lan_command_models import (
    LanCommandOutcome,
    LanCommandPending,
    LanCommandReply,
)
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.native import (
    command_response,
    is_lan_linked,
    lan_row,
    live_status,
)
from scadbuddy.workflows.lan_command import NOT_LAN_LINKED

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
ID = "5f0c3c5e-3b0e-4a44-9b43-2f0d0a3c1111"


def row(**fields: Any) -> PrinterRow:
    base: dict[str, Any] = {
        "id": ID,
        "serial": "01P00A000007276",
        "name": "H2C",
        "lan_host": "192.168.1.9",
        "lan_cert_sha256": "ab" * 32,
        "has_access_code": True,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return PrinterRow.model_validate({**base, **fields})


class Rows:
    def __init__(self, found: PrinterRow | None) -> None:
        self.found = found

    def get(self, printer_id: str) -> PrinterRow | None:
        return self.found


class Reports:
    def __init__(self, report: tuple[dict[str, Any], datetime] | None) -> None:
        self.report = report

    def get(self, printer_id: str) -> tuple[dict[str, Any], datetime] | None:
        return self.report


async def refused(printers: Rows | None, printer_id: str = ID) -> ApiError:
    with pytest.raises(ApiError) as caught:
        await lan_row(printers, printer_id, feature="AMS")  # type: ignore[arg-type]
    return caught.value


async def test_a_linked_printer_passes_the_gate() -> None:
    linked = row()
    assert is_lan_linked(linked)
    assert await lan_row(Rows(linked), ID, feature="AMS") is linked  # type: ignore[arg-type]


async def test_a_bambuddy_only_printer_is_not_lan_linked() -> None:
    only = row(lan_host=None, lan_cert_sha256=None, has_access_code=False, bambuddy_printer_id=3)
    assert not is_lan_linked(only)
    error = await refused(Rows(only))
    assert error.status == 409
    assert error.extensions["code"] == NOT_LAN_LINKED == "not_lan_linked"
    assert "Settings > Printers" in error.detail
    assert error.detail == (
        "H2C is not linked over the LAN, so ScadBuddy cannot reach its AMS."
        " Link it in Settings > Printers."
    )


async def test_a_virtual_printer_is_not_lan_linked() -> None:
    virtual = row(kind="bambuddy_virtual")
    assert not is_lan_linked(virtual)
    assert (await refused(Rows(virtual))).status == 409


@pytest.mark.parametrize("missing", ["lan_host", "lan_cert_sha256"])
async def test_a_row_without_its_host_or_pin_is_not_lan_linked(missing: str) -> None:
    broken = row(**{missing: None})
    assert not is_lan_linked(broken)
    assert (await refused(Rows(broken))).extensions["code"] == NOT_LAN_LINKED


async def test_a_row_without_its_access_code_is_not_lan_linked() -> None:
    nocode = row(has_access_code=False)
    assert not is_lan_linked(nocode)
    assert (await refused(Rows(nocode))).extensions["code"] == NOT_LAN_LINKED


async def test_an_unknown_printer_is_404() -> None:
    assert (await refused(Rows(None))).status == 404
    assert (await refused(Rows(row()), "not-a-uuid")).status == 404
    assert (await refused(None)).status == 404


async def test_no_report_reads_as_offline() -> None:
    status = await live_status(Reports(None), row(), now=NOW)  # type: ignore[arg-type]
    assert status.connected is False
    assert status.ams == []


async def test_a_fresh_report_reads_as_connected() -> None:
    report = ({"gcode_state": "IDLE", "vt_tray": [{"id": "254"}, {"id": "255"}]}, NOW)
    status = await live_status(Reports(report), row(), now=NOW)  # type: ignore[arg-type]
    assert status.connected is True
    assert status.state == "IDLE"
    stale = (report[0], NOW - timedelta(minutes=5))
    assert (await live_status(Reports(stale), row(), now=NOW)).connected is False  # type: ignore[arg-type]


def test_a_finished_run_answers_200_with_its_replies() -> None:
    outcome = LanCommandOutcome(
        replies=[LanCommandReply(confirmed=True), LanCommandReply(confirmed=False)]
    )
    response = command_response(outcome)
    assert response.status_code == 200
    assert json.loads(bytes(response.body)) == {
        "replies": [
            {"confirmed": True, "reply": None, "received_at": None},
            {"confirmed": False, "reply": None, "received_at": None},
        ]
    }


def test_a_run_still_open_answers_202_with_its_id() -> None:
    response = command_response(LanCommandPending(workflow_id="lan-command-x"))
    assert response.status_code == 202
    assert json.loads(bytes(response.body)) == {"workflow_id": "lan-command-x"}


REMOVED = (
    "set_k_profile",
    "set_k_profiles_batch",
    "delete_k_profile",
    "k_profile_notes",
    "set_k_profile_note",
    "delete_k_profile_note",
    "configure_slot",
    "reset_slot",
    "refresh_slot",
    "load_filament",
    "unload_filament",
    "slot_presets",
    "set_slot_preset",
    "delete_slot_preset",
    "ams_labels",
    "set_ams_label",
    "delete_ams_label",
)


def test_no_filament_hardware_write_goes_through_bambuddy() -> None:
    root = Path(__file__).parents[2] / "scadbuddy/bambuddy"
    assert not (root / "slot_control.py").exists()
    src = (root / "client.py").read_text()
    for name in REMOVED:
        assert f"def {name}(" not in src
