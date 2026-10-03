"""Routes that start an ``Operation`` (#1053, spec 2026-10-01 §4.2), with a test-only
kind on the app's own ``bambuddy`` worker: the record lookup first, the answer, 202 past
the deadline, and ``GET /operations/{id}``."""

from __future__ import annotations

import asyncio
import dataclasses
import time
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi import APIRouter, FastAPI, Response
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState, OperationsDep
from scadbuddy.api.operations import IdempotencyKey, run_operation
from scadbuddy.core.problems import ApiError
from scadbuddy.operations.kinds import OperationKind
from scadbuddy.workflows.client import connect_lazily

#: Unique per run: the session's Temporal outlives each test's database schema.
PRESS_1, PRESS_2, PRESS_3, PRESS_4, PRESS_5 = (uuid.uuid4().hex for _ in range(5))

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
    state.operations.kinds["test"] = OperationKind("test", check, run)
    router = APIRouter()

    @router.post("/api/v1/test-op")
    async def post(
        body: dict[str, Any], response: Response, ops: OperationsDep, key: IdempotencyKey = None
    ) -> Any:
        return await run_operation(
            ops,
            response,
            kind=state.operations.kinds["test"],
            subject="s",
            request=body,
            idempotency_key=key,
        )

    app.include_router(router)
    with TestClient(app) as test_client:
        yield test_client


def post(client: TestClient, body: dict[str, Any], key: str | None = None) -> Any:
    headers = {"Idempotency-Key": key} if key else {}
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


def test_without_a_key_each_request_is_its_own_operation(
    client: TestClient, counts: Counts
) -> None:
    post(client, {"a": 1})
    post(client, {"a": 1})
    assert counts.runs == 2


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


def test_a_slow_done_command_answers_202_and_is_followed(client: TestClient) -> None:
    started = post(client, {"delay": 12}, key=PRESS_4)
    assert started.status_code == 202, started.text
    op = follow(client, started.json()["id"])
    assert op["status"] == "succeeded" and op["result"] == {"done": True, "n": 1}


def test_get_operation_404s_an_unknown_id(client: TestClient) -> None:
    assert client.get("/api/v1/operations/nope").status_code == 404


def test_temporal_unreachable_is_a_503_and_writes_nothing(
    client: TestClient, app: FastAPI, pg_conninfo: str
) -> None:
    state: AppState = getattr(app.state, STATE_ATTR)
    ops = state.operations
    state.operations = dataclasses.replace(ops, client=connect_lazily("127.0.0.1:1", "default"))
    try:
        response = post(client, {"a": 1}, key=PRESS_5)
    finally:
        state.operations = ops
    assert response.status_code == 503
    assert response.json()["type"].endswith("/temporal-unavailable")
    with psycopg.connect(pg_conninfo) as conn:
        assert conn.execute("SELECT count(*) FROM operations").fetchone() == (0,)
