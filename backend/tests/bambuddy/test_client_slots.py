"""#7153: a printer's AMS slots through Bambuddy: slot presets, reset, refresh, load,
unload and the AMS labels. The parameters are Bambuddy's ``backend/app/api/routes/
printers.py`` (maziggy/bambuddy v1.3.1; plan ``2026-11-23-ams-slot-control.md``). Writes
are tested with respx only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import SLOT_CONTROL_TIMEOUT, BambuddyClient
from scadbuddy.bambuddy.errors import Scope
from scadbuddy.bambuddy.models import PrinterStatus, SlotPreset
from scadbuddy.core.problems import ApiError
from tests.bambuddy.conftest import BASE_URL

API = f"{BASE_URL}/api/v1"
SPEC = Path(__file__).parents[1] / "fixtures" / "bambuddy-openapi-1.2.6b1.json"
RECORDINGS = Path(__file__).parent / "recordings"
DONE = {"success": True, "message": "done"}


def test_the_status_carries_tray_now_the_ams_state_and_each_units_serial() -> None:
    """``tray_now``, ``ams_status_main`` and each unit's ``serial_number`` are in the
    recorded status; the serial stays out of a dump, as the tag keys do."""
    raw = json.loads((RECORDINGS / "printer-status-fts.json").read_text(encoding="utf-8"))
    raw["ams"][0]["serial_number"] = "19C06A000000001"
    status = PrinterStatus.model_validate(raw)
    assert status.tray_now == 255
    assert status.ams_status_main == 0
    assert status.ams[0].serial_number == "19C06A000000001"
    assert "serial_number" not in status.model_dump()["ams"][0]


@respx.mock
async def test_slot_presets_are_read_as_rows(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/printers/3/slot-presets").mock(
        return_value=httpx.Response(
            200,
            json={
                "1": {
                    "ams_id": 0,
                    "tray_id": 1,
                    "preset_id": "GFSA00",
                    "preset_name": "Bambu PLA Basic @BBL H2C",
                    "tray_info_idx": "GFA00",
                }
            },
        )
    )
    assert await bambuddy.slot_presets(3) == [
        SlotPreset(
            ams_id=0,
            tray_id=1,
            preset_id="GFSA00",
            preset_name="Bambu PLA Basic @BBL H2C",
            tray_info_idx="GFA00",
        )
    ]


@respx.mock
async def test_a_slot_preset_is_put_in_the_query(bambuddy: BambuddyClient) -> None:
    route = respx.put(f"{API}/printers/3/slot-presets/0/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "ams_id": 0,
                "tray_id": 1,
                "preset_id": "GFSA00",
                "preset_name": "Bambu PLA Basic",
                "preset_source": "cloud",
                "tray_info_idx": "GFA00",
            },
        )
    )

    saved = await bambuddy.set_slot_preset(
        3, 0, 1, preset_id="GFSA00", preset_name="Bambu PLA Basic", tray_info_idx="GFA00"
    )

    assert saved.preset_id == "GFSA00"
    assert dict(route.calls.last.request.url.params) == {
        "preset_id": "GFSA00",
        "preset_name": "Bambu PLA Basic",
        "preset_source": "cloud",
        "tray_info_idx": "GFA00",
    }


@respx.mock
async def test_labels_are_read_by_unit(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/printers/3/ams-labels").mock(
        return_value=httpx.Response(200, json={"0": "Left rack", "128": "Dryer"})
    )
    assert await bambuddy.ams_labels(3) == {0: "Left rack", 128: "Dryer"}


@respx.mock
async def test_a_label_is_put_with_the_units_serial(bambuddy: BambuddyClient) -> None:
    route = respx.put(f"{API}/printers/3/ams-labels/0").mock(
        return_value=httpx.Response(200, json={"ams_id": 0, "label": "Left rack"})
    )
    await bambuddy.set_ams_label(3, 0, "Left rack", serial="19C06A000000001")
    assert json.loads(route.calls.last.request.content) == {
        "label": "Left rack",
        "ams_serial": "19C06A000000001",
    }


@respx.mock
async def test_a_label_delete_names_the_serial(bambuddy: BambuddyClient) -> None:
    route = respx.delete(f"{API}/printers/3/ams-labels/0").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    await bambuddy.delete_ams_label(3, 0, serial="19C06A000000001")
    assert route.calls.last.request.url.params["ams_serial"] == "19C06A000000001"


@respx.mock
async def test_load_names_the_global_tray_and_the_extruder(bambuddy: BambuddyClient) -> None:
    route = respx.post(f"{API}/printers/3/ams/load").mock(
        return_value=httpx.Response(200, json=DONE)
    )
    await bambuddy.load_filament(3, 6, extruder_id=1)
    request = route.calls.last.request
    assert dict(request.url.params) == {"tray_id": "6", "extruder_id": "1"}
    assert request.extensions["timeout"]["read"] == SLOT_CONTROL_TIMEOUT


@respx.mock
async def test_load_leaves_the_extruder_out_unless_given(bambuddy: BambuddyClient) -> None:
    route = respx.post(f"{API}/printers/3/ams/load").mock(
        return_value=httpx.Response(200, json=DONE)
    )
    await bambuddy.load_filament(3, 254)
    assert dict(route.calls.last.request.url.params) == {"tray_id": "254"}


@respx.mock
@pytest.mark.parametrize(
    ("method", "path", "call"),
    [
        ("POST", "/printers/3/ams/unload", lambda c: c.unload_filament(3, 1)),
        ("POST", "/printers/3/ams/load", lambda c: c.load_filament(3, 1)),
        ("POST", "/printers/3/ams/0/tray/1/reset", lambda c: c.reset_slot(3, 0, 1)),
        ("POST", "/printers/3/ams/0/slot/1/refresh", lambda c: c.refresh_slot(3, 0, 1)),
    ],
)
async def test_the_printer_writes_take_control_printer(
    bambuddy: BambuddyClient, method: str, path: str, call: Any
) -> None:
    """``PRINTERS_CONTROL`` and ``PRINTERS_AMS_RFID`` map to ``can_control_printer``
    (``core/auth.py`` lines 150 and 152, v1.3.1): R-7153-4."""
    respx.route(method=method, url=f"{API}{path}").mock(return_value=httpx.Response(403))
    with pytest.raises(ApiError) as caught:
        await call(bambuddy)
    assert caught.value.extensions["required_scope"] == Scope.CONTROL_PRINTER.value


@respx.mock
@pytest.mark.parametrize(
    ("method", "path", "call"),
    [
        (
            "PUT",
            "/printers/3/slot-presets/0/1",
            lambda c: c.set_slot_preset(3, 0, 1, preset_id="GFSA00", preset_name="PLA"),
        ),
        ("DELETE", "/printers/3/slot-presets/0/1", lambda c: c.delete_slot_preset(3, 0, 1)),
        ("PUT", "/printers/3/ams-labels/0", lambda c: c.set_ams_label(3, 0, "A", serial="")),
        ("DELETE", "/printers/3/ams-labels/0", lambda c: c.delete_ams_label(3, 0, serial="")),
    ],
)
async def test_preset_and_label_writes_name_the_admin_only_scope(
    bambuddy: BambuddyClient, method: str, path: str, call: Any
) -> None:
    """``PRINTERS_UPDATE`` is in ``_APIKEY_DENIED_PERMISSIONS`` (``core/auth.py`` line
    280, v1.3.1): its 403 says no key can, R-7153-4."""
    respx.route(method=method, url=f"{API}{path}").mock(
        return_value=httpx.Response(403, json={"detail": "Not enough permissions"})
    )
    with pytest.raises(ApiError) as caught:
        await call(bambuddy)
    assert caught.value.status == 409
    assert caught.value.extensions["required_scope"] == Scope.ADMIN_PRINTERS.value
    assert "auth is on" in caught.value.detail


@respx.mock
async def test_an_unload_of_a_slot_no_hotend_holds_is_bambuddys_conflict(
    bambuddy: BambuddyClient,
) -> None:
    respx.post(f"{API}/printers/3/ams/unload").mock(
        return_value=httpx.Response(409, json={"detail": "No hotend is loaded from that slot"})
    )
    with pytest.raises(ApiError) as caught:
        await bambuddy.unload_filament(3, 1)
    assert caught.value.status == 409
    assert "No hotend is loaded" in caught.value.detail


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/v1/printers/{printer_id}/slot-presets", "get"),
        ("/api/v1/printers/{printer_id}/slot-presets/{ams_id}/{tray_id}", "put"),
        ("/api/v1/printers/{printer_id}/slot-presets/{ams_id}/{tray_id}", "delete"),
        ("/api/v1/printers/{printer_id}/ams/{ams_id}/tray/{tray_id}/reset", "post"),
        ("/api/v1/printers/{printer_id}/ams/{ams_id}/slot/{slot_id}/refresh", "post"),
        ("/api/v1/printers/{printer_id}/ams/load", "post"),
        ("/api/v1/printers/{printer_id}/ams/unload", "post"),
        ("/api/v1/printers/{printer_id}/ams-labels", "get"),
        ("/api/v1/printers/{printer_id}/ams-labels/{ams_id}", "put"),
        ("/api/v1/printers/{printer_id}/ams-labels/{ams_id}", "delete"),
    ],
)
def test_the_routes_are_in_bambuddys_openapi(path: str, method: str) -> None:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    assert method in spec["paths"][path]
