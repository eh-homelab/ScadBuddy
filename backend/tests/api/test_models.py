from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from tests.api.conftest import PNG_BYTES

SOURCE = "width = 10;\ncube(width);\n"


def _upload(
    client: TestClient,
    filename: str = "Name Keychain.scad",
    data: dict[str, str] | None = None,
) -> httpx.Response:
    response: httpx.Response = client.post(
        "/api/v1/models",
        files={"file": (filename, SOURCE.encode(), "application/octet-stream")},
        data=data or {},
    )
    return response


def test_list_is_empty_before_anything_is_uploaded(client: TestClient) -> None:
    assert client.get("/api/v1/models").json() == []


def test_upload_kebabs_the_filename_into_a_slug(client: TestClient) -> None:
    response = _upload(client)
    assert response.status_code == 201
    body = response.json()
    assert body["slug"] == "name-keychain"
    assert body["name"] == "name-keychain"
    assert body["has_thumbnail"] is False
    assert body["has_readme"] is False


def test_upload_accepts_metadata_a_thumbnail_and_a_readme(client: TestClient) -> None:
    response = client.post(
        "/api/v1/models",
        files={
            "file": ("widget.scad", SOURCE.encode(), "application/octet-stream"),
            "thumbnail": ("thumb.png", PNG_BYTES, "image/png"),
            "readme": ("README.md", b"# Widget\n", "text/markdown"),
        },
        data={"name": "Widget", "description": "a widget", "tags": '["a", "b"]'},
    )
    assert response.status_code == 201
    body = response.json()
    assert (body["name"], body["description"], body["tags"]) == ("Widget", "a widget", ["a", "b"])
    assert body["has_thumbnail"] is True
    assert body["has_readme"] is True
    assert client.get("/api/v1/models/widget/thumbnail").content == PNG_BYTES


def test_tags_may_also_arrive_comma_separated(client: TestClient) -> None:
    response = _upload(client, data={"tags": "one, two"})
    assert response.json()["tags"] == ["one", "two"]


def test_a_second_upload_of_the_same_slug_conflicts(client: TestClient) -> None:
    assert _upload(client).status_code == 201
    response = _upload(client)
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"


def test_a_binary_upload_is_rejected_before_openscad_sees_it(client: TestClient) -> None:
    response = client.post(
        "/api/v1/models",
        files={"file": ("blob.scad", b"\x00\x01\x02binary", "application/octet-stream")},
    )
    assert response.status_code == 422
    assert "binary" in response.json()["detail"]


def test_source_openscad_cannot_parse_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/models",
        files={"file": ("broken.scad", b"%%FAIL%% not scad\n", "application/octet-stream")},
    )
    assert response.status_code == 422
    body = response.json()
    assert "could not parse" in body["detail"]
    assert body["log_tail"] == ["ERROR: Parser error: syntax error"]


def test_a_thumbnail_that_is_not_a_png_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/models",
        files={
            "file": ("ok.scad", SOURCE.encode(), "application/octet-stream"),
            "thumbnail": ("thumb.png", b"GIF89a", "image/png"),
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "the thumbnail is not a PNG"


def test_get_patch_and_delete_a_model(client: TestClient, model: str) -> None:
    assert client.get(f"/api/v1/models/{model}").json()["name"] == "Demo"

    patched = client.patch(f"/api/v1/models/{model}", json={"name": "Renamed", "tags": ["x"]})
    assert patched.status_code == 200
    assert patched.json()["name"] == "Renamed"
    assert patched.json()["description"] == "a demo"

    assert client.delete(f"/api/v1/models/{model}").status_code == 204
    assert client.get(f"/api/v1/models/{model}").status_code == 404


def test_patching_preserves_the_cached_schema(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    assert client.get(f"/api/v1/models/{model}/schema").status_code == 200
    client.patch(f"/api/v1/models/{model}", json={"description": "edited"})

    meta = json.loads(paths.model_meta(model).read_text(encoding="utf-8"))
    assert meta["description"] == "edited"
    assert meta["schema"]["parameters"][0]["name"] == "width"


def test_deleting_a_model_takes_its_outputs_with_it(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    orphan = paths.outputs / model / "deadbeef"
    orphan.mkdir(parents=True)
    client.delete(f"/api/v1/models/{model}")
    assert not orphan.exists()


def test_unknown_model_routes_answer_with_problem_details(client: TestClient) -> None:
    for path in ("", "/source", "/schema", "/thumbnail"):
        response = client.get(f"/api/v1/models/missing{path}")
        assert response.status_code == 404, path
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["instance"] == f"/api/v1/models/missing{path}"


def test_a_slug_cannot_escape_the_data_directory(client: TestClient) -> None:
    assert client.get("/api/v1/models/..%2F..%2Fetc/source").status_code == 404
    assert client.get("/api/v1/models/Not_A_Slug").status_code == 422


def test_source_is_served_verbatim(client: TestClient, model: str, paths: DataPaths) -> None:
    response = client.get(f"/api/v1/models/{model}/source")
    assert response.status_code == 200
    assert response.text == paths.model_source(model).read_text(encoding="utf-8")
    assert response.headers["content-type"].startswith("text/plain")


def test_schema_is_cached_by_source_sha(client: TestClient, model: str, paths: DataPaths) -> None:
    first = client.get(f"/api/v1/models/{model}/schema").json()
    assert [p["name"] for p in first["parameters"]] == ["width", "label"]
    assert first["source_sha256"]

    # Rewriting the source must invalidate the cache rather than serve the old schema.
    paths.model_source(model).write_text("width = 1;\n// changed\n", encoding="utf-8")
    second = client.get(f"/api/v1/models/{model}/schema").json()
    assert second["source_sha256"] != first["source_sha256"]


def test_a_model_without_a_thumbnail_answers_404(client: TestClient, model: str) -> None:
    assert client.get(f"/api/v1/models/{model}/thumbnail").status_code == 404
