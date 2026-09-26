"""Issue #83: the plate type and the plate index, chosen in the print picker.

Neither rides on a pipeline run — ``PipelineRunCreateRequest`` carries a source,
``copies`` and ``force`` and nothing else — so either one moves the print onto
slice-and-queue, where the slice request's ``bed_type`` and ``plate`` and the queue
item's ``plate_id`` say it. ``PrintQueueItemCreate`` has no ``bed_type``: Bambuddy
reports it on the queue item from the sliced file, which is why the slice is where it
goes. Assertions are on the request bodies, because that is all Bambuddy sees.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import respx
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from tests.api.test_print import API, pipelines_route, presets_routes, printers_route
from tests.api.test_print_options_picker import slice_route
from tests.api.test_send import configure, make_output, upload_route
from tests.api.test_send_options import queue_route, remember
from tests.bambuddy.conftest import recording
from tests.test_bambu3mf import add_plate

H2C_BED_TYPES = [
    {"value": "Engineering Plate", "label": "Engineering Plate"},
    {"value": "Textured PEI Plate", "label": "Textured PEI Plate"},
    {"value": "Supertack Plate", "label": "Bambu Cool Plate SuperTack"},
]


def _class_pipeline(model_class: str) -> dict[str, Any]:
    body = recording("slicer-pipelines-configured.json")
    for row in body["pipelines"]:
        row.update(
            target_kind="printer_class", target_printer_id=None, target_model_class=model_class
        )
    return dict(body)


def _output_3mf(paths: DataPaths, output_id: str) -> Path:
    [path] = paths.outputs.glob(f"*/{output_id}/model.3mf")
    return path


# --- plate type ---------------------------------------------------------------------


@respx.mock
def test_a_pipeline_offers_the_bed_types_its_printer_takes(client: TestClient, model: str) -> None:
    """The recorded pipeline targets printer 1, an H2C, whose Bambu Studio profile
    refuses the Cool and the Smooth PEI / High Temp plates."""
    configure(client)
    pipelines_route()
    printers_route()
    presets_routes()

    pipeline = client.get(f"/api/v1/print/models/{model}/pipelines").json()["pipelines"][0]

    assert pipeline["bed_types"] == H2C_BED_TYPES


@respx.mock
def test_a_class_pipeline_offers_the_bed_types_of_its_class(client: TestClient, model: str) -> None:
    configure(client)
    pipelines_route(_class_pipeline("P1S"))
    printers_route()
    presets_routes()

    pipeline = client.get(f"/api/v1/print/models/{model}/pipelines").json()["pipelines"][0]

    assert [entry["value"] for entry in pipeline["bed_types"]] == [
        "Cool Plate",
        "Engineering Plate",
        "High Temp Plate",
        "Textured PEI Plate",
        "Supertack Plate",
    ]


@respx.mock
def test_the_plate_type_is_remembered_per_printer(client: TestClient, model: str) -> None:
    configure(client)
    pipelines_route()
    printers_route()
    presets_routes()

    answer = client.put("/api/v1/print/printers/1/bed-type", json={"bed_type": "Supertack Plate"})

    assert answer.json() == {"printer_id": 1, "bed_type": "Supertack Plate"}
    body = client.get(f"/api/v1/print/models/{model}/pipelines").json()
    # Per printer, not per model: the plate is on the machine, whatever is printed on it.
    assert body["printer_bed_types"] == {"1": "Supertack Plate"}
    other = client.get("/api/v1/print/models/some-other-model/pipelines").json()
    assert other["printer_bed_types"] == {"1": "Supertack Plate"}

    client.put("/api/v1/print/printers/1/bed-type", json={"bed_type": None})

    body = client.get(f"/api/v1/print/models/{model}/pipelines").json()
    assert body["printer_bed_types"] == {}


@respx.mock
def test_a_chosen_plate_type_is_sliced_with_and_queued_on_the_printer(
    client: TestClient, model: str
) -> None:
    configure(client)
    pipelines_route()
    printers_route()
    output_id = make_output(client, model)
    upload_route()
    run = respx.post(f"{API}/slicer-pipelines/1/run")
    sliced = slice_route()
    queue = queue_route()

    body = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json={"pipeline_id": 1, "printer_id": 1, "bed_type": "Supertack Plate"},
    ).json()

    assert not run.called
    sent = json.loads(sliced.calls.last.request.read())
    # The first layer follows the plate: the slice is where the bed type goes.
    assert sent["bed_type"] == "Supertack Plate"
    # Everything else is still the pipeline's own.
    assert sent["printer_preset"] == {"source": "cloud", "id": "GM041"}
    assert sent["plate"] == 1
    queued = json.loads(queue.calls.last.request.read())
    assert queued["printer_id"] == 1
    assert "bed_type" not in queued
    assert body["route"] == "slice_queue"
    assert body["warnings"] == []


@respx.mock
def test_no_plate_type_slices_with_the_pipelines_own(client: TestClient, model: str) -> None:
    """A remembered option forces the queue route; the bed type is then the pipeline's."""
    configure(client)
    remember(client, "global", {"timelapse": False})
    pipelines_route()
    printers_route()
    output_id = make_output(client, model)
    upload_route()
    sliced = slice_route()
    queue_route()

    client.post(f"/api/v1/print/outputs/{output_id}/run", json={"pipeline_id": 1})

    assert json.loads(sliced.calls.last.request.read())["bed_type"] == "Textured PEI Plate"


@respx.mock
def test_a_plate_type_on_a_class_pipeline_says_it_did_not_fan_out(
    client: TestClient, model: str
) -> None:
    configure(client)
    pipelines_route(_class_pipeline("H2C"))
    printers_route()
    output_id = make_output(client, model)
    upload_route()
    slice_route()
    queue = queue_route()

    result = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json={"pipeline_id": 1, "bed_type": "Engineering Plate"},
    ).json()

    assert json.loads(queue.calls.last.request.read())["target_model"] == "H2C"
    [warning] = result["warnings"]
    assert warning["kind"] == "no-fan-out"
    assert "plate" in warning["message"]
    assert "H2C" in warning["message"]


# --- plate index --------------------------------------------------------------------


@respx.mock
def test_a_scadbuddy_output_is_one_plate(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)

    assert client.get(f"/api/v1/outputs/{output_id}/plates").json() == [
        {"index": 1, "has_thumbnail": False}
    ]


@respx.mock
def test_every_plate_of_a_multi_plate_output_is_listed_with_its_cover(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    configure(client)
    output_id = make_output(client, model)
    add_plate(_output_3mf(paths, output_id), 2, thumbnail=b"\x89PNG plate two")

    assert client.get(f"/api/v1/outputs/{output_id}/plates").json() == [
        {"index": 1, "has_thumbnail": False},
        {"index": 2, "has_thumbnail": True},
    ]
    cover = client.get(f"/api/v1/outputs/{output_id}/plates/2/thumbnail")
    assert cover.headers["content-type"] == "image/png"
    assert cover.content == b"\x89PNG plate two"
    assert client.get(f"/api/v1/outputs/{output_id}/plates/1/thumbnail").status_code == 404


@respx.mock
def test_a_chosen_plate_is_sliced_and_queued_by_its_index(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    configure(client)
    pipelines_route()
    printers_route()
    output_id = make_output(client, model)
    add_plate(_output_3mf(paths, output_id), 2)
    upload_route()
    run = respx.post(f"{API}/slicer-pipelines/1/run")
    sliced = slice_route()
    queue = queue_route()

    body = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json={"pipeline_id": 1, "plate_id": 2}
    ).json()

    # A run slices plate 1 whatever it is asked, so another plate is sliced here.
    assert not run.called
    assert json.loads(sliced.calls.last.request.read())["plate"] == 2
    assert json.loads(queue.calls.last.request.read())["plate_id"] == 2
    assert body["route"] == "slice_queue"


@respx.mock
def test_all_plates_are_queued_as_one_item_each(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    configure(client)
    pipelines_route()
    printers_route()
    output_id = make_output(client, model)
    add_plate(_output_3mf(paths, output_id), 2)
    upload_route()
    sliced = slice_route()
    queue = queue_route()

    body = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json={"pipeline_id": 1, "all_plates": True, "copies": 2},
    ).json()

    assert [json.loads(call.request.read())["plate"] for call in sliced.calls] == [1, 2]
    queued = [json.loads(call.request.read()) for call in queue.calls]
    assert [item["plate_id"] for item in queued] == [1, 2]
    assert [item["quantity"] for item in queued] == [2, 2]
    assert len(body["queue_item_ids"]) == 2
    assert body["copies"] == 2
