"""#7153: a printer's AMS slots through Bambuddy, from ScadBuddy (plan
``docs/superpowers/plans/2026-11-23-ams-slot-control.md``). The routes take SB's registry
printer; each write that reaches the printer reads Bambuddy's status first (R-7153-3).
Bambuddy is respx only."""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.bambuddy.resolver import PRINTER_MODEL
from scadbuddy.filament.catalog import CatalogRefError
from scadbuddy.filament.component import FILAMENT, Filament
from scadbuddy.filament.models import Material, SpoolCreate, SpoolKValueCreate, SpoolPresetCreate
from scadbuddy.filament.sb_presets import create_on
from scadbuddy.printers.models import PrinterCreate
from scadbuddy.slicing.own_presets import sb_ref
from tests.api.test_printer_kprofiles import SERIAL, linked, store
from tests.api.test_send import API, configure

pytestmark = pytest.mark.requires_postgres

RECORDED = json.loads(
    (Path(__file__).parents[1] / "bambuddy" / "recordings" / "printer-status.json").read_text(
        encoding="utf-8"
    )
)
DONE = {"success": True, "message": "Loading filament from AMS 0 slot 2"}


def idle(**values: Any) -> dict[str, Any]:
    """The recorded H2C status (two nozzles, the Filament Track Switch, AMS 0 on inlet B:
    the right extruder, a 0.2 nozzle), idle unless ``values`` say otherwise."""
    body: dict[str, Any] = copy.deepcopy(RECORDED)
    body.update({"state": "IDLE", "tray_now": 255, "ams_status_main": 0})
    body["ams"][0]["serial_number"] = "19C06A000000001"
    body.update(values)
    return body


def status_route(body: dict[str, Any] | None = None) -> respx.Route:
    return respx.get(f"{API}/printers/3/status").mock(
        return_value=httpx.Response(200, json=body or idle())
    )


def view_routes() -> None:
    respx.get(f"{API}/printers/3/slot-presets").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{API}/printers/3/ams-labels").mock(return_value=httpx.Response(200, json={}))


def slot(pid: str, ams: int = 0, tray: int = 1) -> str:
    return f"/api/v1/printers/{pid}/trays/{ams}/{tray}"


def filament(client: TestClient) -> Filament:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state.components.get(FILAMENT)


@respx.mock
def test_load_names_the_global_tray_and_the_hotend_after_the_state(client: TestClient) -> None:
    pid = linked(client, 3)
    status = status_route()
    load = respx.post(f"{API}/printers/3/ams/load").mock(
        return_value=httpx.Response(200, json=DONE)
    )

    response = client.post(f"{slot(pid)}/load")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "ams_id": 0,
        "tray_id": 1,
        "action": "load",
        "message": DONE["message"],
    }
    assert status.called
    # AMS 0 slot 1 is global tray 1; inlet B is the right extruder, 0.
    assert dict(load.calls.last.request.url.params) == {"tray_id": "1", "extruder_id": "0"}


@respx.mock
@pytest.mark.parametrize("action", ["configure", "reset", "refresh", "load", "unload"])
@pytest.mark.parametrize(
    ("values", "code"),
    [
        ({"connected": False}, "printer_offline"),
        ({"state": "RUNNING"}, "printer_busy"),
        ({"state": "PAUSE"}, "printer_busy"),
        ({"ams_status_main": 1}, "ams_busy"),
        ({"ams_status_main": 2}, "ams_busy"),
        ({"ams_status_main": 7}, "ams_busy"),
    ],
)
def test_every_printer_write_refuses_an_unsafe_state(
    client: TestClient, action: str, values: dict[str, Any], code: str
) -> None:
    pid = linked(client, 3)
    status_route(idle(**values))
    writes = respx.post(url__regex=rf"{API}/printers/3/(ams|slots)/.*")

    body = {"material": "PLA", "colour": "#FFFFFF"} if action == "configure" else None
    response = client.post(f"{slot(pid)}/{action}", json=body)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == code
    assert not writes.called


@respx.mock
@pytest.mark.parametrize(
    ("action", "values", "tray", "code"),
    [
        ("load", {}, 1, None),
        ("load", {"tray_now": 1}, 1, "already_loaded"),
        ("refresh", {"tray_now": 6}, 1, "filament_loaded"),
        ("reset", {}, 9, None),
    ],
)
def test_load_and_refresh_check_the_slot(
    client: TestClient, action: str, values: dict[str, Any], tray: int, code: str | None
) -> None:
    pid = linked(client, 3)
    status_route(idle(**values))
    respx.post(url__regex=rf"{API}/printers/3/ams/.*").mock(
        return_value=httpx.Response(200, json=DONE)
    )
    response = client.post(f"{slot(pid, 0, tray)}/{action}")
    if tray > 3:
        assert response.status_code == 422, response.text
    elif code is None:
        assert response.status_code == 200, response.text
    else:
        assert response.status_code == 409, response.text
        assert response.json()["code"] == code


@respx.mock
def test_a_slot_with_no_spool_is_not_loaded(client: TestClient) -> None:
    pid = linked(client, 3)
    body = idle()
    body["ams"][0]["tray"][1].update({"tray_type": "", "state": 9})
    status_route(body)
    load = respx.post(f"{API}/printers/3/ams/load")
    response = client.post(f"{slot(pid)}/load")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "tray_empty"
    assert not load.called


@respx.mock
def test_a_slot_the_status_does_not_show_is_refused(client: TestClient) -> None:
    pid = linked(client, 3)
    status_route()
    response = client.post(f"{slot(pid, 3, 0)}/reset")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "no_slot"


def test_refresh_and_load_refuse_what_bambuddy_cannot_address(client: TestClient) -> None:
    pid = linked(client, 3)
    assert client.post(f"{slot(pid, 255, 0)}/refresh").status_code == 422
    assert client.post(f"{slot(pid, 5, 0)}/load").status_code == 422
    assert client.post(f"{slot(pid, 128, 1)}/reset").status_code == 422


@respx.mock
def test_the_ams_assist_state_is_not_busy(client: TestClient) -> None:
    pid = linked(client, 3)
    status_route(idle(ams_status_main=3))
    respx.post(f"{API}/printers/3/ams/load").mock(return_value=httpx.Response(200, json=DONE))

    response = client.post(f"{slot(pid)}/load")

    assert response.status_code == 200, response.text
    view_routes()
    assert client.get(f"/api/v1/printers/{pid}/ams").json()["ams_busy"] is False


@respx.mock
def test_ext_r_is_not_told_from_nothing_loaded(client: TestClient) -> None:
    """The documented limit (`slot_control.EXTERNAL_TRAY`): Ext-R's tray 255 is also
    `tray_now` for nothing loaded."""
    pid = linked(client, 3)
    body = idle(fila_switch=None, tray_now=255)
    body["vt_tray"][1].update({"tray_type": "PETG", "tray_color": "FF0000FF", "state": 11})
    status_route(body)
    respx.post(f"{API}/printers/3/ams/load").mock(return_value=httpx.Response(200, json=DONE))

    assert client.post(f"{slot(pid, 255, 1)}/load").status_code == 200
    view_routes()
    units = {u["ams_id"]: u for u in client.get(f"/api/v1/printers/{pid}/ams").json()["units"]}
    assert all(not t["loaded"] for t in units[255]["slots"])


@respx.mock
def test_an_external_holder_loads_as_254_and_255(client: TestClient) -> None:
    pid = linked(client, 3)
    body = idle(fila_switch=None)
    body["vt_tray"][1].update({"tray_type": "PETG", "tray_color": "FF0000FF", "state": 11})
    status_route(body)
    load = respx.post(f"{API}/printers/3/ams/load").mock(
        return_value=httpx.Response(200, json=DONE)
    )
    response = client.post(f"{slot(pid, 255, 1)}/load")
    assert response.status_code == 200, response.text
    assert dict(load.calls.last.request.url.params) == {"tray_id": "255"}


@respx.mock
def test_unload_passes_bambuddys_conflict_on(client: TestClient) -> None:
    pid = linked(client, 3)
    status_route()
    respx.post(f"{API}/printers/3/ams/unload").mock(
        return_value=httpx.Response(409, json={"detail": "No hotend is loaded from that slot"})
    )
    response = client.post(f"{slot(pid, 128, 0)}/unload")
    assert response.status_code == 409, response.text
    assert "No hotend is loaded" in response.json()["detail"]


@respx.mock
def test_configure_by_hand_sends_the_values(client: TestClient) -> None:
    pid = linked(client, 3)
    status_route()
    route = respx.post(f"{API}/printers/3/slots/0/1/configure").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    response = client.post(
        f"{slot(pid)}/configure",
        json={
            "material": "PETG",
            "colour": "#00ae42",
            "preset_id": "GFG99",
            "preset_name": "Generic PETG",
            "nozzle_temp_min": 230,
            "nozzle_temp_max": 260,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["colour"] == "#00AE42FF"
    assert response.json()["nozzle_diameter"] == "0.2"  # the right extruder's, from the status
    assert dict(route.calls.last.request.url.params) == {
        "tray_info_idx": "GFG99",
        "tray_type": "PETG",
        "tray_sub_brands": "Generic PETG",
        "tray_color": "00AE42FF",
        "nozzle_temp_min": "230",
        "nozzle_temp_max": "260",
        "nozzle_diameter": "0.2",
    }


@respx.mock
def test_configure_from_any_spool_takes_its_values(client: TestClient) -> None:
    pid = linked(client, 3)
    status_route()
    route = respx.post(f"{API}/printers/3/slots/0/1/configure").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    store_ = filament(client)
    pla = next(
        r.id
        for r in store_.catalog.list("materials")
        if isinstance(r, Material) and r.name == "PLA"
    )
    spool = store_.spools.create(
        SpoolCreate(material_id=pla, rgba="#112233FF", nozzle_temp_min=190, nozzle_temp_max=220),
        actor="ann",
    )

    response = client.post(
        f"{slot(pid)}/configure", json={"spool_id": str(spool.id), "nozzle_diameter": "0.4"}
    )

    assert response.status_code == 200, response.text
    sent = dict(route.calls.last.request.url.params)
    assert (sent["tray_type"], sent["tray_color"]) == ("PLA", "112233FF")
    assert (sent["nozzle_temp_min"], sent["nozzle_temp_max"]) == ("190", "220")
    assert sent["nozzle_diameter"] == "0.4"
    assert response.json()["spool_id"] == str(spool.id)


def preset_spool(client: TestClient, ref: str) -> UUID:
    """A PLA spool whose own preset for the H2C's 0.4 nozzle is ``ref``."""
    store_ = filament(client)
    pla = next(
        r.id
        for r in store_.catalog.list("materials")
        if isinstance(r, Material) and r.name == "PLA"
    )
    spool = store_.spools.create(
        SpoolCreate(material_id=pla, rgba="#112233FF", nozzle_temp_min=190, nozzle_temp_max=220),
        actor="ann",
    )
    store_.spools.set_presets(
        spool.id,
        [
            SpoolPresetCreate(
                printer_model=PRINTER_MODEL,
                nozzle_diameter="0.4",
                slicer_filament=ref,
                slicer_filament_name="My PLA",
            )
        ],
        actor="ann",
    )
    return spool.id


@respx.mock
def test_configure_says_where_a_spools_preset_lives(client: TestClient) -> None:
    """A ScadBuddy preset is its mirror, a Bambuddy local preset, as is a bare Bambuddy
    local preset id; any other ref is Bambu's. A hand-typed id names no source."""
    pid = linked(client, 3)
    status_route()
    route = respx.post(f"{API}/printers/3/slots/0/1/configure").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    with filament(client).spools.transaction() as conn:
        own = create_on(
            conn,
            name="Mirrored",
            source="manual",
            key=f"attachment-{1:064d}",
            sha256=f"{1:064d}",
            doc={"name": "Mirrored"},
            made_by="test",
            bb_preset_id=141,
        )
    cases = [(sb_ref(own), "141", "local"), ("57", "57", "local"), ("GFA00", "GFA00", "cloud")]
    for ref, sent_id, source in cases:
        spool = preset_spool(client, ref)
        response = client.post(
            f"{slot(pid)}/configure", json={"spool_id": str(spool), "nozzle_diameter": "0.4"}
        )
        assert response.status_code == 200, response.text
        assert dict(route.calls.last.request.url.params)["tray_info_idx"] == sent_id
        assert (response.json()["preset_id"], response.json()["preset_source"]) == (
            sent_id,
            source,
        )

    typed = client.post(
        f"{slot(pid)}/configure",
        json={"spool_id": str(spool), "nozzle_diameter": "0.4", "preset_id": "PFUS1234"},
    )
    assert typed.status_code == 200, typed.text
    assert (typed.json()["preset_id"], typed.json()["preset_source"]) == ("PFUS1234", None)


def single_nozzle() -> dict[str, Any]:
    """An X1C-like status: one 0.4 nozzle, Bambuddy's empty second ``nozzles`` entry, no
    AMS map and no Filament Track Switch."""
    body = idle(ams_extruder_map={}, ams_switch_inlet={}, fila_switch=None)
    body["nozzles"][0].update({"nozzle_type": "HS01", "nozzle_diameter": "0.4"})
    body["nozzles"][1].update({"nozzle_type": "", "nozzle_diameter": ""})
    return body


def k_spool(client: TestClient, printer: str, extruder: int) -> UUID:
    store_ = filament(client)
    pla = next(
        r.id
        for r in store_.catalog.list("materials")
        if isinstance(r, Material) and r.name == "PLA"
    )
    spool = store_.spools.create(
        SpoolCreate(material_id=pla, rgba="#112233FF", nozzle_temp_min=190, nozzle_temp_max=220),
        actor="ann",
    )
    store_.spools.set_k_values(
        spool.id,
        [
            SpoolKValueCreate(
                printer_id=UUID(printer),
                extruder=extruder,
                nozzle_diameter="0.4",
                k_value=Decimal("0.031"),
                cali_idx=7,
            )
        ],
        actor="ann",
    )
    return spool.id


@respx.mock
def test_a_load_names_the_hotend_as_configure_does(client: TestClient) -> None:
    """The load reads the hotend through `slot_extruder`, as configure does. The Filament
    Track Switch counts as a second nozzle (`two_nozzles`), so with it and no AMS map the
    load never guesses extruder 0: it is 409 ``extruder_unknown``, and no load is sent."""
    pid = linked(client, 3)
    body = single_nozzle()
    body["fila_switch"] = RECORDED["fila_switch"]
    status_route(body)
    load = respx.post(f"{API}/printers/3/ams/load").mock(
        return_value=httpx.Response(200, json=DONE)
    )

    response = client.post(f"{slot(pid)}/load")

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "extruder_unknown"
    assert not load.called


@respx.mock
def test_a_single_nozzle_printer_takes_the_spools_k_value(client: TestClient) -> None:
    pid = linked(client, 3)
    status_route(single_nozzle())
    route = respx.post(f"{API}/printers/3/slots/0/1/configure").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    spool = k_spool(client, pid, extruder=0)

    response = client.post(f"{slot(pid)}/configure", json={"spool_id": str(spool)})

    assert response.status_code == 200, response.text
    assert (response.json()["nozzle_diameter"], response.json()["k_value"]) == ("0.4", 0.031)
    sent = dict(route.calls.last.request.url.params)
    assert (sent["cali_idx"], sent["k_value"]) == ("7", "0.031")


@respx.mock
def test_configure_from_a_spool_needs_a_nozzle_the_status_names(client: TestClient) -> None:
    pid = linked(client, 3)
    # Two nozzles, but nothing says which one AMS 0 feeds.
    status_route(idle(ams_extruder_map={}, ams_switch_inlet={}))
    configure_ = respx.post(f"{API}/printers/3/slots/0/1/configure")
    spool = k_spool(client, pid, extruder=0)

    response = client.post(f"{slot(pid)}/configure", json={"spool_id": str(spool)})

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "no_nozzle"
    assert not configure_.called


@respx.mock
def test_configure_names_what_is_missing(client: TestClient) -> None:
    pid = linked(client, 3)
    body = idle()
    body["ams"][0]["tray"][1].update({"nozzle_temp_min": 0, "nozzle_temp_max": 0})
    status_route(body)
    configure_ = respx.post(f"{API}/printers/3/slots/0/1/configure")
    response = client.post(f"{slot(pid)}/configure", json={"material": "PLA"})
    assert response.status_code == 422, response.text
    assert "colour, nozzle_temp_min, nozzle_temp_max" in response.json()["detail"]
    assert not configure_.called


def test_configure_refuses_an_unknown_spool(client: TestClient) -> None:
    pid = linked(client, 3)
    missing = "0b7a3a52-0000-4000-8000-000000000000"
    response = client.post(f"{slot(pid)}/configure", json={"spool_id": missing})
    assert response.status_code == 404, response.text


@respx.mock
def test_the_read_shows_slots_presets_labels_and_places(client: TestClient) -> None:
    pid = linked(client, 3)
    status_route(idle(tray_now=1))
    respx.get(f"{API}/printers/3/slot-presets").mock(
        return_value=httpx.Response(
            200,
            json={
                "1": {
                    "ams_id": 0,
                    "tray_id": 1,
                    "preset_id": "GFSG00",
                    "preset_name": "Bambu PETG Basic",
                    "tray_info_idx": "GFG00",
                }
            },
        )
    )
    respx.get(f"{API}/printers/3/ams-labels").mock(
        return_value=httpx.Response(200, json={"0": "Left rack"})
    )
    filament(client).locations.name_ams_unit(UUID(pid), 0, "Left rack")

    response = client.get(f"/api/v1/printers/{pid}/ams")

    assert response.status_code == 200, response.text
    view = response.json()
    assert (view["printing"], view["ams_busy"], view["tray_now"]) == (False, False, 1)
    units = {u["ams_id"]: u for u in view["units"]}
    assert list(units) == [0, 1, 2, 128, 255]
    first = units[0]
    assert (first["unit"], first["label"], first["place_name"]) == (
        "AMS-A",
        "Left rack",
        "Left rack",
    )
    tray = first["slots"][1]
    assert tray["loaded"] is True and tray["filled"] is True
    assert tray["saved_preset"]["preset_id"] == "GFSG00"
    assert units[255]["slots"][0]["refreshable"] is False
    assert "serial" not in json.dumps(view)


@respx.mock
def test_a_label_writes_bambuddy_then_names_the_place(client: TestClient) -> None:
    pid = linked(client, 3)
    status_route()
    label = respx.put(f"{API}/printers/3/ams-labels/0").mock(
        return_value=httpx.Response(200, json={"ams_id": 0, "label": "Left rack"})
    )

    response = client.put(f"/api/v1/printers/{pid}/ams/0/label", json={"label": "Left rack"})

    assert response.status_code == 200, response.text
    assert json.loads(label.calls.last.request.content) == {
        "label": "Left rack",
        "ams_serial": "19C06A000000001",
    }
    assert response.json()["place"]["name"] == "Left rack"
    assert filament(client).locations.ams_units(UUID(pid))[0].name == "Left rack"


@respx.mock
def test_a_refused_label_changes_no_place(client: TestClient) -> None:
    pid = linked(client, 3)
    status_route()
    respx.put(f"{API}/printers/3/ams-labels/0").mock(return_value=httpx.Response(403))
    response = client.put(f"/api/v1/printers/{pid}/ams/0/label", json={"label": "Left rack"})
    assert response.status_code == 409, response.text
    assert response.json()["required_scope"] == "Printers (admin only)"
    assert filament(client).locations.ams_units(UUID(pid)) == {}


@respx.mock
def test_a_cleared_label_names_the_place_by_its_unit_again(client: TestClient) -> None:
    pid = linked(client, 3)
    status_route()
    respx.delete(f"{API}/printers/3/ams-labels/0").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    filament(client).locations.name_ams_unit(UUID(pid), 0, "Left rack")
    response = client.delete(f"/api/v1/printers/{pid}/ams/0/label")
    assert response.status_code == 200, response.text
    assert response.json()["place"]["name"] == "H2C AMS-A"


@respx.mock
def test_two_units_may_share_a_label(client: TestClient) -> None:
    """A printer's places are outside the name index (``WHERE printer_id IS NULL``), so a
    label another unit holds names this unit's place too."""
    pid = linked(client, 3)
    status_route()
    respx.put(f"{API}/printers/3/ams-labels/0").mock(
        return_value=httpx.Response(200, json={"ams_id": 0, "label": "Shelf"})
    )
    places = filament(client).locations
    places.name_ams_unit(UUID(pid), 1, "Shelf")
    response = client.put(f"/api/v1/printers/{pid}/ams/0/label", json={"label": "Shelf"})
    assert response.status_code == 200, response.text
    assert response.json()["place_problem"] is None
    names = {k: v.name for k, v in places.ams_units(UUID(pid)).items()}
    assert (names[0], names[1]) == ("Shelf", "Shelf")


@respx.mock
def test_a_slot_preset_is_saved_and_cleared(client: TestClient) -> None:
    pid = linked(client, 3)
    put = respx.put(f"{API}/printers/3/slot-presets/0/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "ams_id": 0,
                "tray_id": 1,
                "preset_id": "GFSG00",
                "preset_name": "Bambu PETG Basic",
                "preset_source": "cloud",
                "tray_info_idx": "GFG00",
            },
        )
    )
    delete = respx.delete(f"{API}/printers/3/slot-presets/0/1").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    saved = client.put(
        f"{slot(pid)}/preset",
        json={"preset_id": "GFSG00", "preset_name": "Bambu PETG Basic", "tray_info_idx": "GFG00"},
    )
    assert saved.status_code == 200, saved.text
    assert put.calls.last.request.url.params["tray_info_idx"] == "GFG00"
    assert client.delete(f"{slot(pid)}/preset").status_code == 204
    assert delete.called


@respx.mock
def test_a_slot_preset_keeps_a_64_character_filament_id(client: TestClient) -> None:
    """Configure sends a preset id of up to 64 characters as the slot's ``tray_info_idx``,
    so the saved preset that follows it takes as many (#7507)."""
    pid = linked(client, 3)
    long_id = "G" * 64
    put = respx.put(f"{API}/printers/3/slot-presets/0/1").mock(
        return_value=httpx.Response(
            200,
            json={"ams_id": 0, "tray_id": 1, "preset_id": long_id, "preset_name": "Long"},
        )
    )
    saved = client.put(
        f"{slot(pid)}/preset",
        json={"preset_id": long_id, "preset_name": "Long", "tray_info_idx": long_id},
    )
    assert saved.status_code == 200, saved.text
    assert put.calls.last.request.url.params["tray_info_idx"] == long_id


@respx.mock
def test_a_printer_deleted_after_the_label_says_no_printer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bambuddy's label is written, then the place is not named because the printer row
    is gone: the answer says why (#7507)."""
    pid = linked(client, 3)
    status_route()
    respx.put(f"{API}/printers/3/ams-labels/0").mock(
        return_value=httpx.Response(200, json={"ams_id": 0, "label": "Shelf"})
    )

    def gone(*_args: object) -> None:
        raise CatalogRefError()

    monkeypatch.setattr(filament(client).locations, "name_ams_unit", gone)
    response = client.put(f"/api/v1/printers/{pid}/ams/0/label", json={"label": "Shelf"})
    assert response.status_code == 200, response.text
    assert (response.json()["place"], response.json()["place_problem"]) == (None, "no_printer")


def test_an_unlinked_or_unknown_printer_is_refused(client: TestClient) -> None:
    configure(client)
    bare = store(client).create(PrinterCreate(serial=SERIAL, name="Bare")).id
    assert client.post(f"{slot(bare)}/load").status_code == 409
    assert client.get(f"/api/v1/printers/{bare}/ams").status_code == 409
    missing = "0b7a3a52-0000-4000-8000-000000000000"
    assert client.post(f"{slot(missing)}/reset").status_code == 404


def test_a_bad_label_is_a_422(client: TestClient) -> None:
    pid = linked(client, 3)
    for body in ({"label": ""}, {"label": "x" * 101}, {"label": "a\nb"}):
        assert client.put(f"/api/v1/printers/{pid}/ams/0/label", json=body).status_code == 422


@respx.mock
def test_an_ams_unit_place_is_renamed_on_locations_without_bambuddy(client: TestClient) -> None:
    """The rename the parity sweep found untested: PATCH an AMS unit's place. It writes no
    Bambuddy label (R-7153-8): respx refuses any call no route matches."""
    pid = linked(client, 3)
    place = filament(client).locations.name_ams_unit(UUID(pid), 0, None)
    response = client.patch(f"/api/v1/filament/locations/{place.id}", json={"name": "Left rack"})
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Left rack"
    assert filament(client).locations.ams_units(UUID(pid))[0].name == "Left rack"
