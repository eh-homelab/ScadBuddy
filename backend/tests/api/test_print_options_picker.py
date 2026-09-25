"""Issue #124: the print picker honours remembered print options.

A pipeline run cannot carry any queue option, so a remembered option the run route
cannot express moves the picker onto slice-and-queue with the pipeline's own presets,
the same rule the send bar follows (#88). Assertions are on the request bodies,
because that is all Bambuddy sees.
"""

from __future__ import annotations

import json

import httpx
import respx
from fastapi.testclient import TestClient

from tests.api.test_print import API, pipelines_route, printers_route, run_body
from tests.api.test_send import configure, make_output, upload_route
from tests.api.test_send_options import queue_route, remember
from tests.bambuddy.conftest import recording


def slice_route() -> respx.Route:
    route = respx.post(f"{API}/library/files/41/slice").mock(
        return_value=httpx.Response(202, json={"job_id": 9, "status": "pending"})
    )
    respx.get(f"{API}/slice-jobs/9").mock(
        return_value=httpx.Response(
            200, json={"id": 9, "status": "completed", "result": {"library_file_id": 52}}
        )
    )
    return route


@respx.mock
def test_a_remembered_option_moves_the_picker_onto_slice_and_queue(
    client: TestClient, model: str
) -> None:
    configure(client)
    remember(client, "global", {"timelapse": False, "bed_levelling": "off"})
    pipelines_route()
    printers_route()
    output_id = make_output(client, model)
    upload_route()
    run = respx.post(f"{API}/slicer-pipelines/1/run")
    sliced = slice_route()
    queue = queue_route()

    body = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json={"pipeline_id": 1, "copies": 3}
    ).json()

    assert not run.called
    sent = json.loads(sliced.calls.last.request.read())
    # The pipeline's own presets: nothing about what gets sliced changes.
    assert sent["printer_preset"] == {"source": "cloud", "id": "GM041"}
    assert sent["filament_presets"] == [{"source": "cloud", "id": "GFSG00_23"}]
    queued = json.loads(queue.calls.last.request.read())
    assert (queued["timelapse"], queued["bed_levelling"]) == (False, "off")
    # The picker's Copies box is this request's quantity.
    assert queued["quantity"] == 3
    # The pipeline's own target, since the picker named no printer.
    assert queued["printer_id"] == 1
    assert body["route"] == "slice_queue"
    record = client.get(f"/api/v1/outputs/{output_id}").json()
    assert record["queue_item_id"] == body["queue_item_ids"][0]


@respx.mock
def test_a_per_printer_option_applies_to_the_pipelines_printer(
    client: TestClient, model: str
) -> None:
    configure(client)
    remember(client, "printer", {"timelapse": False}, key="1")
    pipelines_route()
    printers_route()
    respx.get(f"{API}/slicer-pipelines/1").mock(
        return_value=httpx.Response(200, json=_pipeline_one())
    )
    output_id = make_output(client, model)
    upload_route()
    slice_route()
    queue = queue_route()

    client.post(f"/api/v1/print/outputs/{output_id}/run", json={"pipeline_id": 1})

    assert json.loads(queue.calls.last.request.read())["timelapse"] is False


@respx.mock
def test_no_remembered_option_still_runs_the_pipeline(client: TestClient, model: str) -> None:
    configure(client)
    pipelines_route()
    printers_route()
    output_id = make_output(client, model)
    upload_route()
    run = respx.post(f"{API}/slicer-pipelines/1/run").mock(
        return_value=httpx.Response(202, json=run_body())
    )
    sliced = respx.post(f"{API}/library/files/41/slice")

    body = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json={"pipeline_id": 1, "copies": 2}
    ).json()

    assert run.called
    assert not sliced.called
    assert body["route"] == "pipeline"


@respx.mock
def test_a_per_model_option_applies_to_the_picker(client: TestClient, model: str) -> None:
    configure(client)
    remember(client, "global", {"timelapse": True})
    remember(client, "model", {"timelapse": False}, key=model)
    pipelines_route()
    printers_route()
    output_id = make_output(client, model)
    upload_route()
    slice_route()
    queue = queue_route()

    client.post(f"/api/v1/print/outputs/{output_id}/run", json={"pipeline_id": 1})

    # The model's own choice beats the global one.
    assert json.loads(queue.calls.last.request.read())["timelapse"] is False


@respx.mock
def test_a_named_printer_scopes_the_options_and_takes_the_queue_item(
    client: TestClient, model: str
) -> None:
    configure(client)
    remember(client, "printer", {"timelapse": False}, key="7")
    pipelines_route()
    printers_route()
    output_id = make_output(client, model)
    upload_route()
    run = respx.post(f"{API}/slicer-pipelines/1/run")
    slice_route()
    queue = queue_route()

    body = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json={"pipeline_id": 1, "printer_id": 7}
    ).json()

    assert not run.called
    queued = json.loads(queue.calls.last.request.read())
    # Printer 7's remembered option applies, and the item goes to printer 7 rather
    # than the pipeline's own target (printer 1).
    assert queued["timelapse"] is False
    assert queued["printer_id"] == 7
    assert body["printer_id"] == 7


def _pipeline_one() -> dict[str, object]:
    pipelines = recording("slicer-pipelines-configured.json")["pipelines"]
    return next(row for row in pipelines if row["id"] == 1)
