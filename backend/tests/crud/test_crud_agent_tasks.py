"""The agent's background tasks and its command record through CRUD routes (#5299 step 8,
`api/crud_agent_tasks.py`): only the ``agent`` credential reaches them, each route is the
agent's SQL as one transaction (`agent_data/background.py`, `agent_data/operations.py`),
a session's active tasks are capped under its lock, and an operation's activity row is
written in the transaction that writes the operation.

The agent's tables are made from its own migration files, as the API applies them at its
start (#5299 D6)."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from scadbuddy.activity.component import ACTIVITY
from scadbuddy.activity.index import ActivityIndex
from scadbuddy.agent_data import background
from scadbuddy.api import crud_agent_tasks, crud_areas
from scadbuddy.auth.check import auth_check
from scadbuddy.auth.component import AUTH
from scadbuddy.auth.gate import AuthGate
from scadbuddy.auth.service_credentials import (
    InProcessProvider,
    ServiceCaller,
    ServiceCredentialStore,
)
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.problems import install_problem_handlers
from scadbuddy.render.agent_migrations import migrate_agent
from tests.conftest import PgPool
from tests.crud.crud_support import _NoSessions, _Noted

pytestmark = pytest.mark.requires_postgres

BASE = "/api/v1/internal/crud/agent"
OWNER = {"kind": "browser", "id": "browser", "label": "You"}
KEY = "ab" * 32


def _app(pool: PgPool) -> FastAPI:
    credentials = ServiceCredentialStore(pool)
    parts: dict[Any, Any] = {
        AUTH: SimpleNamespace(
            would_refuse=_Noted(),
            verifiers={},
            credentials=credentials,
            core=SimpleNamespace(metrics=Metrics()),
        ),
        ACTIVITY: SimpleNamespace(index=ActivityIndex(pool, events=None)),
    }
    app = FastAPI(dependencies=[Depends(auth_check)])
    app.state.scadbuddy = SimpleNamespace(
        settings=SimpleNamespace(auth_mode="enforce"),
        components=SimpleNamespace(get=lambda key: parts[key]),
        settings_store=SimpleNamespace(pool=pool),
    )
    app.include_router(crud_agent_tasks.router, prefix="/api/v1")
    app.include_router(crud_areas.router, prefix="/api/v1")
    install_problem_handlers(app)
    app.add_middleware(AuthGate, sessions=lambda: _NoSessions(), credentials=lambda: credentials)
    return app


def _token(app: FastAPI, caller: ServiceCaller = "agent") -> dict[str, str]:
    credentials = app.state.scadbuddy.components.get(AUTH).credentials
    minted = InProcessProvider(caller, lambda: credentials).token()
    assert minted is not None
    return {"authorization": f"Bearer {minted}"}


class Agent:
    """The agent's calls, with its credential; each answer's JSON, checked 200."""

    def __init__(self, client: TestClient, headers: dict[str, str]) -> None:
        self.client = client
        self.headers = headers

    def get(self, path: str, **params: Any) -> Any:
        response = self.client.get(f"{BASE}{path}", params=params, headers=self.headers)
        assert response.status_code == 200, response.text
        return response.json()

    def post(self, path: str, body: Any) -> Any:
        response = self.client.post(f"{BASE}{path}", json=body, headers=self.headers)
        assert response.status_code == 200, response.text
        return response.json()

    def status(self, method: str, path: str, body: Any = None, **params: Any) -> int:
        response = self.client.request(
            method, f"{BASE}{path}", json=body, params=params, headers=self.headers
        )
        return int(response.status_code)


def _sql(pool: PgPool, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        cur = conn.execute(query.encode(), params)
        return list(cur.fetchall()) if cur.description else []


def _session(pool: PgPool, status: str = "running") -> str:
    session = str(uuid.uuid4())
    _sql(
        pool,
        "INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind,"
        " creator_id, status, max_turns, budget_usd) VALUES (%s, 'chat', 'browser', 'browser',"
        " 'You', 'browser', 'browser', %s, 10, 1)",
        (session, status),
    )
    return session


@pytest.fixture
def tables(pg_pool: PgPool) -> PgPool:
    with pg_pool.connection() as conn:
        migrate_agent(conn)
    return pg_pool


@pytest.fixture
def app(tables: PgPool) -> FastAPI:
    return _app(tables)


@pytest.fixture
def agent(app: FastAPI) -> Iterator[Agent]:
    with TestClient(app) as client:
        yield Agent(client, _token(app))


def _new(session: str, **extra: Any) -> dict[str, Any]:
    return {
        "session_id": session,
        "kind": "cron",
        "description": "every minute",
        "spec": {"cron": "* * * * *", "prompt": "hi"},
        "created_by": OWNER,
        **extra,
    }


def _operation(op_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": op_id,
        "kind": "plugin_package_install",
        "subject": "hindsight",
        "operation_key": KEY,
        "request": {"source": "x"},
        "workflow_id": f"agent-op-{op_id}",
        "workflow_run_id": "run1",
        "actor": {"principal": "token:t1", "session": "s1", "via_tool": "install"},
        **extra,
    }


# --- scope ------------------------------------------------------------------------------


def test_the_areas_are_listed() -> None:
    for area in ("background-tasks", "operations"):
        assert area in crud_areas.CRUD_AREAS["agent"]


def test_only_the_agent_credential_reaches_them(app: FastAPI, tables: PgPool) -> None:
    session = _session(tables)
    with TestClient(app) as client:
        for headers, code in (
            ({}, 401),
            (_token(app, "print"), 403),
            (_token(app, "projects"), 403),
            (_token(app, "render"), 403),
        ):
            create = client.post(f"{BASE}/background-tasks", json=_new(session), headers=headers)
            assert create.status_code == code
            listed = client.get(
                f"{BASE}/background-tasks", params={"session_id": session}, headers=headers
            )
            assert listed.status_code == code
            events = client.post(
                f"{BASE}/background-tasks/0000abcd/events", json={"text": "x"}, headers=headers
            )
            assert events.status_code == code
            insert = client.post(f"{BASE}/operations", json=_operation("o1"), headers=headers)
            assert insert.status_code == code
            assert client.get(f"{BASE}/operations/o1", headers=headers).status_code == code
    assert _sql(tables, "SELECT id FROM ai_background_tasks") == []
    assert _sql(tables, "SELECT id FROM ai_operations") == []


# --- background tasks -------------------------------------------------------------------


def test_a_task_is_made_read_and_listed(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    at = "2026-11-15T08:00:00Z"
    task = agent.post("/background-tasks", _new(session, next_at=at))["task"]
    assert task["session_id"] == session
    assert (task["kind"], task["state"], task["fired"], task["created_by"]) == (
        "cron",
        "active",
        0,
        OWNER,
    )
    assert task["next_at"].startswith("2026-11-15T08:00:00")
    assert agent.get(f"/background-tasks/{task['id']}")["task"]["id"] == task["id"]
    assert agent.get("/background-tasks/0000abcd")["task"] is None
    assert [t["id"] for t in agent.get("/background-tasks", session_id=session)["tasks"]] == [
        task["id"]
    ]
    assert agent.post(f"/background-tasks/{task['id']}/finish", {"state": "done"}) == {
        "finished": True
    }
    assert agent.get("/background-tasks", session_id=session)["tasks"] == []
    assert len(agent.get("/background-tasks", session_id=session, all=True)["tasks"]) == 1


def test_a_session_has_at_most_max_active_tasks(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    for _ in range(background.MAX_ACTIVE):
        assert agent.post("/background-tasks", _new(session))["task"] is not None
    assert agent.post("/background-tasks", _new(session))["task"] is None


def test_concurrent_creates_never_pass_the_cap(app: FastAPI, tables: PgPool) -> None:
    session = _session(tables)
    made: list[bool] = []
    lock = threading.Lock()

    def create() -> None:
        with TestClient(app) as client:
            response = client.post(
                f"{BASE}/background-tasks", json=_new(session), headers=_token(app)
            )
            with lock:
                made.append(response.json()["task"] is not None)

    threads = [threading.Thread(target=create) for _ in range(background.MAX_ACTIVE + 5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert made.count(True) == background.MAX_ACTIVE
    rows = _sql(tables, "SELECT count(*)::int AS n FROM ai_background_tasks")
    assert rows[0]["n"] == background.MAX_ACTIVE


def test_a_task_is_scheduled_fired_and_ended_once(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    task_id = agent.post("/background-tasks", _new(session))["task"]["id"]
    agent.post(f"/background-tasks/{task_id}/scheduled", {"at": "2026-11-15T09:00:00Z"})
    assert agent.get(f"/background-tasks/{task_id}")["task"]["next_at"].startswith(
        "2026-11-15T09:00:00"
    )
    agent.post(f"/background-tasks/{task_id}/fired", {"at": "2026-11-15T09:00:01Z"})
    fired = agent.get(f"/background-tasks/{task_id}")["task"]
    assert (fired["fired"], fired["next_at"]) == (1, None)
    assert fired["last_fired_at"].startswith("2026-11-15T09:00:01")
    agent.post(f"/background-tasks/{task_id}/progress", {"detail": "phase 2"})
    agent.post(f"/background-tasks/{task_id}/spec", {"spec": {"scriptPath": "a.js"}})
    kept = agent.get(f"/background-tasks/{task_id}")["task"]
    assert (kept["detail"], kept["spec"]) == ("phase 2", {"scriptPath": "a.js"})
    end = {"state": "cancelled", "detail": "cancelled by You"}
    assert agent.post(f"/background-tasks/{task_id}/finish", end) == {"finished": True}
    assert agent.post(f"/background-tasks/{task_id}/finish", end) == {"finished": False}
    # An ended task takes no more progress or fires.
    agent.post(f"/background-tasks/{task_id}/progress", {"detail": "late"})
    agent.post(f"/background-tasks/{task_id}/fired", {"at": "2026-11-15T09:00:02Z"})
    ended = agent.get(f"/background-tasks/{task_id}")["task"]
    assert (ended["state"], ended["detail"], ended["fired"]) == (
        "cancelled",
        "cancelled by You",
        1,
    )


def test_the_recurring_crons_are_listed(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    recurring = agent.post("/background-tasks", _new(session))["task"]["id"]
    once = _new(session, spec={"cron": "0 9 * * *", "recurring": False})
    agent.post("/background-tasks", once)
    agent.post("/background-tasks", _new(session, kind="wakeup", spec={"prompt": "x"}))
    listed = agent.get("/background-tasks/recurring-crons")["tasks"]
    assert [t["id"] for t in listed] == [recurring]


def test_a_monitors_events_are_capped_paged_and_delivered(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    task_id = agent.post("/background-tasks", _new(session, kind="monitor"))["task"]["id"]
    path = f"/background-tasks/{task_id}"
    for n in range(background.MAX_EVENTS):
        assert agent.post(f"{path}/events", {"text": f"line {n}"}) == {"appended": True}
    assert agent.post(f"{path}/events", {"text": "over"}) == {"appended": False}
    assert agent.post(f"{path}/events", {"text": "the end", "notice": True}) == {"appended": True}
    assert agent.get(f"{path}/events/count")["count"] == background.MAX_EVENTS + 1
    assert agent.get(f"{path}/events/count", pending=True)["count"] == background.MAX_EVENTS + 1
    first = agent.get(f"{path}/events/pending", max_chars=20)["events"]
    # As many as fit in 20 characters ("line 0", "line 1", "line 2"), and always the oldest.
    assert [e["text"] for e in first] == ["line 0", "line 1", "line 2"]
    assert [e["text"] for e in agent.get(f"{path}/events/pending", max_chars=1)["events"]] == [
        "line 0"
    ]
    agent.post(f"{path}/events/delivered", {"up_to": first[-1]["seq"]})
    assert agent.get(f"{path}/events/count", pending=True)["count"] == background.MAX_EVENTS - 2
    rest = agent.get(f"{path}/events/pending")["events"]
    assert rest[0]["text"] == "line 3" and rest[-1]["text"] == "the end"


def test_a_workflows_journal_keeps_the_first_write(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    task_id = agent.post("/background-tasks", _new(session, kind="workflow"))["task"]["id"]
    path = f"/background-tasks/{task_id}"
    assert agent.get(f"{path}/calls", key=KEY, occurrence=0)["call"] is None
    child = _session(tables)
    agent.post(f"{path}/calls", {"key": KEY, "occurrence": 0, "result": None, "ok": True})
    agent.post(
        f"{path}/calls",
        {"key": KEY, "occurrence": 1, "result": {"a": 1}, "ok": False, "child_id": child},
    )
    agent.post(f"{path}/calls", {"key": KEY, "occurrence": 1, "result": "second", "ok": True})
    # A null result is a row too.
    assert agent.get(f"{path}/calls", key=KEY, occurrence=0)["call"] == {"result": None, "ok": True}
    assert agent.get(f"{path}/calls", key=KEY, occurrence=1)["call"] == {
        "result": {"a": 1},
        "ok": False,
    }


def test_a_journal_row_the_agents_sql_kept_as_empty_reads_as_null(
    agent: Agent, tables: PgPool
) -> None:
    session = _session(tables)
    task_id = agent.post("/background-tasks", _new(session, kind="workflow"))["task"]["id"]
    _sql(
        tables,
        "INSERT INTO ai_workflow_calls (task_id, call_key, occurrence, result, ok)"
        " VALUES (%s, %s, 0, '{}', true)",
        (task_id, KEY),
    )
    path = f"/background-tasks/{task_id}/calls"
    assert agent.get(path, key=KEY, occurrence=0)["call"] == {"result": None, "ok": True}


def test_a_workflows_open_children_are_those_no_journal_names(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    task_id = agent.post("/background-tasks", _new(session, kind="workflow"))["task"]["id"]
    path = f"/background-tasks/{task_id}"
    journalled, done, open_child = _session(tables), _session(tables, "done"), _session(tables)
    for child in (journalled, done, open_child, open_child):
        agent.post(f"{path}/children", {"child_id": child})
    agent.post(
        f"{path}/calls",
        {"key": KEY, "occurrence": 0, "result": 1, "ok": True, "child_id": journalled},
    )
    assert agent.get(f"{path}/children/open")["children"] == [open_child]


def test_bad_bodies_are_refused(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    assert agent.status("POST", "/background-tasks", _new("not-a-uuid")) == 422
    assert agent.status("POST", "/background-tasks", _new(session, kind="daemon")) == 422
    assert agent.status("POST", "/background-tasks", _new(session, description="")) == 422
    owner = {**OWNER, "kind": "root"}
    assert agent.status("POST", "/background-tasks", _new(session, created_by=owner)) == 422
    assert agent.status("GET", "/background-tasks/NOT-HEX") == 422
    too_long = {"text": "x" * (background.NOTICE_MAX_CHARS + 1)}
    assert agent.status("POST", "/background-tasks/0000abcd/events", too_long) == 422
    assert agent.status("POST", "/background-tasks/0000abcd/finish", {"state": "active"}) == 422
    call = {"key": "short", "occurrence": 0, "ok": True}
    assert agent.status("POST", "/background-tasks/0000abcd/calls", call) == 422
    assert agent.status("POST", "/background-tasks/0000abcd/events/delivered", {"up_to": "x"}) == (
        422
    )


# --- operations -------------------------------------------------------------------------


def _activity(pool: PgPool, op_id: str) -> dict[str, Any] | None:
    rows = _sql(
        pool,
        "SELECT kind, status, principal, via_tool, detail FROM activity WHERE id = %s",
        (f"agent_operation:{op_id}",),
    )
    return rows[0] if rows else None


def test_an_operation_is_recorded_once_with_its_activity_row(agent: Agent, tables: PgPool) -> None:
    first = agent.post("/operations", _operation("o1"))["operation"]
    assert (first["id"], first["status"], first["finished_at"]) == ("o1", "running", None)
    assert (first["workflow_id"], first["workflow_run_id"]) == ("agent-op-o1", "run1")
    # A retried activity names the same execution: its row, not a second one.
    again = agent.post("/operations", _operation("o2", workflow_id="agent-op-o1"))["operation"]
    assert again["id"] == "o1"
    assert _sql(tables, "SELECT id FROM ai_operations") == [{"id": "o1"}]
    row = _activity(tables, "o1")
    assert row is not None
    assert (row["kind"], row["status"], row["principal"], row["via_tool"]) == (
        "agent.plugin_package_install",
        "running",
        "token:t1",
        "install",
    )
    assert agent.get("/operations/o1")["operation"]["id"] == "o1"
    assert agent.get(f"/operations/by-key/{KEY}")["operation"]["id"] == "o1"
    assert agent.get("/operations/none")["operation"] is None
    assert agent.get(f"/operations/by-key/{'cd' * 32}")["operation"] is None


def test_an_operation_ends_once(agent: Agent, tables: PgPool) -> None:
    agent.post("/operations", _operation("o1"))
    ended = agent.post("/operations/o1/finish", {"result": {"ok": True}})["operation"]
    assert (ended["status"], ended["result"]) == ("succeeded", {"ok": True})
    assert ended["finished_at"] is not None
    error = {"status": 409, "title": "Conflict", "detail": "late", "extensions": {"x": None}}
    again = agent.post("/operations/o1/finish", {"error": error})["operation"]
    assert (again["status"], again["error"]) == ("succeeded", None)
    row = _activity(tables, "o1")
    assert row is not None and row["status"] == "succeeded"
    assert row["detail"]["result"] == {"ok": True}

    agent.post("/operations", _operation("o2", workflow_id="agent-op-o2"))
    failed = agent.post("/operations/o2/finish", {"error": error})["operation"]
    # The problem as the agent sent it, a null extension kept.
    assert (failed["status"], failed["error"]) == ("failed", error)
    assert agent.status("POST", "/operations/none/finish", {"result": 1}) == 404
    both = {"result": 1, "error": error}
    assert agent.status("POST", "/operations/o2/finish", both) == 422


def test_a_null_result_is_a_success(agent: Agent, tables: PgPool) -> None:
    agent.post("/operations", _operation("o1", request=None))
    ended = agent.post("/operations/o1/finish", {})["operation"]
    assert (ended["status"], ended["result"]) == ("succeeded", None)


def test_an_insert_prunes_operations_past_the_retention(agent: Agent, tables: PgPool) -> None:
    agent.post("/operations", _operation("old"))
    agent.post("/operations/old/finish", {"result": 1})
    _sql(tables, "UPDATE ai_operations SET finished_at = now() - interval '2 hours'")
    agent.post("/operations", _operation("new", workflow_id="agent-op-new", retention_seconds=3600))
    assert _sql(tables, "SELECT id FROM ai_operations") == [{"id": "new"}]


def test_a_retention_under_an_hour_is_refused(agent: Agent, tables: PgPool) -> None:
    assert agent.status("POST", "/operations", _operation("x", retention_seconds=60)) == 422
    assert agent.status("POST", "/operations", _operation("x", retention_seconds=0)) == 422
    assert _sql(tables, "SELECT id FROM ai_operations") == []
