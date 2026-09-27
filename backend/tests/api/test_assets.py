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


# ── samples: files the template ships beside its source ───────────────────────

TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00"
    b"\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def samples(paths: DataPaths, file_model: str) -> Path:
    directory = paths.model_dir(file_model)
    (directory / "sample-cat.svg").write_bytes(HEART_SVG)
    (directory / "sample-leaf.png").write_bytes(TINY_PNG)
    (directory / "thumbnail.png").write_bytes(TINY_PNG)
    (directory / ".secret.svg").write_bytes(HEART_SVG)
    (directory / "notes.txt").write_text("not a picture")
    return directory


def test_the_schema_lists_a_file_parameters_samples(client: TestClient, samples: Path) -> None:
    schema = client.get(f"/api/v1/models/{MODEL_SLUG}/schema").json()
    label = next(p for p in schema["parameters"] if p["name"] == "label")
    assert label["samples"] == ["sample-cat.svg", "sample-leaf.png"]
    width = next(p for p in schema["parameters"] if p["name"] == "width")
    assert width["samples"] == []


def test_a_sample_added_later_is_listed_without_a_source_change(
    client: TestClient, samples: Path
) -> None:
    client.get(f"/api/v1/models/{MODEL_SLUG}/schema")  # warms the schema cache
    (samples / "sample-rings.svg").write_bytes(HEART_SVG)
    schema = client.get(f"/api/v1/models/{MODEL_SLUG}/schema").json()
    label = next(p for p in schema["parameters"] if p["name"] == "label")
    assert "sample-rings.svg" in label["samples"]


def test_a_sample_is_served_as_an_inert_image(client: TestClient, samples: Path) -> None:
    svg = client.get(f"/api/v1/models/{MODEL_SLUG}/samples/sample-cat.svg")
    assert svg.status_code == 200
    assert svg.headers["content-type"] == "image/svg+xml"
    assert "sandbox" in svg.headers["content-security-policy"]
    assert svg.headers["x-content-type-options"] == "nosniff"
    assert svg.headers["cache-control"] == "no-cache"
    assert svg.content == HEART_SVG

    png = client.get(f"/api/v1/models/{MODEL_SLUG}/samples/sample-leaf.png")
    assert png.status_code == 200
    assert png.headers["content-type"] == "image/png"


@pytest.mark.parametrize(
    "name",
    [
        "model.scad",
        "model.json",
        "thumbnail.png",
        ".secret.svg",
        "notes.txt",
        "gone.svg",
        "..%2Fsecret.svg",
        "%2E%2E%2F%2E%2E%2Fetc%2Fpasswd",
    ],
)
def test_nothing_but_a_listed_sample_is_served(
    client: TestClient, samples: Path, name: str
) -> None:
    response = client.get(f"/api/v1/models/{MODEL_SLUG}/samples/{name}")
    assert response.status_code in (404, 422), response.text


def test_a_symlinked_sample_is_not_served(
    client: TestClient, samples: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.svg"
    outside.write_bytes(HEART_SVG)
    (samples / "linked.svg").symlink_to(outside)
    assert client.get(f"/api/v1/models/{MODEL_SLUG}/samples/linked.svg").status_code == 404
    schema = client.get(f"/api/v1/models/{MODEL_SLUG}/schema").json()
    label = next(p for p in schema["parameters"] if p["name"] == "label")
    assert "linked.svg" not in label["samples"]


def test_a_sample_of_an_unknown_model_or_revision_is_a_404(
    client: TestClient, samples: Path
) -> None:
    assert client.get("/api/v1/models/nope/samples/sample-cat.svg").status_code == 404
    response = client.get(
        f"/api/v1/models/{MODEL_SLUG}/samples/sample-cat.svg", params={"version": "0" * 40}
    )
    assert response.status_code in (404, 503)


def test_a_render_takes_a_sample_by_its_bare_name(client: TestClient, samples: Path) -> None:
    accepted = client.post(
        f"/api/v1/models/{MODEL_SLUG}/render", json={"params": {"label": "sample-cat.svg"}}
    )
    assert accepted.status_code == 202, accepted.text
    job = wait_for_job(client, accepted.json()["job_id"])
    assert job["status"] == "done"
    assert job["params"] == {"label": "sample-cat.svg"}


@pytest.mark.parametrize(
    "value", ["thumbnail.png", ".secret.svg", "notes.txt", "model.scad", "sample-gone.svg"]
)
def test_a_render_refuses_a_file_that_is_not_a_listed_sample(
    client: TestClient, samples: Path, value: str
) -> None:
    response = client.post(f"/api/v1/models/{MODEL_SLUG}/render", json={"params": {"label": value}})
    assert response.status_code == 422, response.text
    assert "label" in response.json()["detail"]
