"""Routes that start an ``Operation`` (#1053, spec 2026-10-01 §4.2), with a test-only
kind on the app's own ``bambuddy`` worker: the record lookup first, the answer, 202 past
the deadline, and ``GET /operations/{id}``."""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import importlib
import time
import uuid
from collections.abc import Iterator
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi import APIRouter, FastAPI, Response
from fastapi.testclient import TestClient
from pydantic import BaseModel
from temporalio import activity

import scadbuddy
from scadbuddy.api import operations as operations_api
from scadbuddy.api.accepting import COMMAND_RESEND_PROBLEM
from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.api.operations import IdempotencyKey, run_operation
from scadbuddy.core.authorship import AUTHOR_HEADER, AUTHOR_SESSION_HEADER, current_author
from scadbuddy.core.problems import ApiError
from scadbuddy.operations.component import OPERATIONS, OperationsDep
from scadbuddy.operations.kinds import OperationKind, to_thread_to_end
from scadbuddy.workflows import commands as commands_module
from scadbuddy.workflows.client import connect_lazily
from scadbuddy.workflows.commands import (
    COMMAND_ANSWER_DEADLINE,
    CommandClosedError,
    TemporalBusyError,
    TemporalRefusedError,
    TemporalUnavailableError,
    TemporalUnreachableError,
    start_command,
)
from tests.api.conftest import follow_operation
from tests.support.operations import press

#: Unique per run: the session's Temporal outlives each test's database schema.
PRESS_1, PRESS_2, PRESS_3, PRESS_4, PRESS_5, PRESS_6, PRESS_7 = (uuid.uuid4().hex for _ in range(7))

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]


class Counts:
    def __init__(self) -> None:
        self.runs = 0
        self.cancelled = 0
        self.events: list[str] = []


@pytest.fixture
def counts() -> Counts:
    return Counts()


@pytest.fixture
def client(app: FastAPI, counts: Counts) -> Iterator[TestClient]:
    async def check(request: dict[str, Any]) -> dict[str, Any]:
        if request.get("refuse"):
            raise ApiError(409, "refused, as the route would")
        return {"checked": True}

    async def run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        counts.runs += 1
        await asyncio.sleep(request.get("delay", 0))
        if request.get("fail"):
            raise ApiError(502, "Bambuddy said no", title="Bad Gateway")
        return {"done": checked["checked"], "n": counts.runs}

    state: AppState = getattr(app.state, STATE_ATTR)

    async def where(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        return {"queue": activity.info().task_queue}

    async def slow(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            counts.cancelled += 1
            raise
        return {}

    test_slow_kind = OperationKind("test_slow", check, slow, run_timeout=timedelta(seconds=2))
    gate = asyncio.Lock()

    async def threaded(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        def commit() -> None:
            time.sleep(12)  # past the 2 s timeout and the heartbeat that delivers it
            counts.events.append("committed")

        try:
            async with gate:
                await to_thread_to_end(commit)
        finally:
            counts.events.append("released")
        return {}

    test_threaded_kind = OperationKind(
        "test_threaded", check, threaded, run_timeout=timedelta(seconds=2)
    )

    async def slow_check(request: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(10)
        return {}

    test_slow_check_kind = OperationKind("test_slow_check", slow_check, where, queue="library")
    test_library_kind = OperationKind("test_library", check, where, queue="library")

    async def author(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0)  # off the activity's own task, as a commit in a thread is
        found = current_author()
        return {"principal": found and found.principal, "session": found and found.session}

    test_author_kind = OperationKind("test_author", check, author, queue="library")

    async def unhurried_check(request: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(3)  # past a 1 s FAST_ACCEPT and a 1 s follow, not the 10 s
        return {"checked": True}

    test_unhurried_kind = OperationKind("test_unhurried", unhurried_check, run)
    ops = state.components.get(OPERATIONS)
    added = [
        OperationKind("test", check, run),
        OperationKind("test_where", check, where),
        test_slow_kind,
        test_threaded_kind,
        test_slow_check_kind,
        test_library_kind,
        test_author_kind,
        test_unhurried_kind,
    ]
    # Before the app starts, so its workers serve the kinds too.
    state.components.override(
        OPERATIONS,
        dataclasses.replace(ops, kinds={**ops.kinds, **{kind.name: kind for kind in added}}),
    )
    router = APIRouter()

    @router.post("/api/v1/test-op")
    async def post(
        body: dict[str, Any],
        response: Response,
        ops: OperationsDep,
        key: IdempotencyKey = None,
        kind: str = "test",
    ) -> Any:
        return await run_operation(
            ops,
            response,
            kind=ops.kinds[kind],
            subject="s",
            request=body,
            idempotency_key=key,
        )

    app.include_router(router)
    with TestClient(app) as test_client:
        yield test_client


def post(client: TestClient, body: dict[str, Any], key: str | None = None) -> Any:
    """One press: ``key``, or a fresh one."""
    headers = {"Idempotency-Key": key or uuid.uuid4().hex}
    return client.post("/api/v1/test-op", json=body, headers=headers)


def follow(client: TestClient, op_id: str, timeout: float = 30) -> dict[str, Any]:
    # Read as `follow_operation` reads every later one: a 404 fails clearly (review #3007).
    response = client.get(f"/api/v1/operations/{op_id}")
    assert response.status_code == 200, response.text
    return follow_operation(client, response.json(), timeout)


def test_a_done_operation_answers_its_result(client: TestClient) -> None:
    response = post(client, {"a": 1})
    assert response.status_code == 200, response.text
    assert response.json() == {"done": True, "n": 1}


def test_a_retry_with_the_same_key_answers_the_record_and_runs_nothing(
    client: TestClient, counts: Counts
) -> None:
    first = post(client, {"a": 1}, key=PRESS_1)
    again = post(client, {"a": 1}, key=PRESS_1)
    assert first.json() == again.json() == {"done": True, "n": 1}
    assert counts.runs == 1


def test_a_request_without_a_key_is_refused_and_runs_nothing(
    client: TestClient, counts: Counts, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1143: a keyless write cannot be told from its own retry, so a proxy's re-send
    after a lost answer would upload, enqueue or create twice. It is refused (428) before
    anything starts."""

    async def no_start(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a keyless request must not reach Temporal")

    monkeypatch.setattr(operations_api, "start_command", no_start)
    response = client.post("/api/v1/test-op", json={"a": 1})
    assert response.status_code == 428, response.text
    assert response.json()["type"] == operations_api.KEY_REQUIRED_PROBLEM
    assert "Idempotency-Key" in response.json()["detail"]
    assert counts.runs == 0
    with psycopg.connect(pg_conninfo) as conn:
        assert conn.execute("SELECT count(*) FROM operations").fetchone() == (0,)


def test_a_refusal_answers_the_routes_problem_and_writes_nothing(
    client: TestClient, pg_conninfo: str
) -> None:
    response = post(client, {"refuse": True}, key=PRESS_2)
    assert response.status_code == 409
    assert response.json()["detail"] == "refused, as the route would"
    with psycopg.connect(pg_conninfo) as conn:
        assert conn.execute("SELECT count(*) FROM operations").fetchone() == (0,)


def test_a_retry_after_a_recorded_failure_answers_it_and_runs_nothing(
    client: TestClient, counts: Counts
) -> None:
    first = post(client, {"fail": True}, key=PRESS_3)
    again = post(client, {"fail": True}, key=PRESS_3)
    assert first.status_code == again.status_code == 502
    assert again.json()["detail"] == "Bambuddy said no"
    assert counts.runs == 1


def test_a_retry_whose_record_was_pruned_never_invites_a_repeat(
    client: TestClient, counts: Counts, pg_conninfo: str
) -> None:
    """Review #1063 8: a retention shorter than Temporal's leaves a closed execution
    with no row. The answer says it may have been done; it never runs it again."""
    assert post(client, {"a": 1}, key=PRESS_6).status_code == 200
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute("DELETE FROM operations")
    again = post(client, {"a": 1}, key=PRESS_6)
    assert again.status_code == 409, again.text
    assert "may have been done" in again.json()["detail"]
    assert "Check Bambuddy" in again.json()["detail"]
    assert again.json()["type"] == operations_api.RECORD_GONE_PROBLEM
    assert counts.runs == 1


def test_a_pruned_library_operation_names_the_model_not_bambuddy(
    client: TestClient, pg_conninfo: str
) -> None:
    """Review #1119 2-1: a ``library`` kind's effect is on the model and its checkouts."""
    headers = {"Idempotency-Key": PRESS_7}
    first = client.post("/api/v1/test-op?kind=test_library", json={}, headers=headers)
    assert first.status_code == 200, first.text
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute("DELETE FROM operations")
    again = client.post("/api/v1/test-op?kind=test_library", json={}, headers=headers)
    assert again.status_code == 409, again.text
    assert "Bambuddy" not in again.json()["detail"]
    assert "Check the model and its libraries" in again.json()["detail"]


def test_a_slow_done_command_answers_202_and_is_followed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        operations_api, "start_command", partial(start_command, deadline=timedelta(seconds=1))
    )
    started = post(client, {"delay": 20}, key=PRESS_4)
    # Re-sent with its key until it is recorded, as a client does: a loaded machine
    # may not have written the record within the deadline.
    resend_until = time.monotonic() + 60
    while started.json().get("type") == operations_api.STILL_ACCEPTING_PROBLEM:
        assert time.monotonic() < resend_until, started.text
        started = post(client, {"delay": 20}, key=PRESS_4)
    assert started.status_code == 202, started.text
    assert started.json()["repeated"] is False
    location = f"/api/v1/activity/operation:{started.json()['id']}"
    assert started.headers["Activity-Location"] == location
    op = follow(client, started.json()["id"], timeout=60)
    assert op["status"] == "succeeded" and op["result"] == {"done": True, "n": 1}
    # Review #1063 r6 4: `repeated` belongs to a route's 202, never to the record.
    assert "repeated" not in op


def test_only_the_202_documents_repeated(app: FastAPI) -> None:
    schemas = app.openapi()["components"]["schemas"]
    assert "repeated" not in schemas["Operation"]["properties"]
    assert "repeated" in schemas["OperationAccepted"]["properties"]


def test_get_operation_404s_an_unknown_id(client: TestClient) -> None:
    assert client.get(f"/api/v1/operations/{'0' * 32}").status_code == 404


def test_get_operation_422s_a_malformed_id(client: TestClient) -> None:
    """Review #1063 4: an id is always ``uuid4().hex``; anything else never reaches
    Postgres or the 404's detail."""
    for bad in ("nope", "A" * 32, "0" * 33):
        assert client.get(f"/api/v1/operations/{bad}").status_code == 422


def test_temporal_unreachable_is_a_503_and_writes_nothing(
    client: TestClient, app: FastAPI, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unreachable is known only once the deadline passed: production's, not the tests'.
    monkeypatch.setattr(
        operations_api, "start_command", partial(start_command, deadline=COMMAND_ANSWER_DEADLINE)
    )
    state: AppState = getattr(app.state, STATE_ATTR)
    ops = state.components.get(OPERATIONS)
    state.components.override(
        OPERATIONS, dataclasses.replace(ops, client=connect_lazily("127.0.0.1:1", "default"))
    )
    try:
        response = post(client, {"a": 1}, key=PRESS_5)
    finally:
        state.components.override(OPERATIONS, ops)
    assert response.status_code == 503
    assert response.json()["type"].endswith("/temporal-unavailable")
    with psycopg.connect(pg_conninfo) as conn:
        assert conn.execute("SELECT count(*) FROM operations").fetchone() == (0,)


def test_each_kind_runs_on_its_own_queue(client: TestClient) -> None:
    """§4.3: a worker serves only the kinds whose effect it holds."""
    library = client.post("/api/v1/test-op?kind=test_library", json={}, headers=press())
    bambuddy = client.post("/api/v1/test-op?kind=test_where", json={}, headers=press())
    assert library.status_code == bambuddy.status_code == 200, library.text
    assert library.json()["queue"].endswith("-library")
    assert not bambuddy.json()["queue"].endswith("-library")


def test_a_run_past_its_kinds_timeout_is_cancelled_and_recorded_failed(
    client: TestClient, counts: Counts
) -> None:
    """Review I2: the run heartbeats, so its timeout reaches the coroutine, which then
    stops rather than finishing an effect the record calls failed."""
    response = client.post("/api/v1/test-op?kind=test_slow", json={}, headers=press())
    assert response.status_code == 500, response.text
    deadline = time.monotonic() + 30
    while counts.cancelled == 0 and time.monotonic() < deadline:
        time.sleep(0.2)
    assert counts.cancelled == 1


def test_a_cancelled_run_holds_its_lock_until_its_thread_returns(
    client: TestClient, counts: Counts
) -> None:
    """Review #1119 1: a pin's clone and commit are threads, which a cancel cannot stop.
    The run keeps its gate until the thread has returned, so nothing that waits on the
    gate runs beside it; the effect itself can still land after the record failed."""
    response = client.post("/api/v1/test-op?kind=test_threaded", json={}, headers=press())
    assert response.status_code == 500, response.text
    # Review #1119 2-4: the record says the effect may have landed, not just "failed".
    assert "may have been done" in response.json()["detail"]
    deadline = time.monotonic() + 40
    while len(counts.events) < 2 and time.monotonic() < deadline:
        time.sleep(0.2)
    assert counts.events == ["committed", "released"]


def test_a_library_kinds_slow_check_does_not_blame_bambuddy(client: TestClient) -> None:
    """Review #1119 2: a ``library`` check reads the data volume, not Bambuddy."""
    response = client.post("/api/v1/test-op?kind=test_slow_check", json={}, headers=press())
    assert response.status_code == 504, response.text
    problem = response.json()
    assert problem["type"] == "about:blank"
    assert "Bambuddy" not in problem["detail"]
    assert "the check did not finish" in problem["detail"]


def test_an_execution_ended_before_it_answered_is_still_accepting(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #1061 1c: a terminate before the Update answered recorded nothing, so the
    client sends the same request again, never a bare 500."""

    async def closed_start(*args: Any, **kwargs: Any) -> Any:
        raise CommandClosedError("op-x")

    monkeypatch.setattr(operations_api, "start_command", closed_start)
    response = post(client, {})
    assert response.status_code == 503, response.text
    assert response.json()["type"] == operations_api.STILL_ACCEPTING_PROBLEM


def test_the_run_commits_as_the_requests_agent_author(client: TestClient) -> None:
    """A run is the request's, so a commit it makes is authored as the agent that asked
    (#252), not as ScadBuddy: the worker has no request of its own to read it from."""
    response = client.post(
        "/api/v1/test-op?kind=test_author",
        json={},
        headers={**press(), AUTHOR_HEADER: "token:abc123", AUTHOR_SESSION_HEADER: "s-1"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"principal": "token:abc123", "session": "s-1"}
    plain = client.post("/api/v1/test-op?kind=test_author", json={"again": 1}, headers=press())
    assert plain.json() == {"principal": None, "session": None}


def test_a_refusing_temporal_is_a_500_as_the_reconciler_reads_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #1316 (12) 1: the route reads a failed start as the lost-operation
    reconciler does (`temporal_failure`): a refusal is a misconfiguration, never "try
    again shortly", as the print-run routes answer it."""

    async def refused_start(*args: Any, **kwargs: Any) -> Any:
        raise TemporalRefusedError("op-x")

    monkeypatch.setattr(operations_api, "start_command", refused_start)
    response = post(client, {})
    assert response.status_code == 500, response.text
    assert response.json()["type"] == operations_api.TEMPORAL_REFUSED_PROBLEM
    assert "try again shortly" not in response.text
    assert response.json()["may_have_started"] is True


@pytest.mark.parametrize(
    ("error", "nothing_done"),
    [
        (TemporalUnreachableError("op-x"), True),
        (TemporalUnavailableError("op-x"), False),
        (TemporalBusyError("op-x"), False),
    ],
)
def test_only_a_failed_connect_says_nothing_was_done(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, error: Exception, nothing_done: bool
) -> None:
    """Review #1316 (9) 1a, as the print-run routes: only a failed first connect wrote
    nothing. Any other 503 may follow a persisted start, which the same key follows."""

    async def failing_start(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(operations_api, "start_command", failing_start)
    response = post(client, {})
    assert response.status_code == 503, response.text
    assert response.json()["type"].endswith("/temporal-unavailable")
    assert ("Nothing was done" in response.text) is nothing_done
    assert response.json()["may_have_started"] is not nothing_done
    detail = response.json()["detail"]
    if not nothing_done:
        # The client re-sends the same key on `may_have_started`; the person reads why.
        assert "a retry picks this request up" in detail
    assert "Temporal" not in detail and "Idempotency-Key" not in detail
    # Only a service that did not answer says so; a busy one answered.
    assert ("answer" in detail) is not isinstance(error, TemporalBusyError)


def test_a_command_route_documents_its_temporal_problems(app: FastAPI) -> None:
    """Review #1316 (13) 2a: every route on `run_operation` answers these, so its
    `OPERATION_RESPONSES` says so, quoting the details it sends."""
    responses = app.openapi()["paths"]["/api/v1/outputs/{output_id}/send"]["post"]["responses"]
    assert operations_api.TEMPORAL_REFUSED_PROBLEM in responses["500"]["description"]
    assert operations_api.OPERATION_REFUSED_DETAIL in responses["500"]["description"]
    for detail in (
        operations_api.OPERATION_UNREACHABLE_DETAIL,
        operations_api.OPERATION_DOWN_DETAIL,
        operations_api.OPERATION_BUSY_DETAIL,
        operations_api.OPERATION_STILL_CHECKING_DETAIL,
    ):
        assert detail in responses["503"]["description"]
    assert "may_have_started" in responses["503"]["description"]
    assert operations_api.RECORD_GONE_PROBLEM in responses["default"]["description"]
    assert operations_api.KEY_REQUIRED_PROBLEM in responses["428"]["description"]
    schema = responses["503"]["content"]["application/problem+json"]["schema"]
    assert "may_have_started" in schema["properties"]


@pytest.fixture
def short_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """#2794: a 1 s fast accept and a 1 s follow, so a 3 s check outlasts both."""
    monkeypatch.setattr(operations_api, "FAST_ACCEPT", timedelta(seconds=1))
    monkeypatch.setattr(commands_module, "FOLLOW_WAIT", timedelta(seconds=1))


@pytest.mark.usefixtures("short_waits")
def test_an_operation_still_accepting_answers_202_to_a_client_that_follows(
    client: TestClient, counts: Counts
) -> None:
    """#2794 (spec 2026-10-13): with `Prefer: respond-async`, an Update unanswered past
    `FAST_ACCEPT` is a 202 `accepting` naming the request's key, not a 503 to send
    again; following it answers the route's own body, and the operation ran once."""
    key = uuid.uuid4().hex
    started = client.post(
        "/api/v1/test-op?kind=test_unhurried",
        json={},
        headers={"Idempotency-Key": key, "Prefer": "respond-async"},
    )
    assert started.status_code == 202, started.text
    accepting = started.json()
    assert accepting["status"] == "accepting"
    assert accepting["update_id"] == key
    assert accepting["workflow_id"].startswith("op-test_unhurried-")
    assert started.headers["Preference-Applied"] == "respond-async"

    answer = client.post(accepting["follow_url"])
    deadline = time.monotonic() + 60
    while answer.status_code == 202 and answer.json().get("status") == "accepting":
        assert time.monotonic() < deadline, answer.text
        answer = client.post(accepting["follow_url"])

    assert answer.status_code == 200, answer.text
    assert answer.json() == {"done": True, "n": 1}
    assert counts.runs == 1


@pytest.mark.usefixtures("short_waits")
def test_without_the_preference_an_operation_under_the_deadline_answers_its_body(
    client: TestClient,
) -> None:
    """Every client that does not follow a 202 keeps the re-send it knows (§5.1)."""
    response = client.post("/api/v1/test-op?kind=test_unhurried", json={}, headers=press())
    # The 10 s deadline outlasts the 3 s check: the body, as before this change.
    assert response.status_code == 200, response.text


def test_with_the_preference_a_fast_operation_is_its_body_at_once(client: TestClient) -> None:
    """Review #2836: the preference alone never turns an answer inside `FAST_ACCEPT` into
    a 202: the route's own 200, and no `Preference-Applied`."""
    response = client.post(
        "/api/v1/test-op",
        json={},
        headers={"Idempotency-Key": uuid.uuid4().hex, "Prefer": "respond-async"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"done": True, "n": 1}
    assert "Preference-Applied" not in response.headers


def test_following_an_operation_temporal_does_not_know_is_a_resend(client: TestClient) -> None:
    response = client.post(f"/api/v1/commands/op-test-{'0' * 64}/updates/{uuid.uuid4().hex}")
    assert response.status_code == 409, response.text
    assert response.json()["type"] == COMMAND_RESEND_PROBLEM


def test_only_an_operations_own_id_is_followed(client: TestClient) -> None:
    response = client.post("/api/v1/commands/op-test-nothex/updates/k1")
    assert response.status_code == 404, response.text


@pytest.mark.usefixtures("short_waits")
def test_a_followed_done_kind_still_running_is_its_operation_to_follow(
    client: TestClient,
) -> None:
    """Review #2836: a `done` kind whose Update outlasts the follow's wait while its
    record says `running` answers its 202 `OperationAccepted`, which the client follows
    at /operations/{id}, as the route answers it."""
    started = client.post(
        "/api/v1/test-op?kind=test_unhurried",
        json={"delay": 6},
        headers={"Idempotency-Key": uuid.uuid4().hex, "Prefer": "respond-async"},
    )
    assert started.status_code == 202, started.text
    follow_url = started.json()["follow_url"]

    answer = client.post(follow_url)
    deadline = time.monotonic() + 60
    while answer.status_code == 202 and answer.json().get("status") == "accepting":
        assert time.monotonic() < deadline, answer.text
        answer = client.post(follow_url)

    assert answer.status_code == 202, answer.text
    running = answer.json()
    assert running["status"] == "running" and running["repeated"] is False
    # The route's own header for it too (review #2836).
    assert answer.headers["Activity-Location"] == operations_api.activity_location(running["id"])
    op = follow(client, running["id"], timeout=60)
    assert op["status"] == "succeeded" and op["result"] == {"done": True, "n": 1}


@pytest.mark.usefixtures("short_waits")
@pytest.mark.parametrize("key", ["a/b", ".", ".."])
def test_a_key_the_follow_url_cannot_carry_keeps_the_resend(client: TestClient, key: str) -> None:
    """Review #2836: a key with a `/` cannot be a path segment of the follow URL, so it
    is never offered the 202 (that was a 500 after the operation had started); it is
    answered as without the preference. A dot-only key is collapsed out of the path as
    a dot segment, so it is not offered one either."""
    response = client.post(
        "/api/v1/test-op?kind=test_unhurried",
        json={},
        headers={"Idempotency-Key": key, "Prefer": "respond-async"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["done"] is True


def _answer_models() -> list[type[BaseModel]]:
    """Every model a route hands `operation_answer`, read from the routes' source."""
    found: dict[str, type[BaseModel]] = {}
    for path in sorted((Path(scadbuddy.__file__).parent / "api").glob("*.py")):
        module = importlib.import_module(f"scadbuddy.api.{path.stem}")
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "operation_answer"
                and len(node.args) == 2
                and isinstance(node.args[1], ast.Name)
            ):
                model = getattr(module, node.args[1].id)
                found[f"{model.__module__}.{model.__qualname__}"] = model
    return list(found.values())


def test_every_operation_answer_model_serialises_as_its_stored_result() -> None:
    """Review #2836 (#3038): a follow sends an operation's stored ``result`` as it is,
    where the route sends ``Model.model_validate(result)``. A kind's run stores the
    model's ``model_dump(mode="json")`` (`OperationKind.run`), so an ``exclude`` field is
    absent from both; the two still differ for a model that renames, computes or
    re-serialises a field, so one that starts to is caught here rather than answering a
    follow differently from the route."""
    models = _answer_models()
    assert len(models) > 10, models  # the scan found the routes
    for model in models:
        assert not model.model_computed_fields, model
        assert not model.__pydantic_decorators__.field_serializers, model
        assert not model.__pydantic_decorators__.model_serializers, model
        for name, field in model.model_fields.items():
            assert field.alias in (None, name), (model, name)
            assert field.serialization_alias in (None, name), (model, name)
