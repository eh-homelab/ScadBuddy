"""``POST /print/outputs/{id}/run`` names a registry printer (#3823, plan 2026-10-16 Task
8): the run is recorded with the row it printed on, by identity, whichever field named
it; a ``printer`` that disagrees with ``printer_id`` is refused before anything is
uploaded. ``prepare_run``'s own cases are `tests/printers/test_run_printer.py`."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.printers.component import PRINTERS
from scadbuddy.printers.models import PrinterCreate
from scadbuddy.printers.store import PrinterStore
from tests.api.test_print_batches import BATCHES, choose, new_batch
from tests.api.test_print_filaments import prepared, queue_route, slice_routes
from tests.api.test_print_library import SLICED_RUN, sliced_file
from tests.api.test_print_run_choices import body, follow_run, run_routes
from tests.api.test_send import API, configure, upload_route
from tests.bambuddy.conftest import recording

#: Bambuddy printer 1's serial, as `serial_listing` lists it (the recording's is redacted).
SERIAL = "01P00A0000000AA"


def serial_listing() -> None:
    """Bambuddy's printer listing with printer 1's serial, so a row can be added by it."""
    rows = recording("printers.json")
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(200, json=[{**rows[0], "serial_number": SERIAL}, *rows[1:]])
    )


pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]


def registry(client: TestClient) -> PrinterStore:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state.components.get(PRINTERS).store


def run(client: TestClient, output_id: str, request: dict[str, Any]) -> dict[str, Any]:
    started = client.post(f"/api/v1/print/outputs/{output_id}/run", json=request)
    assert started.status_code in (200, 202), started.text
    return follow_run(client, started.json()["id"])


@respx.mock
def test_a_run_by_bambuddy_id_is_recorded_with_its_registry_printer(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    slice_routes()
    queue_route()

    ended = run(client, output_id, body())

    assert ended["status"] == "succeeded", ended
    row = registry(client).by_bambuddy_id(1)
    assert row is not None
    assert ended["printer"] == row.id
    activity = client.get(f"/api/v1/activity/print_run:{ended['id']}?advanced=1").json()
    assert activity["row"]["facets"]["printer"] == row.id


@respx.mock
def test_a_run_naming_a_printer_queues_on_its_bambuddy_printer(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    slice_routes()
    queued = queue_route()
    row = registry(client).ensure_bambuddy(1, serial="01P00A0000000AA", name="H2C", model="H2C")
    request = {**body(), "printer": row.id}
    del request["printer_id"]

    ended = run(client, output_id, request)

    assert ended["status"] == "succeeded", ended
    assert json.loads(queued.calls.last.request.content)["printer_id"] == 1
    assert ended["printer"] == row.id


@respx.mock
def test_run_refuses_disagreeing_printer_fields(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    uploaded = upload_route()
    run_routes()
    row = registry(client).ensure_bambuddy(1, serial="01P00A0000000AA", name="H2C", model="H2C")

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run",
        json={**body(), "printer": row.id, "printer_id": 2},
    )

    assert response.status_code == 422, response.text
    assert "disagreeing printer" in response.json()["detail"]
    assert not uploaded.called


@respx.mock
def test_the_check_refuses_disagreeing_printer_fields_as_the_run_does(
    client: TestClient, model: str
) -> None:
    """#4442 I2: the dialog's check says before Print what the run refuses, word for word."""
    output_id = prepared(client, model)
    uploaded = upload_route()
    run_routes()
    row = registry(client).ensure_bambuddy(1, serial=SERIAL, name="H2C", model="H2C")
    request = {**body(), "printer": row.id, "printer_id": 2}

    checked = client.post(f"/api/v1/print/outputs/{output_id}/check", json=request)
    ran = client.post(f"/api/v1/print/outputs/{output_id}/run", json=request)

    assert checked.status_code == 200, checked.text
    assert ran.status_code == 422, ran.text
    assert checked.json()["errors"] == [ran.json()["detail"]]
    assert not uploaded.called


@respx.mock
def test_a_body_naming_its_printer_joins_the_run_it_made_before_the_backend_read_it(
    client: TestClient, model: str
) -> None:
    """#4442 M1: the dialog has sent ``printer`` since Task 10; a retry that straddles the
    deploy is the same run, never a second print."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    slice_routes()
    queued = queue_route()

    first = run(client, output_id, body())
    row = registry(client).by_bambuddy_id(1)
    assert row is not None
    again = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json={**body(), "printer": row.id}
    )

    assert again.status_code == 200, again.text
    assert (again.json()["id"], again.json()["repeated"]) == (first["id"], True)
    assert queued.call_count == 1


@respx.mock
def test_a_stored_batch_step_naming_a_merged_printer_still_queues(client: TestClient) -> None:
    """#4442 I1: a batch keeps the body it was chosen with. The strip's synthetic row it
    names is merged into the row added by serial before Queue all: it prints on that row."""
    configure(client)
    sliced_file(104)
    run_routes()
    serial_listing()
    slice_routes()
    queued = queue_route()
    printers = registry(client)
    synthetic = printers.ensure_bambuddy(1, serial=None, name="H2C", model="H2C")
    batch = new_batch(client, 104)
    choose(client, batch["id"], 0, request={**SLICED_RUN, "printer": synthetic.id})
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))
    assert printers.ensure_bambuddy(1, serial=SERIAL, name="H2C", model="H2C").id == mine.id

    done = client.post(f"{BATCHES}/{batch['id']}/queue")

    assert done.status_code == 200, done.text
    [item] = done.json()["items"]
    assert item["outcome"]["status"] == "started", item
    ended = follow_run(client, item["outcome"]["run_id"])
    assert ended["status"] == "succeeded", ended
    assert ended["printer"] == mine.id
    assert queued.call_count == 1
