"""Issues #25 and #26 — POST /outputs/{id}/send, through the real app."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
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
