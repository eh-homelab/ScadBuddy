"""#2672 — a batch print: one stepped dialog over several library files, kept in
Postgres so a reload resumes it, and queued as each file's own library run."""

from __future__ import annotations

import json

import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.print_batches import batch_request_id
from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.batches.store import BatchItem
from tests.api.test_print_filaments import queue_route, slice_routes
from tests.api.test_print_library import SLICED_RUN, sliced_3mf, sliced_file
from tests.api.test_print_run_choices import follow_run, run_routes
from tests.api.test_send import configure

pytestmark = pytest.mark.requires_postgres

BATCHES = "/api/v1/print/batches"


def new_batch(client: TestClient, *ids: int) -> dict:
    response = client.post(
        BATCHES,
        json={
            "files": [
                {"library_file_id": i, "filename": f"file-{i}.gcode.3mf", "sliced": True}
                for i in ids
            ]
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def choose(client: TestClient, batch_id: str, position: int, **choice: object) -> dict:
    response = client.put(f"{BATCHES}/{batch_id}/items/{position}", json=choice)
    assert response.status_code == 200, response.text
    return response.json()


def test_a_batch_keeps_its_step_and_choices_for_a_reload(client: TestClient) -> None:
    """What each step chose, and the step in view, read back as they were left."""
    batch = new_batch(client, 104, 105, 106)
    assert [item["library_file_id"] for item in batch["items"]] == [104, 105, 106]
    assert batch["step"] == 0 and batch["status"] == "open"

    choose(
        client,
        batch["id"],
        0,
        request={**SLICED_RUN, "plate_id": 1},
        summary={"printer": "H2C", "plate": "Plate 1", "spools": ["PLA Blue"]},
        problems=["No spool for slot 2."],
    )
    choose(client, batch["id"], 1, skipped=True)
    assert client.put(f"{BATCHES}/{batch['id']}/step", json={"step": 2}).status_code == 200

    resumed = client.get(f"{BATCHES}/{batch['id']}").json()
    assert resumed["step"] == 2
    first, second, third = resumed["items"]
    assert first["request"]["printer_id"] == 1
    assert first["request"]["filament_plan"]["slots"] == [{"slot_id": 1, "spool_id": 9}]
    # What was not sent stays unsent: the run applies the remembered project.
    assert "project_id" not in first["request"] or first["request"]["project_id"] is None
    assert first["summary"]["spools"] == ["PLA Blue"]
    assert first["problems"] == ["No spool for slot 2."]
    assert second["skipped"] is True and second["request"] is None
    assert third["request"] is None and third["skipped"] is False


def test_a_batch_refuses_a_file_twice_an_unknown_step_and_an_unknown_batch(
    client: TestClient,
) -> None:
    twice = client.post(
        BATCHES,
        json={
            "files": [
                {"library_file_id": 1, "filename": "a"},
                {"library_file_id": 1, "filename": "a"},
            ]
        },
    )
    assert twice.status_code == 422
    batch = new_batch(client, 104)
    # The review is the step after the last file; nothing is past it.
    assert client.put(f"{BATCHES}/{batch['id']}/step", json={"step": 1}).status_code == 200
    assert client.put(f"{BATCHES}/{batch['id']}/step", json={"step": 2}).status_code == 422
    assert client.put(f"{BATCHES}/{batch['id']}/items/5", json={}).status_code == 404
    assert client.get(f"{BATCHES}/nope").status_code == 404
    assert client.delete(f"{BATCHES}/{batch['id']}").status_code == 204
    assert client.get(f"{BATCHES}/{batch['id']}").status_code == 404


@respx.mock
def test_queue_all_runs_each_chosen_file_and_reports_each(client: TestClient) -> None:
    """Each chosen file's own library run: one queued, one refused as a single print
    would be, one with no choices, one skipped and one removed left out entirely."""
    configure(client)
    sliced_file(104)
    sliced_file(105, sliced_3mf(printer_model="Bambu Lab X1 Carbon"))
    run_routes()
    sliced = slice_routes()
    queued = queue_route()
    batch = new_batch(client, 104, 105, 106, 107, 108)
    choose(client, batch["id"], 0, request=SLICED_RUN)
    choose(client, batch["id"], 1, request=SLICED_RUN)
    choose(client, batch["id"], 3, request=SLICED_RUN, skipped=True)
    choose(client, batch["id"], 4, request=SLICED_RUN, removed=True)

    response = client.post(f"{BATCHES}/{batch['id']}/queue")

    assert response.status_code == 200, response.text
    done = response.json()
    assert done["status"] == "queued"
    outcomes = [item["outcome"] for item in done["items"]]
    assert outcomes[0]["status"] == "started"
    assert outcomes[1] == {
        "status": "refused",
        "run_id": None,
        "detail": outcomes[1]["detail"],
    }
    assert "X1" in outcomes[1]["detail"]
    assert outcomes[2]["status"] == "unchosen"
    assert outcomes[3] is None and outcomes[4] is None
    run = follow_run(client, outcomes[0]["run_id"])
    assert run["status"] == "succeeded"
    assert queued.call_count == 1
    assert json.loads(queued.calls.last.request.content)["library_file_id"] == 104
    assert not sliced.called
    # Queued, it no longer changes.
    assert client.put(f"{BATCHES}/{batch['id']}/items/2", json={}).status_code == 409


@respx.mock
def test_queue_all_sent_again_queues_nothing_twice(client: TestClient) -> None:
    """A Queue all retried (a reload mid-request, a second press) leaves a started file
    as it is."""
    configure(client)
    sliced_file(104)
    run_routes()
    queued = queue_route()
    batch = new_batch(client, 104)
    choose(client, batch["id"], 0, request=SLICED_RUN)
    first = client.post(f"{BATCHES}/{batch['id']}/queue").json()
    follow_run(client, first["items"][0]["outcome"]["run_id"])

    again = client.post(f"{BATCHES}/{batch['id']}/queue").json()

    assert again["items"][0]["outcome"] == first["items"][0]["outcome"]
    assert queued.call_count == 1


def test_a_files_press_is_its_choices() -> None:
    """The same choices are the same run (a repeat re-attaches); changed ones after a
    refusal are a new one."""
    item = BatchItem(
        position=2,
        library_file_id=104,
        filename="a",
        request=PrintRunRequest.model_validate(SLICED_RUN),
    )
    same = item.model_copy(update={"request": PrintRunRequest.model_validate(SLICED_RUN)})
    other = item.model_copy(
        update={"request": PrintRunRequest.model_validate({**SLICED_RUN, "printer_id": 2})}
    )
    assert batch_request_id("b1", item) == batch_request_id("b1", same)
    assert batch_request_id("b1", item) != batch_request_id("b1", other)
    assert batch_request_id("b1", item).startswith("batch:b1:2:")
    assert len(batch_request_id("b" * 32, item)) <= 128
