"""#2672 — a batch print: one stepped dialog over several library files, kept in
Postgres so a reload resumes it, and queued as each file's own library run."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from scadbuddy.api import library_print, print_batches
from scadbuddy.api.operations import STILL_ACCEPTING_PROBLEM
from scadbuddy.api.print_batches import batch_request_id
from scadbuddy.api.printing import STILL_CHECKING_DETAIL
from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.bambuddy.runs import PrintRunStore
from scadbuddy.batches.component import PRINT_BATCHES
from scadbuddy.batches.store import BatchChoice, BatchItem, BatchOutcome
from scadbuddy.core.problems import ApiError
from scadbuddy.main import BATCH_TTL, PENDING_BATCH_TTL
from tests.api.test_print_filaments import queue_route, slice_routes
from tests.api.test_print_library import SLICED_RUN, sliced_3mf, sliced_file
from tests.api.test_print_run_choices import follow_run, run_routes
from tests.api.test_send import BASE, configure
from tests.api.test_settings_runtime import _state

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
def test_queue_all_answers_in_time_and_is_followed_from_where_it_stopped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2951: one request starts files only while it can still answer in time, then says
    where to go on from; the follow-up starts the rest and never re-sends a file it
    refused earlier in the same Queue all. With no time left, each request starts one."""
    monkeypatch.setattr(print_batches, "QUEUE_BUDGET_S", 0.0)
    configure(client)
    sliced_file(104)
    sliced_file(105)
    gone = respx.get(f"{API}/library/files/109").mock(return_value=httpx.Response(404))
    run_routes()
    queued = queue_route()
    batch = new_batch(client, 104, 109, 105)
    for position in range(3):
        choose(client, batch["id"], position, request=SLICED_RUN)
    queue = f"{BATCHES}/{batch['id']}/queue"

    first = client.post(queue)
    assert first.status_code == 202, first.text
    assert first.json()["next_position"] == 1
    assert [i["outcome"] and i["outcome"]["status"] for i in first.json()["batch"]["items"]] == [
        "started",
        None,
        None,
    ]
    second = client.post(queue, params={"from_position": 1})
    assert second.status_code == 202, second.text
    assert second.json()["next_position"] == 2
    refused_reads = gone.call_count
    last = client.post(queue, params={"from_position": 2})

    assert last.status_code == 200, last.text
    done = last.json()
    assert [i["outcome"]["status"] for i in done["items"]] == ["started", "refused", "started"]
    assert done["status"] == "open"  # the refused file can be edited and sent again
    assert gone.call_count == refused_reads  # not sent twice in the one Queue all
    for n in (0, 2):
        assert follow_run(client, done["items"][n]["outcome"]["run_id"])["status"] == "succeeded"
    assert queued.call_count == 2
    assert client.post(queue, params={"from_position": -1}).status_code == 422


@pytest.mark.parametrize(
    ("took", "budget", "next_position"),
    [
        # Each start takes 8 s, reckoned at 10: after two (16 s) a third would end at 26.
        pytest.param(8.0, 25.0, 2, id="two-fit"),
        # A start that took 20 s (a download first) is what the next is reckoned at:
        # 20 + 20 > 35, though 20 + ACCEPT_BUDGET would fit.
        pytest.param(20.0, 35.0, 1, id="a-slow-start-is-measured"),
    ],
)
@respx.mock
def test_queue_all_reckons_each_start_at_the_slowest_so_far(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    took: float,
    budget: float,
    next_position: int,
) -> None:
    """#2718 review: a file is started only while ``ACCEPT_BUDGET``, or the slowest start
    so far when one took longer, still fits in ``QUEUE_BUDGET_S``."""
    clock = [0.0]
    monkeypatch.setattr(print_batches, "time", type("Clock", (), {"monotonic": lambda: clock[0]}))
    monkeypatch.setattr(print_batches, "ACCEPT_BUDGET", 10.0)
    monkeypatch.setattr(print_batches, "QUEUE_BUDGET_S", budget)
    real = library_print.start_library_run

    async def timed(*args: Any, **kwargs: Any) -> Any:
        clock[0] += took
        return await real(*args, **kwargs)

    monkeypatch.setattr(print_batches, "start_library_run", timed)
    configure(client)
    for file_id in (104, 105, 106):
        sliced_file(file_id)
    run_routes()
    queue_route()
    batch = new_batch(client, 104, 105, 106)
    for position in range(3):
        choose(client, batch["id"], position, request=SLICED_RUN)

    first = client.post(f"{BATCHES}/{batch['id']}/queue")

    assert first.status_code == 202, first.text
    assert first.json()["next_position"] == next_position


@respx.mock
def test_an_unchosen_file_costs_no_time_against_queue_all(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2718 review: a file with no choices starts nothing, so with no time left the one
    request still records it and starts the chosen file after it."""
    monkeypatch.setattr(print_batches, "QUEUE_BUDGET_S", 0.0)
    configure(client)
    sliced_file(104)
    run_routes()
    queue_route()
    batch = new_batch(client, 105, 106, 104)
    choose(client, batch["id"], 2, request=SLICED_RUN)

    done = client.post(f"{BATCHES}/{batch['id']}/queue")

    assert done.status_code == 200, done.text
    assert [i["outcome"]["status"] for i in done.json()["items"]] == [
        "unchosen",
        "unchosen",
        "started",
    ]


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
    real = library_print.start_library_run
    sent: list[str | None] = []

    async def still_checking_once(file_id: int, request: PrintRunRequest, **rest: Any) -> Any:
        sent.append(request.request_id)
        if len(sent) == 1:
            raise ApiError(503, STILL_CHECKING_DETAIL, type_=STILL_ACCEPTING_PROBLEM)
        return await real(file_id, request, **rest)

    monkeypatch.setattr(print_batches, "start_library_run", still_checking_once)

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
        # Not an answer at all: pending, so the rest of the batch still goes (#2718 review).
        (RuntimeError("the run store went away"), "pending"),
    ],
)
async def test_only_an_answer_that_is_not_a_refusal_is_pending(
    monkeypatch: pytest.MonkeyPatch, error: Exception, outcome: str
) -> None:
    async def answers(*_: Any, **__: Any) -> Any:
        raise error

    monkeypatch.setattr(print_batches, "start_library_run", answers)
    item = BatchItem(
        position=0,
        library_file_id=104,
        filename="a",
        request=PrintRunRequest.model_validate(SLICED_RUN),
    )
    result = await print_batches._queue_one("b1", item, 0, *([None] * 6))  # type: ignore[arg-type]
    assert result.status == outcome


@respx.mock
def test_a_file_that_fails_to_start_twice_is_refused_so_it_can_be_left(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2718 review: an error that is not an answer leaves the file pending, which keeps
    it from being edited, skipped or removed and its batch from Cancel. The same request
    failing that way again is a fault, not an outage: refused, the file can be left."""

    async def broken(*_: Any, **__: Any) -> Any:
        raise RuntimeError("the run store went away")

    monkeypatch.setattr(print_batches, "start_library_run", broken)
    configure(client)
    batch = new_batch(client, 104)
    choose(client, batch["id"], 0, request=SLICED_RUN)
    queue = f"{BATCHES}/{batch['id']}/queue"

    first = client.post(queue).json()["items"][0]["outcome"]
    assert first["status"] == "pending"
    assert client.put(f"{BATCHES}/{batch['id']}/items/0", json={"skipped": True}).status_code == 409

    again = client.post(queue).json()["items"][0]["outcome"]

    assert again["status"] == "refused"
    assert client.put(f"{BATCHES}/{batch['id']}/items/0", json={"skipped": True}).status_code == 200


@respx.mock
def test_a_start_that_answers_a_follow_is_a_refusal_not_an_outage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3886: an `assert` took the run's type on trust; failed, it read as an outage
    (pending). Queue all never asks for a follow, so one is a fault, shown as one."""

    async def follow(*_: Any, **__: Any) -> Any:
        return JSONResponse({"follow": "/print/runs/x"}, status_code=202)

    monkeypatch.setattr(print_batches, "start_library_run", follow)
    configure(client)
    batch = new_batch(client, 104)
    choose(client, batch["id"], 0, request=SLICED_RUN)

    outcome = client.post(f"{BATCHES}/{batch['id']}/queue").json()["items"][0]["outcome"]

    assert outcome["status"] == "refused"
    assert outcome["detail"] == print_batches.NOT_A_RUN


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


def test_a_choice_moves_the_step_in_the_same_write(client: TestClient) -> None:
    """#2718 review: Next saved the choice, then moved the step in a second PUT, so a
    failed second write left a saved choice on a step that had not moved. One write
    now carries both, and a step past the end refuses the choice with it."""
    batch = new_batch(client, 104, 105)

    moved = choose(client, batch["id"], 0, request=SLICED_RUN, step=1)

    assert moved["step"] == 1
    assert moved["items"][0]["request"] is not None
    past = client.put(f"{BATCHES}/{batch['id']}/items/1", json={"skipped": True, "step": 3})
    assert past.status_code == 422, past.text
    kept = client.get(f"{BATCHES}/{batch['id']}").json()
    assert (kept["step"], kept["items"][1]["skipped"]) == (1, False)
    # Without a step, the step stays where it is.
    assert choose(client, batch["id"], 1, skipped=True)["step"] == 1


@respx.mock
def test_a_queued_batchs_step_no_longer_moves(client: TestClient) -> None:
    configure(client)
    sliced_file(104)
    run_routes()
    queue_route()
    batch = new_batch(client, 104)
    choose(client, batch["id"], 0, request=SLICED_RUN)
    queued = client.post(f"{BATCHES}/{batch['id']}/queue").json()
    assert queued["status"] == "queued"

    moved = client.put(f"{BATCHES}/{batch['id']}/step", json={"step": 0})

    assert moved.status_code == 409, moved.text


async def test_a_batch_nobody_touched_for_a_week_is_forgotten(client: TestClient) -> None:
    """#2718 review: a batch closed without Cancel or Done (the tab closed) would stay
    for good; the claims sweep forgets one untouched past BATCH_TTL."""
    stale, fresh = new_batch(client, 104), new_batch(client, 105)
    store = _state(client).components.get(PRINT_BATCHES)
    with store._pool.connection() as conn:
        conn.execute(
            "UPDATE print_batches SET updated_at = now() - %s WHERE id = %s",
            (BATCH_TTL + timedelta(hours=1), stale["id"]),
        )

    assert (await store.prune(BATCH_TTL, pending_older_than=PENDING_BATCH_TTL)).count == 1

    assert client.get(f"{BATCHES}/{stale['id']}").status_code == 404
    assert client.get(f"{BATCHES}/{fresh['id']}").status_code == 200


def _age(client: TestClient, batch_id: str, by: timedelta) -> None:
    store = _state(client).components.get(PRINT_BATCHES)
    with store._pool.connection() as conn:
        conn.execute(
            "UPDATE print_batches SET updated_at = now() - %s WHERE id = %s", (by, batch_id)
        )


async def test_a_batch_following_a_pending_file_is_kept_longer_and_named(
    client: TestClient,
) -> None:
    """#3798: the prune took a batch with a file still being checked, the very batch that
    follows that file's run, so a new batch could send it again. It is kept until
    PENDING_BATCH_TTL, then pruned by name so the log says which."""
    week, month = new_batch(client, 104), new_batch(client, 105)
    store = _state(client).components.get(PRINT_BATCHES)
    for batch in (week, month):
        await store.record_outcome(batch["id"], 0, BatchOutcome(status="pending"))
    _age(client, week["id"], BATCH_TTL + timedelta(hours=1))
    _age(client, month["id"], PENDING_BATCH_TTL + timedelta(hours=1))

    pruned = await store.prune(BATCH_TTL, pending_older_than=PENDING_BATCH_TTL)

    assert (pruned.count, pruned.pending) == (1, [month["id"]])
    assert client.get(f"{BATCHES}/{week['id']}").status_code == 200


async def test_a_batch_following_a_pending_file_cannot_be_deleted(client: TestClient) -> None:
    """#3798: only the dialog's close kept such a batch; the route deleted it for any
    other caller. It answers 409 now, and deletes it once nothing is pending."""
    batch = new_batch(client, 104)
    store = _state(client).components.get(PRINT_BATCHES)
    await store.record_outcome(batch["id"], 0, BatchOutcome(status="pending"))

    refused = client.delete(f"{BATCHES}/{batch['id']}")
    assert refused.status_code == 409, refused.text
    assert client.get(f"{BATCHES}/{batch['id']}").status_code == 200

    await store.record_outcome(batch["id"], 0, BatchOutcome(status="refused", detail="no"))
    assert client.delete(f"{BATCHES}/{batch['id']}").status_code == 204
    assert client.delete(f"{BATCHES}/{batch['id']}").status_code == 404


async def test_the_pending_batches_are_read_together(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3798: each pending batch was read on its own (two queries and a pool checkout
    apiece). They are read in one go, each with its own files."""
    first, second = new_batch(client, 104, 105), new_batch(client, 106)
    store = _state(client).components.get(PRINT_BATCHES)
    for batch in (first, second):
        await store.record_outcome(batch["id"], 0, BatchOutcome(status="pending"))

    def one_at_a_time(*_: Any, **__: Any) -> Any:
        raise AssertionError("a pending batch was read on its own")

    monkeypatch.setattr(store, "_get", one_at_a_time)
    found = {
        batch.id: [item.library_file_id for item in batch.items]
        for batch in await store.with_pending()
    }

    assert found == {first["id"]: [104, 105], second["id"]: [106]}


async def test_an_edited_refusal_says_it_is_of_other_choices(client: TestClient) -> None:
    """#3798: after a refused file was edited, its row still read "Not queued: <the old
    refusal>" against choices that were not the ones refused. The refusal is kept, with
    its attempt, and marked stale; the same choices saved again leave it as it is."""
    batch = new_batch(client, 104)
    choose(client, batch["id"], 0, request=SLICED_RUN)
    store = _state(client).components.get(PRINT_BATCHES)
    refusal = BatchOutcome(status="refused", detail="No spool for slot 2.", attempt=1)
    await store.record_outcome(batch["id"], 0, refusal)

    same = choose(client, batch["id"], 0, request=SLICED_RUN)["items"][0]["outcome"]
    assert same["stale"] is False
    edited = choose(client, batch["id"], 0, request={**SLICED_RUN, "copies": 2})
    outcome = edited["items"][0]["outcome"]

    assert (outcome["status"], outcome["attempt"], outcome["stale"]) == ("refused", 1, True)


async def test_an_edit_back_to_the_refused_choices_is_not_stale(client: TestClient) -> None:
    """#3932 review: once stale, a refusal stayed stale after the file was edited back
    to the very choices refused, and the row said they were others. Stale compares
    against the choices the refusal was of, not the last edit."""
    batch = new_batch(client, 104)
    choose(client, batch["id"], 0, request=SLICED_RUN)
    store = _state(client).components.get(PRINT_BATCHES)
    refusal = BatchOutcome(status="refused", detail="No spool for slot 2.", attempt=1)
    await store.record_outcome(batch["id"], 0, refusal)

    away = choose(client, batch["id"], 0, request={**SLICED_RUN, "copies": 2})
    assert away["items"][0]["outcome"]["stale"] is True
    back = choose(client, batch["id"], 0, request=SLICED_RUN)["items"][0]["outcome"]

    assert (back["status"], back["attempt"], back["stale"]) == ("refused", 1, False)


async def test_a_queued_batchs_step_holds_against_a_late_move(client: TestClient) -> None:
    """#3798: the step's write took the batch open on an earlier read, so a Queue all
    landing in between still moved a queued batch's step."""
    batch = new_batch(client, 104)
    store = _state(client).components.get(PRINT_BATCHES)
    with store._pool.connection() as conn:
        conn.execute("UPDATE print_batches SET status = 'queued' WHERE id = %s", (batch["id"],))

    assert await store.set_step(batch["id"], 1) is None
    assert (await store.get(batch["id"])).step == 0  # type: ignore[union-attr]


def _fail_run(client: TestClient, run_id: str, *, may_have_queued: bool) -> None:
    """The run failed after the 202 (a slice or an upload), as the workflow records it."""
    store = _state(client).components.get(PRINT_BATCHES)
    error = {"status": 502, "title": "Bad Gateway", "detail": "Bambuddy's slice failed."}
    with store._pool.connection() as conn:
        conn.execute(
            "UPDATE print_runs SET status = 'failed', result = NULL, error = %s,"
            " enqueue_attempted = %s WHERE id = %s",
            (Jsonb(error), may_have_queued, run_id),
        )


@respx.mock
def test_a_run_that_failed_after_the_202_can_be_edited_and_sent_again_as_a_new_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2718 review: a started file stayed started when its run then failed, so it could
    neither change nor be sent again, and the batch never settled."""
    configure(client)
    sliced_file(104)
    run_routes()
    queued = queue_route()
    batch = new_batch(client, 104, 105)
    choose(client, batch["id"], 0, request=SLICED_RUN)
    real = library_print.start_library_run
    sent: list[str | None] = []

    async def recorded(file_id: int, request: PrintRunRequest, **rest: Any) -> Any:
        sent.append(request.request_id)
        return await real(file_id, request, **rest)

    monkeypatch.setattr(print_batches, "start_library_run", recorded)
    first = client.post(f"{BATCHES}/{batch['id']}/queue").json()
    run_id = first["items"][0]["outcome"]["run_id"]
    follow_run(client, run_id)
    _fail_run(client, run_id, may_have_queued=False)

    read = client.get(f"{BATCHES}/{batch['id']}").json()

    failed = read["items"][0]["outcome"]
    assert (failed["status"], failed["run_id"]) == ("failed", run_id)
    assert failed["detail"] == "Bambuddy's slice failed."
    # Editable, as a refusal is.
    choose(client, batch["id"], 0, request=SLICED_RUN)

    again = client.post(f"{BATCHES}/{batch['id']}/queue").json()

    retried = again["items"][0]["outcome"]
    assert retried["status"] == "started" and retried["run_id"] != run_id
    assert retried["attempt"] == 1
    # The same choices, yet a new request_id: never re-attached to the failed run.
    assert len(sent) == 2 and sent[0] != sent[1]
    follow_run(client, retried["run_id"])
    assert queued.call_count == 2


@respx.mock
def test_a_run_that_may_have_queued_before_failing_stays_started(client: TestClient) -> None:
    """A run that failed after it tried to queue may be on Bambuddy's queue: sending it
    again could print it twice, so it is not reopened."""
    configure(client)
    sliced_file(104)
    run_routes()
    queue_route()
    batch = new_batch(client, 104)
    choose(client, batch["id"], 0, request=SLICED_RUN)
    run_id = client.post(f"{BATCHES}/{batch['id']}/queue").json()["items"][0]["outcome"]["run_id"]
    follow_run(client, run_id)
    _fail_run(client, run_id, may_have_queued=True)

    read = client.get(f"{BATCHES}/{batch['id']}").json()

    assert read["items"][0]["outcome"]["status"] == "started"
    assert client.put(f"{BATCHES}/{batch['id']}/items/0", json={}).status_code == 409


async def test_queue_all_and_a_choice_never_cross(client: TestClient) -> None:
    """#2718 review: Queue all read a file, then sent it, so a PUT landing in between
    changed choices that were never what got sent. Queue all marks the file pending only
    while it is as read, and a choice never lands on a pending or started file."""
    batch = new_batch(client, 104)
    choose(client, batch["id"], 0, request=SLICED_RUN)
    store = _state(client).components.get(PRINT_BATCHES)

    async def item() -> BatchItem:
        read = await store.get(batch["id"])
        assert read is not None
        return read.items[0]

    read = await item()
    choose(client, batch["id"], 0, request={**SLICED_RUN, "printer_id": 2})

    sending = BatchOutcome(status="pending")
    assert not await store.record_outcome(batch["id"], 0, sending, if_unchanged=read)
    assert (await item()).outcome is None

    now = await item()
    assert await store.record_outcome(batch["id"], 0, sending, if_unchanged=now)
    assert await store.choose(batch["id"], 0, BatchChoice(skipped=True)) is None
    assert (await item()).skipped is False


async def test_queue_all_counts_as_touching_the_batch(client: TestClient) -> None:
    """#2718 review: recording an outcome left ``updated_at`` alone, so a batch last
    queued a week after its last choice was pruned as untouched."""
    batch = new_batch(client, 104)
    store = _state(client).components.get(PRINT_BATCHES)
    with store._pool.connection() as conn:
        conn.execute(
            "UPDATE print_batches SET updated_at = now() - %s WHERE id = %s",
            (BATCH_TTL + timedelta(hours=1), batch["id"]),
        )

    await store.record_outcome(batch["id"], 0, BatchOutcome(status="unchosen"))

    assert (await store.prune(BATCH_TTL, pending_older_than=PENDING_BATCH_TTL)).count == 0


async def test_reading_a_batch_counts_as_touching_it(client: TestClient) -> None:
    """#2718 review: a batch someone keeps reopening on its review, changing nothing,
    was pruned as untouched."""
    batch = new_batch(client, 104)
    store = _state(client).components.get(PRINT_BATCHES)
    with store._pool.connection() as conn:
        conn.execute(
            "UPDATE print_batches SET updated_at = now() - %s WHERE id = %s",
            (BATCH_TTL + timedelta(hours=1), batch["id"]),
        )

    assert client.get(f"{BATCHES}/{batch['id']}").status_code == 200

    assert (await store.prune(BATCH_TTL, pending_older_than=PENDING_BATCH_TTL)).count == 0


async def test_a_batch_is_queued_only_while_every_kept_file_has_started(
    client: TestClient,
) -> None:
    """#2718 review: Queue all checked the batch settled, then marked it queued. A failed
    run reopening it in between was overwritten; the mark now checks as it lands."""
    batch = new_batch(client, 104, 105)
    store = _state(client).components.get(PRINT_BATCHES)
    started = BatchOutcome(status="started", run_id="r1")
    await store.record_outcome(batch["id"], 0, started)
    await store.record_outcome(batch["id"], 1, BatchOutcome(status="failed", run_id="r2"))

    assert not await store.mark_queued(batch["id"])
    assert (await store.get(batch["id"])).status == "open"  # type: ignore[union-attr]

    await store.record_outcome(batch["id"], 1, started.model_copy(update={"run_id": "r3"}))
    assert await store.mark_queued(batch["id"])
    assert (await store.get(batch["id"])).status == "queued"  # type: ignore[union-attr]


async def test_a_batchs_started_runs_are_read_in_one_go(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2718 review: one read for every started run, not one each per GET or save."""
    batch = new_batch(client, 104, 105)
    store = _state(client).components.get(PRINT_BATCHES)
    for position in (0, 1):
        await store.record_outcome(
            batch["id"], position, BatchOutcome(status="started", run_id=f"run-{position}")
        )
    asked: list[list[str]] = []

    async def get_many(self: Any, run_ids: list[str]) -> dict[str, Any]:
        asked.append(run_ids)
        return {}

    async def get(self: Any, run_id: str) -> None:
        raise AssertionError("read one at a time")

    monkeypatch.setattr(PrintRunStore, "get_many", get_many)
    monkeypatch.setattr(PrintRunStore, "get", get)

    assert client.get(f"{BATCHES}/{batch['id']}").status_code == 200
    assert asked == [["run-0", "run-1"]]


async def test_a_batch_kept_for_a_pending_file_is_listed_to_resume(client: TestClient) -> None:
    """#2718 review: Cancel keeps a batch with a file still being checked, so Queue all
    can follow its run; this list is how the Library finds it again. A batch with
    nothing pending, or queued, is not listed."""
    kept = new_batch(client, 104, 105)
    new_batch(client, 106)  # nothing sent yet
    store = _state(client).components.get(PRINT_BATCHES)
    await store.record_outcome(kept["id"], 1, BatchOutcome(status="pending"))

    listed = client.get(BATCHES)

    assert listed.status_code == 200, listed.text
    assert [batch["id"] for batch in listed.json()] == [kept["id"]]


def test_a_removed_files_step_cannot_be_shown(client: TestClient) -> None:
    """#2718 review: the strip no longer shows a removed file, so its step is refused,
    on its own or moved with a choice, and so is the step of the file a choice removes."""
    batch = new_batch(client, 104, 105, 106)
    choose(client, batch["id"], 1, removed=True)
    step = f"{BATCHES}/{batch['id']}/step"

    assert client.put(step, json={"step": 1}).status_code == 422
    assert client.put(f"{BATCHES}/{batch['id']}/items/0", json={"step": 1}).status_code == 422
    assert (
        client.put(f"{BATCHES}/{batch['id']}/items/2", json={"removed": True, "step": 2})
    ).status_code == 422
    assert client.put(step, json={"step": 2}).status_code == 200
    assert client.put(step, json={"step": 3}).status_code == 200  # the review


@respx.mock
def test_a_run_that_fails_after_the_batch_is_queued_reopens_it_to_edit_and_send_again(
    client: TestClient,
) -> None:
    """#2718 review (Blocking): Queue all marks the batch queued once every run has its
    202, long before a slice can fail. A run failing after that left the batch queued,
    so its file could not be edited (409) or sent again, and the dialog showed Done."""
    configure(client)
    sliced_file(104)
    run_routes()
    queued = queue_route()
    batch = new_batch(client, 104)
    choose(client, batch["id"], 0, request=SLICED_RUN)
    first = client.post(f"{BATCHES}/{batch['id']}/queue").json()
    assert first["status"] == "queued"
    run_id = first["items"][0]["outcome"]["run_id"]
    follow_run(client, run_id)
    _fail_run(client, run_id, may_have_queued=False)

    read = client.get(f"{BATCHES}/{batch['id']}").json()

    assert read["status"] == "open"
    assert read["items"][0]["outcome"]["status"] == "failed"
    choose(client, batch["id"], 0, request=SLICED_RUN)
    again = client.post(f"{BATCHES}/{batch['id']}/queue").json()
    assert again["items"][0]["outcome"]["status"] == "started"
    assert again["items"][0]["outcome"]["run_id"] != run_id
    assert again["status"] == "queued"
    follow_run(client, again["items"][0]["outcome"]["run_id"])
    assert queued.call_count == 2


def test_queue_all_with_every_file_left_out_queues_nothing(client: TestClient) -> None:
    """#2718 review: with every file skipped or removed, Queue all marked a batch with
    nothing in it queued."""
    batch = new_batch(client, 104, 105)
    choose(client, batch["id"], 0, skipped=True)
    choose(client, batch["id"], 1, removed=True)

    response = client.post(f"{BATCHES}/{batch['id']}/queue")

    assert response.status_code == 422, response.text
    assert "nothing to queue" in response.json()["detail"]
    assert client.get(f"{BATCHES}/{batch['id']}").json()["status"] == "open"
