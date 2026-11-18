"""The RFID recording (#6264) holds the Bambuddy facts the tag work rests on."""

from __future__ import annotations

import json
from typing import Any

from scadbuddy.bambuddy.errors import Scope
from tests.bambuddy.conftest import RECORDINGS


def _rfid() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (RECORDINGS / "openapi" / "rfid-routes.json").read_text(encoding="utf-8")
    )
    return data


_EXPECTED_ROUTES = {
    ("PATCH", "/api/v1/inventory/spools/{spool_id}/link-tag"): Scope.MANAGE_INVENTORY,
    ("GET", "/api/v1/inventory/spools/by-tag"): Scope.MANAGE_INVENTORY,
    ("PATCH", "/api/v1/inventory/spools/{spool_id}"): Scope.MANAGE_INVENTORY,
    ("GET", "/api/v1/inventory/spools/{spool_id}"): Scope.READ_STATUS,
    ("GET", "/api/v1/settings/"): Scope.READ_STATUS,
    ("PATCH", "/api/v1/settings/"): Scope.ADMIN_SETTINGS,
    (
        "POST",
        "/api/v1/printers/{printer_id}/slots/{ams_id}/{tray_id}/configure",
    ): Scope.CONTROL_PRINTER,
    ("GET", "/api/v1/spoolbuddy/devices"): Scope.READ_STATUS,
    ("POST", "/api/v1/spoolbuddy/nfc/tag-scanned"): Scope.MANAGE_INVENTORY,
    ("POST", "/api/v1/spoolbuddy/nfc/tag-removed"): Scope.MANAGE_INVENTORY,
    ("POST", "/api/v1/spoolbuddy/nfc/write-tag"): Scope.MANAGE_INVENTORY,
    ("POST", "/api/v1/spoolbuddy/nfc/write-result"): Scope.MANAGE_INVENTORY,
    ("POST", "/api/v1/spoolbuddy/devices/{device_id}/cancel-write"): Scope.MANAGE_INVENTORY,
    ("POST", "/api/v1/auth/ws-token"): Scope.READ_STATUS,
}


def test_the_recorded_routes_carry_the_scope_scadbuddy_uses() -> None:
    routes = {(r["method"], r["path"]): r["scadbuddy_scope"] for r in _rfid()["routes"]}
    assert routes == {key: scope.value for key, scope in _EXPECTED_ROUTES.items()}


def test_the_rfid_recording_holds_what_the_plan_rests_on() -> None:
    rec = _rfid()
    schemas = rec["components"]["schemas"]
    assert {"tag_uid", "tray_uuid", "tag_type", "data_origin"} <= set(
        schemas["LinkTagRequest"]["properties"]
    )
    conflict = rec["conflict_409"]
    assert conflict["code"] == "tag_already_linked"
    assert set(conflict) == {"code", "message", "spool_id", "field"}
    assert {"device_id", "spool_id"} <= set(schemas["WriteTagRequest"]["required"])
    assert "auto_add_unknown_rfid" in schemas["AppSettingsUpdate"]["properties"]
    assert {"spoolbuddy_tag_matched", "spoolbuddy_unknown_tag"} <= set(rec["ws_events"])
    assert {"tag_type", "data_origin"} <= set(schemas["SpoolResponse"]["properties"])
    origins = rec["data_origin_values"]
    for value in ("rfid_auto", "rfid_linked"):
        assert origins[value]["source"].rsplit("#L", 1)[1][0].isdigit()
