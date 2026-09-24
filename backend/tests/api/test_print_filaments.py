"""Issue #87 — the filament step, and the route it forces, through the real app.

The GET bodies are the recordings. The slice job, the queue item and the pipeline run
are built inline from Bambuddy's own ``openapi.json``, because posting to the live
instance was out of bounds (see ``tests/bambuddy/recordings/README.md``).

The assertions that matter most are on the **outgoing** queue body: ``ams_mapping``,
``filament_overrides`` and ``required_filament_types`` exist on no other Bambuddy call,
so getting them onto the wire is the whole point of the slice-and-queue route.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.api.test_print import pipelines_route, presets_routes, printers_route
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


def prepared(client: TestClient, model: str) -> str:
    configure(client)
    return make_output(client, model)


@respx.mock
def test_the_filament_step_answers_with_the_inventory_and_a_suggestion(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    pipelines_route()
    presets_routes()
    inventory_routes()

    response = client.get(f"/api/v1/print/outputs/{output_id}/filaments?printer_id=1")
    assert response.status_code == 200
    body = response.json()
    assert body["printer_name"] is not None
    assert [slot["slot_id"] for slot in body["slots"]] == [1, 2]
    # Every spool in the inventory is offered, not only the loaded ones — that is the
    # whole point of #87's "inventory, not just what is loaded".
    assert len(body["spools"]) == len(recording("inventory-spools.json"))
    assert {choice["slot_id"] for choice in body["suggested"]} == {1, 2}
    loaded = [row for row in body["spools"] if row["loaded"]]
    # Where it is, as a label — not an address. No tray number is computed here.
    assert loaded and loaded[0]["loaded"]["printer_id"] == 1
    assert "global_tray_id" not in loaded[0]["loaded"]


@respx.mock
def test_without_a_printer_the_spools_are_still_listed(client: TestClient, model: str) -> None:
    """The inventory does not need a printer; only the reconciled weights do."""
    output_id = prepared(client, model)
    upload_route()
    pipelines_route()
    presets_routes()
    inventory_routes(printer_id=None)

    body = client.get(f"/api/v1/print/outputs/{output_id}/filaments").json()
    assert body["printer_id"] is None
    assert body["spools"]
    assert all(row["loaded"] is None or row["loaded"]["printer_id"] for row in body["spools"])


@respx.mock
def test_a_run_without_a_plan_still_runs_the_pipeline(client: TestClient, model: str) -> None:
    """#86's behaviour is unchanged: no plan, no escalation, and Bambuddy still fans a
    class-targeted pipeline out by its own strategy."""
    from tests.api.test_print import run_body

    output_id = prepared(client, model)
    upload_route()
    ran = respx.post(f"{API}/slicer-pipelines/1/run").mock(
        return_value=httpx.Response(200, json=run_body())
    )
    queued = queue_route()

    body = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json={"pipeline_id": 1, "copies": 2}
    ).json()
    assert body["route"] == "pipeline"
    assert body["run"]["id"] == 12
    assert ran.called
    assert not queued.called


@respx.mock
def test_a_plan_is_sliced_and_queued_with_the_mapping_on_the_wire(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    pipelines_route()
    printers_route()
    presets_routes()
    inventory_routes()
    sliced = slice_routes()
    queued = queue_route()
    ran = respx.post(f"{API}/slicer-pipelines/1/run").mock(
        return_value=httpx.Response(500, json={"detail": "the run route must not be used here"})
    )

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json={
            "pipeline_id": 1,
            "copies": 3,
            "printer_id": 1,
            # Spool 9 is loaded in AMS 0 tray 1 (flat tray 1); spool 5 is on the shelf.
            "filament_plan": {
                "slots": [
                    {"slot_id": 1, "spool_id": 9},
                    {"slot_id": 2, "spool_id": 5},
                ]
            },
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["route"] == "slice_queue"
    assert body["run"] is None
    assert body["queue_item_ids"] == [51]
    assert body["slice_job_id"] == 9
    assert body["printer_id"] == 1
    assert not ran.called

    sent: dict[str, Any] = json.loads(queued.calls.last.request.content)
    assert sent["printer_id"] == 1
    assert sent["library_file_id"] == 77
    assert sent["quantity"] == 3
    # No ams_mapping: Bambuddy computes it from the overrides, against the printer it
    # is actually dispatching to and the filament switcher that printer actually has.
    assert sent.get("ams_mapping") is None
    assert sent["required_filament_types"] == ["PETG", "PLA"]
    assert [override["slot_id"] for override in sent["filament_overrides"]] == [1, 2]

    # The slice borrowed the pipeline's own presets and bed type; nothing was invented.
    slice_body: dict[str, Any] = json.loads(sliced.calls.last.request.content)
    assert slice_body["printer_preset"] == {"source": "cloud", "id": "GM041"}
    assert slice_body["bed_type"] == "Textured PEI Plate"
    assert slice_body["filament_colours"] == ["#688197", "#0047BB"]


@respx.mock
def test_the_plan_carries_scadbuddys_own_warnings_back(client: TestClient, model: str) -> None:
    """A spool on the shelf is a legitimate choice; the answer says to load it rather
    than refusing."""
    output_id = prepared(client, model)
    upload_route()
    pipelines_route()
    printers_route()
    presets_routes()
    inventory_routes()
    slice_routes()
    queue_route()

    body = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json={
            "pipeline_id": 1,
            "printer_id": 1,
            "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 5}]},
        },
    ).json()
    assert any(warning["kind"] == "not-loaded" for warning in body["warnings"])


@respx.mock
def test_a_failed_slice_reports_bambuddys_own_words(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    upload_route()
    pipelines_route()
    printers_route()
    presets_routes()
    inventory_routes()
    respx.route(method="POST", path__regex=r"/api/v1/library/files/\d+/slice").mock(
        return_value=httpx.Response(200, json={"job_id": 9, "status": "pending"})
    )
    respx.get(f"{API}/slice-jobs/9").mock(
        return_value=httpx.Response(
            200, json={"id": 9, "status": "failed", "error": "object outside the build plate"}
        )
    )
    queued = queue_route()

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json={
            "pipeline_id": 1,
            "printer_id": 1,
            "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]},
        },
    )
    assert response.status_code == 502
    assert "object outside the build plate" in response.json()["detail"]
    assert not queued.called


@respx.mock
def test_an_unknown_pipeline_says_so_rather_than_slicing_with_nothing(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    respx.get(f"{API}/slicer-pipelines/").mock(
        return_value=httpx.Response(200, json={"pipelines": []})
    )

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json={
            "pipeline_id": 1,
            "printer_id": 1,
            "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]},
        },
    )
    assert response.status_code == 409
    assert "no longer has slicer pipeline 1" in response.json()["detail"]


@pytest.mark.parametrize("plate_id", [0, -1])
def test_a_plate_below_one_is_rejected(client: TestClient, model: str, plate_id: int) -> None:
    """Bambuddy's plates are 1-based; a 0 would slice the wrong plate silently."""
    configure(client)
    response = client.post(
        "/api/v1/print/outputs/0123456789abcdef0123456789abcdef/run",
        json={"pipeline_id": 1, "plate_id": plate_id},
    )
    assert response.status_code == 422
