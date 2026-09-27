"""`POST /models/{slug}/assets` and what a render does with a `file` value (#204)."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import MAX_ASSET_BYTES
from scadbuddy.render.provenance import read as read_provenance
from tests.api.conftest import MODEL_SLUG, wait_for_job

# The fake openscad exports `width` and `label` (initial "hi"); the annotation is
# ScadBuddy's own overlay, so it turns `label` into a file parameter.
FILE_SOURCE = 'width = 10;\nlabel = "hi"; // file:svg,png\n'

HEART_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
    b'<path d="M10 18 L2 8 L18 8 Z" onclick="steal()"/><script>alert(1)</script></svg>'
)


@pytest.fixture
def file_model(paths: DataPaths, model: str) -> str:
    paths.model_source(model).write_text(FILE_SOURCE, encoding="utf-8")
    return model


@pytest.fixture
def client(app: FastAPI, file_model: str) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _upload(
    client: TestClient, data: bytes, name: str = "heart.svg", slug: str = MODEL_SLUG
) -> dict[str, object]:
    response = client.post(
        f"/api/v1/models/{slug}/assets",
        files={"file": (name, data, "application/octet-stream")},
    )
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


def test_the_schema_types_the_annotated_parameter_as_file(client: TestClient) -> None:
    schema = client.get(f"/api/v1/models/{MODEL_SLUG}/schema").json()
    label = next(p for p in schema["parameters"] if p["name"] == "label")
    assert label["type"] == "file"
    assert label["accept"] == ["svg", "png"]
    width = next(p for p in schema["parameters"] if p["name"] == "width")
    assert width["accept"] == []


def test_an_svg_upload_answers_its_id_and_serves_back_sanitised(client: TestClient) -> None:
    asset = _upload(client, HEART_SVG)

    assert asset["kind"] == "svg"
    assert asset["name"] == "heart.svg"
    assert isinstance(asset["id"], str) and len(asset["id"]) == 64
    assert client.get(f"/api/v1/models/{MODEL_SLUG}/assets/{asset['id']}").json() == asset

    content = client.get(f"/api/v1/models/{MODEL_SLUG}/assets/{asset['id']}/content")
    assert content.status_code == 200
    assert content.headers["content-type"] == "image/svg+xml"
    assert "sandbox" in content.headers["content-security-policy"]
    assert content.headers["x-content-type-options"] == "nosniff"
    assert b"<path" in content.content
    assert b"script" not in content.content and b"onclick" not in content.content


def test_a_png_upload_is_downscaled(client: TestClient) -> None:
    out = io.BytesIO()
    Image.new("L", (1000, 400), 128).save(out, format="PNG")
    asset = _upload(client, out.getvalue(), "photo.png")

    assert (asset["kind"], asset["width"], asset["height"]) == ("png", 256, 102)
    content = client.get(f"/api/v1/models/{MODEL_SLUG}/assets/{asset['id']}/content")
    assert content.headers["content-type"] == "image/png"


def test_anything_but_svg_or_png_is_refused(client: TestClient) -> None:
    response = client.post(
        f"/api/v1/models/{MODEL_SLUG}/assets",
        files={"file": ("sneaky.svg", b"GIF89a....", "image/svg+xml")},
    )
    assert response.status_code == 422
    assert "only SVG and PNG" in response.json()["detail"]


def test_an_oversized_file_is_refused(client: TestClient) -> None:
    response = client.post(
        f"/api/v1/models/{MODEL_SLUG}/assets",
        files={"file": ("big.svg", b"<svg" + b" " * MAX_ASSET_BYTES, "image/svg+xml")},
    )
    assert response.status_code == 413


def test_uploads_need_a_model_and_ids_need_the_right_shape(client: TestClient) -> None:
    missing = client.post(
        "/api/v1/models/nope/assets", files={"file": ("a.svg", HEART_SVG, "image/svg+xml")}
    )
    assert missing.status_code == 404
    assert client.get(f"/api/v1/models/{MODEL_SLUG}/assets/{'0' * 64}").status_code == 404
    assert client.get(f"/api/v1/models/{MODEL_SLUG}/assets/..%2Fx").status_code in (404, 422)


def test_a_render_takes_an_uploaded_id(client: TestClient, paths: DataPaths) -> None:
    asset = _upload(client, HEART_SVG)
    accepted = client.post(
        f"/api/v1/models/{MODEL_SLUG}/render", json={"params": {"label": asset["id"]}}
    )
    assert accepted.status_code == 202, accepted.text
    job = wait_for_job(client, accepted.json()["job_id"])
    assert job["params"] == {"label": asset["id"]}

    output = client.post(
        f"/api/v1/models/{MODEL_SLUG}/outputs", json={"job_id": job["id"], "name": "With heart"}
    ).json()
    # The value IS the asset's hash, so the output's record and its 3MF name the bytes.
    directory = paths.output_dir(MODEL_SLUG, output["id"])
    assert json.loads((directory / "params.json").read_text()) == {"label": asset["id"]}
    provenance = read_provenance(directory / "model.3mf")
    assert provenance is not None and provenance.params == {"label": asset["id"]}
    # And the asset outlives the job, for a re-render or "Customize this version".
    assert client.get(f"/api/v1/models/{MODEL_SLUG}/assets/{asset['id']}").status_code == 200


@pytest.mark.parametrize(
    "value",
    ["../../../etc/passwd", "/etc/passwd", "other.svg", "f" * 64, 12],
)
def test_a_render_refuses_a_file_value_that_is_not_an_upload(
    client: TestClient, value: object
) -> None:
    response = client.post(f"/api/v1/models/{MODEL_SLUG}/render", json={"params": {"label": value}})
    assert response.status_code == 422, response.text
    assert "label" in response.json()["detail"]


def test_a_render_takes_the_empty_value_and_the_models_default(client: TestClient) -> None:
    for value in ("", "hi"):
        response = client.post(
            f"/api/v1/models/{MODEL_SLUG}/render", json={"params": {"label": value}}
        )
        assert response.status_code == 202, response.text


def test_a_kind_the_parameter_does_not_accept_is_refused(
    client: TestClient, paths: DataPaths
) -> None:
    paths.model_source(MODEL_SLUG).write_text(
        'width = 10;\nlabel = "hi"; // file:png\n', encoding="utf-8"
    )
    asset = _upload(client, HEART_SVG)
    response = client.post(
        f"/api/v1/models/{MODEL_SLUG}/render", json={"params": {"label": asset["id"]}}
    )
    assert response.status_code == 422
    assert "accepts png, not svg" in response.json()["detail"]


@pytest.fixture
def builtin_client(app: FastAPI, seed_dir: Path) -> Iterator[TestClient]:
    directory = seed_dir / "overlay"
    directory.mkdir()
    (directory / "model.scad").write_text(FILE_SOURCE, encoding="utf-8")
    (directory / "model.json").write_text(json.dumps({"name": "Overlay"}), encoding="utf-8")
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.requires_git
def test_a_built_in_template_takes_an_upload_and_renders_with_it(
    builtin_client: TestClient,
) -> None:
    asset = _upload(builtin_client, HEART_SVG, slug="builtin:overlay")
    fetched = builtin_client.get(f"/api/v1/models/builtin:overlay/assets/{asset['id']}")
    assert fetched.status_code == 200
    accepted = builtin_client.post(
        "/api/v1/models/builtin:overlay/render", json={"params": {"label": asset["id"]}}
    )
    assert accepted.status_code == 202, accepted.text
    assert wait_for_job(builtin_client, accepted.json()["job_id"])["status"] == "done"
