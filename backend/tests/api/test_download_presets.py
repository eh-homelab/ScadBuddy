"""#769: a downloaded 3MF names the default printer's real Bambu presets."""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any
from xml.etree import ElementTree as ET

import httpx
import psycopg
import pytest
import respx
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.render.bambu3mf import PRESET_PLACEHOLDER, replate_3mf
from scadbuddy.render.plate import PlateGeometry, plate_for
from tests.api.test_print import printers_route
from tests.api.test_send import BASE, configure, make_output
from tests.bambuddy.conftest import recording

pytestmark = pytest.mark.requires_postgres

API = f"{BASE}/api/v1"


def _red_spool() -> list[dict[str, Any]]:
    """The recorded Bambu PLA Basic spool 4, in the fake render's #FF0000."""
    spools: list[dict[str, Any]] = recording("inventory-spools.json")
    return [{**row, "rgba": "FF0000FF"} for row in spools if row["id"] == 4]


def _bambuddy(*, status: bool = True, printer: dict[str, Any] | None = None) -> None:
    printers_route()
    respx.get(f"{API}/printers/1").mock(
        return_value=httpx.Response(200, json=printer or recording("printer.json"))
    )
    if status:
        respx.get(f"{API}/printers/1/status").mock(
            return_value=httpx.Response(200, json=recording("printer-status.json"))
        )
    respx.get(f"{API}/slicer/presets").mock(
        return_value=httpx.Response(200, json=recording("slicer-presets-h2c.json"))
    )
    respx.get(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json=recording("local-presets.json"))
    )
    respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(200, json=_red_spool()))
    respx.get(f"{API}/inventory/assignments").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{API}/printers/1/inventory-remain").mock(
        return_value=httpx.Response(200, json=recording("inventory-remain.json"))
    )
    respx.route(method="GET", path__regex=r"/api/v1/inventory/spools/\d+/filament-presets").mock(
        return_value=httpx.Response(200, json=[])
    )


def _project_settings(payload: bytes) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        loaded: dict[str, Any] = json.loads(archive.read("Metadata/project_settings.config"))
        return loaded


def _stored(paths: DataPaths, model: str, output_id: str) -> bytes:
    return (paths.output_dir(model, output_id) / "model.3mf").read_bytes()


@respx.mock
def test_a_download_names_the_default_printers_presets(client: TestClient, model: str) -> None:
    configure(client, printer_id=1)
    output_id = make_output(client, model)
    _bambuddy()

    response = client.get(f"/api/v1/outputs/{output_id}/model.3mf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "model/3mf"
    assert 'filename="demo-elan.3mf"' in response.headers["content-disposition"]
    settings = _project_settings(response.content)
    # The recorded printer's right-hand nozzle is a 0.2, and the default tier is Standard.
    assert settings["printer_settings_id"] == "Bambu Lab H2C 0.2 nozzle"
    assert settings["print_settings_id"] == "0.10mm Standard @BBL H2C 0.2 nozzle"
    assert settings["filament_settings_id"] == ["Bambu PLA Basic @BBL H2C 0.2 nozzle"]
    assert settings["nozzle_diameter"] == ["0.2", "0.2"]
    assert settings["printer_model"] == "Bambu Lab H2C"
    assert settings["printable_height"]


@respx.mock
def test_an_h2c_that_reports_no_nozzle_count_gets_both_extruders(
    client: TestClient, model: str
) -> None:
    configure(client, printer_id=1)
    output_id = make_output(client, model)
    _bambuddy(printer={**recording("printer.json"), "nozzle_count": None})

    body = _project_settings(client.get(f"/api/v1/outputs/{output_id}/model.3mf").content)

    assert body["nozzle_diameter"] == ["0.2", "0.2"]


@respx.mock
def test_a_download_uses_the_models_remembered_choices(
    client: TestClient, model: str, settings: Settings
) -> None:
    configure(client, printer_id=1)
    output_id = make_output(client, model)
    with psycopg.connect(settings.database_url) as conn:
        conn.execute(
            "INSERT INTO model_print_choices (model_id, choices) VALUES (%s, %s)",
            (
                model,
                Jsonb(
                    {
                        "printer_id": 1,
                        "nozzles": [{"size": "0.4"}],
                        "tier": "fine",
                        "filament_plan": [{"slot_id": 1, "spool_id": 4}],
                    }
                ),
            ),
        )
    # No status route: remembered nozzles need no mounted ones.
    _bambuddy(status=False)

    body = _project_settings(client.get(f"/api/v1/outputs/{output_id}/model.3mf").content)

    assert body["printer_settings_id"] == "Bambu Lab H2C 0.4 nozzle"
    assert body["print_settings_id"] == "0.12mm High Quality @BBL H2C"
    assert body["filament_settings_id"] == ["Bambu PLA Basic @BBL H2C"]
    assert body["nozzle_diameter"] == ["0.4", "0.4"]


@respx.mock
def test_choices_remembered_for_another_printer_are_not_used(
    client: TestClient, model: str, settings: Settings
) -> None:
    configure(client, printer_id=1)
    output_id = make_output(client, model)
    with psycopg.connect(settings.database_url) as conn:
        conn.execute(
            "INSERT INTO model_print_choices (model_id, choices) VALUES (%s, %s)",
            (model, Jsonb({"printer_id": 2, "nozzles": [{"size": "0.4"}], "tier": "fine"})),
        )
    _bambuddy()

    body = _project_settings(client.get(f"/api/v1/outputs/{output_id}/model.3mf").content)

    # Printer 1's own right-hand 0.2 and the default tier, not printer 2's 0.4 Fine.
    assert body["printer_settings_id"] == "Bambu Lab H2C 0.2 nozzle"
    assert body["print_settings_id"] == "0.10mm Standard @BBL H2C 0.2 nozzle"


@respx.mock
def test_an_unreported_right_hand_nozzle_is_not_the_left_ones_size(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    configure(client, printer_id=1)
    output_id = make_output(client, model)
    _bambuddy(status=False)
    status = {
        **recording("printer-status.json"),
        "nozzles": [
            {"nozzle_type": "", "nozzle_diameter": ""},
            {"nozzle_type": "HS01", "nozzle_diameter": "0.4"},
        ],
    }
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(200, json=status))

    response = client.get(f"/api/v1/outputs/{output_id}/model.3mf")

    assert response.status_code == 200
    # No mounted size is known, so the presets stay placeholders; the plate still fits.
    stored = _stored(paths, model, output_id)
    _assert_refitted_on_placeholders(response.content, stored, plate_for("H2C"))


def test_without_a_default_printer_the_stored_file_is_served(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    configure(client)
    output_id = make_output(client, model)

    response = client.get(f"/api/v1/outputs/{output_id}/model.3mf")

    assert response.status_code == 200
    assert response.content == _stored(paths, model, output_id)
    assert _project_settings(response.content)["printer_settings_id"] == PRESET_PLACEHOLDER


def test_the_stored_file_is_still_served_in_ranges(client: TestClient, model: str) -> None:
    """Unchanged, it is the FileResponse it was before #769, so a download can resume."""
    configure(client)
    output_id = make_output(client, model)

    response = client.get(f"/api/v1/outputs/{output_id}/model.3mf", headers={"Range": "bytes=0-3"})

    assert response.status_code == 206
    assert response.content == b"PK\x03\x04"


@respx.mock
def test_an_unreachable_bambuddy_serves_the_stored_file(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    configure(client, printer_id=1)
    output_id = make_output(client, model)
    respx.route(host="bambuddy.test").mock(side_effect=httpx.ConnectError("refused"))

    response = client.get(f"/api/v1/outputs/{output_id}/model.3mf")

    assert response.status_code == 200
    assert response.content == _stored(paths, model, output_id)


def _item_transform(payload: bytes) -> str | None:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        root = ET.fromstring(archive.read("3D/3dmodel.model"))
    item = root.find(".//{*}item")
    assert item is not None
    return item.get("transform")


def _assert_refitted_on_placeholders(content: bytes, stored: bytes, plate: PlateGeometry) -> None:
    """Re-plated for the printer, with the preset names left as the file had them."""
    assert content != stored
    assert _item_transform(content) == _item_transform(replate_3mf(stored, plate))
    settings = _project_settings(content)
    assert settings["printer_settings_id"] == PRESET_PLACEHOLDER
    assert settings["print_settings_id"] == PRESET_PLACEHOLDER
    assert (
        settings["printable_height"]
        == _project_settings(replate_3mf(stored, plate))["printable_height"]
    )


@respx.mock
def test_a_resolver_refusal_still_refits_the_plate(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """No spool matches the render's red, so the slot has no filament preset."""
    configure(client, printer_id=1)
    output_id = make_output(client, model)
    _bambuddy()
    respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(200, json=[]))

    response = client.get(f"/api/v1/outputs/{output_id}/model.3mf")

    assert response.status_code == 200
    stored = _stored(paths, model, output_id)
    _assert_refitted_on_placeholders(response.content, stored, plate_for("H2C"))


@respx.mock
def test_presets_bambuddy_cannot_read_still_refit_the_plate(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    configure(client, printer_id=1)
    output_id = make_output(client, model)
    _bambuddy()
    respx.get(f"{API}/slicer/presets").mock(return_value=httpx.Response(500))

    response = client.get(f"/api/v1/outputs/{output_id}/model.3mf")

    assert response.status_code == 200
    stored = _stored(paths, model, output_id)
    _assert_refitted_on_placeholders(response.content, stored, plate_for("H2C"))


@respx.mock
def test_a_printer_without_presets_still_gets_its_plate(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """The resolver names H2C presets only; an A1 mini still gets its own plate."""
    configure(client, printer_id=1)
    output_id = make_output(client, model)
    printer = {**recording("printers.json")[0], "model": "A1 mini"}
    respx.get(f"{API}/printers/").mock(return_value=httpx.Response(200, json=[printer]))
    respx.get(f"{API}/printers/1").mock(return_value=httpx.Response(200, json=printer))

    response = client.get(f"/api/v1/outputs/{output_id}/model.3mf")

    assert response.status_code == 200
    assert 'filename="demo-elan.3mf"' in response.headers["content-disposition"]
    stored = _stored(paths, model, output_id)
    _assert_refitted_on_placeholders(response.content, stored, plate_for("A1 mini"))
