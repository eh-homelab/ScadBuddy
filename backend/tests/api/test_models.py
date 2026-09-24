from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
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
    before = json.loads(paths.model_meta(model).read_text())["schema"]["source_sha256"]

    replacement = 'width = 42;\nlabel = "new";\ncube(width);\n'
    response = client.put(f"/api/v1/models/{model}/source", json={"source": replacement})
    assert response.status_code == 200
    assert response.json()["slug"] == model
    assert client.get(f"/api/v1/models/{model}/source").text == replacement

    after = json.loads(paths.model_meta(model).read_text())["schema"]["source_sha256"]
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
