"""#470: ``POST /print/outputs/{id}/run`` answers 202 and prints in the background.

The run used to upload, slice, wait for every slice and queue inside the one request,
which the proxies in front cut after 15-100 s while the backend went on and queued the
print anyway. Now the request makes only the refusals it can make cheaply, answers 202
with a run, and ``GET /print/runs/{id}`` follows it. A repeat of the same request finds
that run rather than queueing a second print.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import psycopg
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import DATABASE_REQUIRED_PROBLEM, STATE_ATTR
from scadbuddy.bambuddy.errors import UNAVAILABLE_PROBLEM
from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.bambuddy.runs import PrintRunStore, run_key
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from scadbuddy.workflows.client import connect_lazily
from tests.api.test_print_filaments import prepared, queue_route
from tests.api.test_print_run_choices import (
    API,
    body,
    follow_run,
    run_request,
    run_routes,
)
from tests.api.test_send import upload_route
from tests.test_bambu3mf import add_plate

pytestmark = pytest.mark.requires_postgres


class Gate:
    """Holds every slice job at ``running`` until opened, from the test's thread."""

    def __init__(self) -> None:
        self._open = threading.Event()

    def open(self) -> None:
        self._open.set()

    async def slice_job(self, request: httpx.Request) -> httpx.Response:
        # Async, so the app's loop keeps serving the status route while this waits.
        while not self._open.is_set():
            await asyncio.sleep(0.01)
        return httpx.Response(
            200,
            json={
                "id": 9,
                "status": "completed",
                "result": {"library_file_id": 77, "filament_used_g": 12.0},
            },
        )


def gated_slice_routes(gate: Gate) -> respx.Route:
    posted = respx.route(method="POST", path__regex=r"/api/v1/library/files/\d+/slice").mock(
        return_value=httpx.Response(200, json={"job_id": 9, "status": "pending"})
    )
    respx.get(f"{API}/slice-jobs/9").mock(side_effect=gate.slice_job)
    return posted


@pytest.fixture
def gate() -> Iterator[Gate]:
    opened = Gate()
    yield opened
    opened.open()  # never leave a run parked in the background


def start(client: TestClient, output_id: str, request: dict[str, Any]) -> httpx.Response:
    response: httpx.Response = client.post(f"/api/v1/print/outputs/{output_id}/run", json=request)
    return response


@respx.mock
def test_a_run_answers_202_before_the_slice_finishes_and_ends_with_its_result(
    client: TestClient, model: str, gate: Gate
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    gated_slice_routes(gate)
    queued = queue_route()

    began = time.monotonic()
    response = start(client, output_id, body())
    assert time.monotonic() - began < 5

    assert response.status_code == 202, response.text
    run = response.json()
    assert run["status"] == "running"
    assert run["output_id"] == output_id
    assert run["result"] is None and run["error"] is None
    assert client.get(f"/api/v1/print/runs/{run['id']}").json()["status"] == "running"
    assert not queued.called

    gate.open()
    ended = follow_run(client, run["id"])

    assert ended["status"] == "succeeded"
    assert ended["finished_at"] is not None
    assert ended["result"]["queue_item_ids"] == [51]
    assert ended["result"]["route"] == "slice_queue"
    assert isinstance(ended["result"]["warnings"], list)
    assert ended["result"]["bambuddy_url"].endswith("/queue")
    assert queued.call_count == 1


@respx.mock
def test_a_retry_while_the_run_is_in_flight_returns_that_run(
    client: TestClient, model: str, gate: Gate
) -> None:
    output_id = prepared(client, model)
    uploaded = upload_route()
    run_routes()
    sliced = gated_slice_routes(gate)
    queued = queue_route()

    first = start(client, output_id, body())
    second = start(client, output_id, body())

    assert first.status_code == 202
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    gate.open()
    assert follow_run(client, first.json()["id"])["status"] == "succeeded"
    assert uploaded.call_count == 1
    assert sliced.call_count == 1
    assert queued.call_count == 1


@respx.mock
def test_a_retry_after_the_run_succeeded_returns_it_and_queues_nothing_more(
    client: TestClient, model: str, gate: Gate
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    gated_slice_routes(gate)
    queued = queue_route()
    gate.open()

    first = start(client, output_id, body())
    follow_run(client, first.json()["id"])
    again = start(client, output_id, body())

    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["repeated"] is True
    assert again.json()["status"] == "succeeded"
    assert again.json()["result"]["queue_item_ids"] == [51]
    assert first.json()["repeated"] is False
    assert client.get(f"/api/v1/print/runs/{first.json()['id']}").json()["repeated"] is False
    assert queued.call_count == 1


@respx.mock
def test_a_retry_with_the_same_request_id_returns_its_run(
    client: TestClient, model: str, gate: Gate
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    gated_slice_routes(gate)
    queued = queue_route()
    gate.open()
    press = {**body(), "request_id": str(uuid.uuid4())}

    first = start(client, output_id, press)
    follow_run(client, first.json()["id"])
    again = start(client, output_id, press)

    assert again.status_code == 200, again.text
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["repeated"] is True
    assert queued.call_count == 1


@respx.mock
def test_a_deliberate_reprint_with_the_same_choices_is_a_new_print(
    client: TestClient, model: str, gate: Gate
) -> None:
    """The user prints, deletes the item in Bambuddy, and prints again with the same
    choices inside the repeat window: a new ``request_id`` is a new print."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    gated_slice_routes(gate)
    queued = queue_route()
    gate.open()

    first = start(client, output_id, {**body(), "request_id": str(uuid.uuid4())})
    follow_run(client, first.json()["id"])
    second = start(client, output_id, {**body(), "request_id": str(uuid.uuid4())})

    assert second.status_code == 202, second.text
    assert second.json()["id"] != first.json()["id"]
    assert follow_run(client, second.json()["id"])["status"] == "succeeded"
    assert queued.call_count == 2


@respx.mock
def test_another_request_for_the_same_output_is_another_run(
    client: TestClient, model: str, gate: Gate
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    gated_slice_routes(gate)
    queued = queue_route()
    gate.open()

    first = start(client, output_id, run_request(copies=1))
    follow_run(client, first.json()["id"])
    second = start(client, output_id, run_request(copies=2))

    assert second.status_code == 202
    assert second.json()["id"] != first.json()["id"]
    follow_run(client, second.json()["id"])
    assert queued.call_count == 2


@respx.mock
def test_a_slot_error_found_after_the_upload_is_the_runs_failure(
    client: TestClient, model: str
) -> None:
    """Only a library file answers a plate's slots, so this refusal comes after the
    202: the run fails with the 422 and the message the route used to answer with."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    sliced = respx.route(method="POST", path__regex=r"/api/v1/library/files/\d+/slice")

    response = start(client, output_id, {**body(), "filament_plan": {"slots": []}})
    assert response.status_code == 202, response.text
    run = follow_run(client, response.json()["id"])

    assert run["status"] == "failed"
    assert run["result"] is None
    assert run["error"]["status"] == 422
    assert "Slot 1 has no spool chosen." in run["error"]["detail"]
    assert not sliced.called


@respx.mock
def test_a_failed_slice_is_the_runs_failure_in_bambuddys_words(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    respx.route(method="POST", path__regex=r"/api/v1/library/files/\d+/slice").mock(
        return_value=httpx.Response(200, json={"job_id": 9, "status": "pending"})
    )
    respx.get(f"{API}/slice-jobs/9").mock(
        return_value=httpx.Response(
            200, json={"id": 9, "status": "failed", "error": "object floats above the bed"}
        )
    )
    queued = queue_route()

    response = start(client, output_id, body())
    run = follow_run(client, response.json()["id"])

    assert run["status"] == "failed"
    assert run["error"]["status"] == 502
    assert "object floats above the bed" in run["error"]["detail"]
    assert run["error"]["extensions"] == {"slice_job_id": 9}
    assert not queued.called


@respx.mock
def test_a_retry_after_a_failure_before_any_enqueue_starts_a_new_run(
    client: TestClient, model: str
) -> None:
    """Nothing was queued (a slot error comes before any slice), so the key is free."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    request = {**body(), "filament_plan": {"slots": []}}

    first = start(client, output_id, request)
    failed = follow_run(client, first.json()["id"])
    assert failed["status"] == "failed"
    assert failed["may_have_queued"] is False
    second = start(client, output_id, request)

    assert second.status_code == 202
    assert second.json()["id"] != first.json()["id"]
    follow_run(client, second.json()["id"])


@respx.mock
def test_a_queue_call_that_timed_out_holds_the_key_so_a_retry_queues_nothing(
    client: TestClient, model: str, gate: Gate
) -> None:
    """The ``POST /queue/`` timed out (a 504): Bambuddy may have created the item, so
    the run says it may be queued and a retry answers with it instead of queueing."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    gated_slice_routes(gate)
    gate.open()
    queued = respx.post(f"{API}/queue/").mock(side_effect=httpx.ReadTimeout("slow"))

    first = start(client, output_id, body())
    failed = follow_run(client, first.json()["id"])
    assert failed["status"] == "failed"
    assert failed["error"]["status"] == 504
    # The problem's type, as the synchronous 504 carried it, so a client can tell a
    # Bambuddy call that may have gone through from any other failure.
    assert failed["error"]["type"] == UNAVAILABLE_PROBLEM
    assert failed["may_have_queued"] is True

    again = start(client, output_id, body())
    assert again.status_code == 200, again.text
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["may_have_queued"] is True
    assert queued.call_count == 1


@respx.mock
def test_a_later_plate_failing_after_an_earlier_one_queued_holds_the_key(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """All plates: plate 1 is queued, then plate 2's slice fails. A retry must not
    queue plate 1 again."""
    output_id = prepared(client, model)
    [path] = paths.outputs.glob(f"*/{output_id}/model.3mf")
    add_plate(path, 2)
    upload_route()
    run_routes()
    respx.route(method="POST", path__regex=r"/api/v1/library/files/\d+/slice").mock(
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
    queued = queue_route()
    request = run_request(all_plates=True)

    first = start(client, output_id, request)
    failed = follow_run(client, first.json()["id"])
    assert failed["status"] == "failed"
    assert failed["error"]["status"] == 502
    assert failed["may_have_queued"] is True

    again = start(client, output_id, request)
    assert again.status_code == 200, again.text
    assert again.json()["id"] == first.json()["id"]
    assert queued.call_count == 1


@respx.mock
def test_a_refusal_the_choices_decide_is_still_answered_before_any_run(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    uploaded = upload_route()
    run_routes()

    response = start(client, output_id, body(nozzles=[{"size": "0.2"}, {"size": "0.4"}]))

    assert response.status_code == 422
    assert "different sizes" in response.json()["detail"]
    assert not uploaded.called


@respx.mock
def test_two_posts_that_race_get_one_run(client: TestClient, model: str, gate: Gate) -> None:
    """Both reach Temporal before either has a record: one execution (USE_EXISTING),
    one row, and the second answer is the first's run (#1052, spec 2026-10-01 §5.1)."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    gated_slice_routes(gate)
    queued = queue_route()
    answers: list[httpx.Response] = []

    def post() -> None:
        answers.append(start(client, output_id, body()))

    racers = [threading.Thread(target=post) for _ in range(2)]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join()

    assert sorted(answer.status_code for answer in answers) == [200, 202]
    assert len({answer.json()["id"] for answer in answers}) == 1
    gate.open()
    follow_run(client, answers[0].json()["id"])
    assert queued.call_count == 1


@respx.mock
def test_a_request_id_retry_after_a_recorded_failure_returns_that_run(
    client: TestClient, model: str
) -> None:
    """One press is one print (§5.2): its retry, however late, gets the run as
    recorded, here a failure found after the upload, and starts nothing."""
    output_id = prepared(client, model)
    uploaded = upload_route()
    run_routes()
    request = {**body(), "filament_plan": {"slots": []}, "request_id": uuid.uuid4().hex}

    first = start(client, output_id, request)
    assert first.status_code == 202, first.text
    failed = follow_run(client, first.json()["id"])
    assert failed["status"] == "failed"
    calls = uploaded.call_count

    retry = start(client, output_id, request)

    assert retry.status_code == 200, retry.text
    assert retry.json()["id"] == first.json()["id"]
    assert retry.json()["repeated"] is True
    assert uploaded.call_count == calls


def test_temporal_unreachable_is_a_503_and_writes_nothing(
    client: TestClient, model: str, app: FastAPI, pg_conninfo: str
) -> None:
    output_id = prepared(client, model)
    state = getattr(app.state, STATE_ATTR)
    runs = state.print_runs
    state.print_runs = dataclasses.replace(runs, client=connect_lazily("127.0.0.1:1", "default"))
    try:
        response = start(client, output_id, body())
    finally:
        state.print_runs = runs

    assert response.status_code == 503, response.text
    assert response.json()["type"].endswith("/temporal-unavailable")
    assert response.headers["Retry-After"] == "5"
    with psycopg.connect(pg_conninfo) as conn:
        assert conn.execute("SELECT count(*) FROM print_runs").fetchone() == (0,)


def _insert_run(conninfo: str, output_id: str, key: str) -> str:
    run_id = uuid.uuid4().hex
    with psycopg.connect(conninfo) as conn:
        conn.execute(
            "INSERT INTO print_runs (id, output_id, idempotency_key, status)"
            " VALUES (%s, %s, %s, 'running')",
            (run_id, output_id, key),
        )
    return run_id


def test_a_live_run_found_by_its_key_is_returned_without_touching_bambuddy(
    client: TestClient, model: str, pg_conninfo: str
) -> None:
    """Our record is read first (§4.2): a retry answers with the run in flight, even
    with Bambuddy unreachable (no route is mocked here) and without calling Temporal."""
    output_id = prepared(client, model)
    request = body()
    key = run_key(output_id, PrintRunRequest.model_validate(request))
    live = _insert_run(pg_conninfo, output_id, key)

    response = start(client, output_id, request)

    assert response.status_code == 200, response.text
    assert response.json()["id"] == live
    assert response.json()["status"] == "running"


@respx.mock
def test_the_run_survives_the_app_that_accepted_it(
    settings: Settings, model: str, gate: Gate
) -> None:
    """The process that answered 202 goes away mid-slice; the next one, on the same
    database and queue, finishes the run (#1052: run 3c241a88 was lost this way)."""
    with TestClient(create_app(settings)) as first:
        output_id = prepared(first, model)
        upload_route()
        run_routes()
        gated_slice_routes(gate)
        queued = queue_route()
        run_id = start(first, output_id, body()).json()["id"]
    gate.open()
    with TestClient(create_app(settings)) as second:
        ended = follow_run(second, run_id, timeout=90)
    assert ended["status"] == "succeeded", ended
    assert queued.call_count == 1


def test_the_key_is_the_output_and_the_request_not_its_spelling() -> None:
    one = PrintRunRequest.model_validate(json.loads(json.dumps(body())))
    reordered = PrintRunRequest.model_validate(dict(reversed(list(body().items()))))
    assert run_key("a" * 32, one) == run_key("a" * 32, reordered)
    assert run_key("a" * 32, one) != run_key("b" * 32, one)
    assert run_key("a" * 32, one) != run_key(
        "a" * 32, PrintRunRequest.model_validate(run_request(copies=2))
    )


def test_the_request_id_is_part_of_the_key_and_its_absence_keeps_the_old_key() -> None:
    plain = PrintRunRequest.model_validate(body())
    one = PrintRunRequest.model_validate({**body(), "request_id": "one"})
    two = PrintRunRequest.model_validate({**body(), "request_id": "two"})
    assert len({run_key("a" * 32, r) for r in (plain, one, two)}) == 3
    # An older client that sends none keeps the key it had before the field existed.
    before = json.dumps(
        plain.model_dump(
            mode="json", exclude={"request_id", "print_sequence", "rack_position", "rack_algorithm"}
        ),
        sort_keys=True,
        separators=(",", ":"),
    )
    assert run_key("a" * 32, plain) == hashlib.sha256(f"{'a' * 32}\n{before}".encode()).hexdigest()


def test_the_print_sequence_is_part_of_the_key_only_when_chosen() -> None:
    """#907: a request with no sequence keeps the key it had before the field existed,
    and each sequence is a print of its own."""
    plain = PrintRunRequest.model_validate(body())
    by_object = PrintRunRequest.model_validate({**body(), "print_sequence": "by object"})
    by_layer = PrintRunRequest.model_validate({**body(), "print_sequence": "by layer"})
    assert len({run_key("a" * 32, r) for r in (plain, by_object, by_layer)}) == 3
    assert "print_sequence" not in json.dumps(plain.model_dump(mode="json", exclude_none=True))


def test_the_rack_pick_is_part_of_the_key_only_when_chosen() -> None:
    """#836: a request with no rack position or algorithm keeps the key it had before
    the fields existed, and each choice is a print of its own."""
    plain = PrintRunRequest.model_validate(body())
    by_hand = PrintRunRequest.model_validate({**body(), "rack_position": 3})
    other_hand = PrintRunRequest.model_validate({**body(), "rack_position": 4})
    oldest = PrintRunRequest.model_validate({**body(), "rack_algorithm": "oldest_first"})
    keys = {run_key("a" * 32, r) for r in (plain, by_hand, other_hand, oldest)}
    assert len(keys) == 4
    dumped = json.dumps(plain.model_dump(mode="json", exclude_none=True))
    assert "rack_position" not in dumped and "rack_algorithm" not in dumped


def test_an_unknown_run_is_a_404(client: TestClient) -> None:
    response = client.get(f"/api/v1/print/runs/{uuid.uuid4().hex}")
    assert response.status_code == 404
    assert client.get("/api/v1/print/runs/not-a-run").status_code == 422


@respx.mock
def test_without_a_database_both_routes_are_a_503_and_nothing_is_uploaded(
    client: TestClient, model: str, app: FastAPI
) -> None:
    """Runs live only in Postgres: the established 503, not a 500 naming an exception."""
    output_id = prepared(client, model)
    run_routes()
    uploaded = upload_route()
    state = getattr(app.state, STATE_ATTR)
    runs = state.print_runs
    state.print_runs = dataclasses.replace(runs, store=PrintRunStore(None))
    try:
        for response in (
            start(client, output_id, body()),
            client.get(f"/api/v1/print/runs/{uuid.uuid4().hex}"),
        ):
            assert response.status_code == 503, response.text
            problem = response.json()
            assert problem["type"] == DATABASE_REQUIRED_PROBLEM
            assert "Error" not in problem["detail"]
    finally:
        state.print_runs = runs
    assert not uploaded.called
