"""Bambuddy's maintenance tracker, read for a printer's machine rate (#4625, order
operations spec 2026-11-07 §6.1). The body is ``PrinterMaintenanceOverview`` as
Bambuddy's ``backend/app/schemas/maintenance.py`` declares it at maziggy/bambuddy
2f87192334f66bc4b27b84ba48c6a2d216dd8afc
(https://github.com/maziggy/bambuddy/blob/2f87192334f66bc4b27b84ba48c6a2d216dd8afc/backend/app/schemas/maintenance.py),
written by hand: reading it live creates the printer's maintenance rows in Bambuddy, so
no recording was taken. ``GET /api/v1/maintenance/printers/{printer_id}`` is in
``recordings/openapi/routes.txt``."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.errors import SCOPE_PROBLEM
from scadbuddy.core.problems import ApiError
from scadbuddy.orders.bambuddy import maintenance_tasks
from scadbuddy.orders.integrations import MaintenanceTask
from tests.bambuddy.conftest import BASE_URL

API = f"{BASE_URL}/api/v1"


def _item(name: str, interval: float, kind: str = "hours", *, enabled: bool = True) -> Any:
    return {
        "id": 1,
        "printer_id": 1,
        "printer_name": "3DP-31B-598",
        "printer_model": "H2C",
        "maintenance_type_id": 3,
        "maintenance_type_name": name,
        "maintenance_type_icon": None,
        "maintenance_type_wiki_url": None,
        "enabled": enabled,
        "interval_hours": interval,
        "interval_type": kind,
        "current_hours": 812.5,
        "hours_since_maintenance": 12.5,
        "hours_until_due": interval - 12.5,
        "days_since_maintenance": None,
        "days_until_due": None,
        "is_due": False,
        "is_warning": False,
        "last_performed_at": None,
    }


OVERVIEW = {
    "printer_id": 1,
    "printer_name": "3DP-31B-598",
    "printer_model": "H2C",
    "total_print_hours": 812.5,
    "maintenance_items": [
        _item("Lubricate Carbon Rods", 100.0),
        _item("Clean Nozzle", 30, "days"),
        _item("Check Belt Tension", 200.0, enabled=False),
    ],
    "due_count": 0,
    "warning_count": 0,
}


@respx.mock
async def test_a_printers_maintenance_is_read_with_read_status(bambuddy: BambuddyClient) -> None:
    route = respx.get(f"{API}/maintenance/printers/1").mock(
        return_value=httpx.Response(200, json=OVERVIEW)
    )
    overview = await bambuddy.printer_maintenance(1)
    assert route.called
    assert [item.maintenance_type_name for item in overview.maintenance_items] == [
        "Lubricate Carbon Rods",
        "Clean Nozzle",
        "Check Belt Tension",
    ]
    assert maintenance_tasks(overview) == [
        MaintenanceTask(task="Lubricate Carbon Rods", interval_hours=100.0),
        MaintenanceTask(task="Clean Nozzle", interval_hours=30, interval_type="days"),
        MaintenanceTask(task="Check Belt Tension", interval_hours=200.0, enabled=False),
    ]


@respx.mock
async def test_a_refused_read_names_its_scope(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/maintenance/printers/1").mock(
        return_value=httpx.Response(403, json={"detail": "no"})
    )
    with pytest.raises(ApiError) as raised:
        await bambuddy.printer_maintenance(1)
    assert raised.value.type == SCOPE_PROBLEM
    assert "Read Status" in str(raised.value.detail)
