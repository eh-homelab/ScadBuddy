"""Issue #124: the print picker honours remembered print options.

The run always slices then queues (spec 2026-09-27 §4), so every remembered option the
queue item can carry rides on it: global → per-printer → per-model → this request's own.
Assertions are on the request bodies, because that is all Bambuddy sees.
"""

from __future__ import annotations

import json

import respx
from fastapi.testclient import TestClient

from tests.api.test_print_filaments import slice_routes
from tests.api.test_print_run_choices import body, run_request, run_routes
from tests.api.test_send import configure, make_output, upload_route
from tests.api.test_send_options import queue_route, remember


def _prepared(client: TestClient, model: str, *, printer_id: int = 1) -> str:
    output_id = make_output(client, model)
    upload_route()
    run_routes(printer_id=printer_id)
    slice_routes()
    return output_id


@respx.mock
def test_remembered_options_ride_on_the_queue_item(client: TestClient, model: str) -> None:
    configure(client)
    remember(client, "global", {"timelapse": False, "bed_levelling": "off"})
    output_id = _prepared(client, model)
    queue = queue_route()

    result = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json=run_request(copies=3)
    ).json()

    queued = json.loads(queue.calls.last.request.read())
    assert (queued["timelapse"], queued["bed_levelling"]) == (False, "off")
    # The picker's Copies box is this request's quantity.
    assert queued["quantity"] == 3
    # #148: the queue route reports the copies it queued, not the item count.
    assert result["copies"] == 3
    assert queued["printer_id"] == 1
    assert result["route"] == "slice_queue"
    record = client.get(f"/api/v1/outputs/{output_id}").json()
    assert record["queue_item_id"] == result["queue_item_ids"][0]


@respx.mock
def test_a_per_printer_option_applies_to_the_chosen_printer(client: TestClient, model: str) -> None:
    configure(client)
    remember(client, "printer", {"timelapse": False}, key="1")
    output_id = _prepared(client, model)
    queue = queue_route()

    client.post(f"/api/v1/print/outputs/{output_id}/run", json=body())

    assert json.loads(queue.calls.last.request.read())["timelapse"] is False


@respx.mock
def test_with_no_printer_named_the_configured_one_prints_with_its_options(
    client: TestClient, model: str
) -> None:
    """#141: the per-printer scope keys on the printer the item goes to, which is the
    configured one when the request names none."""
    configure(client, printer_id=1)
    remember(client, "global", {"timelapse": False})
    remember(client, "printer", {"bed_levelling": "off"}, key="1")
    output_id = _prepared(client, model)
    queue = queue_route()

    result = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json=run_request(printer_id=None, copies=2)
    ).json()

    queued = json.loads(queue.calls.last.request.read())
    assert queued["printer_id"] == 1
    assert (queued["timelapse"], queued["bed_levelling"]) == (False, "off")
    assert queued["quantity"] == 2
    assert [override["slot_id"] for override in queued["filament_overrides"]] == [1, 2]
    assert result["printer_id"] == 1


@respx.mock
def test_a_per_model_option_applies_to_the_picker(client: TestClient, model: str) -> None:
    configure(client)
    remember(client, "global", {"timelapse": True})
    remember(client, "model", {"timelapse": False}, key=model)
    output_id = _prepared(client, model)
    queue = queue_route()

    client.post(f"/api/v1/print/outputs/{output_id}/run", json=body())

    # The model's own choice beats the global one.
    assert json.loads(queue.calls.last.request.read())["timelapse"] is False


@respx.mock
def test_a_named_printer_scopes_the_options_and_takes_the_queue_item(
    client: TestClient, model: str
) -> None:
    configure(client, printer_id=1)
    remember(client, "printer", {"timelapse": False}, key="7")
    output_id = _prepared(client, model, printer_id=7)
    queue = queue_route()

    result = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json=run_request(printer_id=7)
    ).json()

    queued = json.loads(queue.calls.last.request.read())
    # Printer 7's remembered option applies, and the item goes to printer 7 rather
    # than the configured printer 1.
    assert queued["timelapse"] is False
    assert queued["printer_id"] == 7
    assert result["printer_id"] == 7


@respx.mock
def test_a_remembered_quantity_is_queued_unless_copies_is_set(
    client: TestClient, model: str
) -> None:
    """An omitted `copies` lets the remembered quantity through, and an explicit one
    still wins. #148: the result is the only place the queued quantity shows up after
    the click."""
    configure(client)
    remember(client, "global", {"timelapse": False, "quantity": 4})
    output_id = _prepared(client, model)
    queue = queue_route()

    remembered = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json=run_request(copies=None)
    ).json()
    assert json.loads(queue.calls.last.request.read())["quantity"] == 4
    assert remembered["copies"] == 4

    explicit = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json=run_request(copies=2)
    ).json()
    assert json.loads(queue.calls.last.request.read())["quantity"] == 2
    assert explicit["copies"] == 2


@respx.mock
def test_a_remembered_project_is_not_filed_on_the_item(client: TestClient, model: str) -> None:
    """The picker's project comes from its own control, so a remembered project_id
    must not be half-applied."""
    configure(client)
    remember(client, "global", {"project_id": 5})
    output_id = _prepared(client, model)
    queue = queue_route()

    result = client.post(f"/api/v1/print/outputs/{output_id}/run", json=body()).json()

    assert json.loads(queue.calls.last.request.read()).get("project_id") is None
    assert result["project_id"] is None


@respx.mock
def test_the_pickers_own_options_ride_on_this_print_only(client: TestClient, model: str) -> None:
    """#78: the Print dialog's options disclosure, like the send bar's, overrides every
    remembered layer for this request and is remembered nowhere. ``copies`` still wins
    over a quantity sent alongside it, as on the send bar."""
    configure(client)
    remember(client, "global", {"timelapse": False})
    output_id = _prepared(client, model)
    queue = queue_route()

    client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json=run_request(copies=2, options={"timelapse": True, "quantity": 5}),
    )

    queued = json.loads(queue.calls.last.request.read())
    assert (queued["timelapse"], queued["quantity"]) == (True, 2)
    remembered = client.get("/api/v1/settings/print-options").json()
    assert remembered["global_options"]["timelapse"] is False
