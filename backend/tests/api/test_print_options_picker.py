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

from tests.api.test_print import API, pipelines_route, presets_routes, printers_route, run_body
from tests.api.test_print_filaments import inventory_routes
from tests.api.test_print_filaments import queue_route as plan_queue_route
from tests.api.test_print_filaments import slice_routes as plan_slice_routes
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
    # #148: the queue route reports the copies it queued, not the item count.
    assert body["copies"] == 3
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
def test_a_filament_plan_carries_the_remembered_options_too(client: TestClient, model: str) -> None:
    """#141: the plan route queues with the options as well as the plan's overrides.

    No printer is named, so the per-printer scope keys on the pipeline's own target.
    """
    configure(client)
    remember(client, "global", {"timelapse": False})
    remember(client, "printer", {"bed_levelling": "off"}, key="1")
    pipelines_route()
    printers_route()
    presets_routes()
    inventory_routes()
    output_id = make_output(client, model)
    upload_route()
    run = respx.post(f"{API}/slicer-pipelines/1/run")
    plan_slice_routes()
    queue = plan_queue_route()

    body = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json={
            "pipeline_id": 1,
            "copies": 2,
            "filament_plan": {
                "slots": [
                    {"slot_id": 1, "spool_id": 9},
                    {"slot_id": 2, "spool_id": 5},
                ]
            },
        },
    ).json()

    assert not run.called
    assert body["route"] == "slice_queue"
    queued = json.loads(queue.calls.last.request.read())
    assert (queued["timelapse"], queued["bed_levelling"]) == (False, "off")
    assert queued["quantity"] == 2
    assert [override["slot_id"] for override in queued["filament_overrides"]] == [1, 2]


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


@respx.mock
def test_a_remembered_quantity_reaches_the_pipeline_run(client: TestClient, model: str) -> None:
    """Quantity is the one option a pipeline run carries; an omitted `copies` lets the
    remembered one through, and an explicit one still wins."""
    configure(client)
    remember(client, "global", {"quantity": 3})
    pipelines_route()
    printers_route()
    output_id = make_output(client, model)
    upload_route()
    run = respx.post(f"{API}/slicer-pipelines/1/run").mock(
        return_value=httpx.Response(202, json=run_body())
    )

    remembered = client.post(f"/api/v1/print/outputs/{output_id}/run", json={"pipeline_id": 1})
    assert json.loads(run.calls.last.request.read())["copies"] == 3
    # #148: the result says how many were queued, whichever value won.
    assert remembered.json()["copies"] == 3

    explicit = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json={"pipeline_id": 1, "copies": 2}
    )
    assert json.loads(run.calls.last.request.read())["copies"] == 2
    assert explicit.json()["copies"] == 2


@respx.mock
def test_a_deleted_pipeline_is_a_friendly_conflict_on_the_scope_path(
    client: TestClient, model: str
) -> None:
    """A per-printer option makes the picker read the pipeline before anything else; a
    pipeline Bambuddy no longer has must still be "not configured", not an upstream 404."""
    configure(client)
    remember(client, "printer", {"timelapse": False}, key="1")
    respx.get(f"{API}/slicer-pipelines/").mock(
        return_value=httpx.Response(200, json={"pipelines": []})
    )
    printers_route()
    output_id = make_output(client, model)
    upload_route()

    response = client.post(f"/api/v1/print/outputs/{output_id}/run", json={"pipeline_id": 1})

    assert response.status_code == 409
    assert "no longer has slicer pipeline 1" in response.json()["detail"]


@respx.mock
def test_a_class_pipeline_forced_onto_the_queue_says_it_did_not_fan_out(
    client: TestClient, model: str
) -> None:
    configure(client)
    remember(client, "global", {"timelapse": False})
    body = recording("slicer-pipelines-configured.json")
    for row in body["pipelines"]:
        row.update(target_kind="printer_class", target_printer_id=None, target_model_class="H2C")
    pipelines_route(body)
    printers_route()
    output_id = make_output(client, model)
    upload_route()
    slice_route()
    queue = queue_route()

    result = client.post(f"/api/v1/print/outputs/{output_id}/run", json={"pipeline_id": 1}).json()

    queued = json.loads(queue.calls.last.request.read())
    assert queued["target_model"] == "H2C"
    assert queued.get("printer_id") is None
    [warning] = result["warnings"]
    assert warning["kind"] == "no-fan-out"
    assert "H2C" in warning["message"]
    assert "timelapse" not in warning["message"]


@respx.mock
def test_a_remembered_project_alone_still_runs_the_pipeline(client: TestClient, model: str) -> None:
    """The picker's project comes from its own control, so a remembered project_id
    must neither force the queue route nor be half-applied."""
    configure(client)
    remember(client, "global", {"project_id": 5})
    pipelines_route()
    printers_route()
    output_id = make_output(client, model)
    upload_route()
    run = respx.post(f"{API}/slicer-pipelines/1/run").mock(
        return_value=httpx.Response(202, json=run_body())
    )
    sliced = respx.post(f"{API}/library/files/41/slice")

    body = client.post(f"/api/v1/print/outputs/{output_id}/run", json={"pipeline_id": 1}).json()

    assert run.called
    assert not sliced.called
    assert body["route"] == "pipeline"


@respx.mock
def test_a_remembered_quantity_is_reported_on_the_queue_route(
    client: TestClient, model: str
) -> None:
    """#148: with no Copies box set, the result is the only place the remembered quantity
    that was actually queued shows up after the click."""
    configure(client)
    remember(client, "global", {"timelapse": False, "quantity": 4})
    pipelines_route()
    printers_route()
    output_id = make_output(client, model)
    upload_route()
    slice_route()
    queue = queue_route()

    body = client.post(f"/api/v1/print/outputs/{output_id}/run", json={"pipeline_id": 1}).json()

    assert json.loads(queue.calls.last.request.read())["quantity"] == 4
    assert body["copies"] == 4
