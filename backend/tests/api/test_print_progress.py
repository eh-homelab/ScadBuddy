"""Issue #89 — GET /api/v1/print/outputs/{id}/progress, through the real app."""

from __future__ import annotations

import json
import time

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, get_print_watcher
from scadbuddy.core.paths import DataPaths
from tests.api.test_print_filaments import queue_route, slice_routes
from tests.api.test_print_run_choices import run_print, run_request, run_routes
from tests.api.test_send import BASE, configure, make_output, upload_route

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
    assert detail.json()["library_files"] == []
    assert detail.json()["queue_item_id"] is None
    assert client.get(f"/api/v1/models/{model}/outputs").status_code == 200
    assert client.get(f"/api/v1/print/outputs/{output_id}/progress").json() is None


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
    # `print_succeed` records the run, then starts the watcher: the run can read
    # `succeeded` a moment before it is watched.
    deadline = time.monotonic() + 10
    while output_id not in state.print_watcher.watching and time.monotonic() < deadline:
        time.sleep(0.05)
    assert output_id in state.print_watcher.watching
