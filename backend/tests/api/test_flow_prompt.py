"""A flow's `prompt()` (#7364) in the whole app: a run really parked, its task on the run's
page, and the answer through `POST /tasks/{id}/answer`."""

from __future__ import annotations

import asyncio
import base64
import functools
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client

from scadbuddy.core.settings import Settings
from scadbuddy.flows import keys as flow_keys
from scadbuddy.tasks.models import Task
from scadbuddy.tasks.store import TaskNotOpenError, TaskStore
from scadbuddy.workflows.flow_tools import OPEN_WAIT
from tests.api.test_flow_reset import preview
from tests.api.test_flows import AGENT, register, script, start, until
from tests.crud.crud_support import TEST_KEK, DerivedPayloadKeys
from tests.flows.test_project_replay import HISTORIES, RECORD
from tests.support.agent_tables import AGENT_KEY_MIGRATIONS

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

EMAIL: dict[str, Any] = {
    "type": "object",
    "required": ["email"],
    "properties": {
        "email": {"type": "string", "format": "email"},
        "also_sms": {"type": "boolean", "default": True},
        "note": {"type": "string"},
    },
}
JANE = {"email": "jane@example.com"}
FULL = {**JANE, "also_sms": True, "note": None}
ASK = f"a = await prompt('Where to?', {EMAIL!r}, context=[{{'text': 'hi'}}], place='bench')"


@pytest.fixture
def settings(
    settings: Settings, tmp_path: Path, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> Settings:
    """Flows on, under keys a recorded history opens again with (`DerivedPayloadKeys`)."""
    key = tmp_path / "kek"
    key.write_text(base64.b64encode(TEST_KEK.key).decode())
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        for sql in AGENT_KEY_MIGRATIONS:
            conn.execute(sql.read_text().encode())
    monkeypatch.setattr(flow_keys, "PgPayloadKeys", DerivedPayloadKeys)
    return settings.model_copy(update={"secret_key_file": key})


def parked(client: TestClient, *body: str) -> tuple[str, dict[str, Any]]:
    """A run of `body` parked on its one prompt: its id, and its page's entry."""
    definition = register(client, script(*body))
    run_id = start(client, definition["id"], "w").json()["id"]
    view = until(client, run_id, lambda v: v["status"] == "waiting" and v["live"] and v["pending"])
    [entry] = view["pending"]
    return run_id, entry


def answer(client: TestClient, task_id: str, value: Any, key: str = "k1") -> Any:
    return client.post(
        f"/api/v1/tasks/{task_id}/answer", json={"answer": value}, headers={"Idempotency-Key": key}
    )


def task(client: TestClient, task_id: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/tasks/{task_id}")
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


def finished(client: TestClient, run_id: str) -> dict[str, Any]:
    done = until(client, run_id, lambda v: v["status"] in ("succeeded", "failed"))
    assert done["status"] == "succeeded", done
    return done


async def _history(settings: Settings, run_id: str) -> Any:
    temporal = await Client.connect(settings.temporal_address)
    return await temporal.get_workflow_handle(f"flow-{run_id}").fetch_history()


def test_a_prompt_answer_reaches_the_script_as_a_dict(
    client: TestClient, settings: Settings
) -> None:
    run_id, entry = parked(client, ASK, "return a['email']")
    assert (entry["kind"], entry["fn"], entry["prompt"]) == ("answer", "prompt", "Where to?")
    call_id = entry["call_id"]
    # The run's page has the task itself, as its own route and the list do.
    held = task(client, call_id)
    assert entry["task"] == held
    assert (held["state"], held["title"], held["schema"]) == ("open", "Where to?", EMAIL)
    assert (held["run_id"], held["place"], held["context"]) == (run_id, "bench", [{"text": "hi"}])
    listed = client.get("/api/v1/tasks", params={"place": "bench"}).json()["tasks"]
    assert [t["id"] for t in listed] == [call_id]
    # A text answer is not an answer to it, nor one that does not fit.
    text = client.post(
        f"/api/v1/workflow-runs/{run_id}/answer",
        json={"call_id": call_id, "answer": "jane@example.com"},
        headers={"Idempotency-Key": "t"},
    )
    assert text.status_code == 409 and text.json()["task_id"] == call_id
    bad = answer(client, call_id, {"email": "nope"}, "k0")
    assert bad.status_code == 422, bad.text
    agent = client.post(
        f"/api/v1/tasks/{call_id}/answer",
        json={"answer": JANE},
        headers={"Idempotency-Key": "g", **AGENT},
    )
    assert agent.status_code == 403
    assert task(client, call_id) == held

    given = answer(client, call_id, JANE)
    assert given.status_code == 200, given.text
    done = finished(client, run_id)
    assert done["run"]["result"] == "result: 'jane@example.com'"
    assert done["pending"] == []
    after = task(client, call_id)
    assert (after["state"], after["answer"], after["answered_by"]) == ("answered", JANE, "browser")
    again = answer(client, call_id, JANE, "k2")
    assert again.status_code == 409 and again.json()["state"] == "answered"
    history = asyncio.run(_history(settings, run_id))
    timers = [
        e.timer_started_event_attributes.start_to_fire_timeout.ToTimedelta()
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_TIMER_STARTED
    ]
    assert timers == [OPEN_WAIT]
    if RECORD:
        (HISTORIES / "prompted.json").write_text(history.to_json())


def test_omitted_fields_reach_the_script_and_the_task_ends_answered(client: TestClient) -> None:
    run_id, entry = parked(client, ASK, "return a")
    assert answer(client, entry["call_id"], JANE).status_code == 200
    assert finished(client, run_id)["run"]["result"] == f"result: {FULL!r}"
    after = task(client, entry["call_id"])
    # As it was sent.
    assert (after["state"], after["answer"]) == ("answered", JANE)


COUNT = {"type": "object", "properties": {"n": {"type": "integer"}}}


@pytest.mark.parametrize(
    ("body", "status", "said"),
    [
        (
            [
                "try:",
                f"    await prompt('How many?', {COUNT!r})",
                "except ValueError as e:",
                "    return str(e)",
            ],
            "succeeded",
            "the answer does not fit: n: ",
        ),
        ([f"await prompt('How many?', {COUNT!r})"], "failed", ""),
    ],
    ids=["caught", "uncaught"],
)
def test_an_answer_the_model_refuses_is_the_scripts_error(
    client: TestClient, body: list[str], status: str, said: str
) -> None:
    """JSON Schema takes 1e20 as an integer, and so does the route; the model that types it
    for the script does not."""
    run_id, entry = parked(client, *body)
    assert answer(client, entry["call_id"], {"n": 1e20}).status_code == 200
    done = until(client, run_id, lambda v: v["status"] in ("succeeded", "failed"))
    assert done["status"] == status
    assert said in (done["run"]["result"] or "")
    after = task(client, entry["call_id"])
    assert (after["state"], after["answer"]) == ("answered", {"n": 1e20})


def _marks(
    monkeypatch: pytest.MonkeyPatch, operation: Callable[..., Task], run: Callable[..., Task]
) -> None:
    """Every mark goes to `operation`, or with `final` to `run`, with the store's own."""
    original = TaskStore.mark_answered

    def mark(self: TaskStore, *args: Any, final: bool = False) -> Task:
        return (run if final else operation)(functools.partial(original, self), *args)

    monkeypatch.setattr(TaskStore, "mark_answered", mark)


def test_the_runs_mark_answers_the_task_when_the_operations_mark_failed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fails(store: Callable[..., Task], *args: Any) -> Task:
        raise RuntimeError("away")

    _marks(monkeypatch, fails, lambda store, *args: store(*args, final=True))
    run_id, entry = parked(client, ASK, "return a['email']")
    assert answer(client, entry["call_id"], JANE).status_code == 200
    finished(client, run_id)
    after = task(client, entry["call_id"])
    assert (after["state"], after["answer"], after["answered_by"]) == ("answered", JANE, None)


def test_the_runs_mark_replaces_a_wrong_best_effort_mark(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    marked = threading.Event()
    wrong = {"email": "wrong@example.com"}

    def another(store: Callable[..., Task], task_id: str, _: Any, *args: Any) -> Task:
        held = store(task_id, wrong, *args)
        assert held.answer == wrong
        marked.set()
        return held

    def after(store: Callable[..., Task], *args: Any) -> Task:
        assert marked.wait(30)
        return store(*args, final=True)

    _marks(monkeypatch, another, after)
    run_id, entry = parked(client, ASK, "return a['email']")
    assert answer(client, entry["call_id"], JANE).status_code == 200
    assert finished(client, run_id)["run"]["result"] == "result: 'jane@example.com'"
    held = task(client, entry["call_id"])
    assert (held["state"], held["answer"], held["answered_by"]) == ("answered", JANE, None)


def test_a_wrong_best_effort_mark_after_the_runs_changes_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = threading.Event()
    refused: list[str] = []

    def another(store: Callable[..., Task], task_id: str, _: Any, *args: Any) -> Task:
        assert final.wait(30)
        try:
            return store(task_id, {"email": "wrong@example.com"}, *args)
        except TaskNotOpenError as error:
            refused.append(error.state)
            raise

    def first(store: Callable[..., Task], *args: Any) -> Task:
        held = store(*args, final=True)
        final.set()
        return held

    _marks(monkeypatch, another, first)
    run_id, entry = parked(client, ASK, "return a['email']")
    assert answer(client, entry["call_id"], JANE).status_code == 200
    finished(client, run_id)
    assert refused == ["answered"]
    assert task(client, entry["call_id"])["answer"] == JANE


def test_a_reset_before_the_answer_opens_the_task_again(client: TestClient) -> None:
    run_id, entry = parked(
        client, ASK, "b = await wait_for_human('Second?')", "return [a['email'], b['answer']]"
    )
    call_id = entry["call_id"]
    first = {"email": "first@example.com"}
    assert answer(client, call_id, first).status_code == 200
    second = until(client, run_id, lambda v: [p["prompt"] for p in v["pending"]] == ["Second?"])
    assert task(client, call_id)["state"] == "answered"
    # The latest point that does the prompt again: the task that took its answer.
    points = client.get(f"/api/v1/workflow-runs/{run_id}/reset-points").json()["points"]
    point = max(p["event_id"] for p in points if p["first_step"] == 1)
    shown = preview(client, run_id, point)
    reset = client.post(
        f"/api/v1/workflow-runs/{run_id}/reset",
        json={
            "event_id": point,
            "as_of_event_id": shown["as_of_event_id"],
            "workflow_run_id": shown["workflow_run_id"],
        },
        headers={"Idempotency-Key": "reset-1"},
    )
    assert reset.status_code == 200, reset.text
    again = until(
        client,
        run_id,
        lambda v: (
            v["run"]["workflow_run_id"] != second["run"]["workflow_run_id"]
            and v["live"]
            and [p["call_id"] for p in v["pending"]] == [call_id]
        ),
    )
    held = again["pending"][0]["task"]
    assert (held["state"], held["answer"], held["answered_by"]) == ("open", None, None)
    given = answer(client, call_id, JANE, "k2")
    assert given.status_code == 200, given.text
    view = until(client, run_id, lambda v: [p["prompt"] for p in v["pending"]] == ["Second?"])
    go = client.post(
        f"/api/v1/workflow-runs/{run_id}/answer",
        json={"call_id": view["pending"][0]["call_id"], "answer": "go"},
        headers={"Idempotency-Key": "go"},
    )
    assert go.status_code == 200, go.text
    assert finished(client, run_id)["run"]["result"] == "result: ['jane@example.com', 'go']"
    assert task(client, call_id)["answer"] == JANE


def test_a_run_page_reads_its_tasks_once_and_only_its_own(
    client: TestClient, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    definition = register(
        client,
        script(
            f"await asyncio.gather(prompt('A?', {EMAIL!r}), prompt('B?', {EMAIL!r}),"
            " wait_for_human('C?'))"
        ),
    )
    run_id = start(client, definition["id"], "w").json()["id"]
    until(client, run_id, lambda v: v["live"] and len(v["pending"]) == 3)
    reads: list[tuple[str, list[str]]] = []
    original = TaskStore.of_run

    def of_run(self: TaskStore, run: str, ids: Any) -> dict[str, Task]:
        reads.append((run, sorted(ids)))
        return original(self, run, ids)

    monkeypatch.setattr(TaskStore, "of_run", of_run)
    view = client.get(f"/api/v1/workflow-runs/{run_id}").json()
    assert reads == [(run_id, sorted(p["call_id"] for p in view["pending"]))]
    by_prompt = {p["prompt"]: p["task"] for p in view["pending"]}
    assert by_prompt["C?"] is None
    assert {by_prompt[q]["title"] for q in ("A?", "B?")} == {"A?", "B?"}
    # A run with nothing parked reads none.
    reads.clear()
    other = start(client, register(client, script("return 1"), name="other")["id"], "o").json()[
        "id"
    ]
    finished(client, other)
    assert reads == []
    # A row of another run under the call's id is not this run's.
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute("UPDATE tasks SET run_id = %s WHERE title = 'A?'", (other,))
    view = client.get(f"/api/v1/workflow-runs/{run_id}").json()
    by_prompt = {p["prompt"]: p["task"] for p in view["pending"]}
    assert by_prompt["A?"] is None and by_prompt["B?"] is not None
