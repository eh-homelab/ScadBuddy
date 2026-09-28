"""Issue #89 — GET /api/v1/print/outputs/{id}/progress, through the real app."""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, get_print_watcher
from tests.api.test_print import pipelines_route, printers_route, run_body
from tests.api.test_print_filaments import queue_route, slice_routes
from tests.api.test_print_run_choices import run_print, run_request, run_routes
from tests.api.test_send import BASE, configure, make_output, upload_route
from tests.bambuddy.conftest import recording

API = f"{BASE}/api/v1"


def recording_watcher(client: TestClient) -> list[str]:
    """Swap the app's print watcher for one that records what it is asked to watch."""
    watched: list[str] = []

    class Recording:
        def watch(self, output_id: str) -> None:
            watched.append(output_id)

    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_print_watcher] = Recording
    return watched


@respx.mock
def test_an_output_that_has_never_printed_answers_null(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    watched = recording_watcher(client)
    response = client.get(f"/api/v1/print/outputs/{output_id}/progress")
    assert response.status_code == 200
    assert response.json() is None
    assert watched == []


@pytest.mark.requires_postgres
@respx.mock
def test_a_pipeline_run_is_followed_to_its_queue_entries(client: TestClient, model: str) -> None:
    # The send bar is what still runs a pipeline; the picker slices and queues.
    configure(client, pipeline_id=1)
    output_id = make_output(client, model)
    upload_route()
    # A send resolves the target printer's plate before uploading, to lay the 3MF out
    # on it (#105) — a read of the pipeline list and the printers.
    pipelines_route()
    printers_route()
    respx.post(f"{API}/slicer-pipelines/1/run").mock(
        return_value=httpx.Response(200, json=run_body())
    )
    assert (
        client.post(
            f"/api/v1/outputs/{output_id}/send", json={"mode": "queue", "copies": 2}
        ).status_code
        == 200
    )

    # The run answered 202 with no queue entries; they appear on the next read.
    respx.get(f"{API}/pipeline-runs/12").mock(
        return_value=httpx.Response(
            200,
            json={
                **run_body(),
                "status": "completed",
                "completed_at": "2026-09-24T04:20:00",
                "copies_completed": 2,
                "copies_in_progress": 0,
                "sliced_library_file_id": 52,
                "jobs": [
                    {
                        "id": 31,
                        "pipeline_run_id": 12,
                        "copy_index": 0,
                        "assigned_printer_name": "3DP-31B-598",
                        "queue_entry_id": 71,
                        "status": "completed",
                    },
                    {
                        "id": 32,
                        "pipeline_run_id": 12,
                        "copy_index": 1,
                        "assigned_printer_name": "3DP-31B-598",
                        "queue_entry_id": 72,
                        "status": "completed",
                    },
                ],
            },
        )
    )
    body = client.get(f"/api/v1/print/outputs/{output_id}/progress").json()
    assert body["route"] == "pipeline"
    assert body["settled"] is True
    assert [copy["queue_entry_id"] for copy in body["copies_detail"]] == [71, 72]


@pytest.mark.requires_postgres
@respx.mock
def test_a_run_whose_slice_failed_reports_bambuddys_words_and_the_fix(
    client: TestClient, model: str
) -> None:
    """The recorded run: still ``in_progress`` by its own status, with a slice failure.
    A send bar that waited for the status to move would spin over it forever."""
    configure(client, pipeline_id=1)
    output_id = make_output(client, model)
    upload_route()
    # A send resolves the target printer's plate before uploading, to lay the 3MF out
    # on it (#105) — a read of the pipeline list and the printers.
    pipelines_route()
    printers_route()
    respx.post(f"{API}/slicer-pipelines/1/run").mock(
        return_value=httpx.Response(200, json=run_body(1))
    )
    client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"})
    respx.get(f"{API}/pipeline-runs/1").mock(
        return_value=httpx.Response(200, json=recording("pipeline-run.json"))
    )

    body = client.get(f"/api/v1/print/outputs/{output_id}/progress").json()
    assert body["stage"] == "failed"
    assert body["settled"] is True
    assert "Slice failed" in body["error_message"]
    assert "slice" in body["fix"].lower()


@pytest.mark.requires_postgres
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

    ran = run_print(client, output_id, json=run_request()).json()
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
    watched = recording_watcher(client)
    body = client.get(f"/api/v1/print/outputs/{output_id}/progress").json()
    # Not settled: the read makes sure the backend follows it (#268).
    assert watched == [output_id]
    assert body["route"] == "slice_queue"
    assert body["queue_item_id"] == 51
    assert body["slice_job_id"] == 9
    assert body["settled"] is False
    # Waiting is not failing.
    assert body["error_message"] is None
    assert body["copies_detail"][0]["waiting_reason"] == "No active H2C printers are idle"


@pytest.mark.requires_postgres
@respx.mock
def test_a_send_starts_the_print_watcher(client: TestClient, model: str) -> None:
    configure(client, pipeline_id=1)
    output_id = make_output(client, model)
    upload_route()
    pipelines_route()
    printers_route()
    respx.post(f"{API}/slicer-pipelines/1/run").mock(
        return_value=httpx.Response(200, json=run_body())
    )
    sent = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "queue"})
    assert sent.status_code == 200, sent.text

    state = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    assert output_id in state.print_watcher.watching


@pytest.mark.requires_postgres
@respx.mock
def test_a_run_starts_the_print_watcher(client: TestClient, model: str) -> None:
    """#268: the backend follows the print itself from the moment it starts."""
    configure(client)
    output_id = make_output(client, model)
    upload_route()
    run_routes()
    slice_routes()
    queue_route()
    ran = run_print(client, output_id, json=run_request())
    assert ran.status_code == 200, ran.text

    state = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    assert output_id in state.print_watcher.watching
