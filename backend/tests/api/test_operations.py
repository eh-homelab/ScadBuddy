"""Routes that start an ``Operation`` (#1053, spec 2026-10-01 §4.2), with a test-only
kind on the app's own ``bambuddy`` worker: the record lookup first, the answer, 202 past
the deadline, and ``GET /operations/{id}``."""

from __future__ import annotations

import asyncio
import dataclasses
import time
import uuid
from collections.abc import Iterator
from datetime import timedelta
from functools import partial
from typing import Any

import psycopg
import pytest
from fastapi import APIRouter, FastAPI, Response
from fastapi.testclient import TestClient

from scadbuddy.api import operations as operations_api
from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.api.operations import IdempotencyKey, run_operation
from scadbuddy.core.problems import ApiError
from scadbuddy.operations.component import OPERATIONS, OperationsDep
from scadbuddy.operations.kinds import OperationKind
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

#: Unique per run: the session's Temporal outlives each test's database schema.
PRESS_1, PRESS_2, PRESS_3, PRESS_4, PRESS_5, PRESS_6, PRESS_7 = (uuid.uuid4().hex for _ in range(7))

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]


class Counts:
    def __init__(self) -> None:
        self.runs = 0


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
    ops = state.components.get(OPERATIONS)
    test_kind = OperationKind("test", check, run)
    # Before the app starts, so its `bambuddy` worker serves the kind too.
    state.components.override(
        OPERATIONS, dataclasses.replace(ops, kinds={**ops.kinds, "test": test_kind})
    )
    router = APIRouter()

    @router.post("/api/v1/test-op")
    async def post(
        body: dict[str, Any], response: Response, ops: OperationsDep, key: IdempotencyKey = None
    ) -> Any:
        return await run_operation(
            ops,
            response,
            kind=test_kind,
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
    deadline = time.monotonic() + timeout
    while True:
        op: dict[str, Any] = client.get(f"/api/v1/operations/{op_id}").json()
        if op["status"] != "running" or time.monotonic() > deadline:
            return op
        time.sleep(0.2)


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
    if not nothing_done:
        assert "Idempotency-Key" in response.json()["detail"]
    assert ("cannot reach" in response.text) is not isinstance(error, TemporalBusyError)


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
