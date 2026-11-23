"""The sync recording (#6265) holds the Bambuddy write routes the filament sync rests on."""

from __future__ import annotations

import json
import re
from typing import Any

from scadbuddy.bambuddy.errors import Scope
from tests.bambuddy.conftest import RECORDINGS


def _sync() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (RECORDINGS / "openapi" / "sync-routes.json").read_text(encoding="utf-8")
    )
    return data


_SCHEMAS = {
    "SpoolCreate",
    "SpoolUpdate",
    "SpoolResponse",
    "SpoolKProfileBase",
    "SpoolFilamentPresetBase",
    "SpoolSupplierLinkInput",
    "SupplierCreate",
    "SupplierUpdate",
    "SupplierResponse",
    "LocationCreate",
    "LocationUpdate",
    "LocationResponse",
    "CatalogEntryCreate",
    "CatalogEntryUpdate",
    "FilamentCreate",
    "FilamentUpdate",
    "FilamentResponse",
}

_SPOOL = "/api/v1/inventory/spools/{spool_id}"
_EXPECTED_ROUTES = {
    ("POST", "/api/v1/inventory/spools"): Scope.MANAGE_INVENTORY,
    ("PATCH", _SPOOL): Scope.MANAGE_INVENTORY,
    ("POST", f"{_SPOOL}/archive"): Scope.MANAGE_INVENTORY,
    ("POST", f"{_SPOOL}/restore"): Scope.MANAGE_INVENTORY,
    ("POST", f"{_SPOOL}/reset-consumed-counter"): Scope.MANAGE_INVENTORY,
    ("PUT", f"{_SPOOL}/k-profiles"): Scope.MANAGE_INVENTORY,
    ("PUT", f"{_SPOOL}/filament-presets"): Scope.MANAGE_INVENTORY,
    ("PUT", f"{_SPOOL}/suppliers"): Scope.MANAGE_INVENTORY,
    ("GET", "/api/v1/inventory/suppliers"): Scope.READ_STATUS,
    ("POST", "/api/v1/inventory/suppliers"): Scope.MANAGE_INVENTORY,
    ("PATCH", "/api/v1/inventory/suppliers/{supplier_id}"): Scope.MANAGE_INVENTORY,
    ("GET", "/api/v1/inventory/locations"): Scope.READ_STATUS,
    ("POST", "/api/v1/inventory/locations"): Scope.MANAGE_INVENTORY,
    ("PATCH", "/api/v1/inventory/locations/{location_id}"): Scope.MANAGE_INVENTORY,
    ("POST", "/api/v1/inventory/catalog"): Scope.MANAGE_INVENTORY,
    ("PUT", "/api/v1/inventory/catalog/{entry_id}"): Scope.MANAGE_INVENTORY,
    ("POST", "/api/v1/filament-catalog/"): Scope.ADMIN_FILAMENTS,
    ("PATCH", "/api/v1/filament-catalog/{filament_id}"): Scope.ADMIN_FILAMENTS,
}


def test_each_schema_the_sync_writes_is_recorded() -> None:
    schemas = _sync()["components"]["schemas"]
    assert set(schemas) >= _SCHEMAS
    assert "last_scale_weight" not in schemas["SpoolUpdate"]["properties"]
    assert {"note", "low_stock_threshold_pct", "suppliers", "k_profiles"} <= set(
        schemas["SpoolResponse"]["properties"]
    )


def test_the_recorded_routes_carry_the_scope_scadbuddy_uses() -> None:
    rec = _sync()
    routes = {(r["method"], r["path"]): r["scadbuddy_scope"] for r in rec["routes"]}
    assert routes == {key: scope.value for key, scope in _EXPECTED_ROUTES.items()}
    for route in rec["routes"]:
        assert route["path"] in rec["paths"]
        assert route["method"].lower() in rec["paths"][route["path"]]
        assert re.search(r"/blob/v1\.3\.1/.+#L\d+$", route["permission_source"]), route


def test_the_recording_names_its_version_and_measurement() -> None:
    rec = _sync()
    measured = rec["measured_2026-11-18"]
    assert measured["openapi"]["info_version"] == rec["info"]["version"] == "1.3.1"
    assert measured["source_tag"]["tag"] == "v1.3.1"
    assert measured["method"].startswith("GET only")
