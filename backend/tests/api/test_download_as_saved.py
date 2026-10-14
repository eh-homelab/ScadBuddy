"""#2652: a downloaded 3MF is the output's saved file, byte for byte. What it needs to
open in Bambu Studio, the template's ``print_settings`` (#770) among it, is written
when the output is saved."""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from tests.api.conftest import wait_for_job
from tests.api.test_send import BASE, configure, make_output
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres

KEYCHAIN = {"enable_support": "0", "enable_prime_tower": "1", "wipe_tower_no_sparse_layers": "1"}


def _project_settings(payload: bytes) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        loaded: dict[str, Any] = json.loads(archive.read("Metadata/project_settings.config"))
        return loaded


def _stored(paths: DataPaths, model: str, output_id: str) -> bytes:
    return (paths.output_dir(model, output_id) / "model.3mf").read_bytes()


def _declare(paths: DataPaths, model: str, settings: dict[str, Any]) -> None:
    meta = json.loads(paths.model_meta(model).read_text(encoding="utf-8"))
    meta["print_settings"] = settings
    paths.model_meta(model).write_text(json.dumps(meta), encoding="utf-8")


@respx.mock
def test_a_download_is_the_saved_file_and_asks_bambuddy_nothing(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """With a default printer in Settings, the download used to be re-plated and named
    on presets read from Bambuddy (#769). Now it is the saved file, whatever Settings
    say and whether or not Bambuddy answers."""
    configure(client, printer_id=1)
    output_id = make_output(client, model)
    bambuddy = respx.route(host=httpx.URL(BASE).host).mock(return_value=httpx.Response(500))

    response = client.get(f"/api/v1/outputs/{output_id}/model.3mf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "model/3mf"
    assert 'filename="demo-elan.3mf"' in response.headers["content-disposition"]
    assert response.content == _stored(paths, model, output_id)
    assert not bambuddy.called


def test_the_saved_file_is_served_in_ranges(client: TestClient, model: str) -> None:
    """A FileResponse, so a download can resume."""
    configure(client, printer_id=1)
    output_id = make_output(client, model)

    response = client.get(f"/api/v1/outputs/{output_id}/model.3mf", headers={"Range": "bytes=0-3"})

    assert response.status_code == 206
    assert response.content == b"PK\x03\x04"


def test_the_templates_print_settings_are_saved_into_the_file(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """#770, written when the output is saved: over the derived process, and listed as
    edits to it, so Bambu Studio shows them as changes to the system preset."""
    configure(client)
    _declare(paths, model, KEYCHAIN)
    output_id = make_output(client, model)

    stored = _stored(paths, model, output_id)
    settings = _project_settings(stored)
    assert settings["enable_prime_tower"] == "1"
    assert settings["wipe_tower_no_sparse_layers"] == "1"
    assert settings["enable_support"] == "0"
    # The process first, then one per filament, then the printer: as Bambu Studio saves it.
    assert settings["different_settings_to_system"] == [
        "enable_prime_tower;wipe_tower_no_sparse_layers;enable_support",
        "",
        "",
    ]
    assert client.get(f"/api/v1/outputs/{output_id}/model.3mf").content == stored


def test_print_settings_declared_after_the_save_leave_its_file_alone(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    configure(client)
    output_id = make_output(client, model)
    stored = _stored(paths, model, output_id)
    _declare(paths, model, KEYCHAIN)

    assert client.get(f"/api/v1/outputs/{output_id}/model.3mf").content == stored
    assert "different_settings_to_system" not in _project_settings(stored)


def test_a_template_s_invalid_model_json_fails_the_save_naming_the_key(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """#2868: the print settings are read when the output is saved, so a model.json made
    invalid since the render fails the save, as a 409 naming it, never a 500. It leaves
    nothing held behind: the same save succeeds once model.json is fixed."""
    configure(client)
    job_id = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}}).json()[
        "job_id"
    ]
    wait_for_job(client, job_id)
    _declare(paths, model, {"enable_support": 1})

    saved = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": job_id, "name": "Elan"}, headers=press()
    )

    assert saved.status_code == 409, saved.text
    assert saved.json()["title"] == "Invalid Model Metadata"
    assert "model.json" in saved.json()["detail"]
    assert "print_settings" in saved.json()["detail"]

    _declare(paths, model, KEYCHAIN)
    retried = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": job_id, "name": "Elan"}, headers=press()
    )
    assert retried.status_code == 201, retried.text
    assert (
        _project_settings(_stored(paths, model, retried.json()["id"]))["enable_prime_tower"] == "1"
    )
