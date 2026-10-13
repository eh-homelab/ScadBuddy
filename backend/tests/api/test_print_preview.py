"""#2169 — the dialog's background slice, and #2164 — recording the spool in a tray.

A background slice goes through the run's own path and is never queued; a run of the
same choices queues that slice rather than slicing again. The tray's spool is recorded
through Bambuddy's ``POST /inventory/assignments`` on the person's own "yes".
"""

from __future__ import annotations

import asyncio
import io
import json
import struct
import time
import zipfile
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.bambuddy import preview
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.preview import _TOOLPATHS, PREVIEW_GRACE, sweep_preview_slices
from scadbuddy.core.paths import DataPaths
from tests.api.test_print_filaments import prepared, queue_route
from tests.api.test_print_run_choices import body, run_print, run_routes, two_plate_output
from tests.api.test_send import BASE, configure, upload_route
from tests.api.test_settings_runtime import _state
from tests.bambuddy.conftest import recording
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres

API = f"{BASE}/api/v1"
SLICED = 77
#: The copy the run uploads (``upload_route``), which the slice job slices.
COPY = 41
CREATED = "2026-10-10T23:40:00.123456"


def start_slice(client: TestClient, output_id: str, json: dict[str, Any]) -> dict[str, Any]:
    """Start the background slice and follow it: the route answers 202 with the running
    operation as soon as it is recorded, never after the slice (#2462), which used to
    hold every start for the whole 10 s command deadline."""
    started = client.post(
        f"/api/v1/print/outputs/{output_id}/preview-slice", json=json, headers=press()
    )
    assert started.status_code == 202, started.text
    op: dict[str, Any] = started.json()
    assert op["status"] == "running"
    deadline = time.monotonic() + 30
    while op["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.1)
        op = client.get(f"/api/v1/operations/{op['id']}").json()
    assert op["status"] == "succeeded", op
    result: dict[str, Any] = op["result"]
    return result


class Job:
    """Bambuddy's slice job 9 as ``GET /slice-jobs/9`` answers it, changeable mid-test
    the way a Bambuddy restart or a deleted file changes it."""

    def __init__(self) -> None:
        self.body: dict[str, object] = {
            "job_id": 9,
            "status": "completed",
            "kind": "library_file",
            "source_id": COPY,
            "created_at": CREATED,
            "result": {
                "library_file_id": SLICED,
                "name": "demo.gcode.3mf",
                "filament_used_g": 12.0,
            },
        }

    def routes(self) -> respx.Route:
        posted = respx.route(method="POST", path__regex=r"/api/v1/library/files/\d+/slice").mock(
            return_value=httpx.Response(200, json={"job_id": 9, "status": "pending"})
        )
        respx.get(f"{API}/slice-jobs/9").mock(
            side_effect=lambda _: httpx.Response(200, json=self.body)
        )
        return posted


def sliced_file_route(status: int = 200, filename: str = "demo.gcode.3mf") -> None:
    respx.get(f"{API}/library/files/{SLICED}").mock(
        return_value=httpx.Response(status, json={"id": SLICED, "filename": filename})
    )


def sliced_routes() -> None:
    """The sliced file: still in the library, and its G-code loading two filaments, then
    the first again."""
    sliced_file_route()
    gcode = b"G28\nM620 S0A\nG1 X1\nM620 S1A\nG1 X2\nM620 S255A\nM620 S0A\n"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("Metadata/plate_1.gcode", gcode)
    respx.get(f"{API}/library/files/{SLICED}/download").mock(
        return_value=httpx.Response(200, content=archive.getvalue())
    )


@respx.mock
def test_a_background_slice_is_read_back_and_a_run_of_the_same_choices_queues_it(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    sliced = Job().routes()
    sliced_routes()
    queued = queue_route()

    started = start_slice(client, output_id, body())

    assert started["job_id"] == 9
    assert sliced.call_count == 1
    assert not queued.called

    preview = client.get(f"/api/v1/print/outputs/{output_id}/preview-slices/9")
    assert preview.status_code == 200, preview.text
    answer = preview.json()
    assert answer["status"] == "completed"
    assert answer["filament_used_g"] == 12.0
    # Loads of 0, 1 and 0 again (255 is an unload): two changes after the first.
    assert answer["filament_changes"] == 2

    response = run_print(client, output_id, json=body())

    assert response.status_code == 200, response.text
    # The run queued the background slice: nothing was sliced again.
    assert sliced.call_count == 1
    assert json.loads(queued.calls.last.request.content)["library_file_id"] == SLICED


@respx.mock
def test_a_background_slice_totals_its_slots_not_bambuddys_first_filament(
    client: TestClient, model: str
) -> None:
    """#2450: Bambuddy's ``filament_used_g`` is the first filament's grams alone, so the
    dialog's "Filament" read less than the slots listed above it."""
    output_id = prepared(client, model)
    slots = [(1, 5.2), (2, 5.6), (3, 0.3)]
    # Registered first, so it answers for the sliced file ahead of the copy's route.
    respx.get(f"{API}/library/files/{SLICED}/filament-requirements").mock(
        return_value=httpx.Response(
            200,
            json={
                "file_id": SLICED,
                "filaments": [
                    {"slot_id": slot, "used_grams": grams, "used_in_plate": True}
                    for slot, grams in slots
                ],
            },
        )
    )
    upload_route()
    run_routes()
    job = Job()
    job.body["result"] = {**job.body["result"], "filament_used_g": 5.2}  # type: ignore[dict-item]
    job.routes()
    sliced_routes()

    start_slice(client, output_id, body())
    answer = client.get(f"/api/v1/print/outputs/{output_id}/preview-slices/9").json()

    assert [slot["grams"] for slot in answer["slots"]] == [5.2, 5.6, 0.3]
    assert answer["filament_used_g"] == pytest.approx(11.1)


@respx.mock
def test_a_background_slice_with_a_project_chosen_files_nothing_in_the_project(
    client: TestClient, model: str
) -> None:
    """The dialog slices on every change; a copy in a project's folder is that project's
    record and is never removed (#317), so the preview's copy goes to the inbox."""
    output_id = prepared(client, model)
    folder = respx.get(f"{API}/library/folders/by-project/7").mock(
        return_value=httpx.Response(200, json=[{"id": 9, "name": "Kids' room", "project_id": 7}])
    )
    upload = upload_route()
    run_routes()
    Job().routes()
    sliced_routes()

    start_slice(client, output_id, {**body(), "project_id": 7})

    assert upload.call_count == 1
    assert upload.calls.last.request.url.params.get("folder_id") != "9"
    assert not folder.called


@respx.mock
def test_an_unknown_background_slice_is_a_404(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    assert client.get(f"/api/v1/print/outputs/{output_id}/preview-slices/12345").status_code == 404
    # Nor is one read through another source.
    assert client.get("/api/v1/print/library/5/preview-slices/12345").status_code == 404


@pytest.fixture(autouse=True)
def _no_toolpaths_kept() -> Iterator[None]:
    """Toolpaths are kept per process by the slice's row id, which each test's fresh
    database hands out again."""
    _TOOLPATHS.clear()
    yield
    _TOOLPATHS.clear()


TOOLPATH_GCODE = (
    b"G28\nM83\n; CHANGE_LAYER\n; Z_HEIGHT: 0.2\nG1 Z0.2 F600\n; FEATURE: Outer wall\n"
    b"G1 X10 Y0 E1 F3000\nG1 X10 Y10 E1\n"
)


def _toolpath_slice(
    client: TestClient, model: str, gcode: bytes = TOOLPATH_GCODE
) -> tuple[str, Job, respx.Route]:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    job = Job()
    job.routes()
    sliced_file_route()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("Metadata/plate_1.gcode", gcode)
    download = respx.get(f"{API}/library/files/{SLICED}/download").mock(
        return_value=httpx.Response(200, content=archive.getvalue())
    )
    start_slice(client, output_id, body())
    return output_id, job, download


@respx.mock
def test_a_finished_background_slice_answers_its_toolpath_read_once(
    client: TestClient, model: str
) -> None:
    """#2072: the plate's G-code as segments, for the preview's Speed, Movement and
    Layers views; read from the sliced file once, then from memory."""
    output_id, _, download = _toolpath_slice(client, model)
    url = f"/api/v1/print/outputs/{output_id}/preview-slices/9/toolpath"

    first = client.get(url)

    assert first.status_code == 200, first.text
    assert first.headers["content-type"] == "application/octet-stream"
    (size,) = struct.unpack_from("<I", first.content)
    header = json.loads(first.content[4 : 4 + size])
    assert header["features"] == ["Outer wall"]
    assert header["layers"] == [{"z": 0.2, "first_segment": 0}]
    # The Z move, then the two walls.
    assert header["segment_count"] == 3
    assert len(first.content) == 4 + size + 3 * 32
    calls = download.call_count
    assert client.get(url).content == first.content
    assert download.call_count == calls, "read once"


@respx.mock
def test_a_toolpath_waits_for_the_slice_and_needs_its_g_code(
    client: TestClient, model: str
) -> None:
    output_id, job, _ = _toolpath_slice(client, model, gcode=b"")
    url = f"/api/v1/print/outputs/{output_id}/preview-slices/9/toolpath"
    job.body = {**job.body, "status": "running", "result": None}
    assert client.get(url).status_code == 409

    job.body = Job().body
    respx.get(f"{API}/library/files/{SLICED}/download").mock(
        return_value=httpx.Response(200, content=b"not a zip")
    )
    refused = client.get(url)
    assert refused.status_code == 422, refused.text

    assert (
        client.get(f"/api/v1/print/outputs/{output_id}/preview-slices/123/toolpath").status_code
        == 404
    )
    assert client.get("/api/v1/print/library/5/preview-slices/9/toolpath").status_code == 404


@respx.mock
def test_a_toolpath_read_with_no_turn_free_is_told_to_come_back(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reads are bounded: one that cannot get a turn soon is a 503, not a queue."""
    output_id, _, _ = _toolpath_slice(client, model)
    monkeypatch.setattr(preview, "TOOLPATH_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(preview, "_READING_TOOLPATHS", asyncio.Semaphore(0))

    busy = client.get(f"/api/v1/print/outputs/{output_id}/preview-slices/9/toolpath")

    assert busy.status_code == 503, busy.text


def _preview_then_change(client: TestClient, model: str, change: str) -> tuple[str, respx.Route]:
    """A background slice, then Bambuddy's job 9 or its file changed by ``change``."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    job = Job()
    sliced = job.routes()
    sliced_routes()
    queue_route()
    start_slice(client, output_id, body())
    if change == "another-file":
        job.body = {**job.body, "source_id": 999}
    elif change == "restart":
        # Bambuddy restarted: job 9 is a new job, of the same copy, created since.
        job.body = {**job.body, "created_at": "2026-10-11T08:00:00.000001"}
    elif change == "deleted":
        sliced_file_route(status=404)
    elif change == "replaced":
        sliced_file_route(filename="someone-else.gcode.3mf")
    return output_id, sliced


@respx.mock
@pytest.mark.parametrize("change", ["another-file", "restart", "deleted", "replaced"])
def test_a_job_that_no_longer_is_the_background_slice_is_sliced_again(
    client: TestClient, model: str, change: str
) -> None:
    """Bambuddy's slice jobs live in its memory and their ids restart from 1, and a
    sliced file can be deleted: a recorded job is queued only while it is still the
    slice shown."""
    output_id, sliced = _preview_then_change(client, model, change)

    run_print(client, output_id, json=body())

    # Sliced again rather than queueing the recorded job.
    assert sliced.call_count == 2
    # Nor is it read back as this output's slice: the stale row is retired.
    read = client.get(f"/api/v1/print/outputs/{output_id}/preview-slices/9")
    assert read.status_code == 404, read.text


@respx.mock
def test_yes_records_the_spool_in_the_tray(client: TestClient) -> None:
    configure(client)
    assigned = respx.post(f"{API}/inventory/assignments").mock(
        return_value=httpx.Response(
            200,
            json={
                **recording("inventory-assignments.json")[0],
                "spool_id": 16,
                "ams_id": 3,
                "tray_id": 3,
            },
        )
    )

    response = client.post(
        "/api/v1/print/printers/1/trays/3/3/spool", json={"spool_id": 16}, headers=press()
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"spool_id": 16, "printer_id": 1, "ams_id": 3, "tray_id": 3}
    assert json.loads(assigned.calls.last.request.content) == {
        "spool_id": 16,
        "printer_id": 1,
        "ams_id": 3,
        "tray_id": 3,
    }


@respx.mock
def test_a_key_without_manage_inventory_is_named(client: TestClient) -> None:
    configure(client)
    respx.post(f"{API}/inventory/assignments").mock(
        return_value=httpx.Response(403, json={"detail": "Missing permission"})
    )

    response = client.post(
        "/api/v1/print/printers/1/trays/3/3/spool", json={"spool_id": 16}, headers=press()
    )

    assert response.status_code >= 400
    assert "Manage Inventory" in response.text


def test_a_plate_clicked_in_the_dialog_is_previewed_on_its_own(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """#2169: the pane shows the plate clicked, read from the output's 3MF."""
    output_id = two_plate_output(client, model, paths)

    # ``add_plate`` adds a plate with nothing on it, so plate 1 holds the model.
    plate = client.get(f"/api/v1/outputs/{output_id}/preview.glb?plate=1")
    assert plate.status_code == 200, plate.text
    assert plate.headers["content-type"] == "model/gltf-binary"
    assert plate.content[:4] == b"glTF"
    assert client.get(f"/api/v1/outputs/{output_id}/preview.glb?plate=2").status_code == 422
    assert client.get(f"/api/v1/outputs/{output_id}/preview.glb?plate=9").status_code == 404


def _sweep(client: TestClient, grace: timedelta = PREVIEW_GRACE) -> int:
    state = _state(client)
    settings = state.settings_store.load()

    async def sweep() -> int:
        async with client_for(settings) as bambuddy:
            return await sweep_preview_slices(bambuddy, state.uploads, grace=grace)

    return asyncio.run(sweep())


def _previewed(client: TestClient, model: str) -> tuple[str, Job, respx.Route]:
    """A background slice of job 9, its sliced file 77 readable and deletable."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    job = Job()
    job.routes()
    sliced_routes()
    queue_route()
    deleted = respx.delete(f"{API}/library/files/{SLICED}").mock(
        return_value=httpx.Response(200, json={"trashed": True})
    )
    start_slice(client, output_id, body())
    return output_id, job, deleted


@respx.mock
def test_a_background_slice_no_run_queued_is_removed_after_its_grace(
    client: TestClient, model: str
) -> None:
    """Every settled change slices in Bambuddy's library; one never printed is removed
    once past its grace, with its row, and one within it is kept."""
    output_id, _, deleted = _previewed(client, model)

    assert _sweep(client) == 0
    assert not deleted.called

    assert _sweep(client, grace=timedelta(0)) == 1
    assert deleted.call_count == 1
    assert client.get(f"/api/v1/print/outputs/{output_id}/preview-slices/9").status_code == 404
    assert _sweep(client, grace=timedelta(0)) == 0


@respx.mock
def test_a_background_slice_a_run_queued_is_left_to_the_print(
    client: TestClient, model: str
) -> None:
    output_id, _, deleted = _previewed(client, model)
    assert run_print(client, output_id, json=body()).status_code == 200

    assert _sweep(client, grace=timedelta(0)) == 0
    assert not deleted.called


@respx.mock
def test_a_retired_background_slice_is_removed_at_once_while_its_file_is_its_own(
    client: TestClient, model: str
) -> None:
    """A Bambuddy restart retires the row; its sliced file, still under the name the job
    gave it, goes on the next sweep."""
    output_id, job, deleted = _previewed(client, model)
    read = f"/api/v1/print/outputs/{output_id}/preview-slices/9"
    assert client.get(read).status_code == 200  # learns the sliced file and its name
    job.body = {**job.body, "created_at": "2026-10-11T08:00:00.000001"}
    assert client.get(read).status_code == 404

    assert _sweep(client) == 1
    assert deleted.call_count == 1


@respx.mock
def test_a_sliced_file_replaced_under_its_id_is_never_removed(
    client: TestClient, model: str
) -> None:
    output_id, _, deleted = _previewed(client, model)
    read = f"/api/v1/print/outputs/{output_id}/preview-slices/9"
    assert client.get(read).status_code == 200
    sliced_file_route(filename="someone-else.gcode.3mf")
    assert client.get(read).status_code == 404

    assert _sweep(client) == 0
    assert not deleted.called
