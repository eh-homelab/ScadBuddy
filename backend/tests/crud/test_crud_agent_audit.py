"""The agent's audit log through its CRUD routes (#5299 step 10, area ``audit``,
`api/crud_agent_audit.py`): only the ``agent`` credential reaches them; a row is written as the
agent's ``audit/log.ts`` wrote it, a tool call's approver copied in the same statement; a page
is newest first with its cursor; a malformed id or cursor names nothing; and the prune deletes
only past the retention, through the table's trigger."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.agent_data.audit_log import cap
from scadbuddy.api import crud_agent_audit
from scadbuddy.render.agent_migrations import migrate_agent
from tests.conftest import PgPool
from tests.crud.test_crud_agent_gate import (
    BASE,
    HASH,
    ME,
    Agent,
    _app,
    _rid,
    _session,
    _sql,
    _token,
)

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def tables(pg_pool: PgPool) -> PgPool:
    with pg_pool.connection() as conn:
        migrate_agent(conn)
    return pg_pool


@pytest.fixture
def app(tables: PgPool) -> FastAPI:
    app = _app(tables)
    app.include_router(crud_agent_audit.router, prefix="/api/v1")
    return app


@pytest.fixture
def agent(app: FastAPI) -> Iterator[Agent]:
    with TestClient(app) as client:
        yield Agent(client, _token(app))


def _entry(**extra: Any) -> dict[str, Any]:
    return {
        "kind": "tool_call",
        "action": "print_output",
        "surface": "harness",
        "actor": ME,
        "outcome": "ok",
        **extra,
    }


def test_only_the_agent_credential_reaches_the_routes(app: FastAPI, tables: PgPool) -> None:
    with TestClient(app) as client:
        for caller in ("print", "projects", "render"):
            refused = client.post(
                f"{BASE}/audit/record", json=_entry(), headers=_token(app, caller)
            )
            assert refused.status_code == 403, refused.text
        assert client.post(f"{BASE}/audit/list", json={}).status_code == 401
    assert _sql(tables, "SELECT count(*) AS n FROM ai_audit")[0]["n"] == 0


def test_the_areas_list_audit(agent: Agent) -> None:
    assert "audit" in agent.get("/areas")["areas"]


def test_a_body_out_of_bounds_is_refused(agent: Agent, tables: PgPool) -> None:
    for body in (
        _entry(detail="d" * 501),
        _entry(input_summary="s" * 1001),
        _entry(actor={**ME, "kind": "k" * 51}),
        _entry(kind="nonsense"),
        _entry(extra=1),
    ):
        assert agent.status("/audit/record", body) == 422
    assert agent.status("/audit/list", {"before": -1}) == 422
    assert _sql(tables, "SELECT count(*) AS n FROM ai_audit")[0]["n"] == 0


def test_a_row_is_written_with_its_durable_approver(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    rid = _rid(session)
    _sql(
        tables,
        "INSERT INTO ai_input_responses (request_id, session_id, kind, outcome, responder)"
        " VALUES (%s, %s, 'approval', 'approved', %s::jsonb)",
        (rid, session, '{"kind": "browser", "id": "browser", "label": "You"}'),
    )
    body = _entry(
        session_id=session,
        turn_id="not-a-uuid",
        tool_use_id="toolu_1",
        tier="outward",
        input_hash=HASH,
        request_id=rid,
        started_at="2026-11-15T10:00:00.000Z",
        finished_at="2026-11-15T10:00:01.250Z",
        cost_usd=-0.0000001,
    )
    assert agent.post("/audit/record", body) == {"done": True}
    [row] = _sql(
        tables,
        "SELECT session_id::text AS session_id, turn_id, approved_by_kind, approved_by_label,"
        " request_id, duration_ms, cost_usd FROM ai_audit",
    )
    assert row == {
        "session_id": session,
        "turn_id": None,
        "approved_by_kind": "browser",
        "approved_by_label": "You",
        "request_id": rid,
        "duration_ms": 1250,
        "cost_usd": 0.0,
    }
    # Not a tool call: no approver is copied.
    agent.post("/audit/record", _entry(kind="approval", action="approved", request_id=rid))
    rows = _sql(tables, "SELECT approved_by_kind FROM ai_audit WHERE kind = 'approval'")
    assert rows == [{"approved_by_kind": None}]


def test_a_page_is_newest_first_with_its_cursor(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    for n in range(3):
        agent.post("/audit/record", _entry(action=f"tool_{n}", session_id=session))
    agent.post("/audit/record", _entry(kind="approval", action="approved"))
    first = agent.post("/audit/list", {"kind": "tool_call", "limit": 2})
    assert [r["action"] for r in first["rows"]] == ["tool_2", "tool_1"]
    assert first["next"] == first["rows"][-1]["id"]
    rest = agent.post("/audit/list", {"kind": "tool_call", "before": int(first["next"])})
    assert [r["action"] for r in rest["rows"]] == ["tool_0"] and rest["next"] is None
    mine = agent.post("/audit/list", {"session_id": session})
    assert len(mine["rows"]) == 3
    for missing in (str(uuid.uuid4()), "../../areas", "not-a-uuid"):
        assert agent.post("/audit/list", {"session_id": missing}) == {"rows": [], "next": None}


def test_the_prune_deletes_only_past_the_retention(agent: Agent, tables: PgPool) -> None:
    agent.post("/audit/record", _entry(action="new"))
    _sql(
        tables,
        "INSERT INTO ai_audit (at, kind, action, surface, principal_kind, principal_id,"
        " principal_label, outcome) VALUES (now() - interval '40 days', 'tool_call', 'old',"
        " 'harness', 'browser', 'browser', 'You', 'ok')",
    )
    assert agent.post("/audit/prune", {"days": 30}) == {"deleted": 1}
    assert [r["action"] for r in _sql(tables, "SELECT action FROM ai_audit")] == ["new"]
    # Days below the floor prune at the floor: a day.
    assert agent.post("/audit/prune", {"days": -5}) == {"deleted": 0}


def test_a_cap_counts_utf16_units_and_keeps_a_pair_whole() -> None:
    """``audit_log.cap`` is the agent's ``cap``: the API caps what it writes as the agent did."""
    assert cap("abc", 3) == "abc"
    assert cap("abcd", 3) == "ab…"
    # The pair would be cut in half at the boundary: it goes whole, never as a lone half.
    assert cap("a\U0001f600bc", 3) == "a…"
    assert cap("ab\U0001f600", 4) == "ab\U0001f600"
