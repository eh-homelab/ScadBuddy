"""The one API test that drives a real openscad end to end: upload, render, persist."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.config import load_config
from scadbuddy.core.paths import DataPaths
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


def test_paste_check_and_replace_against_a_real_openscad(client: TestClient) -> None:
    """The paste routes' parse check is OpenSCAD's own, so it is proved against the
    real binary here rather than only against the stub the other API tests use."""
    checked = client.post("/api/v1/models/check", json={"source": TWO_COLOUR})
    assert checked.status_code == 200
    assert checked.json()["ok"] is True
    assert checked.json()["checked"] is True

    broken = client.post("/api/v1/models/check", json={"source": "cube(;\n"})
    assert broken.json()["ok"] is False
    assert broken.json()["diagnostics"], broken.json()

    created = client.post("/api/v1/models", json={"name": "Pasted", "source": TWO_COLOUR})
    assert created.status_code == 201, created.text
    schema = client.get("/api/v1/models/pasted/schema").json()
    assert [parameter["name"] for parameter in schema["parameters"]] == ["size"]

    refused = client.put("/api/v1/models/pasted/source", json={"source": "cube(;\n"})
    assert refused.status_code == 422
    replaced = client.put(
        "/api/v1/models/pasted/source",
        json={"source": "width = 3; // [1:1:9]\ncube(width);\n"},
    )
    assert replaced.status_code == 200, replaced.text
    schema = client.get("/api/v1/models/pasted/schema").json()
    assert [parameter["name"] for parameter in schema["parameters"]] == ["width"]


def test_the_check_resolves_a_sibling_include_through_the_models_slug(
    client: TestClient, data_dir: Path
) -> None:
    """`POST /models/check` with a `slug` checks in that model's directory (#128), so an
    include of a file beside the model resolves; without one, OpenSCAD cannot see it."""
    source = "include <helper.scad>\nwidth = 3; // [1:1:9]\ncube([width, 2, helper_depth]);\n"
    created = client.post(
        "/api/v1/models", json={"name": "Widget", "source": source, "force": True}
    )
    assert created.status_code == 201, created.text
    DataPaths(data_dir).model_dir("widget").joinpath("helper.scad").write_text(
        "helper_depth = 4;\n", encoding="utf-8"
    )

    with_slug = client.post("/api/v1/models/check", json={"source": source, "slug": "widget"})
    without = client.post("/api/v1/models/check", json={"source": source})

    assert with_slug.json()["diagnostics"] == [], with_slug.json()
    assert any("include" in d["message"] for d in without.json()["diagnostics"]), without.json()


# #204: an uploaded SVG reaches import() -- in the main render AND in every per-colour
# wrapper render (§6.3), which is where a missing copy would silently lose it.
OVERLAY = """\
// Picture to inlay
overlay = ""; // file:svg
color("#FF0000") cube([30, 30, 2]);
if (overlay != "")
  color("#0000FF") translate([5, 5, 2]) linear_extrude(1) resize([20, 20]) import(overlay);
"""

TRIANGLE_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10">'
    b'<path d="M0 0 L10 0 L10 10 Z"/><script>alert(1)</script></svg>'
)


def test_an_uploaded_svg_is_rendered_into_every_part(client: TestClient, data_dir: Path) -> None:
    created = client.post("/api/v1/models", json={"name": "Overlay", "source": OVERLAY})
    assert created.status_code == 201, created.text
    schema = client.get("/api/v1/models/overlay/schema").json()
    assert schema["parameters"][0]["type"] == "file"
    assert schema["parameters"][0]["accept"] == ["svg"]

    asset = client.post(
        "/api/v1/models/overlay/assets",
        files={"file": ("triangle.svg", TRIANGLE_SVG, "image/svg+xml")},
    ).json()
    accepted = client.post(
        "/api/v1/models/overlay/render", json={"params": {"overlay": asset["id"]}}
    )
    assert accepted.status_code == 202, accepted.text
    job = wait_for_job(client, accepted.json()["job_id"])

    assert job["status"] == "done", job["error"]
    assert job["colors"] == ["#FF0000", "#0000FF"]
    # No "the solid render was empty" fallback: the wrapper render found the file too.
    assert job["warnings"] == []
    assert [part["watertight"] for part in job["parts"]] == [True, True]
    assert job["bbox_mm"]["size"] == [30.0, 30.0, 3.0]
    # Nothing staged is left beside the model.
    model_dir = DataPaths(data_dir).model_dir("overlay")
    assert sorted(p.name for p in model_dir.iterdir() if not p.name.startswith(".")) == [
        "model.json",
        "model.scad",
    ]


def test_a_file_openscad_cannot_open_is_a_warning_not_silence(client: TestClient) -> None:
    source = OVERLAY.replace('overlay = "";', 'overlay = "missing.svg";')
    created = client.post("/api/v1/models", json={"name": "Missing", "source": source})
    assert created.status_code == 201, created.text

    accepted = client.post("/api/v1/models/missing/render", json={"params": {}})
    job = wait_for_job(client, accepted.json()["job_id"])

    assert job["status"] == "done", job["error"]
    assert "OpenSCAD could not open missing.svg; the model rendered without it" in job["warnings"]


def test_a_sample_the_template_ships_is_rendered_into_every_part(
    client: TestClient, data_dir: Path
) -> None:
    created = client.post("/api/v1/models", json={"name": "Sampled", "source": OVERLAY})
    assert created.status_code == 201, created.text
    model_dir = DataPaths(data_dir).model_dir("sampled")
    (model_dir / "sample-triangle.svg").write_bytes(TRIANGLE_SVG)
    schema = client.get("/api/v1/models/sampled/schema").json()
    assert schema["parameters"][0]["samples"] == ["sample-triangle.svg"]

    accepted = client.post(
        "/api/v1/models/sampled/render", json={"params": {"overlay": "sample-triangle.svg"}}
    )
    assert accepted.status_code == 202, accepted.text
    job = wait_for_job(client, accepted.json()["job_id"])

    assert job["status"] == "done", job["error"]
    assert job["colors"] == ["#FF0000", "#0000FF"]
    assert job["warnings"] == []
    assert [part["watertight"] for part in job["parts"]] == [True, True]
    assert job["bbox_mm"]["size"] == [30.0, 30.0, 3.0]
    # Read in place: nothing is staged for a sample, and the sample itself stays.
    assert sorted(p.name for p in model_dir.iterdir() if not p.name.startswith(".")) == [
        "model.json",
        "model.scad",
        "sample-triangle.svg",
    ]
