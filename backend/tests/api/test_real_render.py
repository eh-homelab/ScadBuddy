"""The one API test that drives a real openscad end to end: upload, render, persist."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.config import load_config
from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from tests.api.conftest import wait_for_job

pytestmark = pytest.mark.requires_openscad

TWO_COLOUR = """\
size = 10; // [5:1:20]
color("#FF0000") cube(size);
color("#0000FF") translate([size, 0, 0]) cube(size);
"""


@pytest.fixture
def client(data_dir: Path, seed_dir: Path) -> Iterator[TestClient]:
    settings = Settings(
        openscad=load_config().openscad,
        data_dir=data_dir,
        seed_models_dir=seed_dir,
        frontend_dir=Path("/nonexistent"),
        render_concurrency=1,
        render_timeout=120.0,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_upload_render_and_persist_against_a_real_openscad(client: TestClient) -> None:
    created = client.post(
        "/api/v1/models",
        files={"file": ("Two Colour.scad", TWO_COLOUR.encode(), "application/octet-stream")},
    )
    assert created.status_code == 201, created.text
    slug = created.json()["slug"]
    assert slug == "two-colour"

    schema = client.get(f"/api/v1/models/{slug}/schema").json()
    assert [parameter["name"] for parameter in schema["parameters"]] == ["size"]
    assert schema["parameters"][0]["type"] == "slider"

    accepted = client.post(f"/api/v1/models/{slug}/render", json={"params": {"size": 6}})
    assert accepted.status_code == 202
    job = wait_for_job(client, accepted.json()["job_id"])
    assert job["status"] == "done", job["error"]
    assert job["colors"] == ["#FF0000", "#0000FF"]
    assert job["warnings"] == []
    assert job["bbox_mm"]["size"] == [12.0, 6.0, 6.0]

    preview = client.get(f"/api/v1/jobs/{job['id']}/preview.glb")
    assert preview.headers["content-type"] == "model/gltf-binary"
    assert preview.content.startswith(b"glTF")

    output = client.post(
        f"/api/v1/models/{slug}/outputs", json={"job_id": job["id"], "name": "Six"}
    )
    assert output.status_code == 201
    download = client.get(f"/api/v1/outputs/{output.json()['id']}/model.3mf")
    assert 'filename="two-colour-six.3mf"' in download.headers["content-disposition"]

    archive = zipfile.ZipFile(io.BytesIO(download.content))
    assert "3D/3dmodel.model" in archive.namelist()
    assert "Metadata/model_settings.config" in archive.namelist()


def test_an_unparsable_upload_is_rejected_by_the_real_binary(client: TestClient) -> None:
    response = client.post(
        "/api/v1/models",
        files={"file": ("broken.scad", b"cube(\n", "application/octet-stream")},
    )
    assert response.status_code == 422
    assert "could not parse" in response.json()["detail"]
