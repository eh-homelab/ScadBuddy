"""Issue #89 — GET /api/v1/print/outputs/{id}/progress, through the real app."""

from __future__ import annotations

import json

import httpx
import respx
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from tests.api.test_print_filaments import queue_route, slice_routes
from tests.api.test_print_run_choices import run_request, run_routes
from tests.api.test_send import BASE, configure, make_output, upload_route

API = f"{BASE}/api/v1"


@respx.mock
def test_an_output_that_has_never_printed_answers_null(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    response = client.get(f"/api/v1/print/outputs/{output_id}/progress")
    assert response.status_code == 200
    assert response.json() is None


@respx.mock
def test_the_slice_and_queue_route_reports_through_the_same_shape(
    client: TestClient, model: str
) -> None:
    """#87's route records a queue item rather than a run, and #89 must still follow it
    — that is the gap the print-options work flagged."""
    configure(client)
    output_id = make_output(client, model)
    upload_route()
    run_routes()
    slice_routes()
    queue_route()

    ran = client.post(f"/api/v1/print/outputs/{output_id}/run", json=run_request()).json()
    assert ran["route"] == "slice_queue"

    respx.get(f"{API}/queue/51").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 51,
                "printer_id": 1,
                "printer_name": "3DP-31B-598",
                "status": "pending",
                "waiting_reason": "No active H2C printers are idle",
            },
        )
    )
    body = client.get(f"/api/v1/print/outputs/{output_id}/progress").json()
    assert body["route"] == "slice_queue"
    assert body["queue_item_id"] == 51
    assert body["slice_job_id"] == 9
    assert body["settled"] is False
    # Waiting is not failing.
    assert body["error_message"] is None
    assert body["copies_detail"][0]["waiting_reason"] == "No active H2C printers are idle"


@respx.mock
def test_an_output_last_printed_by_a_pipeline_run_still_opens(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """#312: a record written by the old send bar must not become unreadable."""
    configure(client)
    output_id = make_output(client, model)
    path = paths.output_dir(model, output_id) / "meta.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(print_route="pipeline", pipeline_run_id=12, queue_item_id=7, library_file_id=41)
    path.write_text(json.dumps(record), encoding="utf-8")

    detail = client.get(f"/api/v1/outputs/{output_id}")
    assert detail.status_code == 200
    assert "pipeline_run_id" not in detail.json()
    assert detail.json()["library_file_id"] == 41
    assert detail.json()["queue_item_id"] is None
    assert client.get(f"/api/v1/models/{model}/outputs").status_code == 200
    assert client.get(f"/api/v1/print/outputs/{output_id}/progress").json() is None
