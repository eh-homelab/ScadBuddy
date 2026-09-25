from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from scadbuddy.render.jobs import Job, JobStore
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


def test_the_derived_schema_is_cached_outside_the_versioned_tree(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Deriving a schema must not dirty `models/`.

    It is written lazily, by a read, outside any commit -- so if it lived in
    `model.json` the repository would sit permanently dirty and the next
    metadata commit would carry a cache blob it has nothing to do with.
    """
    assert client.get(f"/api/v1/models/{model}/schema").status_code == 200
    client.patch(f"/api/v1/models/{model}", json={"description": "edited"})

    meta = json.loads(paths.model_meta(model).read_text(encoding="utf-8"))
    assert meta["description"] == "edited"
    assert "schema" not in meta
    cached = json.loads(paths.model_schema_cache(model).read_text(encoding="utf-8"))
    assert cached["schema"]["parameters"][0]["name"] == "width"


def test_deleting_a_model_takes_its_outputs_with_it(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    orphan = paths.outputs / model / "deadbeef"
    orphan.mkdir(parents=True)
    client.delete(f"/api/v1/models/{model}")
    assert not orphan.exists()


def test_a_deleted_model_leaves_the_list(client: TestClient) -> None:
    slug = _upload(client).json()["slug"]
    assert [entry["slug"] for entry in client.get("/api/v1/models").json()] == [slug]

    response = client.delete(f"/api/v1/models/{slug}")

    assert response.status_code == 204
    assert response.content == b""
    assert client.get("/api/v1/models").json() == []


def test_deleting_an_unknown_model_is_a_problem_404(client: TestClient) -> None:
    response = client.delete("/api/v1/models/missing")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["detail"] == "no model named 'missing'"


def test_deleting_a_model_clears_its_derived_cache(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    assert client.get(f"/api/v1/models/{model}/schema").status_code == 200
    assert paths.model_schema_cache(model).is_file()
    export = paths.model_revision_dir(model, "0" * 40)
    export.mkdir(parents=True)

    assert client.delete(f"/api/v1/models/{model}").status_code == 204

    assert not paths.model_dir(model).exists()
    assert not paths.model_schema_cache(model).exists()
    assert not (paths.model_revisions / model).exists()
    # The tombstone the directory was renamed to is gone too.
    assert list(paths.tombstones.iterdir()) == []


def test_a_stale_tombstone_is_swept_at_startup(app: FastAPI, paths: DataPaths) -> None:
    stale = paths.tombstones / "old-model.0123abcd"
    (stale / "nested").mkdir(parents=True)
    (stale / "nested" / "model.scad").write_text("cube(1);\n", encoding="utf-8")

    with TestClient(app):
        assert list(paths.tombstones.iterdir()) == []


def test_a_failed_tombstone_removal_is_logged_and_retried(
    client: TestClient, model: str, paths: DataPaths, caplog: pytest.LogCaptureFixture
) -> None:
    catalogue = client.app.state.scadbuddy.catalogue  # type: ignore[attr-defined]
    with patch("scadbuddy.library.catalogue.shutil.rmtree", side_effect=OSError("busy")):
        assert client.delete(f"/api/v1/models/{model}").status_code == 204
    assert "could not remove a deleted model's files" in caplog.text
    assert [entry.name.split(".")[0] for entry in paths.tombstones.iterdir()] == [model]

    assert catalogue.sweep_tombstones() != []
    assert list(paths.tombstones.iterdir()) == []


def test_concurrent_sweeps_log_nothing_and_leave_nothing(
    client: TestClient, paths: DataPaths, caplog: pytest.LogCaptureFixture
) -> None:
    catalogue = client.app.state.scadbuddy.catalogue  # type: ignore[attr-defined]
    for index in range(20):
        tree = paths.tombstones / f"model-{index}.{index:032x}"
        for depth in range(5):
            (tree / f"d{depth}").mkdir(parents=True)
            for leaf in range(10):
                (tree / f"d{depth}" / f"f{leaf}").write_text("x", encoding="utf-8")

    barrier = threading.Barrier(4)

    def sweep() -> None:
        barrier.wait()
        catalogue.sweep_tombstones()

    with ThreadPoolExecutor(max_workers=4) as pool:
        for future in [pool.submit(sweep) for _ in range(4)]:
            future.result()

    assert list(paths.tombstones.iterdir()) == []
    assert "could not remove" not in caplog.text


def test_a_delete_is_a_revision_of_the_shared_history(client: TestClient) -> None:
    slug = _upload(client).json()["slug"]
    assert client.delete(f"/api/v1/models/{slug}").status_code == 204

    state = client.app.state.scadbuddy  # type: ignore[attr-defined]
    revisions = state.history.log(slug)
    assert [revision.message for revision in revisions] == [f"Delete {slug}", f"Add {slug}"]
    # The model's revisions stay in the history, and the delete touched only it.
    assert {change.path.split("/")[0] for change in revisions[0].files} == {slug}
    assert {change.status for change in revisions[0].files} == {"D"}


def test_a_model_with_a_render_in_progress_cannot_be_deleted(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    # The test queue starts lazily, and starting fails every unfinished job left
    # by a "previous run" -- so start it before planting the running one.
    assert client.delete("/api/v1/models/missing").status_code == 404
    store = JobStore(paths)
    job = Job(id="a" * 32, slug=model, state="running", created_at=datetime.now(UTC))
    store.write(job)

    response = client.delete(f"/api/v1/models/{model}")

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    assert paths.model_source(model).is_file()

    job.state = "done"
    store.write(job)
    assert client.delete(f"/api/v1/models/{model}").status_code == 204


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
