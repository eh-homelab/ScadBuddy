"""#2169 — the dialog's background slice, and #2164 — recording the spool in a tray.

A background slice goes through the run's own path and is never queued; a run of the
same choices queues that slice rather than slicing again. The tray's spool is recorded
through Bambuddy's ``POST /inventory/assignments`` on the person's own "yes".
"""

from __future__ import annotations

import io
import json
import zipfile

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.api.test_print_filaments import prepared, queue_route, slice_routes
from tests.api.test_print_run_choices import body, run_print, run_routes
from tests.api.test_send import BASE, configure, upload_route
from tests.bambuddy.conftest import recording
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres

API = f"{BASE}/api/v1"
SLICED = 77


def sliced_routes() -> None:
    """The sliced file: still in the library, and its G-code loading two filaments, then
    the first again."""
    respx.get(f"{API}/library/files/{SLICED}").mock(
        return_value=httpx.Response(200, json={"id": SLICED, "filename": "demo.gcode.3mf"})
    )
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
    sliced = slice_routes(sliced_id=SLICED, job_id=9)
    sliced_routes()
    queued = queue_route()

    started = client.post(
        f"/api/v1/print/outputs/{output_id}/preview-slice", json=body(), headers=press()
    )

    assert started.status_code == 200, started.text
    assert started.json()["job_id"] == 9
    assert sliced.call_count == 1
    assert not queued.called

    preview = client.get("/api/v1/print/preview-slices/9")
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
def test_an_unknown_background_slice_is_a_404(client: TestClient) -> None:
    configure(client)
    assert client.get("/api/v1/print/preview-slices/12345").status_code == 404


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
