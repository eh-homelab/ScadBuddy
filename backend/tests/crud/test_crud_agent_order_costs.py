"""The order items a session revised and its spend's report through CRUD routes (#5299 step 9,
part c, `api/crud_agent_order_costs.py`): only the ``agent`` credential reaches them, a report's
writes run only while the caller's token holds the session's lease (``order-cost:<id>``), and the
lease is busy while an agent pod still on SQL holds the session's advisory lock.

The agent's tables are made from its own migration files, as the API applies them at its
start (#5299 D6)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from scadbuddy.agent_data import leases
from scadbuddy.api import crud_agent_order_costs, crud_areas
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


def _app(pool: PgPool) -> FastAPI:
    credentials = ServiceCredentialStore(pool)
    parts: dict[Any, Any] = {
        AUTH: SimpleNamespace(
            would_refuse=_Noted(),
            verifiers={},
            credentials=credentials,
            core=SimpleNamespace(metrics=Metrics()),
        ),
    }
    app = FastAPI(dependencies=[Depends(auth_check)])
    app.state.scadbuddy = SimpleNamespace(
        settings=SimpleNamespace(auth_mode="enforce"),
        components=SimpleNamespace(get=lambda key: parts[key]),
        settings_store=SimpleNamespace(pool=pool),
    )
    app.include_router(crud_agent_order_costs.router, prefix="/api/v1")
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
    def __init__(self, client: TestClient, headers: dict[str, str]) -> None:
        self.client = client
        self.headers = headers

    def post(self, path: str, body: Any, expect: int = 200) -> Any:
        response = self.client.post(f"{BASE}{path}", json=body, headers=self.headers)
        assert response.status_code == expect, response.text
        return response.json()


def _sql(pool: PgPool, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        cur = conn.execute(query.encode(), params)
        return list(cur.fetchall()) if cur.description else []


def _session(pool: PgPool, *, status: str = "idle", cost: float = 0.0) -> str:
    sid = str(uuid.uuid4())
    _sql(
        pool,
        "INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind,"
        " creator_id, status, max_turns, budget_usd, cost_usd) VALUES (%s, 'chat', 'bearer',"
        " 'token:42', 'CI', 'bearer', 'token:42', %s, 10, 5, %s)",
        (sid, status, cost),
    )
    return sid


def _lease(pool: PgPool, sid: str) -> str | None:
    token = str(uuid.uuid4())
    with pool.connection() as conn, conn.transaction():
        held = leases.acquire(conn, f"order-cost:{sid}", token, leases.LEASE_TTL)
    return token if held is not None else None


@pytest.fixture
def tables(pg_pool: PgPool) -> PgPool:
    with pg_pool.connection() as conn:
        migrate_agent(conn)
    return pg_pool


@pytest.fixture
def agent(tables: PgPool) -> Iterator[Agent]:
    app = _app(tables)
    with TestClient(app) as client:
        yield Agent(client, _token(app))


def test_only_the_agent_credential_reaches_the_routes(tables: PgPool) -> None:
    app = _app(tables)
    with TestClient(app) as client:
        assert client.post(f"{BASE}/order-costs/sweep", json={"limit": 1}).status_code == 401
        for caller in ("render", "print", "projects"):
            wrong = client.post(
                f"{BASE}/order-costs/sweep", json={"limit": 1}, headers=_token(app, caller)
            )
            assert wrong.status_code == 403
        listed = client.get(f"{BASE}/areas", headers=_token(app)).json()["areas"]
        assert "session-order-costs" in listed


def test_a_report_runs_under_the_sessions_lease(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables, cost=1.5)
    path = f"/sessions/{sid}/order-costs"
    agent.post(path, {"order_id": "o1", "order_item_id": "i1"})
    agent.post(path, {"order_id": "o1", "order_item_id": "i2"})
    agent.post(path, {"order_id": "o2", "order_item_id": "i3"})
    agent.post(path, {"order_id": "o1", "order_item_id": "i1"})  # written once
    token = _lease(tables, sid)
    assert token is not None
    # A second holder is busy while the first holds it.
    assert _lease(tables, sid) is None
    report = agent.post(f"{path}/report", {"token": token})["report"]
    assert [p["order_item_id"] for p in report["pending"]] == ["i1", "i2", "i3"]
    assert report == {**report, "cost": 1.5, "carried": 0.0, "principal": "token:42"}
    assert report["over"] is False
    tried = _sql(
        tables, "SELECT tried_at FROM ai_session_order_costs WHERE session_id = %s", (sid,)
    )
    assert all(r["tried_at"] is not None for r in tried)
    agent.post(
        f"{path}/attempt",
        {"token": token, "order_id": "o1", "items": ["i1", "i2"], "amount_usd": 0.75},
    )
    agent.post(f"{path}/settle", {"token": token, "order_id": "o1", "outcome": "reported"})
    agent.post(f"{path}/settle", {"token": token, "order_id": "o2", "outcome": "closed"})
    rows = _sql(
        tables,
        "SELECT order_item_id, reported_usd, attempted_usd, reported_at IS NOT NULL AS done"
        " FROM ai_session_order_costs WHERE session_id = %s ORDER BY order_item_id",
        (sid,),
    )
    assert [(r["reported_usd"], r["attempted_usd"], r["done"]) for r in rows] == [
        (0.375, None, True),
        (0.375, None, True),
        (0.0, None, True),
    ]
    assert agent.post(f"{path}/report", {"token": token}) == {"report": None}
    # A reported order takes no new row in this session.
    agent.post(path, {"order_id": "o1", "order_item_id": "i9"})
    assert agent.post(f"{path}/report", {"token": token}) == {"report": None}


def test_a_share_past_the_budget_is_sent_and_read_back_to_send_again(
    agent: Agent, tables: PgPool
) -> None:
    # The SDK stops a query only once it is over its budget, so the spend can pass the largest
    # budget; the share is taken whatever its size, as the SQL path takes it.
    sid = _session(tables, cost=100.4)
    path = f"/sessions/{sid}/order-costs"
    agent.post(path, {"order_id": "o1", "order_item_id": "i1"})
    token = _lease(tables, sid)
    assert token is not None
    assert agent.post(f"{path}/report", {"token": token})["report"]["cost"] == 100.4
    agent.post(
        f"{path}/attempt", {"token": token, "order_id": "o1", "items": ["i1"], "amount_usd": 100.4}
    )
    # No answer came: the next report reads the share it sent, as a number, to send it again.
    pending = agent.post(f"{path}/report", {"token": token})["report"]["pending"]
    assert pending == [{"order_id": "o1", "order_item_id": "i1", "attempted_usd": 100.4}]


def test_a_write_under_a_lost_lease_is_refused(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables)
    path = f"/sessions/{sid}/order-costs"
    agent.post(path, {"order_id": "o1", "order_item_id": "i1"})
    token = _lease(tables, sid)
    assert token is not None
    _sql(tables, "UPDATE agent_leases SET expires_at = now() - interval '1 second'")
    for route, body in (
        ("report", {"token": token}),
        ("attempt", {"token": token, "order_id": "o1", "items": ["i1"], "amount_usd": 1}),
        ("settle", {"token": token, "order_id": "o1", "outcome": "reported"}),
    ):
        refused = agent.post(f"{path}/{route}", body, expect=409)
        assert refused["reason"] == "lease-lost"
    other = str(uuid.uuid4())
    assert agent.post(f"{path}/report", {"token": other}, expect=409)["reason"] == "lease-lost"
    rows = _sql(tables, "SELECT tried_at, attempted_usd, reported_at FROM ai_session_order_costs")
    assert rows == [{"tried_at": None, "attempted_usd": None, "reported_at": None}]


def test_the_lease_is_busy_while_an_sql_pod_reports(tables: PgPool) -> None:
    sid = _session(tables)
    with tables.connection() as conn:
        conn.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))", (f"ai_session_order_costs:{sid}",)
        )
        try:
            assert _lease(tables, sid) is None
        finally:
            conn.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                (f"ai_session_order_costs:{sid}",),
            )
    assert _lease(tables, sid) is not None


def test_the_sweep_takes_idle_sessions_with_old_rows(agent: Agent, tables: PgPool) -> None:
    idle = _session(tables)
    running = _session(tables, status="running")
    fresh = _session(tables)
    for sid in (idle, running, fresh):
        agent.post(f"/sessions/{sid}/order-costs", {"order_id": "o", "order_item_id": "i"})
    _sql(
        tables,
        "UPDATE ai_session_order_costs SET at = now() - interval '1 hour'"
        " WHERE session_id = ANY(%s::uuid[])",
        ([idle, running],),
    )
    assert agent.post("/order-costs/sweep", {"limit": 20}) == {"sessions": [idle]}


def test_the_bodies_and_ids_are_bounded(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables)
    path = f"/sessions/{sid}/order-costs"
    token = str(uuid.uuid4())
    for route, body in (
        ("", {"order_id": "", "order_item_id": "i"}),
        ("", {"order_id": "o" * 201, "order_item_id": "i"}),
        ("", {"order_id": "o", "order_item_id": "i", "extra": 1}),
        ("/report", {"token": "not-a-uuid"}),
        ("/attempt", {"token": token, "order_id": "o", "items": [], "amount_usd": 1}),
        ("/attempt", {"token": token, "order_id": "o", "items": ["i"], "amount_usd": -1}),
        ("/attempt", {"token": token, "order_id": "o", "items": ["i"], "amount_usd": "inf"}),
        ("/settle", {"token": token, "order_id": "o", "outcome": "lost"}),
    ):
        agent.post(f"{path}{route}", body, expect=422)
    agent.post("/order-costs/sweep", {"limit": 101}, expect=422)
    for bad in ("not-a-uuid", "..", sid.upper()):
        response = agent.client.post(
            f"{BASE}/sessions/{bad}/order-costs",
            json={"order_id": "o", "order_item_id": "i"},
            headers=agent.headers,
        )
        assert response.status_code in (404, 422)
    huge = agent.client.post(
        f"{BASE}{path}",
        content=b'{"x":"' + b"y" * crud_agent_order_costs.BODY_BYTES_MAX + b'"}',
        headers={**agent.headers, "content-type": "application/json"},
    )
    assert huge.status_code == 413
    assert _sql(tables, "SELECT 1 FROM ai_session_order_costs") == []
