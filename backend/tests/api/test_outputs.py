from __future__ import annotations

import json
import re
import shutil
import zipfile
from types import SimpleNamespace
from typing import Any

import pytest
import trimesh
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.core.paths import DataPaths
from scadbuddy.library import outputs as outputs_module
from scadbuddy.library.outputs import OutputMeta
from scadbuddy.render.bambu3mf import PlateParts, write_plates_3mf
from scadbuddy.render.inputs import InputsError
from scadbuddy.render.provenance import Provenance, source_version
from scadbuddy.render.provenance import read as read_provenance
from scadbuddy.render.split import ColourPart
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
    assert body["library_files"] == []
    assert "pipeline_run_id" not in body
    assert body["queue_item_id"] is None

    directory = paths.output_dir(model, body["id"])
    assert sorted(path.name for path in directory.iterdir()) == [
        "inputs.json",
        "meta.json",
        "model.3mf",
        "params.json",
        "preview.glb",
    ]
    assert json.loads((directory / "params.json").read_text(encoding="utf-8")) == {"width": 12}


@pytest.mark.requires_postgres
def test_outputs_are_listed_newest_first(client: TestClient, model: str) -> None:
    first = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": _finished_job(client, model, 1)}
    ).json()
    second = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": _finished_job(client, model, 2)}
    ).json()

    listed = client.get(f"/api/v1/models/{model}/outputs").json()
    assert [row["id"] for row in listed] == [second["id"], first["id"]]


@pytest.mark.requires_postgres
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


@pytest.mark.requires_postgres
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


@pytest.mark.requires_postgres
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


def test_the_version_is_the_source_the_render_saw_not_the_one_on_disk_at_save(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Generate persists an earlier render; the model may have moved on since.

    The stamp has to describe the source that produced the geometry, so the hash is
    taken when the render runs, not when the output is saved.
    """
    rendered = source_version(paths.model_dir(model))
    job_id = _finished_job(client, model)

    paths.model_source(model).write_text('width = 10;\nlabel = "edited";\n', encoding="utf-8")
    assert source_version(paths.model_dir(model)) != rendered

    body = client.post(f"/api/v1/models/{model}/outputs", json={"job_id": job_id}).json()
    assert body["model_version"] == rendered

    stamped = read_provenance(paths.output_dir(model, body["id"]) / "model.3mf")
    assert stamped is not None
    assert stamped.version == rendered


def test_an_edit_link_for_a_model_that_is_gone_is_a_404(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """A deep link outlives its output; it must not outlive its model.

    Every other route through a slug asks the catalogue first. Answering 200 here
    would send the customizer to a model that cannot be loaded, which reads as a
    broken page rather than as the "that output is gone" the link already has.
    """
    created = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": _finished_job(client, model)}
    ).json()
    assert client.get(f"/api/v1/outputs/{created['id']}/edit").status_code == 200

    shutil.rmtree(paths.model_dir(model))
    assert client.get(f"/api/v1/outputs/{created['id']}/edit").status_code == 404


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
        "inputs": {"params": {"width": 12}, "v": 0},
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
        "inputs": {"params": {"width": 12}, "v": 0},
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


def test_the_geometry_of_an_output_is_measured_and_cached(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    created = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": _finished_job(client, model)}
    ).json()

    response = client.get(f"/api/v1/outputs/{created['id']}/geometry")
    assert response.status_code == 200, response.text
    body = response.json()
    # The stub's 3MF is one closed 10 x 10 x 5 box.
    assert [(part["source"], part["open_edges"]) for part in body["parts"]] == [("solid", 0)]
    assert body["edges"] == []
    assert body["height_mm"] == 5
    assert body["bed_contact_area_mm2"] == 100
    assert body["height_to_base_ratio"] == 0.5
    assert body["thinnest_wall"]["thickness_mm"] == 5

    cache = paths.output_dir(model, created["id"]) / "geometry.json"
    assert json.loads(cache.read_text(encoding="utf-8")) == body
    # Served from the cache from then on: a doctored cache comes back as written.
    cache.write_text(json.dumps({**body, "height_mm": 42}), encoding="utf-8")
    assert client.get(f"/api/v1/outputs/{created['id']}/geometry").json()["height_mm"] == 42


def test_the_geometry_of_an_output_without_a_3mf_is_404(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    created = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": _finished_job(client, model)}
    ).json()
    (paths.output_dir(model, created["id"]) / "model.3mf").unlink()

    response = client.get(f"/api/v1/outputs/{created['id']}/geometry")
    assert response.status_code == 404
    assert client.get(f"/api/v1/outputs/{'0' * 32}/geometry").status_code == 404


def test_the_geometry_of_a_damaged_3mf_is_422(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """A truncated object entry raises ``ET.ParseError`` -- a ``SyntaxError``, not a
    ``ValueError`` -- which must still come back as the documented 422, not a 500."""
    created = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": _finished_job(client, model)}
    ).json()
    path = paths.output_dir(model, created["id"]) / "model.3mf"
    with zipfile.ZipFile(path) as archive:
        entries = [(info, archive.read(info)) for info in archive.infolist()]
    with zipfile.ZipFile(path, "w") as archive:
        for info, payload in entries:
            if info.filename == "3D/Objects/object_1.model":
                payload = payload[: len(payload) // 2]
            archive.writestr(info, payload)

    response = client.get(f"/api/v1/outputs/{created['id']}/geometry")
    assert response.status_code == 422, response.text
    assert "cannot be analysed" in response.json()["detail"]


def test_the_geometry_of_a_multi_plate_output_is_measured_a_plate_at_a_time(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    created = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": _finished_job(client, model)}
    ).json()
    directory = paths.output_dir(model, created["id"])
    small = ColourPart(1, "Color 1", "#FF0000", trimesh.creation.box(extents=(10, 10, 10)))
    big = ColourPart(1, "Color 1", "#FF0000", trimesh.creation.box(extents=(30, 30, 30)))
    write_plates_3mf(
        [PlateParts((small,), (1,)), PlateParts((big,), (1,))],
        ["#FF0000"],
        directory / "model.3mf",
        thumbnails=None,
    )
    url = f"/api/v1/outputs/{created['id']}/geometry"

    first = client.get(url).json()
    second = client.get(url, params={"plate": 2}).json()

    assert (first["plate"], first["plates"], first["height_mm"]) == (1, 2, 10)
    assert (second["plate"], second["plates"], second["height_mm"]) == (2, 2, 30)
    assert json.loads((directory / "geometry-plate-2.json").read_text(encoding="utf-8")) == second
    missing = client.get(url, params={"plate": 3})
    assert missing.status_code == 404
    assert "no plate 3" in missing.json()["detail"]
    assert client.get(url, params={"plate": 0}).status_code == 422


def test_an_old_records_upload_keys_are_ignored() -> None:
    """The keys a ``meta.json`` carried for its Bambuddy uploads before they moved to
    Postgres (#455) still load, and mean nothing: no data is migrated."""
    meta = OutputMeta.model_validate(
        {
            "id": "a" * 32,
            "slug": "demo",
            "job_id": "b" * 32,
            "created_at": "2026-09-22T10:00:00Z",
            "bbox_mm": {"min": [0, 0, 0], "max": [1, 1, 1], "size": [1, 1, 1]},
            "library_file_id": 41,
            "library_file_plate": "H2C@0.4",
            "library_files": [{"id": 41, "folder_id": 2, "target_key": "H2C@0.4"}],
        }
    )
    dumped = meta.model_dump()
    assert not {"library_file_id", "library_file_plate", "library_files"} & dumped.keys()


def _rendered(client: TestClient, model: str, body: dict[str, object]) -> str:
    accepted = client.post(f"/api/v1/models/{model}/render", json=body)
    assert accepted.status_code == 202, accepted.text
    job = wait_for_job(client, accepted.json()["job_id"])
    assert job["status"] == "done", job
    return str(job["id"])


def test_an_output_records_the_inputs_it_was_saved_with(client: TestClient, model: str) -> None:
    job_id = _rendered(client, model, {"inputs": {"params": {"width": 12}, "ui": {"tab": "a"}}})
    sent = {"params": {"width": 12}, "ui": {"tab": "b"}}
    created = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": job_id, "inputs": sent}
    )
    assert created.status_code == 201, created.text
    output = created.json()
    assert output["inputs"] == {**sent, "v": 0}
    assert client.get(f"/api/v1/outputs/{output['id']}").json()["inputs"] == output["inputs"]
    assert client.get(f"/api/v1/outputs/{output['id']}/edit").json()["inputs"] == output["inputs"]


@pytest.mark.parametrize(
    ("rendered", "sent"),
    [
        ({"width": 12}, {"width": 13}),
        # Type as well as value, as the store checks them: 12.0 is not 12, True is not 1.
        ({"width": 12}, {"width": 12.0}),
        ({"width": 1}, {"width": True}),
        # A job that rendered the defaults did not render a width.
        ({}, {"width": 12}),
    ],
)
def test_an_output_refuses_inputs_the_job_did_not_render(
    client: TestClient,
    model: str,
    paths: DataPaths,
    rendered: dict[str, object],
    sent: dict[str, object],
) -> None:
    job_id = _rendered(client, model, {"params": rendered})
    refused = client.post(
        f"/api/v1/models/{model}/outputs",
        json={"job_id": job_id, "inputs": {"params": sent}},
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == f"inputs.params are not the parameters job {job_id} rendered"
    output_dir = paths.outputs / model
    assert not output_dir.exists() or not any(output_dir.iterdir())


@pytest.mark.parametrize(
    ("inputs", "detail"),
    [
        ({"params": "not-an-object"}, "inputs.params must be an object of parameter values"),
        ({"params": {"width": 12}, "ui": {"note": "x" * 70_000}}, "bytes; at most 65536"),
    ],
    ids=["malformed", "oversized"],
)
def test_an_output_refuses_inputs_that_are_not_inputs(
    client: TestClient,
    model: str,
    paths: DataPaths,
    inputs: dict[str, object],
    detail: str,
) -> None:
    job_id = _rendered(client, model, {"params": {"width": 12}})
    refused = client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": job_id, "inputs": inputs}
    )
    assert refused.status_code == 422, refused.text
    assert detail in refused.json()["detail"]
    output_dir = paths.outputs / model
    assert not output_dir.exists() or not any(output_dir.iterdir())


def test_the_store_leaves_nothing_behind_for_inputs_it_refuses(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    job_id = _rendered(client, model, {"params": {"width": 12}})
    state = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    job = state.render.store.read(job_id)
    with pytest.raises(InputsError):
        state.outputs.create(job, inputs={"params": {"width": "12"}})
    output_dir = paths.outputs / model
    assert not output_dir.exists() or not any(output_dir.iterdir())


def test_a_corrupt_inputs_file_reads_as_the_params_the_output_rendered(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    job_id = _rendered(client, model, {"inputs": {"params": {"width": 12}, "ui": {"tab": "a"}}})
    output = client.post(f"/api/v1/models/{model}/outputs", json={"job_id": job_id}).json()
    inputs_path = paths.output_dir(model, output["id"]) / "inputs.json"
    inputs_path.write_text("{not json", encoding="utf-8")
    expected = {"params": {"width": 12}, "v": 0}
    assert client.get(f"/api/v1/outputs/{output['id']}").json()["inputs"] == expected
    assert client.get(f"/api/v1/outputs/{output['id']}/edit").json()["inputs"] == expected


def test_an_output_saved_without_inputs_records_the_jobs(client: TestClient, model: str) -> None:
    job_id = _rendered(client, model, {"inputs": {"params": {"width": 12}, "ui": {"tab": "a"}}})
    output = client.post(f"/api/v1/models/{model}/outputs", json={"job_id": job_id}).json()
    assert output["inputs"] == {"params": {"width": 12}, "ui": {"tab": "a"}, "v": 0}


def test_an_output_from_before_inputs_reads_as_params_v0(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    job_id = _rendered(client, model, {"params": {"width": 12}})
    output = client.post(f"/api/v1/models/{model}/outputs", json={"job_id": job_id}).json()
    (paths.output_dir(model, output["id"]) / "inputs.json").unlink()
    assert client.get(f"/api/v1/outputs/{output['id']}").json()["inputs"] == {
        "params": {"width": 12},
        "v": 0,
    }
    assert client.get(f"/api/v1/outputs/{output['id']}/edit").json()["inputs"] == {
        "params": {"width": 12},
        "v": 0,
    }


def test_saving_a_job_whose_result_is_gone_is_a_404(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Not a 500 carrying a server path: the store no longer has the piece's files."""
    job_id = _finished_job(client, model)
    result = getattr(client.app.state, STATE_ATTR).render.store.read(job_id).result  # type: ignore[attr-defined]
    (paths.root / result.model_3mf).unlink()
    response = client.post(f"/api/v1/models/{model}/outputs", json={"job_id": job_id})
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert "is gone" in response.json()["detail"]
    assert str(paths.root) not in response.text
    outputs = paths.output_dir(model, "x").parent
    assert not outputs.exists() or not any(outputs.iterdir())


@pytest.mark.parametrize("swept", ["model_3mf", "preview_glb"])
def test_a_result_swept_while_it_is_copied_is_a_404(
    client: TestClient,
    model: str,
    paths: DataPaths,
    monkeypatch: pytest.MonkeyPatch,
    swept: str,
) -> None:
    """#672 gate: eviction or the sweep can take the files after the route's check. The
    output's directory goes too, whichever file was taken, so nothing is left behind."""
    job_id = _finished_job(client, model)
    state = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    result = state.render.store.read(job_id).result
    create = state.outputs.create

    def swept_first(*args: Any, **kwargs: Any) -> Any:
        (paths.root / getattr(result, swept)).unlink()
        return create(*args, **kwargs)

    monkeypatch.setattr(state.outputs, "create", swept_first)
    response = client.post(f"/api/v1/models/{model}/outputs", json={"job_id": job_id})
    assert response.status_code == 404
    assert "is gone" in response.json()["detail"]
    assert str(paths.root) not in response.text
    outputs = paths.output_dir(model, "x").parent
    assert not outputs.exists() or not any(outputs.iterdir())


def test_a_copy_that_fails_otherwise_is_the_same_404_without_a_path(
    client: TestClient, model: str, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any copy failure, not only a missing file, is the result being gone: the problem
    names no server path, and no partial output is left behind."""
    job_id = _finished_job(client, model)
    denied = str(paths.root / "blobs" / "denied")

    def copyfile(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError(13, "Permission denied", denied)

    monkeypatch.setattr(
        outputs_module, "shutil", SimpleNamespace(copyfile=copyfile, rmtree=shutil.rmtree)
    )
    response = client.post(f"/api/v1/models/{model}/outputs", json={"job_id": job_id})
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == f"the result of job {job_id!r} is gone"
    assert str(paths.root) not in response.text
    outputs = paths.output_dir(model, "x").parent
    assert not outputs.exists() or not any(outputs.iterdir())
