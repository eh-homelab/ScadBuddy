"""Issue #87 — the filament step, through the real app.

The GET bodies are the recordings. The slice job and the queue item the helpers below
answer with are built from Bambuddy's own ``openapi.json``, because posting to the live
instance was out of bounds (see ``tests/bambuddy/recordings/README.md``).

The run that queues the chosen spools is tested in ``test_print_run_choices.py``, which
uses these helpers.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.bambuddy import send
from scadbuddy.bambuddy.print_source import OutputSource
from tests.api.test_print import presets_routes
from tests.api.test_send import BASE, configure, make_output, upload_route
from tests.bambuddy.conftest import recording

API = f"{BASE}/api/v1"


def inventory_routes(*, printer_id: int | None = 1) -> None:
    respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(200, json=recording("inventory-spools.json"))
    )
    respx.get(f"{API}/inventory/assignments").mock(
        return_value=httpx.Response(200, json=recording("inventory-assignments.json"))
    )
    respx.route(method="GET", path__regex=r"/api/v1/library/files/\d+/filament-requirements").mock(
        return_value=httpx.Response(200, json=recording("filament-requirements.json"))
    )
    if printer_id is not None:
        respx.get(f"{API}/printers/{printer_id}").mock(
            return_value=httpx.Response(200, json=recording("printer.json"))
        )
        respx.get(f"{API}/printers/{printer_id}/inventory-remain").mock(
            return_value=httpx.Response(200, json=recording("inventory-remain.json"))
        )


def slice_routes(*, sliced_id: int = 77, job_id: int = 9) -> respx.Route:
    """``SliceJobAccepted`` then a finished ``SliceJob``, as Bambuddy's schema declares."""
    posted = respx.route(method="POST", path__regex=r"/api/v1/library/files/\d+/slice").mock(
        return_value=httpx.Response(200, json={"job_id": job_id, "status": "pending"})
    )
    respx.get(f"{API}/slice-jobs/{job_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": job_id,
                "status": "completed",
                "result": {"library_file_id": sliced_id, "filament_used_g": 12.0},
            },
        )
    )
    return posted


def queue_route(item_id: int = 51) -> respx.Route:
    return respx.post(f"{API}/queue/").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": item_id,
                "printer_id": 1,
                "library_file_id": 77,
                "position": 1,
                "status": "queued",
                "plate_id": 1,
            },
        )
    )


def slots_from_bambuddy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The output's 3MF read as one its slots cannot be read from (#2720), so the slots
    come from Bambuddy's ``filament-requirements``, which the test answers."""

    async def unreadable(self: OutputSource) -> None:
        return None

    monkeypatch.setattr(OutputSource, "read_local", unreadable)


def prepared(client: TestClient, model: str) -> str:
    configure(client)
    return make_output(client, model)


@pytest.mark.requires_postgres
@respx.mock
def test_the_filament_step_answers_with_the_inventory_and_a_suggestion(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    slots_from_bambuddy(monkeypatch)
    output_id = prepared(client, model)
    upload_route()
    presets_routes()
    inventory_routes()
    nozzle_routes()

    response = client.get(f"/api/v1/print/outputs/{output_id}/filaments?printer_id=1")
    assert response.status_code == 200
    body = response.json()
    assert body["printer_name"] is not None
    assert [slot["slot_id"] for slot in body["slots"]] == [1, 2]
    # Every spool in the inventory is offered, not only the loaded ones — that is the
    # whole point of #87's "inventory, not just what is loaded".
    # Every spool, and each filled tray Bambuddy has no spool for, as itself (#2164).
    spools = [spool for spool in body["spools"] if not spool.get("tray_only")]
    assert len(spools) == len(recording("inventory-spools.json"))
    assert {choice["slot_id"] for choice in body["suggested"]} == {1, 2}
    loaded = [row for row in body["spools"] if row["loaded"]]
    # Where it is, as a label — not an address. No tray number is computed here.
    assert loaded and loaded[0]["loaded"]["printer_id"] == 1
    assert "global_tray_id" not in loaded[0]["loaded"]


@pytest.mark.requires_postgres
@respx.mock
def test_without_a_printer_the_spools_are_still_listed(client: TestClient, model: str) -> None:
    """The inventory does not need a printer; only the reconciled weights do."""
    output_id = prepared(client, model)
    upload_route()
    presets_routes()
    inventory_routes(printer_id=None)

    body = client.get(f"/api/v1/print/outputs/{output_id}/filaments").json()
    assert body["printer_id"] is None
    assert body["spools"]
    assert all(row["loaded"] is None or row["loaded"]["printer_id"] for row in body["spools"])


def nozzle_routes(*diameters: str) -> respx.Route:
    status = recording("printer-status.json")
    if diameters:
        status["nozzles"] = [
            {"nozzle_type": "HS00", "nozzle_diameter": diameter} for diameter in diameters
        ]
    return respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(200, json=status))


@pytest.mark.requires_postgres
@respx.mock
def test_the_filament_step_shows_the_mounted_nozzles(client: TestClient, model: str) -> None:
    """#78 — the recorded H2C carries a 0.2 and a 0.4.

    No pipeline nozzle is compared against any more (the dialog's nozzle step owns the
    nozzle), so neither a pipeline nor the ~4000-row preset catalogue is read here.
    """
    output_id = prepared(client, model)
    upload_route()
    local = respx.get(f"{API}/local-presets/")
    pipeline = respx.get(f"{API}/slicer-pipelines/1")
    inventory_routes()
    nozzle_routes()

    body = client.get(f"/api/v1/print/outputs/{output_id}/filaments?printer_id=1").json()
    assert [nozzle["nozzle_diameter"] for nozzle in body["nozzles"]] == ["0.2", "0.4"]
    # #768: nothing is judged against the rack any more, so its spares aren't sent.
    assert "rack" not in body
    assert "pipeline_nozzle_diameter" not in body
    assert not local.called
    assert not pipeline.called


@pytest.mark.requires_postgres
@respx.mock
def test_an_outputs_filament_step_reads_its_own_3mf_and_uploads_nothing(
    client: TestClient, model: str
) -> None:
    """#2720: the slots came from Bambuddy reading an uploaded copy, and laying that copy
    out and uploading it kept an output's first dialog waiting ~20 s. The output's own
    3MF has the same slots."""
    output_id = prepared(client, model)
    upload = upload_route()
    inventory_routes()
    nozzle_routes()

    response = client.get(f"/api/v1/print/outputs/{output_id}/filaments?printer_id=1")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [slot["slot_id"] for slot in body["slots"]] == [1]
    assert body["library_file_id"] is None
    assert not upload.called
    assert not [
        call for call in respx.calls if call.request.url.path.endswith("/filament-requirements")
    ]


@pytest.mark.requires_postgres
@respx.mock
def test_the_upload_is_laid_out_off_the_event_loop(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2354: an output's first dialog uploads it, and laying the 3MF out for the
    printer held the event loop ~23 s, stalling every other request."""
    on_loop: list[bool] = []
    real = send._laid_out_for

    def watched(payload: bytes, target: send.Target) -> bytes:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real(payload, target)

    monkeypatch.setattr(send, "_laid_out_for", watched)
    slots_from_bambuddy(monkeypatch)
    output_id = prepared(client, model)
    upload_route()
    inventory_routes()
    nozzle_routes()

    response = client.get(f"/api/v1/print/outputs/{output_id}/filaments?printer_id=1")

    assert response.status_code == 200, response.text
    assert on_loop == [False]


@pytest.mark.requires_postgres
@respx.mock
def test_an_unreadable_status_opens_the_filament_step_with_no_nozzles(
    client: TestClient, model: str
) -> None:
    """#2509 review: the filament step reads the status itself when no caller shares
    one, alongside the spools; an offline printer still opens it, with no nozzles."""
    output_id = prepared(client, model)
    upload_route()
    inventory_routes()
    status = respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(503))

    response = client.get(f"/api/v1/print/outputs/{output_id}/filaments?printer_id=1")

    assert response.status_code == 200, response.text
    assert response.json()["nozzles"] == []
    assert response.json()["slots"]
    assert status.call_count == 1


@pytest.mark.requires_postgres
@respx.mock
def test_each_loaded_spool_says_which_extruder_it_feeds(client: TestClient, model: str) -> None:
    """#469 — the recorded H2C has the Filament Track Switch: AMS 0/1 on inlet B (the
    right extruder), AMS 2 on inlet A (the left). A shelf spool has no side."""
    output_id = prepared(client, model)
    upload_route()
    inventory_routes()
    nozzle_routes()

    body = client.get(f"/api/v1/print/outputs/{output_id}/filaments?printer_id=1").json()
    sides = {spool["spool_id"]: (spool["extruder"], spool["side"]) for spool in body["spools"]}
    assert sides[9] == (0, "R")  # AMS 0 tray 1
    assert sides[7] == (0, "R")  # AMS 1 tray 0
    assert sides[10] == (1, "L")  # AMS 2 tray 0
    assert sides[5] == (None, None)  # on the shelf
    # With the switch any AMS reaches either nozzle, so those sides are where each rests.
    assert body["track_switch"] is True


def test_the_filament_step_takes_no_nozzle_diameter(client: TestClient) -> None:
    """Fix round 1 #6 — the pipeline-era ``nozzle_diameter`` query is gone."""
    route = client.get("/openapi.json").json()["paths"][
        "/api/v1/print/outputs/{output_id}/filaments"
    ]["get"]
    assert "nozzle_diameter" not in {param["name"] for param in route["parameters"]}


@pytest.mark.requires_postgres
@respx.mock
def test_a_class_target_with_no_printer_chosen_reads_no_nozzles(
    client: TestClient, model: str
) -> None:
    """No printer, no mounted nozzles to compare against — not an error."""
    output_id = prepared(client, model)
    upload_route()
    presets_routes()
    inventory_routes(printer_id=None)
    status = nozzle_routes()

    body = client.get(f"/api/v1/print/outputs/{output_id}/filaments").json()
    assert body["nozzles"] == []
    assert not status.called
