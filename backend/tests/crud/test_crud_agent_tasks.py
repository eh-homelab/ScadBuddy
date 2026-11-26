"""The agent's command record through CRUD routes (#5299 step 8, `api/crud_agent_tasks.py`):
only the ``agent`` credential reaches them, each route is the agent's SQL as one transaction
(`agent_data/operations.py`), and an operation's activity row is written in the transaction
that writes the operation. Area ``background-tasks`` and the operation reads are Go's
(#7667), tested in ``tools/go/core/internal/tasks``.

The agent's tables are made from its own migration files, as the API applies them at its
start (#5299 D6)."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from scadbuddy.activity.component import ACTIVITY
from scadbuddy.activity.index import ActivityIndex
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


def test_the_area_is_listed() -> None:
    assert "operations" in crud_areas.CRUD_AREAS["agent"]


def test_only_the_agent_credential_reaches_them(app: FastAPI, tables: PgPool) -> None:
    with TestClient(app) as client:
        for headers, code in (
            ({}, 401),
            (_token(app, "print"), 403),
            (_token(app, "projects"), 403),
            (_token(app, "render"), 403),
        ):
            insert = client.post(f"{BASE}/operations", json=_operation("o1"), headers=headers)
            assert insert.status_code == code
            finish = client.post(f"{BASE}/operations/o1/finish", json={}, headers=headers)
            assert finish.status_code == code
    assert _sql(tables, "SELECT id FROM ai_operations") == []


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
