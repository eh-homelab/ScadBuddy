"""#2672 — a batch print: one stepped dialog over several library files, kept in
Postgres so a reload resumes it, and queued as each file's own library run."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api import library_print, print_batches
from scadbuddy.api.operations import STILL_ACCEPTING_PROBLEM
from scadbuddy.api.print_batches import batch_request_id
from scadbuddy.api.printing import STILL_CHECKING_DETAIL
from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.batches.store import BatchItem
from scadbuddy.core.problems import ApiError
from tests.api.test_print_filaments import queue_route, slice_routes
from tests.api.test_print_library import SLICED_RUN, sliced_3mf, sliced_file
from tests.api.test_print_run_choices import follow_run, run_routes
from tests.api.test_send import BASE, configure

pytestmark = pytest.mark.requires_postgres

BATCHES = "/api/v1/print/batches"
API = f"{BASE}/api/v1"


def new_batch(client: TestClient, *ids: int) -> Any:
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


def choose(client: TestClient, batch_id: str, position: int, **choice: object) -> Any:
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
    """Each chosen file's own library run: a sliced file that fits and one sliced for
    another model both queue (a sliced file is never refused, #2671), a file Bambuddy no
    longer has is refused as a single print would be, one with no choices is said to be,
    and a skipped and a removed one are left out entirely."""
    configure(client)
    sliced_file(104)
    sliced_file(105, sliced_3mf(printer_model="Bambu Lab X1 Carbon"))
    respx.get(f"{API}/library/files/109").mock(return_value=httpx.Response(404))
    run_routes()
    sliced = slice_routes()
    queued = queue_route()
    batch = new_batch(client, 104, 105, 106, 107, 108, 109)
    choose(client, batch["id"], 0, request=SLICED_RUN)
    choose(client, batch["id"], 1, request=SLICED_RUN)
    choose(client, batch["id"], 3, request=SLICED_RUN, skipped=True)
    choose(client, batch["id"], 4, request=SLICED_RUN, removed=True)
    choose(client, batch["id"], 5, request=SLICED_RUN)

    response = client.post(f"{BATCHES}/{batch['id']}/queue")

    assert response.status_code == 200, response.text
    done = response.json()
    # A file refused and one unchosen: open still, so they can be fixed and sent again.
    assert done["status"] == "open"
    outcomes = [item["outcome"] for item in done["items"]]
    assert [o and o["status"] for o in outcomes] == [
        "started",
        "started",
        "unchosen",
        None,
        None,
        "refused",
    ]
    assert outcomes[5]["detail"]
    first, other_model = (follow_run(client, outcomes[n]["run_id"]) for n in (0, 1))
    assert first["status"] == "succeeded"
    assert other_model["status"] == "succeeded"
    assert any(
        warning["kind"] == "sliced-other-model" for warning in other_model["result"]["warnings"]
    )
    assert queued.call_count == 2
    sent = sorted(json.loads(call.request.content)["library_file_id"] for call in queued.calls)
    assert sent == [104, 105]
    assert not sliced.called
    # A started file no longer changes; an unchosen one still can.
    assert client.put(f"{BATCHES}/{batch['id']}/items/0", json={}).status_code == 409
    assert client.put(f"{BATCHES}/{batch['id']}/items/2", json={}).status_code == 200


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
    # Every file started: the batch is queued, and nothing in it changes.
    assert (first["status"], again["status"]) == ("queued", "queued")
    assert client.put(f"{BATCHES}/{batch['id']}/items/0", json={}).status_code == 409


@respx.mock
def test_still_checking_is_pending_and_queue_all_follows_the_same_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2718 review: a 503 "still checking" is not a refusal; the run may go on. The
    file is pending, the batch stays open, and Queue all again sends the same request_id
    rather than starting a second print."""
    configure(client)
    sliced_file(104)
    run_routes()
    queued = queue_route()
    batch = new_batch(client, 104)
    choose(client, batch["id"], 0, request=SLICED_RUN)
    real = library_print.post_library_run
    sent: list[str | None] = []

    async def still_checking_once(file_id: int, request: PrintRunRequest, *rest: Any) -> Any:
        sent.append(request.request_id)
        if len(sent) == 1:
            raise ApiError(503, STILL_CHECKING_DETAIL, type_=STILL_ACCEPTING_PROBLEM)
        return await real(file_id, request, *rest)

    monkeypatch.setattr(print_batches, "post_library_run", still_checking_once)

    first = client.post(f"{BATCHES}/{batch['id']}/queue").json()

    assert first["status"] == "open"
    assert first["items"][0]["outcome"]["status"] == "pending"
    # Pending may be running: its choices cannot change into a second print.
    assert client.put(f"{BATCHES}/{batch['id']}/items/0", json={}).status_code == 409

    again = client.post(f"{BATCHES}/{batch['id']}/queue").json()

    assert again["items"][0]["outcome"]["status"] == "started"
    assert again["status"] == "queued"
    assert sent[0] is not None and sent == [sent[0], sent[0]]
    follow_run(client, again["items"][0]["outcome"]["run_id"])
    assert queued.call_count == 1


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (ApiError(503, "Temporal is down.", may_have_started=False), "pending"),
        (ApiError(500, "Temporal refused.", may_have_started=True), "pending"),
        (ApiError(500, "Temporal refused.", may_have_started=False), "refused"),
        (ApiError(422, "No such plate."), "refused"),
    ],
)
async def test_only_an_answer_that_is_not_a_refusal_is_pending(
    monkeypatch: pytest.MonkeyPatch, error: ApiError, outcome: str
) -> None:
    async def answers(*_: Any) -> Any:
        raise error

    monkeypatch.setattr(print_batches, "post_library_run", answers)
    item = BatchItem(
        position=0,
        library_file_id=104,
        filename="a",
        request=PrintRunRequest.model_validate(SLICED_RUN),
    )
    result = await print_batches._queue_one("b1", item, *([None] * 4))  # type: ignore[arg-type]
    assert result.status == outcome


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
