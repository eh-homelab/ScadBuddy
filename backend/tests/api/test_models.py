from __future__ import annotations

import json
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.models import MAX_SOURCE_CHARS
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.catalogue import Catalogue
from scadbuddy.render.jobs import Job, JobStore
from scadbuddy.render.runner import ProcessOutput, RenderTimeoutError
from scadbuddy.render.schema import source_sha256
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


def test_a_failed_startup_sweep_does_not_stop_the_boot(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        patch.object(Catalogue, "sweep_tombstones", side_effect=OSError("EIO")),
        TestClient(app) as client,
    ):
        assert client.get("/healthz").status_code == 200
    assert "could not sweep tombstones" in caplog.text


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


def test_losing_a_delete_race_is_a_404_not_a_500(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    # Both requests pass the existence checks; the other one renames first.
    shutil.rmtree(paths.model_dir(model))
    with patch.object(Catalogue, "exists", return_value=True):
        response = client.delete(f"/api/v1/models/{model}")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["detail"] == f"no model named {model!r}"


def test_a_metadata_edit_that_loses_a_delete_race_is_a_404_and_resurrects_nothing(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    # The PATCH passes its existence checks; a DELETE renames the model away first.
    shutil.rmtree(paths.model_dir(model))
    with patch.object(Catalogue, "exists", return_value=True):
        response = client.patch(f"/api/v1/models/{model}", json={"name": "Renamed"})

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["detail"] == f"no model named {model!r}"
    assert not paths.model_dir(model).exists()


def test_a_metadata_write_racing_a_delete_does_not_recreate_the_model_dir(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    # The delete lands after the PATCH has read the metadata, before it writes it.
    real_read = Catalogue.read_raw_meta

    def read_then_lose_the_race(self: Catalogue, slug: str) -> dict[str, object]:
        meta = real_read(self, slug)
        if slug == model and paths.model_dir(slug).exists():
            self.delete(slug)
        return meta

    with patch.object(Catalogue, "read_raw_meta", read_then_lose_the_race):
        response = client.patch(f"/api/v1/models/{model}", json={"name": "Renamed"})

    assert response.status_code == 404
    assert response.json()["detail"] == f"no model named {model!r}"
    assert not paths.model_dir(model).exists()
    assert list(paths.tombstones.iterdir()) == []


def test_a_source_edit_that_loses_a_delete_race_is_a_404_and_resurrects_nothing(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    shutil.rmtree(paths.model_dir(model))
    with patch.object(Catalogue, "exists", return_value=True):
        response = client.put(
            f"/api/v1/models/{model}/source", json={"source": "width = 7;\n", "force": True}
        )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["detail"] == f"no model named {model!r}"
    assert not paths.model_dir(model).exists()


def test_a_source_swap_racing_a_delete_is_a_404_and_resurrects_nothing(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    # The delete renames the model away between staging the new source and swapping it in.
    real_replace = os.replace

    def lose_the_race_then_replace(src: str, dst: object) -> None:
        paths.model_dir(model).rename(paths.tombstones / f"{model}.racing")
        real_replace(src, dst)  # type: ignore[arg-type]

    paths.tombstones.mkdir(parents=True, exist_ok=True)
    with patch("scadbuddy.library.catalogue.os.replace", lose_the_race_then_replace):
        response = client.put(
            f"/api/v1/models/{model}/source", json={"source": "width = 7;\n", "force": True}
        )

    assert response.status_code == 404
    assert response.json()["detail"] == f"no model named {model!r}"
    assert not paths.model_dir(model).exists()


def test_a_failed_cleanup_step_does_not_fail_a_completed_delete(
    client: TestClient, model: str, paths: DataPaths, caplog: pytest.LogCaptureFixture
) -> None:
    assert client.get(f"/api/v1/models/{model}/schema").status_code == 200
    schema_cache = paths.model_schema_cache(model)
    export = paths.model_revision_dir(model, "0" * 40)
    export.mkdir(parents=True)
    output = paths.outputs / model / "deadbeef"
    output.mkdir(parents=True)
    real_unlink = Path.unlink

    def failing_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == schema_cache:
            raise PermissionError("read-only")
        real_unlink(self, missing_ok=missing_ok)

    with patch.object(Path, "unlink", failing_unlink):
        response = client.delete(f"/api/v1/models/{model}")

    assert response.status_code == 204
    assert [getattr(record, "path", None) for record in caplog.records if record.exc_info] == [
        str(schema_cache)
    ]
    assert schema_cache.exists()
    assert not paths.model_dir(model).exists()
    assert not (paths.model_revisions / model).exists()
    assert not (paths.outputs / model).exists()
    assert list(paths.tombstones.iterdir()) == []


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


def test_a_json_body_creates_a_model_from_pasted_source(client: TestClient) -> None:
    response = client.post(
        "/api/v1/models",
        json={"name": "Name Keychain", "source": SOURCE, "tags": ["pasted"]},
    )
    assert response.status_code == 201
    body = response.json()
    assert (body["slug"], body["name"]) == ("name-keychain", "Name Keychain")
    assert body["tags"] == ["pasted"]
    assert client.get("/api/v1/models/name-keychain/source").text == SOURCE


def test_a_plain_text_paste_takes_its_name_from_the_header(client: TestClient) -> None:
    response = client.post(
        "/api/v1/models",
        content=SOURCE.encode(),
        headers={"Content-Type": "text/plain; charset=utf-8", "X-Model-Name": "Pasted Thing"},
    )
    assert response.status_code == 201
    assert response.json()["slug"] == "pasted-thing"


def test_a_plain_text_paste_without_a_name_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/models", content=SOURCE.encode(), headers={"Content-Type": "text/plain"}
    )
    assert response.status_code == 422
    assert "X-Model-Name" in response.json()["detail"]


def test_a_name_that_yields_no_slug_is_rejected(client: TestClient) -> None:
    response = client.post("/api/v1/models", json={"name": "***", "source": SOURCE})
    assert response.status_code == 422
    assert "slug" in response.json()["detail"]


def test_pasted_source_openscad_cannot_parse_is_rejected(client: TestClient) -> None:
    response = client.post("/api/v1/models", json={"name": "Broken", "source": "%%FAIL%%\n"})
    assert response.status_code == 422
    body = response.json()
    assert body["log_tail"] == ["ERROR: Parser error: syntax error"]
    assert body["diagnostics"] == [
        {
            "severity": "error",
            "message": "Parser error: syntax error",
            "line": None,
            "file": None,
        }
    ]
    assert client.get("/api/v1/models/broken").status_code == 404


def test_force_saves_source_that_does_not_parse(client: TestClient) -> None:
    response = client.post(
        "/api/v1/models", json={"name": "Broken", "source": "%%FAIL%%\n", "force": True}
    )
    assert response.status_code == 201
    assert client.get("/api/v1/models/broken/source").text == "%%FAIL%%\n"


def test_the_force_query_parameter_forces_a_json_paste(client: TestClient) -> None:
    response = client.post(
        "/api/v1/models?force=true", json={"name": "Broken", "source": "%%FAIL%%\n"}
    )
    assert response.status_code == 201


@pytest.mark.parametrize("force", [False, True])
def test_a_nul_in_pasted_source_is_refused_even_when_forced(
    client: TestClient, model: str, force: bool
) -> None:
    source = "cube(1);\x00\n"
    created = client.post("/api/v1/models", json={"name": "Blob", "source": source, "force": force})
    assert created.status_code == 422
    assert "NUL" in created.json()["detail"]

    replaced = client.put(f"/api/v1/models/{model}/source", json={"source": source, "force": force})
    assert replaced.status_code == 422


def test_pasting_over_an_existing_slug_conflicts(client: TestClient) -> None:
    assert _upload(client).status_code == 201
    response = client.post("/api/v1/models", json={"name": "name keychain", "source": SOURCE})
    assert response.status_code == 409


def test_a_multipart_post_without_a_file_is_rejected(client: TestClient) -> None:
    response = client.post("/api/v1/models", data={"name": "No File"})
    assert response.status_code == 422
    assert "file part" in response.json()["detail"]


def test_replacing_the_source_rederives_the_schema(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    assert client.get(f"/api/v1/models/{model}/schema").status_code == 200
    before = json.loads(paths.model_schema_cache(model).read_text())["schema"]["source_sha256"]

    replacement = 'width = 42;\nlabel = "new";\ncube(width);\n'
    response = client.put(f"/api/v1/models/{model}/source", json={"source": replacement})
    assert response.status_code == 200
    assert response.json()["slug"] == model
    assert client.get(f"/api/v1/models/{model}/source").text == replacement

    after = json.loads(paths.model_schema_cache(model).read_text())["schema"]["source_sha256"]
    assert after == source_sha256(replacement)
    assert after != before


def test_replacing_the_source_keeps_the_metadata(client: TestClient, model: str) -> None:
    client.put(f"/api/v1/models/{model}/source", json={"source": "width = 1;\n"})
    assert client.get(f"/api/v1/models/{model}").json()["name"] == "Demo"


def test_a_replacement_that_does_not_parse_is_refused_unless_forced(
    client: TestClient, model: str
) -> None:
    original = client.get(f"/api/v1/models/{model}/source").text
    refused = client.put(f"/api/v1/models/{model}/source", json={"source": "%%FAIL%%\n"})
    assert refused.status_code == 422
    assert client.get(f"/api/v1/models/{model}/source").text == original

    forced = client.put(
        f"/api/v1/models/{model}/source", json={"source": "%%FAIL%%\n", "force": True}
    )
    assert forced.status_code == 200
    assert client.get(f"/api/v1/models/{model}/source").text == "%%FAIL%%\n"


def test_the_force_query_parameter_forces_a_replacement(client: TestClient, model: str) -> None:
    """The same spelling `POST /models` takes, so a client forces both routes one way."""
    response = client.put(
        f"/api/v1/models/{model}/source?force=true", json={"source": "%%FAIL%%\n"}
    )
    assert response.status_code == 200
    assert client.get(f"/api/v1/models/{model}/source").text == "%%FAIL%%\n"


def test_replacing_the_source_of_a_model_that_is_not_there(client: TestClient) -> None:
    assert client.put("/api/v1/models/nope/source", json={"source": SOURCE}).status_code == 404


def test_the_check_endpoint_reports_diagnostics_without_saving_anything(
    client: TestClient,
) -> None:
    response = client.post("/api/v1/models/check", json={"source": "%%FAIL%%\n"})
    assert response.status_code == 200
    body = response.json()
    assert (body["ok"], body["checked"]) == (False, True)
    assert body["diagnostics"][0]["message"] == "Parser error: syntax error"
    assert client.get("/api/v1/models").json() == []


def test_the_check_endpoint_passes_source_that_parses(client: TestClient) -> None:
    body = client.post("/api/v1/models/check", json={"source": SOURCE}).json()
    assert (body["ok"], body["checked"], body["diagnostics"]) == (True, True, [])
    # The check derives the schema too, so a source that parses but yields no
    # customizer panel is caught here rather than after it is saved.
    assert body["parameters"] == 2


def test_a_plain_text_paste_that_is_not_utf8_is_rejected(client: TestClient) -> None:
    """The multipart branch has always answered 422 here; text/plain must match it."""
    response = client.post(
        "/api/v1/models",
        content=b"\xff\xfe cube(1);",
        headers={"Content-Type": "text/plain", "X-Model-Name": "Bad Bytes"},
    )
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert "UTF-8" in response.json()["detail"]


def test_a_plain_text_paste_with_a_nul_byte_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/models",
        content=b"cube(1);\x00",
        headers={"Content-Type": "text/plain", "X-Model-Name": "Binary"},
    )
    assert response.status_code == 422
    assert "binary" in response.json()["detail"]


def test_a_body_that_is_not_json_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/models", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert "JSON" in response.json()["detail"]


def test_an_empty_json_body_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/models", content=b"", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422


def test_a_json_body_of_the_wrong_shape_is_rejected(client: TestClient) -> None:
    response = client.post("/api/v1/models", json=["not", "an", "object"])
    assert response.status_code == 422


def test_a_json_body_missing_its_source_is_rejected_like_any_other_body(
    client: TestClient,
) -> None:
    response = client.post("/api/v1/models", json={"name": "No Source"})
    assert response.status_code == 422
    body = response.json()
    assert body["errors"][0]["loc"] == ["body", "source"]


def test_replacing_the_source_runs_openscad_once(
    client: TestClient, model: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deriving the schema IS the check, so a save must not pay for two subprocesses."""
    log = tmp_path / "invocations.log"
    monkeypatch.setenv("FAKE_OPENSCAD_LOG", str(log))

    replacement = 'width = 3;\nlabel = "x";\n'
    put = client.put(f"/api/v1/models/{model}/source", json={"source": replacement})
    assert put.status_code == 200
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1

    # And the schema the check derived was kept, so opening the customizer adds none.
    assert client.get(f"/api/v1/models/{model}/schema").status_code == 200
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


def test_a_pasted_model_opens_without_deriving_its_schema_again(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "invocations.log"
    monkeypatch.setenv("FAKE_OPENSCAD_LOG", str(log))

    created = client.post("/api/v1/models", json={"name": "Pasted", "source": SOURCE})
    assert created.status_code == 201
    assert client.get("/api/v1/models/pasted/schema").status_code == 200
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


def test_a_text_content_type_other_than_plain_is_not_a_paste(client: TestClient) -> None:
    """`text/*` at large is a wider contract than the route documents."""
    response = client.post(
        "/api/v1/models",
        content=b"<html>not openscad</html>",
        headers={"Content-Type": "text/html", "X-Model-Name": "Sneaky"},
    )
    assert response.status_code == 415
    assert "text/plain" in response.json()["detail"]
    assert client.get("/api/v1/models").json() == []


def test_a_body_with_no_content_type_at_all_is_refused(client: TestClient) -> None:
    response = client.post("/api/v1/models", content=b"cube(1);")
    assert response.status_code == 415


def test_the_schema_of_a_forced_save_answers_a_problem_not_a_crash(client: TestClient) -> None:
    """`force` is the first way unparseable source can reach the catalogue, and the UI
    goes straight to the customizer after one."""
    forced = client.post(
        "/api/v1/models", json={"name": "Broken", "source": "%%FAIL%%\n", "force": True}
    )
    assert forced.status_code == 201

    response = client.get("/api/v1/models/broken/schema")
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert "schema" in body["detail"]
    assert body["log_tail"] == ["ERROR: Parser error: syntax error"]


def test_a_refusal_says_when_it_was_a_timeout(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`timed_out` has to survive the save path, or the editor blames the syntax."""

    async def timing_out(*args: object, **kwargs: object) -> ProcessOutput:
        raise RenderTimeoutError("openscad timed out after 120s", ["Compiling design..."])

    monkeypatch.setattr("scadbuddy.library.scad.run_openscad", timing_out)

    response = client.put(f"/api/v1/models/{model}/source", json={"source": "cube(1);\n"})
    assert response.status_code == 422
    body = response.json()
    assert body["timed_out"] is True
    assert "timed out" in body["detail"]


def test_replacing_the_source_swaps_the_file_rather_than_truncating_it(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """A render may have the old file open; the replacement must not be seen half-written."""
    source_path = paths.model_source(model)
    before = source_path.stat().st_ino

    replacement = "width = 7;\n"
    assert (
        client.put(f"/api/v1/models/{model}/source", json={"source": replacement}).status_code
        == 200
    )

    assert source_path.read_text(encoding="utf-8") == replacement
    assert source_path.stat().st_ino != before
    assert sorted(p.name for p in paths.model_dir(model).iterdir()) == ["model.json", "model.scad"]


def test_the_schema_of_an_unusable_export_is_a_problem_not_a_crash(client: TestClient) -> None:
    """The same failure `inspect_source` reports as a diagnostic must not become a 500
    when the read path hits it — which `force` makes reachable."""
    created = client.post(
        "/api/v1/models", json={"name": "Odd Export", "source": "%%BADPARAM%%\n", "force": True}
    )
    assert created.status_code == 201

    response = client.get("/api/v1/models/odd-export/schema")
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert "schema" in response.json()["detail"]


def test_an_unusable_export_is_refused_at_save_time_without_force(client: TestClient) -> None:
    response = client.post(
        "/api/v1/models", json={"name": "Odd Export", "source": "%%BADPARAM%%\n"}
    )
    assert response.status_code == 422
    assert "could not be derived" in response.json()["diagnostics"][0]["message"]


def test_a_source_too_large_to_be_a_model_is_refused_before_openscad_runs(
    client: TestClient, model: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check runs one OpenSCAD at a time, so a body nobody could have typed is a
    way to hold that permit — it has to be refused on shape, before it is spent."""
    log = tmp_path / "invocations.log"
    monkeypatch.setenv("FAKE_OPENSCAD_LOG", str(log))
    huge = "x" * (MAX_SOURCE_CHARS + 1)

    checked = client.post("/api/v1/models/check", json={"source": huge})
    assert checked.status_code == 422

    created = client.post("/api/v1/models", json={"name": "Huge", "source": huge})
    assert created.status_code == 422

    replaced = client.put(f"/api/v1/models/{model}/source", json={"source": huge})
    assert replaced.status_code == 422

    assert not log.exists(), "openscad ran for a body that was refused on shape"


def test_a_text_plain_paste_is_capped_the_same_way(client: TestClient) -> None:
    """The bare-source branch never sees the pydantic model, so its cap is its own."""
    response = client.post(
        "/api/v1/models",
        content="y" * (MAX_SOURCE_CHARS + 1),
        headers={"Content-Type": "text/plain", "X-Model-Name": "Huge Text"},
    )
    assert response.status_code == 422
    assert "too large" in response.json()["detail"]
