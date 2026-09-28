"""Issue #83: the plate type and the plate index, chosen in the print picker.

The slice request's ``bed_type`` and ``plate`` and the queue item's ``plate_id`` say
them. ``PrintQueueItemCreate`` has no ``bed_type``: Bambuddy reports it on the queue
item from the sliced file, which is why the slice is where it goes. Assertions are on
the request bodies, because that is all Bambuddy sees.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

import httpx
import respx
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from tests.api.test_print import API
from tests.api.test_print_filaments import queue_route as filament_queue_route
from tests.api.test_print_filaments import slice_routes
from tests.api.test_print_options_picker import queue_route
from tests.api.test_print_run_choices import body as choices_body
from tests.api.test_print_run_choices import run_request, run_routes
from tests.api.test_send import configure, make_output, upload_route
from tests.bambuddy.conftest import recording
from tests.test_bambu3mf import add_plate


def _output_3mf(paths: DataPaths, output_id: str) -> Path:
    [path] = paths.outputs.glob(f"*/{output_id}/model.3mf")
    return path


# --- plate type ---------------------------------------------------------------------


def test_the_plate_type_is_remembered_per_printer(client: TestClient, data_dir: Path) -> None:
    """The plate lives on the machine, not on any one model, so the route takes only a
    printer id (#83) — no pipeline resolves it any more, and ``printer_bed_types`` has
    no model key to begin with, so there is nothing for a model to leak into.

    Verified by reading the persisted file back: a store that ignored the printer id
    would still echo each PUT's own response correctly while sharing one entry between
    printers, and only a real readback of both keys together catches that.
    """
    answer = client.put("/api/v1/print/printers/1/bed-type", json={"bed_type": "Supertack Plate"})
    assert answer.json() == {"printer_id": 1, "bed_type": "Supertack Plate"}
    other = client.put("/api/v1/print/printers/2/bed-type", json={"bed_type": "Cool Plate"})
    assert other.json() == {"printer_id": 2, "bed_type": "Cool Plate"}

    stored = json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))
    assert stored["printer_bed_types"] == {"1": "Supertack Plate", "2": "Cool Plate"}

    cleared = client.put("/api/v1/print/printers/1/bed-type", json={"bed_type": None})
    assert cleared.json() == {"printer_id": 1, "bed_type": None}

    stored = json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))
    assert stored["printer_bed_types"] == {"2": "Cool Plate"}


@respx.mock
def test_a_chosen_plate_type_is_sliced_with_and_queued_on_the_printer(
    client: TestClient, model: str
) -> None:
    configure(client)
    output_id = make_output(client, model)
    upload_route()
    run_routes()
    sliced = slice_routes()
    queue = queue_route()

    result = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json=choices_body(bed_type="Supertack Plate")
    ).json()

    sent = json.loads(sliced.calls.last.request.read())
    # The first layer follows the plate: the slice is where the bed type goes.
    assert sent["bed_type"] == "Supertack Plate"
    assert sent["plate"] == 1
    queued = json.loads(queue.calls.last.request.read())
    assert queued["printer_id"] == 1
    assert "bed_type" not in queued
    assert result["route"] == "slice_queue"


@respx.mock
def test_no_plate_type_slices_on_textured_pei(client: TestClient, model: str) -> None:
    """Textured PEI is the default when the dialog names no plate (spec §4.4)."""
    configure(client)
    output_id = make_output(client, model)
    upload_route()
    run_routes()
    sliced = slice_routes()
    queue_route()

    client.post(f"/api/v1/print/outputs/{output_id}/run", json=choices_body())

    assert json.loads(sliced.calls.last.request.read())["bed_type"] == "Textured PEI Plate"


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
    output_id = make_output(client, model)
    add_plate(_output_3mf(paths, output_id), 2)
    upload_route()
    run_routes()
    sliced = slice_routes()
    queue = queue_route()

    body = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json=run_request(plate_id=2)
    ).json()

    assert json.loads(sliced.calls.last.request.read())["plate"] == 2
    assert json.loads(queue.calls.last.request.read())["plate_id"] == 2
    assert body["route"] == "slice_queue"


@respx.mock
def test_all_plates_are_queued_as_one_item_each(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    configure(client)
    output_id = make_output(client, model)
    add_plate(_output_3mf(paths, output_id), 2)
    upload_route()
    run_routes()
    sliced = slice_routes()
    queue = queue_route()

    body = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json=run_request(all_plates=True, copies=2)
    ).json()

    assert [json.loads(call.request.read())["plate"] for call in sliced.calls] == [1, 2]
    queued = [json.loads(call.request.read()) for call in queue.calls]
    assert [item["plate_id"] for item in queued] == [1, 2]
    assert [item["quantity"] for item in queued] == [2, 2]
    assert len(body["queue_item_ids"]) == 2
    assert body["copies"] == 2


@respx.mock
def test_plates_queued_before_a_later_plate_fails_are_still_recorded(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Plate 1 is on Bambuddy's queue when plate 2's slice fails; the output keeps it."""
    configure(client)
    output_id = make_output(client, model)
    add_plate(_output_3mf(paths, output_id), 2)
    upload_route()
    run_routes()
    respx.post(f"{API}/library/files/41/slice").mock(
        side_effect=[
            httpx.Response(202, json={"job_id": 9, "status": "pending"}),
            httpx.Response(202, json={"job_id": 10, "status": "pending"}),
        ]
    )
    respx.get(f"{API}/slice-jobs/9").mock(
        return_value=httpx.Response(
            200, json={"id": 9, "status": "completed", "result": {"library_file_id": 52}}
        )
    )
    respx.get(f"{API}/slice-jobs/10").mock(
        return_value=httpx.Response(200, json={"id": 10, "status": "failed", "error": "no fit"})
    )
    queue = queue_route()

    answer = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json=run_request(all_plates=True)
    )

    assert answer.status_code == 502
    assert queue.call_count == 1
    meta = json.loads(_output_3mf(paths, output_id).with_name("meta.json").read_text())
    assert meta["queue_item_id"] == 9
    assert meta["print_route"] == "slice_queue"
    assert meta["slice_job_id"] == 9
    assert meta["plates"] == [{"plate_id": 1, "queue_item_id": 9, "slice_job_id": 9}]


@respx.mock
def test_every_plate_of_an_all_plates_print_is_recorded(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """The single ids hold one plate; ``plates`` keeps each plate's queue item and slice."""
    configure(client)
    output_id = make_output(client, model)
    add_plate(_output_3mf(paths, output_id), 2)
    upload_route()
    run_routes()
    respx.post(f"{API}/library/files/41/slice").mock(
        side_effect=[
            httpx.Response(202, json={"job_id": 9, "status": "pending"}),
            httpx.Response(202, json={"job_id": 10, "status": "pending"}),
        ]
    )
    for job_id, sliced_id in ((9, 52), (10, 53)):
        respx.get(f"{API}/slice-jobs/{job_id}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": job_id,
                    "status": "completed",
                    "result": {"library_file_id": sliced_id},
                },
            )
        )
    item = recording("queue-item.json")
    respx.post(f"{API}/queue/").mock(
        side_effect=[
            httpx.Response(200, json={**item, "id": 71}),
            httpx.Response(200, json={**item, "id": 72}),
        ]
    )

    answer = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json=run_request(all_plates=True)
    )

    assert answer.status_code == 200
    meta = client.get(f"/api/v1/outputs/{output_id}").json()
    assert meta["plates"] == [
        {"plate_id": 1, "queue_item_id": 71, "slice_job_id": 9},
        {"plate_id": 2, "queue_item_id": 72, "slice_job_id": 10},
    ]
    assert (meta["queue_item_id"], meta["slice_job_id"]) == (72, 10)


@respx.mock
def test_all_plates_of_a_3mf_that_lists_none_is_refused(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    configure(client)
    output_id = make_output(client, model)
    upload = upload_route()
    _drop_plates(_output_3mf(paths, output_id))
    queue = queue_route()

    answer = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json=run_request(all_plates=True)
    )

    assert answer.status_code == 422
    assert "no plates" in answer.json()["detail"]
    assert not upload.called
    assert not queue.called


def _drop_plates(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    config = entries["Metadata/model_settings.config"].decode("utf-8")
    entries["Metadata/model_settings.config"] = re.sub(
        r" <plate>.*?</plate>\n", "", config, flags=re.DOTALL
    ).encode("utf-8")
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)


def test_a_meta_json_without_plates_still_loads(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    output_id = make_output(client, model)
    path = _output_3mf(paths, output_id).with_name("meta.json")
    raw = json.loads(path.read_text())
    del raw["plates"]
    path.write_text(json.dumps(raw))

    assert client.get(f"/api/v1/outputs/{output_id}").json()["plates"] == []


# --- one filament plan across every plate -------------------------------------------


def _plates_use(used: dict[int, set[int]], grams: float = 0) -> None:
    """Each plate's requirements: the recording's two slots, marked used per plate,
    each needing ``grams`` (``0`` is Bambuddy's "unknown")."""

    def answer(request: httpx.Request) -> httpx.Response:
        body = recording("filament-requirements.json")
        plate = int(request.url.params["plate_id"])
        body["plate_id"] = plate
        for filament in body["filaments"]:
            filament["used_in_plate"] = filament["slot_id"] in used[plate]
            filament["used_grams"] = grams
        return httpx.Response(200, json=body)

    # Registered after `inventory_routes`, so this one answers.
    respx.route(method="GET", path__regex=r"/api/v1/library/files/\d+/filament-requirements").mock(
        side_effect=answer
    )


def _plan_run(
    client: TestClient,
    model: str,
    paths: DataPaths,
    used: dict[int, set[int]],
    grams: float = 0,
    *,
    all_plates: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    configure(client)
    output_id = make_output(client, model)
    add_plate(_output_3mf(paths, output_id), 2)
    upload_route()
    run_routes()
    _plates_use(used, grams)
    slice_routes()
    queue = filament_queue_route()
    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json=run_request(
            all_plates=all_plates,
            # Picked against plate 1, as the picker does for "all plates".
            filament_plan={"slots": [{"slot_id": 1, "spool_id": 9}]},
        ),
    )
    return response.json() | {"status": response.status_code}, [
        json.loads(call.request.read()) for call in queue.calls
    ]


@respx.mock
def test_one_plan_maps_the_same_slot_on_every_plate(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """A slot is a colour-numbered project filament (#180), so plate 2's slot 1 is the
    colour plate 1's slot 1 is, and the spool picked for it prints it there too."""
    body, queued = _plan_run(client, model, paths, {1: {1}, 2: {1}})

    assert len(queued) == 2
    for item in queued:
        [override] = item["filament_overrides"]
        # Spool 9: PETG, #688197.
        assert (override["slot_id"], override["type"], override["color"]) == (
            1,
            "PETG",
            "#688197",
        )
    assert not any(warning["kind"] == "no-choice" for warning in body["warnings"])


@respx.mock
def test_a_slot_only_a_later_plate_uses_is_refused_before_anything_is_queued(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Plate 2 uses slot 2, which the plan (built from plate 1) does not cover. A slot
    with no spool has no filament preset (spec §4.3), and every plate is resolved before
    any is sliced, so plate 1 is not left on the queue on its own."""
    body, queued = _plan_run(client, model, paths, {1: {1}, 2: {1, 2}})

    assert body["status"] == 422
    assert "Plate 2" in body["detail"]
    assert "Slot 2" in body["detail"]
    assert queued == []


@respx.mock
def test_the_filament_check_sums_what_every_plate_needs(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Spool 9 has 1000 g left: each plate's 600 g fits, the run's 1200 g does not (#198)."""
    body, queued = _plan_run(client, model, paths, {1: {1}, 2: {1}}, grams=600)

    assert len(queued) == 2
    [warning] = [warning for warning in body["warnings"] if warning["kind"] == "low-filament"]
    assert warning["slot_id"] == 1
    assert "needs 1200 g" in warning["message"]


@respx.mock
def test_one_plate_short_of_filament_is_still_warned_about(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """The summed check (#198) runs for every planned print, so one plate is its own total."""
    body, queued = _plan_run(client, model, paths, {1: {1}, 2: {1}}, grams=1200, all_plates=False)

    assert len(queued) == 1
    [warning] = [warning for warning in body["warnings"] if warning["kind"] == "low-filament"]
    assert warning["slot_id"] == 1
    assert "needs 1200 g" in warning["message"]


@respx.mock
def test_plates_that_fit_the_spool_together_say_nothing_about_it(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    body, _ = _plan_run(client, model, paths, {1: {1}, 2: {1}}, grams=400)

    assert not any(warning["kind"] == "low-filament" for warning in body["warnings"])
