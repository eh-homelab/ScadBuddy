"""#6265 Task 2: the client's spool, supplier, location and catalog writes. Routes, bodies
and scopes come from ``recordings/openapi/sync-routes.json`` (Bambuddy 1.3.1); every sent
body is validated against the recorded schema, and no call reaches a live Bambuddy."""

from __future__ import annotations

import inspect
import json
import re
import typing

import httpx
import pytest
import respx

from scadbuddy.bambuddy import client as client_module
from scadbuddy.bambuddy.client import BambuddyClient, Operation
from scadbuddy.bambuddy.errors import BAD_BODY_PROBLEM, NOT_FOUND_PROBLEM, SCOPE_PROBLEM, Scope
from scadbuddy.bambuddy.models import Spool
from scadbuddy.core.problems import ApiError
from tests.bambuddy.conftest import BASE_URL, assert_recorded_body, recording, shaped

API = f"{BASE_URL}/api/v1"
SYNC = "sync-routes.json"


def _spool(**values: object) -> dict[str, object]:
    return shaped("SpoolResponse", id=3, material="PLA", **values)


def _sent(route: respx.Route) -> typing.Any:
    return json.loads(route.calls.last.request.content)


@respx.mock
async def test_create_spool_sends_a_spool_create(bambuddy: BambuddyClient) -> None:
    body = {
        "material": "PLA",
        "subtype": "Basic",
        "rgba": "FF0000FF",
        "label_weight": 1000,
        "core_weight": 250,
        "note": "from ScadBuddy",
        "low_stock_threshold_pct": 20,
        "location_id": None,
    }
    supplier = shaped(
        "SpoolSupplierResponse", id=1, supplier_id=4, supplier_name="Bambu", is_purchase_source=True
    )
    route = respx.post(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(
            200,
            json=_spool(note="from ScadBuddy", low_stock_threshold_pct=20, suppliers=[supplier]),
        )
    )

    spool = await bambuddy.create_spool(body)

    assert _sent(route) == body
    assert_recorded_body(SYNC, "SpoolCreate", _sent(route))
    assert route.calls.last.request.headers["X-API-Key"] == "s3cret"
    assert spool.id == 3
    assert spool.note == "from ScadBuddy"
    assert spool.low_stock_threshold_pct == 20
    assert [(s.supplier_id, s.supplier_name, s.is_purchase_source) for s in spool.suppliers] == [
        (4, "Bambu", True)
    ]


@respx.mock
async def test_update_spool_sends_only_the_given_fields(bambuddy: BambuddyClient) -> None:
    route = respx.patch(f"{API}/inventory/spools/3").mock(
        return_value=httpx.Response(200, json=_spool(weight_locked=True))
    )

    spool = await bambuddy.update_spool(3, {"note": None, "weight_locked": True})

    assert _sent(route) == {"note": None, "weight_locked": True}
    assert_recorded_body(SYNC, "SpoolUpdate", _sent(route))
    assert spool.weight_locked is True


@respx.mock
async def test_archive_and_restore_post_to_their_routes(bambuddy: BambuddyClient) -> None:
    archive = respx.post(f"{API}/inventory/spools/3/archive").mock(
        return_value=httpx.Response(200, json=_spool(archived_at="2026-11-18T12:00:00"))
    )
    restore = respx.post(f"{API}/inventory/spools/3/restore").mock(
        return_value=httpx.Response(200, json=_spool(archived_at=None))
    )

    archived = await bambuddy.archive_spool(3)
    restored = await bambuddy.restore_spool(3)

    assert archive.calls.last.request.content == b""
    assert restore.calls.last.request.content == b""
    assert archived.archived_at is not None
    assert restored.archived_at is None


@respx.mock
async def test_put_k_profiles_sends_a_list(bambuddy: BambuddyClient) -> None:
    rows = [
        {
            "printer_id": 1,
            "extruder": 0,
            "nozzle_diameter": "0.4",
            "nozzle_type": "HS",
            "k_value": 0.02,
            "name": "PLA Basic",
            "cali_idx": 5,
            "setting_id": "GFSA00",
        },
        {"printer_id": 1, "extruder": 1, "nozzle_diameter": "0.2", "k_value": 0.035},
    ]
    route = respx.put(f"{API}/inventory/spools/3/k-profiles").mock(
        return_value=httpx.Response(200, json=[])
    )

    await bambuddy.put_spool_k_profiles(3, rows)

    assert _sent(route) == rows
    assert_recorded_body(SYNC, "SpoolKProfileBase", _sent(route), many=True)


@respx.mock
async def test_put_presets_and_suppliers_send_lists(bambuddy: BambuddyClient) -> None:
    presets = [{"printer_model": "H2C", "nozzle_diameter": "0.4", "slicer_filament": "GFA00"}]
    links = [{"supplier_id": 4, "quoted_price_per_kg": 18.5, "is_purchase_source": True}]
    put_presets = respx.put(f"{API}/inventory/spools/3/filament-presets").mock(
        return_value=httpx.Response(200, json=[])
    )
    put_links = respx.put(f"{API}/inventory/spools/3/suppliers").mock(
        return_value=httpx.Response(200, json=[])
    )

    await bambuddy.put_spool_presets(3, presets)
    await bambuddy.put_spool_suppliers(3, links)
    await bambuddy.put_spool_suppliers(3, [])

    assert json.loads(put_presets.calls.last.request.content) == presets
    assert_recorded_body(SYNC, "SpoolFilamentPresetBase", presets, many=True)
    assert json.loads(put_links.calls[0].request.content) == links
    assert_recorded_body(SYNC, "SpoolSupplierLinkInput", links, many=True)
    assert json.loads(put_links.calls[1].request.content) == []


@respx.mock
async def test_supplier_and_location_round_trip(bambuddy: BambuddyClient) -> None:
    supplier = shaped(
        "SupplierResponse",
        id=4,
        name="Bambu",
        website="https://bambulab.com",
        customer_number=None,
        note=None,
        spool_count=2,
        created_at="2026-11-01T10:00:00",
        updated_at="2026-11-02T10:00:00",
    )
    location = shaped(
        "LocationResponse",
        id=7,
        name="Dry box",
        identifier="DB1",
        spool_count=0,
        created_at="2026-11-01T10:00:00",
        updated_at="2026-11-02T10:00:00",
    )
    respx.get(f"{API}/inventory/suppliers").mock(return_value=httpx.Response(200, json=[supplier]))
    create_supplier = respx.post(f"{API}/inventory/suppliers").mock(
        return_value=httpx.Response(201, json=supplier)
    )
    update_supplier = respx.patch(f"{API}/inventory/suppliers/4").mock(
        return_value=httpx.Response(200, json=supplier)
    )
    respx.get(f"{API}/inventory/locations").mock(return_value=httpx.Response(200, json=[location]))
    create_location = respx.post(f"{API}/inventory/locations").mock(
        return_value=httpx.Response(201, json=location)
    )
    update_location = respx.patch(f"{API}/inventory/locations/7").mock(
        return_value=httpx.Response(200, json=location)
    )

    [listed] = await bambuddy.suppliers()
    created = await bambuddy.create_supplier({"name": "Bambu", "website": "https://bambulab.com"})
    updated = await bambuddy.update_supplier(4, {"note": "spools"})
    [place] = await bambuddy.locations()
    made = await bambuddy.create_location({"name": "Dry box", "identifier": "DB1"})
    moved = await bambuddy.update_location(7, {"name": "Dry box"})

    assert (listed.id, listed.name, listed.website, listed.customer_number, listed.note) == (
        4,
        "Bambu",
        "https://bambulab.com",
        None,
        None,
    )
    assert listed.updated_at is not None
    assert created.id == updated.id == 4
    assert_recorded_body(SYNC, "SupplierCreate", _sent(create_supplier))
    assert _sent(update_supplier) == {"note": "spools"}
    assert_recorded_body(SYNC, "SupplierUpdate", _sent(update_supplier))
    assert (place.id, place.name, place.identifier) == (7, "Dry box", "DB1")
    assert place.updated_at is not None
    assert made.id == moved.id == 7
    assert_recorded_body(SYNC, "LocationCreate", _sent(create_location))
    assert _sent(update_location) == {"name": "Dry box"}
    assert_recorded_body(SYNC, "LocationUpdate", _sent(update_location))


@respx.mock
async def test_catalog_entry_writes_send_a_name_and_whole_grams(bambuddy: BambuddyClient) -> None:
    entry = shaped(
        "CatalogEntryResponse", id=30, name="Cardboard 1 kg", weight=147, is_default=False
    )
    create = respx.post(f"{API}/inventory/catalog").mock(
        return_value=httpx.Response(200, json=entry)
    )
    update = respx.put(f"{API}/inventory/catalog/30").mock(
        return_value=httpx.Response(200, json=entry)
    )

    created = await bambuddy.create_catalog_entry("Cardboard 1 kg", 146.6)
    updated = await bambuddy.update_catalog_entry(30, "Cardboard 1 kg", 147.0)

    assert _sent(create) == {"name": "Cardboard 1 kg", "weight": 147}
    assert_recorded_body(SYNC, "CatalogEntryCreate", _sent(create))
    assert _sent(update) == {"name": "Cardboard 1 kg", "weight": 147}
    assert_recorded_body(SYNC, "CatalogEntryUpdate", _sent(update))
    assert (created.id, created.weight) == (updated.id, updated.weight) == (30, 147)


@respx.mock
async def test_filament_writes_send_their_bodies(bambuddy: BambuddyClient) -> None:
    filament = shaped(
        "FilamentResponse",
        id=2,
        name="PLA Basic",
        type="PLA",
        brand="Bambu Lab",
        color_hex="#FF0000",
        density=1.24,
        created_at="2026-11-01T10:00:00",
        updated_at="2026-11-02T10:00:00",
    )
    create = respx.post(f"{API}/filament-catalog/").mock(
        return_value=httpx.Response(200, json=filament)
    )
    update = respx.patch(f"{API}/filament-catalog/2").mock(
        return_value=httpx.Response(200, json=filament)
    )

    made = await bambuddy.create_filament({"name": "PLA Basic", "type": "PLA", "density": 1.24})
    changed = await bambuddy.update_filament(2, {"density": 1.24})

    assert_recorded_body(SYNC, "FilamentCreate", _sent(create))
    assert _sent(update) == {"density": 1.24}
    assert_recorded_body(SYNC, "FilamentUpdate", _sent(update))
    assert (made.id, made.density) == (changed.id, changed.density) == (2, 1.24)


@respx.mock
async def test_a_403_names_manage_inventory(bambuddy: BambuddyClient) -> None:
    respx.post(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(403, json={"detail": "Forbidden"})
    )

    with pytest.raises(ApiError) as caught:
        await bambuddy.create_spool({"material": "PLA"})

    assert caught.value.status == 409
    assert caught.value.type == SCOPE_PROBLEM
    assert caught.value.extensions["required_scope"] == Scope.MANAGE_INVENTORY.value


@respx.mock
async def test_a_403_on_a_filament_write_says_no_key_can_make_it(bambuddy: BambuddyClient) -> None:
    respx.patch(f"{API}/filament-catalog/2").mock(
        return_value=httpx.Response(403, json={"detail": "Forbidden"})
    )

    with pytest.raises(ApiError) as caught:
        await bambuddy.update_filament(2, {"density": 1.24})

    assert caught.value.status == 409
    assert caught.value.type == SCOPE_PROBLEM
    assert caught.value.extensions["required_scope"] == Scope.ADMIN_FILAMENTS.value
    assert "No Bambuddy API key can" in caught.value.detail


@respx.mock
async def test_a_404_on_update_is_not_found(bambuddy: BambuddyClient) -> None:
    respx.patch(f"{API}/inventory/spools/9").mock(
        return_value=httpx.Response(404, json={"detail": "Spool not found"})
    )

    with pytest.raises(ApiError) as caught:
        await bambuddy.update_spool(9, {"note": "x"})

    assert caught.value.status == 404
    assert caught.value.type == NOT_FOUND_PROBLEM
    assert caught.value.extensions["bambuddy_status"] == 404


def test_a_k_profile_row_parses_its_printer_and_k() -> None:
    # No live spool had a K-profile on 2026-11-18 (sync-routes.json `measured_2026-11-18`),
    # so a recorded row carries one built from the recorded SpoolKProfileResponse.
    row = recording("inventory-spools.json")[0]
    profile = shaped(
        "SpoolKProfileResponse",
        id=11,
        spool_id=row["id"],
        printer_id=1,
        extruder=1,
        nozzle_diameter="0.4",
        nozzle_type="HS",
        k_value=0.025,
        name="PETG Basic",
        cali_idx=3,
        setting_id="GFSG00",
        created_at="2026-11-01T10:00:00",
    )

    spool = Spool.model_validate({**row, "k_profiles": [profile]})

    [k] = spool.k_profiles
    assert (k.printer_id, k.extruder, k.nozzle_diameter, k.k_value) == (1, 1, "0.4", 0.025)
    assert (k.id, k.spool_id, k.nozzle_type, k.name, k.cali_idx, k.setting_id) == (
        11,
        row["id"],
        "HS",
        "PETG Basic",
        3,
        "GFSG00",
    )
    assert k.created_at is not None


def test_rows_parsed_today_still_parse() -> None:
    spool = Spool.model_validate(
        {**recording("inventory-spools.json")[0], "k_profiles": [{"setting_id": "GFSA00"}]}
    )

    assert spool.k_profiles[0].setting_id == "GFSA00"
    assert spool.k_profiles[0].printer_id is None
    assert spool.suppliers == []
    assert spool.note is None


def test_every_sync_operation_is_a_declared_name() -> None:
    passed = set(re.findall(r'operation="([a-z_.]+)"', inspect.getsource(client_module)))
    new = {
        "spools.create",
        "spools.update",
        "spools.archive",
        "spools.restore",
        "spools.k_profiles",
        "spools.presets_put",
        "spools.suppliers_put",
        "suppliers.list",
        "suppliers.create",
        "suppliers.update",
        "locations.list",
        "locations.create",
        "locations.update",
        "catalog.create",
        "catalog.update",
        "filament_catalog.create",
        "filament_catalog.update",
    }
    assert new <= passed
    assert new <= set(typing.get_args(Operation))


@pytest.mark.parametrize(
    ("answer", "detail"),
    [
        (httpx.Response(200, text="<html>proxy</html>"), "not JSON"),
        (httpx.Response(200, json={"not": "a list"}), "dict, not a list"),
    ],
)
@respx.mock
async def test_a_listing_body_of_the_wrong_kind_is_a_bad_body(
    bambuddy: BambuddyClient, answer: httpx.Response, detail: str
) -> None:
    """R-T7-10: a listing that is not JSON, or not a JSON array, is a 502 of its own type,
    so `rfid_push.error_label` says what happened; Bambuddy's success status is not kept."""
    respx.get(f"{API}/inventory/suppliers").mock(return_value=answer)

    with pytest.raises(ApiError) as caught:
        await bambuddy.suppliers()

    assert caught.value.status == 502 and caught.value.type == BAD_BODY_PROBLEM
    assert detail in caught.value.detail
    assert "bambuddy_status" not in caught.value.extensions
