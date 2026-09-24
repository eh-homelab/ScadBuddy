from __future__ import annotations

import json
import re
import zipfile

from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from scadbuddy.render.provenance import Provenance, source_version
from scadbuddy.render.provenance import read as read_provenance
from tests.api.conftest import FAIL_WIDTH, PNG_BYTES, wait_for_job


def _finished_job(client: TestClient, slug: str, width: float = 12) -> str:
    job_id: str = client.post(
        f"/api/v1/models/{slug}/render", json={"params": {"width": width}}
    ).json()["job_id"]
    wait_for_job(client, job_id)
    return job_id


def test_persisting_a_job_writes_the_documented_layout(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    job_id = _finished_job(client, model)
    response = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": job_id, "name": "First Try"}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["slug"] == model
    assert body["name"] == "First Try"
    assert body["job_id"] == job_id
    assert body["params"] == {"width": 12}
    assert body["colors"] == ["#FF0000"]
    assert body["has_thumbnail"] is False
    # Reserved for the Bambuddy epic.
    assert body["library_file_id"] is None
    assert body["pipeline_run_id"] is None
    assert body["queue_item_id"] is None

    directory = paths.output_dir(model, body["id"])
    assert sorted(path.name for path in directory.iterdir()) == [
        "meta.json",
        "model.3mf",
        "params.json",
        "preview.glb",
    ]
    assert json.loads((directory / "params.json").read_text(encoding="utf-8")) == {"width": 12}


def test_outputs_are_listed_newest_first(client: TestClient, model: str) -> None:
    first = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": _finished_job(client, model, 1)}
    ).json()
    second = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": _finished_job(client, model, 2)}
    ).json()

    listed = client.get(f"/api/v1/models/{model}/outputs").json()
    assert [row["id"] for row in listed] == [second["id"], first["id"]]


def test_an_output_is_readable_by_id_alone(client: TestClient, model: str) -> None:
    created = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": _finished_job(client, model)}
    ).json()
    fetched = client.get(f"/api/v1/outputs/{created['id']}").json()
    assert fetched["id"] == created["id"]
    assert fetched["slug"] == model


def test_an_unfinished_or_foreign_job_cannot_be_persisted(client: TestClient, model: str) -> None:
    failed = client.post(
        f"/api/v1/models/{model}/render", json={"params": {"width": FAIL_WIDTH}}
    ).json()["job_id"]
    wait_for_job(client, failed)
    response = client.post(f"/api/v1/models/{model}/outputs", json={"job_id": failed})
    assert response.status_code == 409
    assert "failed" in response.json()["detail"]

    missing = client.post(f"/api/v1/models/{model}/outputs", json={"job_id": "0" * 32})
    assert missing.status_code == 404


def test_a_job_from_another_model_is_refused(client: TestClient, model: str) -> None:
    client.post(
        "/api/v1/models",
        files={"file": ("other.scad", b"width = 1;\n", "application/octet-stream")},
    )
    job_id = _finished_job(client, "other")
    response = client.post(f"/api/v1/models/{model}/outputs", json={"job_id": job_id})
    assert response.status_code == 409
    assert "not 'demo'" in response.json()["detail"]


def test_the_3mf_downloads_under_a_readable_filename(client: TestClient, model: str) -> None:
    created = client.post(
        f"/api/v1/models/{model}/outputs",
        json={"job_id": _finished_job(client, model), "name": "Big Blue"},
    ).json()

    response = client.get(f"/api/v1/outputs/{created['id']}/model.3mf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "model/3mf"
    assert 'filename="demo-big-blue.3mf"' in response.headers["content-disposition"]


def test_an_unnamed_output_downloads_under_its_id(client: TestClient, model: str) -> None:
    created = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": _finished_job(client, model)}
    ).json()
    response = client.get(f"/api/v1/outputs/{created['id']}/model.3mf")
    assert f"demo-{created['id']}.3mf" in response.headers["content-disposition"]


def test_the_viewer_can_upload_and_read_back_a_thumbnail(client: TestClient, model: str) -> None:
    created = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": _finished_job(client, model)}
    ).json()
    assert client.get(f"/api/v1/outputs/{created['id']}/thumbnail").status_code == 404

    put = client.put(
        f"/api/v1/outputs/{created['id']}/thumbnail",
        files={"file": ("capture.png", PNG_BYTES, "image/png")},
    )
    assert put.status_code == 204

    response = client.get(f"/api/v1/outputs/{created['id']}/thumbnail")
    assert response.status_code == 200
    assert response.content == PNG_BYTES
    assert client.get(f"/api/v1/outputs/{created['id']}").json()["has_thumbnail"] is True


def test_a_thumbnail_that_is_not_a_png_is_refused(client: TestClient, model: str) -> None:
    created = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": _finished_job(client, model)}
    ).json()
    response = client.put(
        f"/api/v1/outputs/{created['id']}/thumbnail",
        files={"file": ("capture.png", b"not a png", "image/png")},
    )
    assert response.status_code == 422


def test_deleting_an_output_removes_it(client: TestClient, model: str) -> None:
    created = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": _finished_job(client, model)}
    ).json()
    assert client.delete(f"/api/v1/outputs/{created['id']}").status_code == 204
    assert client.get(f"/api/v1/outputs/{created['id']}").status_code == 404
    assert client.get(f"/api/v1/models/{model}/outputs").json() == []


def test_an_unknown_output_is_a_404(client: TestClient) -> None:
    assert client.get("/api/v1/outputs/" + "0" * 32).status_code == 404


def test_every_output_records_its_model_version_and_stamps_the_3mf(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Issue #80 — the record and the file agree on what produced the output."""
    assert (
        client.put("/api/v1/settings", json={"public_url": "https://scad.test/"}).status_code == 200
    )
    job_id = _finished_job(client, model)
    body = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": job_id, "name": "Stamped"}
    ).json()

    expected = source_version(paths.model_dir(model))
    assert body["model_version"] == expected

    stamped = read_provenance(paths.output_dir(model, body["id"]) / "model.3mf")
    assert stamped == Provenance(
        model=model,
        version=expected,
        output=body["id"],
        params={"width": 12},
        edit_url=f"https://scad.test/edit/{body['id']}",
    )


def test_the_stamp_leaves_out_a_link_when_no_public_url_is_set(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    body = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": _finished_job(client, model)}
    ).json()
    stamped = read_provenance(paths.output_dir(model, body["id"]) / "model.3mf")
    assert stamped is not None
    assert stamped.edit_url is None


# --- #80 the edit deep link ----------------------------------------------------------


def test_the_edit_target_comes_from_the_record(client: TestClient, model: str) -> None:
    created = client.post(
        f"/api/v1/models/{model}/outputs",
        json={"job_id": _finished_job(client, model), "name": "Reagan"},
    ).json()

    body = client.get(f"/api/v1/outputs/{created['id']}/edit").json()
    assert body == {
        "output_id": created["id"],
        "slug": model,
        "name": "Reagan",
        "params": {"width": 12},
        "model_version": created["model_version"],
        "source": "record",
    }


def test_the_edit_target_falls_back_to_the_3mf_when_the_record_is_gone(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    created = client.post(
        f"/api/v1/models/{model}/outputs",
        json={"job_id": _finished_job(client, model), "name": "Reagan"},
    ).json()
    directory = paths.output_dir(model, created["id"])
    (directory / "meta.json").unlink()
    (directory / "params.json").unlink()

    assert client.get(f"/api/v1/outputs/{created['id']}").status_code == 404
    body = client.get(f"/api/v1/outputs/{created['id']}/edit").json()
    assert body == {
        "output_id": created["id"],
        "slug": model,
        "name": None,
        "params": {"width": 12},
        "model_version": created["model_version"],
        "source": "3mf",
    }


def test_an_edit_link_to_nothing_at_all_is_a_404(client: TestClient, model: str) -> None:
    response = client.get(f"/api/v1/outputs/{'0' * 32}/edit")
    assert response.status_code == 404
    assert "0" * 32 in response.json()["detail"]


def test_an_unreadable_stamp_404s_rather_than_500s(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """The record is gone and the 3MF's stamp does not parse as today's shape."""
    created = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": _finished_job(client, model)}
    ).json()
    directory = paths.output_dir(model, created["id"])
    (directory / "meta.json").unlink()
    (directory / "params.json").unlink()

    path = directory / "model.3mf"
    with zipfile.ZipFile(path) as archive:
        entries = [(name, archive.read(name)) for name in archive.namelist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            if name == "3D/3dmodel.model":
                payload = re.sub(rb'(name="ScadBuddy:provenance">)[^<]*', rb"\1{}", payload)
            archive.writestr(name, payload)

    response = client.get(f"/api/v1/outputs/{created['id']}/edit")
    assert response.status_code == 404
    assert created["id"] in response.json()["detail"]
