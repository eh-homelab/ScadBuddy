"""Issues #25 and #26 — POST /outputs/{id}/send, through the real app."""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any
from xml.etree import ElementTree as ET

import httpx
import pytest
import respx
import trimesh
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from scadbuddy.render.bambu3mf import write_bambu_3mf
from scadbuddy.render.split import ColourPart
from tests.api.conftest import wait_for_job
from tests.bambuddy.conftest import recording

BASE = "https://bambuddy.test"
API = f"{BASE}/api/v1"

PRESETS: dict[str, Any] = {
    "printer_preset": {"source": "cloud", "id": "GM041"},
    "process_preset": {"source": "cloud", "id": "GP252"},
    "filament_presets": [
        {"source": "cloud", "id": "GFSA05_22"},
        {"source": "cloud", "id": "GFSA00_22"},
    ],
    "bed_type": "Textured PEI Plate",
}


def make_output(client: TestClient, slug: str, name: str = "Elan") -> str:
    job_id: str = client.post(
        f"/api/v1/models/{slug}/render", json={"params": {"width": 12}}
    ).json()["job_id"]
    wait_for_job(client, job_id)
    created: str = client.post(
        f"/api/v1/models/{slug}/outputs", json={"job_id": job_id, "name": name}
    ).json()["id"]
    return created


def configure(client: TestClient, **extra: Any) -> None:
    body: dict[str, Any] = {
        "bambuddy_url": BASE,
        "bambuddy_api_key": "s3cret",
        "library_folder_id": 2,
    }
    body.update(extra)
    assert client.put("/api/v1/settings", json=body).status_code == 200


def upload_route(file_id: int = 41) -> respx.Route:
    return respx.post(f"{API}/library/files").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": file_id,
                "filename": "demo-elan.3mf",
                "file_type": "3mf",
                "file_size": 9,
                "thumbnail_path": None,
            },
        )
    )


def plate_routes(
    *, pipeline_id: int | None = None, printer_id: int | None = None, model: str = "H2C"
) -> None:
    """Mock what the send path reads to learn which printer's plate to lay out for.

    Registered by every test that configures a pipeline or a printer, because
    ``upload_output`` re-places the 3MF for that printer before uploading (#105).
    """
    respx.get(f"{API}/slicer-pipelines/").mock(
        return_value=httpx.Response(
            200,
            json={
                "pipelines": [
                    {
                        "id": pipeline_id,
                        "name": "keychains",
                        "target_kind": "printer_class",
                        "target_model_class": model,
                        "fanout_strategy": "max_parallel",
                    }
                ]
                if pipeline_id is not None
                else []
            },
        )
    )
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": printer_id if printer_id is not None else 1,
                    "name": "3DP-31B-598",
                    "model": model,
                    "is_active": True,
                    "nozzle_count": 2,
                }
            ],
        )
    )


# --- #25 library mode ---------------------------------------------------------------


@respx.mock
def test_library_mode_uploads_to_the_configured_folder_and_records_the_id(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    configure(client)
    output_id = make_output(client, model)
    route = upload_route()

    response = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"})

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "library"
    assert body["library_file_id"] == 41
    assert body["bambuddy_url"] == f"{BASE}/library"
    assert body["queue_item_id"] is None

    request = route.calls.last.request
    assert request.url.params["folder_id"] == "2"
    assert request.headers["X-API-Key"] == "s3cret"
    assert b"demo-elan.3mf" in request.content

    meta = json.loads(
        (paths.output_dir(model, output_id) / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["library_file_id"] == 41

    # And the detail route reports it, so the UI can deep-link without re-sending.
    assert client.get(f"/api/v1/outputs/{output_id}").json()["library_file_id"] == 41


@respx.mock
def test_a_re_send_deletes_the_previous_file_rather_than_duplicating_it(
    client: TestClient, model: str
) -> None:
    configure(client)
    output_id = make_output(client, model)
    upload = upload_route()
    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"})

    delete = respx.delete(f"{API}/library/files/41").mock(return_value=httpx.Response(200, json={}))
    upload.mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 42,
                "filename": "demo-elan.3mf",
                "file_type": "3mf",
                "file_size": 9,
                "thumbnail_path": None,
            },
        )
    )

    body = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}).json()

    assert delete.called
    assert body["library_file_id"] == 42


@respx.mock
def test_a_re_send_survives_the_file_having_been_deleted_in_bambuddy(
    client: TestClient, model: str
) -> None:
    configure(client)
    output_id = make_output(client, model)
    upload_route()
    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"})

    respx.delete(f"{API}/library/files/41").mock(
        return_value=httpx.Response(404, json={"detail": "Not found"})
    )

    response = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"})

    assert response.status_code == 200


def test_sending_without_a_url_configured_is_a_conflict(client: TestClient, model: str) -> None:
    output_id = make_output(client, model)

    response = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"})

    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"


def test_sending_an_unknown_output_is_a_404(client: TestClient) -> None:
    response = client.post(f"/api/v1/outputs/{'0' * 32}/send", json={"mode": "library"})
    assert response.status_code == 404


# --- #26 queue mode -----------------------------------------------------------------


@respx.mock
def test_queue_mode_runs_the_configured_pipeline(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    configure(client, pipeline_id=4)
    plate_routes(pipeline_id=4)
    output_id = make_output(client, model)
    upload_route()
    run = respx.post(f"{API}/slicer-pipelines/4/run").mock(
        return_value=httpx.Response(
            202,
            json={
                "id": 12,
                "pipeline_id": 4,
                "source_library_file_id": 41,
                "copies": 3,
                "status": "queued",
                "slice_job_id": None,
                "sliced_library_file_id": None,
                "eligibility_overridden": False,
                "created_by": None,
                "created_at": "2026-09-23T01:00:00Z",
                "started_at": None,
                "completed_at": None,
            },
        )
    )

    body = client.post(
        f"/api/v1/outputs/{output_id}/send", json={"mode": "queue", "copies": 3}
    ).json()

    assert body["pipeline_run_id"] == 12
    assert body["queue_item_id"] is None
    assert body["bambuddy_url"] == f"{BASE}/queue"
    assert json.loads(run.calls.last.request.read())["copies"] == 3

    meta = json.loads(
        (paths.output_dir(model, output_id) / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["pipeline_run_id"] == 12


@respx.mock
def test_an_ineligible_pipeline_surfaces_bambuddys_report_verbatim(
    client: TestClient, model: str
) -> None:
    configure(client, pipeline_id=4)
    plate_routes(pipeline_id=4)
    output_id = make_output(client, model)
    upload_route()
    report = {
        "ok": False,
        "target_printer_name": "3DP-31B-598",
        "issues": [
            {"kind": "filament_type_mismatch", "slot_index": 0, "expected": "PLA", "actual": "PETG"}
        ],
    }
    respx.post(f"{API}/slicer-pipelines/4/run").mock(return_value=httpx.Response(409, json=report))

    response = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"})

    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["bambuddy_body"] == report


@respx.mock
def test_queue_mode_without_a_pipeline_slices_then_enqueues(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    configure(client, printer_id=1, **PRESETS)
    plate_routes(printer_id=1)
    output_id = make_output(client, model)
    upload_route()
    slice_route = respx.post(f"{API}/library/files/41/slice").mock(
        return_value=httpx.Response(202, json={"job_id": 9, "status": "pending"})
    )
    respx.get(f"{API}/slice-jobs/9").mock(
        side_effect=[
            httpx.Response(200, json={"id": 9, "status": "running"}),
            httpx.Response(
                200,
                json={"id": 9, "status": "completed", "result": {"library_file_id": 52}},
            ),
        ]
    )
    queue = respx.post(f"{API}/queue/").mock(
        return_value=httpx.Response(200, json=recording("queue-item.json"))
    )

    body = client.post(
        f"/api/v1/outputs/{output_id}/send", json={"mode": "queue", "copies": 2}
    ).json()

    assert body["queue_item_id"] == 9
    assert body["pipeline_run_id"] is None
    assert body["bambuddy_url"] == f"{BASE}/queue"

    sliced = json.loads(slice_route.calls.last.request.read())
    # The output's colours are the extruder order, so they are the slot colours.
    assert sliced["filament_colours"] == ["#FF0000"]
    assert sliced["bed_type"] == "Textured PEI Plate"
    assert sliced["plate"] == 1

    queued = json.loads(queue.calls.last.request.read())
    assert queued["library_file_id"] == 52  # the SLICED file, not the uploaded one
    assert queued["printer_id"] == 1
    assert queued["quantity"] == 2

    meta = json.loads(
        (paths.output_dir(model, output_id) / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["queue_item_id"] == 9


@respx.mock
def test_a_failed_slice_is_reported_rather_than_queued(client: TestClient, model: str) -> None:
    configure(client, printer_id=1, **PRESETS)
    plate_routes(printer_id=1)
    output_id = make_output(client, model)
    upload_route()
    respx.post(f"{API}/library/files/41/slice").mock(
        return_value=httpx.Response(202, json={"job_id": 9, "status": "pending"})
    )
    respx.get(f"{API}/slice-jobs/9").mock(
        return_value=httpx.Response(
            200, json={"id": 9, "status": "failed", "error": "unprintable geometry"}
        )
    )
    queue = respx.post(f"{API}/queue/")

    response = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"})

    assert response.status_code == 502
    assert "unprintable geometry" in response.json()["detail"]
    assert not queue.called


@respx.mock
def test_queue_mode_with_neither_a_pipeline_nor_presets_says_so(
    client: TestClient, model: str
) -> None:
    configure(client, printer_id=1)
    plate_routes(printer_id=1)
    output_id = make_output(client, model)
    upload_route()

    response = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"})

    assert response.status_code == 409
    assert "presets" in response.json()["detail"]


@respx.mock
def test_queue_mode_with_no_printer_and_no_pipeline_says_so(client: TestClient, model: str) -> None:
    configure(client, **PRESETS)
    output_id = make_output(client, model)
    upload_route()

    response = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"})

    assert response.status_code == 409
    assert "printer" in response.json()["detail"]


@respx.mock
def test_more_colours_than_filament_slots_is_refused_before_slicing(
    client: TestClient, model: str
) -> None:
    configure(
        client,
        printer_id=1,
        printer_preset=PRESETS["printer_preset"],
        process_preset=PRESETS["process_preset"],
        filament_presets=[],
    )
    plate_routes(printer_id=1)
    output_id = make_output(client, model)
    upload_route()
    sliced = respx.post(f"{API}/library/files/41/slice")

    response = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"})

    assert response.status_code == 409
    assert not sliced.called


@pytest.mark.parametrize("copies", [0, 1001])
def test_copies_is_bounded(client: TestClient, model: str, copies: int) -> None:
    output_id = make_output(client, model)
    response = client.post(
        f"/api/v1/outputs/{output_id}/send", json={"mode": "queue", "copies": copies}
    )
    assert response.status_code == 422


# --- #105 the plate follows the target printer --------------------------------------


@respx.mock
def test_the_upload_is_laid_out_for_the_target_printers_plate(
    client: TestClient, model: str
) -> None:
    """An H2C reaches x 25..325, so its centre is 175,160 — not the 256-plate's 128,128."""
    configure(client, pipeline_id=4)
    plate_routes(pipeline_id=4, model="H2C")
    output_id = make_output(client, model)
    upload = upload_route()
    respx.post(f"{API}/slicer-pipelines/4/run").mock(
        return_value=httpx.Response(
            202,
            json={
                "id": 12,
                "pipeline_id": 4,
                "source_library_file_id": 41,
                "copies": 1,
                "status": "queued",
                "slice_job_id": None,
                "sliced_library_file_id": None,
                "eligibility_overridden": False,
                "created_by": None,
                "created_at": "2026-09-23T01:00:00Z",
                "started_at": None,
                "completed_at": None,
            },
        )
    )

    assert client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"}).status_code

    uploaded = _uploaded_3mf(upload)
    with zipfile.ZipFile(io.BytesIO(uploaded)) as archive:
        root = ET.fromstring(archive.read("3D/3dmodel.model"))
    item = root.find(".//{*}item")
    assert item is not None
    transform = [float(value) for value in (item.get("transform") or "").split()]
    assert transform[9:11] == [175.0, 160.0]


@respx.mock
def test_an_unknown_printer_model_still_uploads_on_the_default_plate(
    client: TestClient, model: str
) -> None:
    configure(client, printer_id=1)
    plate_routes(printer_id=1, model="SomeFuturePrinter")
    output_id = make_output(client, model)
    upload = upload_route()

    assert client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}).status_code

    with zipfile.ZipFile(io.BytesIO(_uploaded_3mf(upload))) as archive:
        root = ET.fromstring(archive.read("3D/3dmodel.model"))
    item = root.find(".//{*}item")
    assert item is not None
    assert [float(v) for v in (item.get("transform") or "").split()][9:11] == [128.0, 128.0]


@respx.mock
def test_a_model_too_big_for_the_printer_is_refused_before_the_upload(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    configure(client, printer_id=1)
    plate_routes(printer_id=1, model="A1 mini")
    output_id = make_output(client, model)
    # 200 mm across does not fit an A1 mini's 180 mm bed.
    write_bambu_3mf(
        [ColourPart(1, "Color 1", "#FF0000", trimesh.creation.box(extents=(200, 200, 4)))],
        paths.output_dir(model, output_id) / "model.3mf",
        thumbnails=None,
        model_name=model,
    )
    upload = upload_route()

    response = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"})

    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert "A1 mini" in response.json()["detail"]
    assert not upload.called


def _uploaded_3mf(route: respx.Route) -> bytes:
    """The ``file`` part of the multipart upload Bambuddy received."""
    request = route.calls.last.request
    boundary = request.headers["content-type"].split("boundary=", 1)[1].encode()
    for part in request.read().split(b"--" + boundary):
        head, _, body = part.partition(b"\r\n\r\n")
        if b'name="file"' in head:
            return body.rsplit(b"\r\n", 1)[0]
    raise AssertionError("the upload carried no file part")


@respx.mock
def test_a_refused_re_send_leaves_the_previous_file_in_place(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """The fit check runs before the delete, so a refusal costs nothing.

    Deleting first would strand ``library_file_id`` pointing at a file that is no
    longer in Bambuddy: the button would report 409 and the deep link would 404.
    """
    respx.get(f"{API}/slicer-pipelines/").mock(
        return_value=httpx.Response(200, json={"pipelines": []})
    )
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 1, "name": "big", "model": "H2C", "is_active": True, "nozzle_count": 2},
                {
                    "id": 2,
                    "name": "small",
                    "model": "A1 mini",
                    "is_active": True,
                    "nozzle_count": 1,
                },
            ],
        )
    )
    configure(client, printer_id=1)
    output_id = make_output(client, model)
    upload_route()
    assert (
        client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}).json()[
            "library_file_id"
        ]
        == 41
    )

    delete = respx.delete(f"{API}/library/files/41").mock(return_value=httpx.Response(200, json={}))
    # Same output, but now aimed at a printer it cannot possibly fit on.
    write_bambu_3mf(
        [ColourPart(1, "Color 1", "#FF0000", trimesh.creation.box(extents=(200, 200, 4)))],
        paths.output_dir(model, output_id) / "model.3mf",
        thumbnails=None,
        model_name=model,
    )
    configure(client, printer_id=2)

    response = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"})

    assert response.status_code == 409
    assert not delete.called, "the old file was removed before the refusal"
    assert client.get(f"/api/v1/outputs/{output_id}").json()["library_file_id"] == 41


@respx.mock
def test_a_failed_delete_keeps_the_recorded_library_file_id(client: TestClient, model: str) -> None:
    """A delete that is not a 404 leaves the previous send intact and retryable.

    Clearing the id first would strand the file in Bambuddy with nothing pointing
    at it, and every retry would upload another copy — the duplication this delete
    exists to prevent.
    """
    configure(client)
    output_id = make_output(client, model)
    upload = upload_route()
    assert (
        client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}).json()[
            "library_file_id"
        ]
        == 41
    )

    respx.delete(f"{API}/library/files/41").mock(
        return_value=httpx.Response(500, json={"detail": "boom"})
    )

    response = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"})

    assert response.status_code >= 500
    assert upload.call_count == 1, "a second copy was uploaded despite the failed delete"
    assert client.get(f"/api/v1/outputs/{output_id}").json()["library_file_id"] == 41


# --- #80 the Edit in ScadBuddy back-link ---------------------------------------------


def annotate_route(file_id: int = 41) -> respx.Route:
    return respx.put(f"{API}/library/files/{file_id}").mock(
        return_value=httpx.Response(200, json={"id": file_id, "filename": "demo-elan.3mf"})
    )


@respx.mock
def test_the_edit_link_is_attached_to_the_uploaded_file(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    configure(client, public_url="https://scad.test/")
    output_id = make_output(client, model)
    upload_route()
    annotate = annotate_route()

    body = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}).json()

    edit_url = f"https://scad.test/edit/{output_id}"
    assert body["edit_url"] == edit_url
    assert annotate.called
    assert json.loads(annotate.calls.last.request.content) == {
        "notes": f"Edit in ScadBuddy: {edit_url}"
    }


@respx.mock
def test_nothing_is_attached_when_no_public_url_is_configured(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    configure(client)
    output_id = make_output(client, model)
    upload_route()
    annotate = annotate_route()

    body = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"}).json()

    assert body["edit_url"] is None
    assert not annotate.called


def pipeline_run_route(run_id: int = 12) -> respx.Route:
    return respx.post(f"{API}/slicer-pipelines/4/run").mock(
        return_value=httpx.Response(
            202,
            json={
                "id": run_id,
                "pipeline_id": 4,
                "source_library_file_id": 41,
                "copies": 1,
                "status": "queued",
                "slice_job_id": None,
                "sliced_library_file_id": None,
                "eligibility_overridden": False,
                "created_by": None,
                "created_at": "2026-09-23T01:00:00Z",
                "started_at": None,
                "completed_at": None,
            },
        )
    )


@respx.mock
def test_a_failed_annotation_still_queues_the_print(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """The note is cosmetic; queueing the print is the point of the request."""
    configure(client, pipeline_id=4, public_url="https://scad.test")
    plate_routes(pipeline_id=4)
    output_id = make_output(client, model)
    upload_route()
    run = pipeline_run_route()
    annotate = respx.put(f"{API}/library/files/41").mock(
        return_value=httpx.Response(500, json={"detail": "boom"})
    )

    response = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"})

    assert response.status_code == 200
    body = response.json()
    assert body["pipeline_run_id"] == 12
    # Nothing was attached, so the result does not claim a link.
    assert body["edit_url"] is None
    assert annotate.called
    assert run.called

    meta = json.loads(
        (paths.output_dir(model, output_id) / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["pipeline_run_id"] == 12


@respx.mock
def test_the_annotation_runs_after_the_work_that_matters(client: TestClient, model: str) -> None:
    """A slow or broken annotate must not sit in front of the pipeline run."""
    configure(client, pipeline_id=4, public_url="https://scad.test")
    plate_routes(pipeline_id=4)
    output_id = make_output(client, model)
    upload_route()
    pipeline_run_route()
    annotate_route()

    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"})

    order = [(call.request.method, call.request.url.path) for call in respx.calls]
    assert order.index(("POST", "/api/v1/slicer-pipelines/4/run")) < order.index(
        ("PUT", "/api/v1/library/files/41")
    )


@respx.mock
def test_the_annotation_is_a_partial_update_of_notes_alone(client: TestClient, model: str) -> None:
    """Bambuddy's ``update_file`` guards every assignment with ``if data.X is not
    None``, so an omitted field is left alone — see the citation in client.py."""
    configure(client, public_url="https://scad.test")
    output_id = make_output(client, model)
    upload_route()
    annotate = annotate_route()

    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"})

    assert json.loads(annotate.calls.last.request.content) == {
        "notes": f"Edit in ScadBuddy: https://scad.test/edit/{output_id}"
    }


@respx.mock
def test_the_slice_and_queue_branch_annotates_both_files_last(
    client: TestClient, model: str
) -> None:
    """The third send path reaches attach_edit_link too, and only after enqueueing.

    Slicing leaves a second library entry, and it is that one the queue references —
    so it is the one a reader opens from the queue, and it needs the link as much as
    the 3MF ScadBuddy uploaded.
    """
    configure(client, printer_id=1, public_url="https://scad.test", **PRESETS)
    plate_routes(printer_id=1)
    output_id = make_output(client, model)
    upload_route()
    respx.post(f"{API}/library/files/41/slice").mock(
        return_value=httpx.Response(202, json={"job_id": 9, "status": "pending"})
    )
    respx.get(f"{API}/slice-jobs/9").mock(
        return_value=httpx.Response(
            200, json={"id": 9, "status": "completed", "result": {"library_file_id": 52}}
        )
    )
    respx.post(f"{API}/queue/").mock(
        return_value=httpx.Response(200, json=recording("queue-item.json"))
    )
    annotate = annotate_route()
    annotate_sliced = annotate_route(52)

    body = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"}).json()

    assert body["queue_item_id"] == 9
    assert body["edit_url"] == f"https://scad.test/edit/{output_id}"
    assert annotate.called
    assert annotate_sliced.called
    assert json.loads(annotate_sliced.calls.last.request.content) == {
        "notes": f"Edit in ScadBuddy: https://scad.test/edit/{output_id}"
    }

    order = [(call.request.method, call.request.url.path) for call in respx.calls]
    assert order.index(("POST", "/api/v1/queue/")) < order.index(
        ("PUT", "/api/v1/library/files/41")
    )
    assert order.index(("POST", "/api/v1/queue/")) < order.index(
        ("PUT", "/api/v1/library/files/52")
    )


@respx.mock
def test_a_failed_annotation_still_returns_the_queued_item(client: TestClient, model: str) -> None:
    configure(client, printer_id=1, public_url="https://scad.test", **PRESETS)
    plate_routes(printer_id=1)
    output_id = make_output(client, model)
    upload_route()
    respx.post(f"{API}/library/files/41/slice").mock(
        return_value=httpx.Response(202, json={"job_id": 9, "status": "pending"})
    )
    respx.get(f"{API}/slice-jobs/9").mock(
        return_value=httpx.Response(
            200, json={"id": 9, "status": "completed", "result": {"library_file_id": 52}}
        )
    )
    respx.post(f"{API}/queue/").mock(
        return_value=httpx.Response(200, json=recording("queue-item.json"))
    )
    # Both library entries refuse the note; the queued print is unaffected either way.
    respx.put(f"{API}/library/files/41").mock(return_value=httpx.Response(502))
    respx.put(f"{API}/library/files/52").mock(return_value=httpx.Response(502))

    response = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"})

    assert response.status_code == 200
    assert response.json()["queue_item_id"] == 9
    assert response.json()["edit_url"] is None
