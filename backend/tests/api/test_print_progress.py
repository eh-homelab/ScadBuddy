"""Issue #89 — GET /api/v1/print/outputs/{id}/progress, through the real app."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from temporalio.client import Client
from temporalio.service import RPCError

from scadbuddy.api import printing
from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.core.paths import DataPaths
from scadbuddy.workflows.follow import follow_id
from tests.api.test_print_filaments import queue_route, slice_routes
from tests.api.test_print_run_choices import run_print, run_request, run_routes
from tests.api.test_send import BASE, configure, make_output, upload_route

API = f"{BASE}/api/v1"


@pytest.fixture
def watched(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The prints the progress route asks to follow, recorded instead (#1053)."""
    asked: list[str] = []

    async def recording(client: object, task_queue: str, output_id: str) -> None:
        asked.append(output_id)

    monkeypatch.setattr(printing, "follow", recording)
    return asked


@respx.mock
def test_an_output_that_has_never_printed_answers_null(
    client: TestClient, model: str, watched: list[str]
) -> None:
    configure(client)
    output_id = make_output(client, model)
    response = client.get(f"/api/v1/print/outputs/{output_id}/progress")
    assert response.status_code == 200
    assert response.json() is None
    assert watched == []


@pytest.mark.requires_postgres
@respx.mock
def test_the_slice_and_queue_route_reports_through_the_same_shape(
    client: TestClient, model: str, watched: list[str]
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
    body = client.get(f"/api/v1/print/outputs/{output_id}/progress").json()
    # Not settled: the read makes sure the backend follows it (#268).
    # The route asks in the background, so its read never waits on Temporal.
    deadline = time.monotonic() + 5
    while not watched and time.monotonic() < deadline:
        time.sleep(0.02)
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
def test_a_run_starts_its_follow(client: TestClient, model: str) -> None:
    """#268, #1053: the print is followed on Temporal from the moment it starts."""
    configure(client)
    output_id = make_output(client, model)
    upload_route()
    run_routes()
    slice_routes()
    queue_route()
    ran = run_print(client, output_id, json=run_request())
    assert ran.status_code == 200, ran.text

    state = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]

    async def described() -> str:
        temporal = await Client.connect(
            state.settings.temporal_address, namespace=state.settings.temporal_namespace
        )
        handle = temporal.get_workflow_handle(follow_id(output_id))
        # Started just after the run recorded its success, in the workflow's next task.
        async with asyncio.timeout(10):
            while True:
                try:
                    return (await handle.describe()).workflow_type
                except RPCError:
                    await asyncio.sleep(0.1)

    assert asyncio.run(described()) == "FollowPrint"
