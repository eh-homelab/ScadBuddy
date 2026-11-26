"""The task routes (#7364) on a real Temporal and Postgres. A task row is written for a
parked `wait_for_human` call and the harness is stubbed, so each test says what the run
answers."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from temporal_agent_harness.harness.agent_client import AgentClient, CallbackResultError
from temporalio.client import (
    Client,
    WorkflowUpdateHandle,
    WorkflowUpdateRPCTimeoutOrCancelledError,
)
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.api import tasks as tasks_api
from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.auth.service_credentials import ServiceCredentialStore
from scadbuddy.core.settings import Settings
from scadbuddy.tasks.store import TaskStore
from tests.api.test_flows import AGENT, register, script, settings, start, until
from tests.flows.flows_support import history_payloads

__all__ = ["settings"]

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

EMAIL = {
    "type": "object",
    "required": ["email"],
    "properties": {
        "email": {"type": "string", "format": "email"},
        "note": {"type": "string"},
    },
}
NOT_OPEN = "https://scadbuddy.dev/problems/tasks/not-open"
INVALID = "https://scadbuddy.dev/problems/tasks/invalid-answer"
GOOD = {"email": "jane@example.com"}


def refused(error_type: str) -> CallbackResultError:
    return CallbackResultError("no", error_type=error_type)


class Harness:
    """The run's side of `provide_callback_result`: one answer for each call, and the
    same Update id again is the same answer."""

    def __init__(self) -> None:
        self.sends: list[tuple[str, Any, str | None]] = []
        self.resolved: dict[str, tuple[str | None, Any]] = {}
        #: Raised, in order, by the next sends, before anything lands.
        self.fails: list[BaseException] = []
        self.delay = 0.0

    async def provide(
        self, tool_id: str, *, result: Any = None, error: Any = None, update_id: Any = None
    ) -> None:
        self.sends.append((tool_id, result, update_id))
        if self.fails:
            raise self.fails.pop(0)
        await asyncio.sleep(self.delay)
        held = self.resolved.get(tool_id)
        if held is None:
            self.resolved[tool_id] = (update_id, result)
        elif held[0] != update_id:
            raise refused("CallbackAlreadyResolved")


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    fake = Harness()

    async def provide(_self: AgentClient, *args: Any, **kwargs: Any) -> None:
        await fake.provide(*args, **kwargs)

    monkeypatch.setattr(AgentClient, "provide_callback_result", provide)
    return fake


class Rows:
    def __init__(self, conninfo: str) -> None:
        self.conn = psycopg.connect(conninfo, autocommit=True)

    def add(
        self,
        run_id: str,
        task_id: str | None = None,
        *,
        place: str | None = None,
        state: str = "open",
    ) -> str:
        task_id = task_id or str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO tasks (id, run_id, title, schema, place, state)"
            " VALUES (%s, %s, 'Where to?', %s, %s, %s)",
            (task_id, run_id, Jsonb(EMAIL), place, state),
        )
        return task_id

    def task(self, task_id: str) -> tuple[Any, ...]:
        found = self.conn.execute(
            "SELECT state, answer, answered_by FROM tasks WHERE id = %s", (task_id,)
        ).fetchone()
        assert found is not None
        return found

    def decisions(self, call_id: str) -> list[Any]:
        return [
            row[0]
            for row in self.conn.execute(
                "SELECT response FROM workflow_run_decisions WHERE call_id = %s", (call_id,)
            )
        ]


@pytest.fixture
def rows(client: TestClient, pg_conninfo: str) -> Iterator[Rows]:
    held = Rows(pg_conninfo)
    yield held
    held.conn.close()


def parked(client: TestClient) -> tuple[str, str]:
    """A run waiting on one question: its id and the call's."""
    definition = register(client, script("a = await wait_for_human('q?', 600)", "return a"))
    run_id = start(client, definition["id"], "w").json()["id"]
    view = until(client, run_id, lambda v: v["status"] == "waiting" and v["pending"])
    return run_id, view["pending"][0]["call_id"]


@pytest.fixture
def task(client: TestClient, rows: Rows) -> str:
    """An open task on a parked call; its id is the call's."""
    return rows.add(*parked(client))


def post(client: TestClient, task_id: str, answer: Any, key: str = "k1", **headers: str) -> Any:
    return client.post(
        f"/api/v1/tasks/{task_id}/answer",
        json={"answer": answer},
        headers={"Idempotency-Key": key, **headers},
    )


def post_text(client: TestClient, task_id: str, body: str | bytes, **headers: str) -> Any:
    return client.post(
        f"/api/v1/tasks/{task_id}/answer",
        content=body,
        headers={"Idempotency-Key": "k1", **headers},
    )


JSON = {"Content-Type": "application/json"}


def test_a_valid_answer_resolves_the_callback_and_marks_the_task_answered(
    client: TestClient, task: str, rows: Rows, harness: Harness
) -> None:
    response = post(client, task, GOOD)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["id"], body["state"], body["answer"]) == (task, "answered", GOOD)
    assert body["schema"] == EMAIL
    [(call_id, result, update_id)] = harness.sends
    assert (call_id, result) == (task, {"answer": GOOD})
    assert update_id is not None and f":{task}:" in update_id
    assert rows.decisions(task) == [{"answer": GOOD}]
    assert rows.task(task) == ("answered", GOOD, "browser")
    assert client.get(f"/api/v1/tasks/{task}").json() == body


def test_an_invalid_email_is_422_naming_the_field_and_the_task_stays_open(
    client: TestClient, task: str, rows: Rows, harness: Harness
) -> None:
    response = post(client, task, {"email": "nope-secret"})
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["type"] == INVALID
    assert body["problems"] == [{"field": "email", "message": "not a valid email"}]
    assert "nope-secret" not in response.text
    assert rows.task(task) == ("open", None, None)
    assert harness.sends == []
    # The same key with a good answer is another request, and is taken.
    assert post(client, task, GOOD).status_code == 200


def test_what_is_no_address_is_422_and_the_task_stays_open(
    client: TestClient, task: str, rows: Rows, harness: Harness
) -> None:
    for bad in ("a@b", "@", "jane@example.com\n", "jane@example..com"):
        response = post(client, task, {"email": bad})
        assert response.status_code == 422, bad
        assert response.json()["type"] == INVALID
        assert response.json()["problems"] == [{"field": "email", "message": "not a valid email"}]
    assert rows.task(task) == ("open", None, None)
    assert harness.sends == []


def test_the_answer_is_checked_off_the_event_loop(
    client: TestClient, task: str, harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    off_loop: list[bool] = []

    def spy(schema: Any, answer: Any) -> list[Any]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            off_loop.append(True)
        else:
            off_loop.append(False)
        return []

    monkeypatch.setattr(tasks_api, "validate_answer", spy)
    assert post(client, task, GOOD).status_code == 200
    assert off_loop == [True]


def test_an_answer_the_database_cannot_hold_is_422(
    client: TestClient, task: str, rows: Rows, harness: Harness
) -> None:
    for bad in ('{"email": "a\\u0000"}', '{"email": "\\ud800"}', '{"email": NaN}'):
        response = post_text(client, task, '{"answer": ' + bad + "}", **JSON)
        assert (response.status_code, response.json()["type"]) == (422, INVALID), bad
    assert rows.task(task)[0] == "open"
    assert harness.sends == []


def test_two_answers_at_once_one_wins_the_other_is_409(
    client: TestClient, task: str, rows: Rows, harness: Harness
) -> None:
    harness.delay = 2.0
    answers = [{"email": "a@example.com"}, {"email": "b@example.com"}]
    with ThreadPoolExecutor(2) as pool:
        got = list(pool.map(lambda n: post(client, task, answers[n], f"k{n}"), range(2)))
    assert sorted(r.status_code for r in got) == [200, 409], [r.text for r in got]
    won = next(n for n, r in enumerate(got) if r.status_code == 200)
    lost = got[1 - won].json()
    # The winner's mark may still be on the way.
    assert lost["type"] == NOT_OPEN and lost["state"] in ("open", "answered")
    assert len(harness.sends) == 1
    assert rows.task(task)[:2] == ("answered", answers[won])
    assert rows.decisions(task) == [{"answer": answers[won]}]


def test_an_already_resolved_callback_is_409_with_the_tasks_own_state(
    client: TestClient, task: str, rows: Rows, harness: Harness
) -> None:
    harness.fails = [refused("CallbackAlreadyResolved")]
    response = post(client, task, GOOD)
    assert response.status_code == 409, response.text
    # The run has an answer and its mark is still on the way.
    assert (response.json()["type"], response.json()["state"]) == (NOT_OPEN, "open")
    assert rows.task(task) == ("open", None, None)
    assert rows.decisions(task) == []
    rows.conn.execute("UPDATE tasks SET state = 'expired'")
    again = post(client, task, GOOD)
    assert (again.status_code, again.json()["type"]) == (409, NOT_OPEN)
    assert again.json()["state"] == "expired"
    assert len(harness.sends) == 1


def test_an_answer_the_run_cannot_take_is_422_and_the_task_stays_open(
    client: TestClient, task: str, rows: Rows, harness: Harness
) -> None:
    harness.fails = [refused("MalformedCallbackResult")]
    response = post(client, task, GOOD)
    assert response.status_code == 422, response.text
    assert rows.task(task) == ("open", None, None)
    assert rows.decisions(task) == []


def test_a_call_the_run_no_longer_knows_is_409_and_the_task_is_left(
    client: TestClient, task: str, rows: Rows, harness: Harness
) -> None:
    harness.fails = [refused("UnknownCallback")]
    response = post(client, task, GOOD)
    assert response.status_code == 409, response.text
    assert response.json()["type"].endswith("/stale-entry")
    assert rows.task(task) == ("open", None, None)
    assert rows.decisions(task) == []


def test_a_task_whose_call_is_not_parked_is_409(
    client: TestClient, rows: Rows, harness: Harness
) -> None:
    run_id, _ = parked(client)
    other = rows.add(run_id)
    response = post(client, other, GOOD)
    assert response.status_code == 409, response.text
    assert response.json()["type"].endswith("/stale-entry")
    assert rows.task(other) == ("open", None, None)
    assert harness.sends == []


def test_a_task_of_a_run_that_ended_is_409(
    client: TestClient, rows: Rows, harness: Harness, task: str
) -> None:
    rows.conn.execute("UPDATE workflow_runs SET status = 'succeeded'")
    response = post(client, task, GOOD)
    assert response.status_code == 409, response.text
    assert response.json()["type"].endswith("/flow-run-closed")
    assert rows.task(task) == ("open", None, None)
    assert harness.sends == []


@pytest.mark.parametrize("state", ["cancelled", "expired", "answered"])
def test_a_task_that_is_not_open_is_409_with_its_state(
    client: TestClient, rows: Rows, harness: Harness, state: str
) -> None:
    run_id, call_id = parked(client)
    rows.add(run_id, call_id, state=state)
    # A bad answer too: the state is said first.
    for n, answer in enumerate((GOOD, {"email": 1})):
        response = post(client, call_id, answer, f"k{n}")
        assert response.status_code == 409, response.text
        assert (response.json()["type"], response.json()["state"]) == (NOT_OPEN, state)
    assert harness.sends == []


def test_the_same_request_again_answers_the_task_and_another_key_is_409(
    client: TestClient, task: str, harness: Harness
) -> None:
    first = post(client, task, {"email": "a@example.com", "note": "n"}, "once")
    assert first.status_code == 200, first.text
    again = post(client, task, {"note": "n", "email": "a@example.com"}, "once")
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    other = post(client, task, {"email": "a@example.com", "note": "n"}, "twice")
    assert other.status_code == 409, other.text
    assert (other.json()["type"], other.json()["state"]) == (NOT_OPEN, "answered")
    assert len(harness.sends) == 1


def test_a_failed_mark_answers_the_task_as_it_stands_and_is_not_made_again(
    client: TestClient,
    task: str,
    rows: Rows,
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marks: list[int] = []

    def fails(self: TaskStore, *args: Any, **kwargs: Any) -> Any:
        marks.append(1)
        raise RuntimeError("the database is away")

    monkeypatch.setattr(TaskStore, "mark_answered", fails)
    response = post(client, task, GOOD)
    assert response.status_code == 200, response.text
    assert (response.json()["state"], response.json()["answer"]) == ("open", None)
    assert len(marks) == 1
    assert len(harness.sends) == 1
    assert rows.task(task) == ("open", None, None)
    assert rows.decisions(task) == [{"answer": GOOD}]


def test_a_send_that_failed_is_retried_and_marked_once_it_landed(
    client: TestClient, task: str, rows: Rows, harness: Harness
) -> None:
    harness.fails = [RPCError("away", RPCStatusCode.UNAVAILABLE, b"")]
    response = post(client, task, GOOD)
    assert response.status_code == 200, response.text
    assert len(harness.sends) == 2
    assert rows.task(task)[:2] == ("answered", GOOD)


def never_lands(
    client: TestClient,
    rows: Rows,
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    body: dict[str, Any],
) -> None:
    """The first answer, recorded: each attempt's send ends with no answer from Temporal,
    which cannot say either whether it landed."""
    original = WorkflowUpdateHandle.result

    async def silent(self: WorkflowUpdateHandle[Any], **kwargs: Any) -> Any:
        if not self.id.startswith("flow:"):
            return await original(self, **kwargs)
        raise WorkflowUpdateRPCTimeoutOrCancelledError() from RPCError(
            "deadline", RPCStatusCode.DEADLINE_EXCEEDED, b""
        )

    harness.fails = [RPCError("away", RPCStatusCode.UNAVAILABLE, b"") for _ in range(3)]
    with monkeypatch.context() as away:
        away.setattr(WorkflowUpdateHandle, "result", silent)
        response = client.post(url, json=body, headers={"Idempotency-Key": "first"})
        assert response.status_code != 200, response.text
        deadline = time.monotonic() + 60
        while rows.conn.execute("SELECT 1 FROM operations WHERE status = 'running'").fetchone():
            assert time.monotonic() < deadline
            time.sleep(0.1)
    assert len(harness.sends) == 3
    assert harness.resolved == {}
    harness.sends.clear()


def test_an_answer_that_never_landed_does_not_block_the_task(
    client: TestClient,
    task: str,
    rows: Rows,
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = {"answer": {"email": "first@example.com"}}
    never_lands(client, rows, harness, monkeypatch, f"/api/v1/tasks/{task}/answer", first)
    assert rows.decisions(task) == [first]
    response = post(client, task, GOOD, "second")
    assert response.status_code == 200, response.text
    assert [send[1] for send in harness.sends] == [{"answer": GOOD}]
    assert rows.decisions(task) == [{"answer": GOOD}]
    assert rows.task(task)[:2] == ("answered", GOOD)


def test_of_two_answers_after_one_that_never_landed_one_wins(
    client: TestClient,
    task: str,
    rows: Rows,
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = {"answer": {"email": "first@example.com"}}
    never_lands(client, rows, harness, monkeypatch, f"/api/v1/tasks/{task}/answer", first)
    harness.delay = 2.0
    answers = [{"email": "a@example.com"}, {"email": "b@example.com"}]
    with ThreadPoolExecutor(2) as pool:
        got = list(pool.map(lambda n: post(client, task, answers[n], f"k{n}"), range(2)))
    assert sorted(r.status_code for r in got) == [200, 409], [r.text for r in got]
    won = next(n for n, r in enumerate(got) if r.status_code == 200)
    assert [send[1] for send in harness.sends] == [{"answer": answers[won]}]
    assert rows.decisions(task) == [{"answer": answers[won]}]
    assert rows.task(task)[:2] == ("answered", answers[won])


def test_an_answer_that_never_landed_does_not_block_a_question(
    client: TestClient, rows: Rows, harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`wait_for_human`, answered through the flow's own route."""
    run_id, call_id = parked(client)
    url = f"/api/v1/workflow-runs/{run_id}/answer"
    first = {"call_id": call_id, "answer": "first"}
    never_lands(client, rows, harness, monkeypatch, url, first)
    response = client.post(
        url,
        json={"call_id": call_id, "answer": "second"},
        headers={"Idempotency-Key": "second"},
    )
    assert response.status_code == 200, response.text
    assert [send[1] for send in harness.sends] == [{"answer": "second"}]
    assert rows.decisions(call_id) == [{"answer": "second"}]


def test_the_clear_answer_is_in_no_record_no_history_and_no_log(
    client: TestClient,
    task: str,
    rows: Rows,
    harness: Harness,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    secret = "only-the-lid-7f3e"
    # One the run refuses, one the schema refuses, and one that is taken.
    harness.fails = [refused("MalformedCallbackResult")]
    assert post(client, task, {**GOOD, "note": f"{secret}-a"}, "a").status_code == 422
    assert post(client, task, {**GOOD, "note": secret, secret: 1}, "b").status_code == 422
    assert post(client, task, {**GOOD, "note": secret}, "c").status_code == 200
    found = rows.conn.execute(
        "SELECT o::text, workflow_id, request FROM operations o WHERE kind = 'task_answer'"
    ).fetchall()
    assert len(found) == 2

    async def history(workflow_id: str) -> bytes:
        temporal = await Client.connect(settings.temporal_address)
        return await history_payloads(temporal, workflow_id)

    for text, workflow_id, request in found:
        assert secret not in text
        assert set(request) == {"run_id", "task_id", "answer_sealed", "by"}
        payloads = asyncio.run(history(workflow_id))
        assert b"answer_sealed" in payloads
        assert secret.encode() not in payloads
    assert secret not in caplog.text


def test_an_answer_that_is_not_an_object_is_422(
    client: TestClient, task: str, rows: Rows, harness: Harness
) -> None:
    for bad in ("text", ["a"], 1, None):
        assert post(client, task, bad).status_code == 422, bad
    assert rows.task(task)[0] == "open"
    assert harness.sends == []


def test_a_17_kb_answer_is_refused(
    client: TestClient, task: str, rows: Rows, harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    walked: list[Any] = []

    def spy(schema: Any, answer: Any) -> list[Any]:
        walked.append(answer)
        return []

    monkeypatch.setattr(tasks_api, "validate_answer", spy)
    assert post(client, task, {**GOOD, "note": "x" * 17 * 1024}).status_code == 413
    # Small in characters, not in bytes.
    assert post(client, task, {**GOOD, "note": "é" * 9 * 1024}).status_code == 413
    # Before the task is even looked for.
    missing = post(client, str(uuid.uuid4()), {"note": "x" * 17 * 1024})
    assert missing.status_code == 413
    assert walked == []
    assert harness.sends == []
    assert rows.task(task)[0] == "open"


def test_an_answer_that_fits_is_taken_however_it_is_escaped(
    client: TestClient, task: str, harness: Harness
) -> None:
    escaped = json.dumps({"answer": {**GOOD, "note": "é" * 6000}})
    assert len(escaped) > 32 * 1024
    response = post_text(client, task, escaped, **JSON)
    assert response.status_code == 200, response.text


def test_a_large_body_is_refused_before_it_is_parsed(client: TestClient, task: str) -> None:
    for headers in (JSON, {}):
        # Not JSON: a parser would answer 422.
        response = post_text(client, task, b"{" + b" " * 200 * 1024, **headers)
        assert response.status_code == 413, response.text


def test_a_deep_body_is_refused(client: TestClient, task: str, harness: Harness) -> None:
    for depth in (100, 8000, 9900, 50_000):
        body = '{"answer": {"email": ' + "[" * depth + "]" * depth + "}}"
        response = post_text(client, task, body, **JSON)
        assert response.status_code in (400, 413, 422), (depth, response.text)
    assert harness.sends == []


def test_the_answer_route_refuses_a_service_caller(
    client: TestClient, app: FastAPI, task: str, rows: Rows, harness: Harness
) -> None:
    store = getattr(app.state, STATE_ATTR).settings_store
    issued = ServiceCredentialStore(store.pool).issue("projects", "bootstrap", rate_limited=False)
    for headers in ({"Authorization": f"Bearer {issued.token}"}, AGENT):
        response = post(client, task, GOOD, **headers)
        assert response.status_code == 403, response.text
        assert response.json()["type"].endswith("/flow-answer-not-allowed")
        # Who asks is checked before the size.
        assert post(client, task, {"note": "x" * 17 * 1024}, **headers).status_code == 403
    assert rows.task(task)[0] == "open"
    assert harness.sends == []


def test_an_answer_needs_an_idempotency_key(client: TestClient, task: str) -> None:
    response = client.post(f"/api/v1/tasks/{task}/answer", json={"answer": GOOD})
    assert response.status_code == 428


def test_an_unknown_task_is_404_and_an_id_that_is_no_uuid_is_422(client: TestClient) -> None:
    missing = str(uuid.uuid4())
    assert client.get(f"/api/v1/tasks/{missing}").status_code == 404
    assert post(client, missing, GOOD).status_code == 404
    for bad in ("t1", missing.upper(), f"{missing}%0A", f"{missing}%00"):
        assert client.get(f"/api/v1/tasks/{bad}").status_code == 422, bad
        assert post(client, bad, GOOD).status_code == 422, bad


def test_the_flow_answer_route_takes_no_answer_for_a_prompt(
    client: TestClient, rows: Rows, harness: Harness
) -> None:
    """A typed question is answered through its task only, where its answer is checked."""
    run_id, call_id = parked(client)
    rows.add(run_id, call_id)
    # A text the parked callback would take, and one that is the answer's JSON.
    for n, text in enumerate(("yes", json.dumps(GOOD))):
        response = client.post(
            f"/api/v1/workflow-runs/{run_id}/answer",
            json={"call_id": call_id, "answer": text},
            headers={"Idempotency-Key": f"f{n}"},
        )
        assert response.status_code == 409, response.text
        assert response.json()["type"] == "https://scadbuddy.dev/problems/flow-answer-is-a-task"
    assert harness.sends == []
    assert rows.decisions(call_id) == []
    assert rows.task(call_id) == ("open", None, None)
    assert rows.conn.execute("SELECT 1 FROM operations WHERE kind = 'flow_answer'").fetchall() == []


def test_the_list_has_no_answer_and_not_who_gave_it(
    client: TestClient, task: str, harness: Harness
) -> None:
    held = post(client, task, GOOD).json()
    assert (held["answer"], held["answered_by"]) == (GOOD, "browser")
    assert client.get(f"/api/v1/tasks/{task}").json() == held
    response = client.get("/api/v1/tasks", params={"state": "answered"})
    del held["answer"], held["answered_by"]
    assert response.json() == {"tasks": [held]}
    assert GOOD["email"] not in response.text


def test_the_list_is_the_open_tasks_by_place_and_is_bounded(client: TestClient, rows: Rows) -> None:
    run_id, _ = parked(client)
    bench = rows.add(run_id, place="packing bench")
    desk = rows.add(run_id)
    done = rows.add(run_id, place="packing bench", state="answered")

    def ids(**params: Any) -> list[str]:
        response = client.get("/api/v1/tasks", params=params)
        assert response.status_code == 200, response.text
        return [t["id"] for t in response.json()["tasks"]]

    assert ids() == [bench, desk]
    assert ids(place="packing bench") == [bench]
    assert ids(state="answered") == [done]
    assert ids(limit=1) == [bench]
    # An agent reads them, as it reads a flow run.
    assert client.get("/api/v1/tasks", headers=AGENT).status_code == 200
    bad_params: list[dict[str, Any]] = [
        {"limit": 0},
        {"limit": 201},
        {"state": "gone"},
        {"place": ""},
        {"place": "x" * 101},
        {"place": "a\nb"},
        {"place": "a\x00"},
    ]
    for bad in bad_params:
        assert client.get("/api/v1/tasks", params=bad).status_code == 422, bad
