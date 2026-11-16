"""The agent's classic approvals through its CRUD routes (#5299 step 10, area ``approvals``,
`api/crud_agent_approvals.py`): only the ``agent`` credential reaches them; a decision is written
once, with its events and its audit row in its transaction, and never for an expired row; a
sessionless approval is used once, only by its asker for its input; an MCP prepare keeps its
bounds under its lock; and a malformed id names nothing."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api import crud_agent_approvals
from scadbuddy.render.agent_migrations import migrate_agent
from tests.conftest import PgPool
from tests.crud.test_crud_agent_gate import (
    BASE,
    HASH,
    ME,
    Agent,
    _app,
    _session,
    _sql,
    _token,
    _types,
)

pytestmark = pytest.mark.requires_postgres

AGENT = {"kind": "bearer", "id": "agent-1", "label": "Agent one"}
WHO = {"kind": "bearer", "id": "agent-1"}
TRACE = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


@pytest.fixture
def tables(pg_pool: PgPool) -> PgPool:
    with pg_pool.connection() as conn:
        migrate_agent(conn)
    return pg_pool


@pytest.fixture
def app(tables: PgPool) -> FastAPI:
    app = _app(tables)
    app.include_router(crud_agent_approvals.router, prefix="/api/v1")
    return app


@pytest.fixture
def agent(app: FastAPI) -> Iterator[Agent]:
    with TestClient(app) as client:
        yield Agent(client, _token(app))


def _new(session: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "session_id": session,
        "turn_id": None,
        "tool_use_id": "toolu_1",
        "tool": "print_output",
        "input_summary": '{"output":"box"}',
        "input_hash": HASH,
        "tier": "outward",
        "requested_by": AGENT,
        "expiry_s": 600,
        **extra,
    }


def _settle(approval_id: str, decision: str = "approved", **extra: Any) -> dict[str, Any]:
    by = {"by": ME} if decision in ("approved", "denied") else {}
    return {"id": approval_id, "decision": decision, "expiry_s": 300, **by, **extra}


def test_only_the_agent_credential_reaches_the_routes(app: FastAPI, tables: PgPool) -> None:
    with TestClient(app) as client:
        for caller in ("print", "projects", "render"):
            refused = client.post(
                f"{BASE}/approvals/create", json=_new(None), headers=_token(app, caller)
            )
            assert refused.status_code == 403, refused.text
        assert client.post(f"{BASE}/approvals/settle", json=_settle("x")).status_code == 401
    assert _sql(tables, "SELECT count(*) AS n FROM ai_approvals")[0]["n"] == 0


def test_the_areas_list_approvals(agent: Agent) -> None:
    assert "approvals" in agent.get("/areas")["areas"]


def test_a_body_out_of_bounds_is_refused(agent: Agent, tables: PgPool) -> None:
    for body in (
        _new(None, id="../x"),
        _new("../not-a-session"),
        _new(None, input_summary="s" * (64 * 1024 + 1)),
        _new(None, extra=1),
    ):
        assert agent.status("/approvals/create", body) == 422
    # Only the system expires or cancels: a person's decision names who made it.
    assert (
        agent.status("/approvals/settle", {"id": str(uuid.uuid4()), "decision": "approved"}) == 422
    )
    assert _sql(tables, "SELECT count(*) AS n FROM ai_approvals")[0]["n"] == 0


def test_a_session_approval_is_made_with_its_events(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    made = agent.post("/approvals/create", _new(session))
    approval = made["approval"]
    [batch] = made["logged"]
    assert [e["type"] for e in batch["events"]] == ["approval.required", "input.requested"]
    assert batch["events"][0] == {
        "v": 1,
        "type": "approval.required",
        "sessionId": session,
        "id": approval["id"],
        "tool": "toolu_1",
        "summary": 'print_output {"output":"box"}',
        "risk": "outward",
    }
    assert batch["events"][1]["entry"]["id"] == f"approval:{approval['id']}"
    assert approval["decision"] is None and approval["due"] is False
    assert agent.get("/approvals/row", id=approval["id"])["approval"]["id"] == approval["id"]
    listed = agent.post("/approvals/list", {"session_id": session, "pending": True})
    assert [a["id"] for a in listed["approvals"]] == [approval["id"]]
    mine = agent.post("/approvals/visible-pending", {"kind": "browser", "id": "browser"})
    assert [a["id"] for a in mine["approvals"]] == [approval["id"]]
    assert agent.post("/approvals/visible-pending", WHO)["approvals"] == []


def test_a_decision_is_written_once_with_its_audit_row(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    approval = agent.post("/approvals/create", _new(session))["approval"]
    settled = agent.post("/approvals/settle", _settle(approval["id"], traceparent=TRACE))
    row = settled["approval"]
    assert row["decision"] == "approved" and row["decided_by_label"] == "You"
    assert row["decision_traceparent"] == TRACE and row["usable_until"] is not None
    assert [e["type"] for e in settled["logged"][0]["events"]] == [
        "approval.resolved",
        "input.resolved",
    ]
    assert settled["logged"][0]["events"][0]["by"] == ME
    [audit] = _sql(
        tables,
        "SELECT kind, action, surface, principal_label, outcome, approval_id::text AS a, detail"
        " FROM ai_audit",
    )
    assert audit == {
        "kind": "approval",
        "action": "approved",
        "surface": "http",
        "principal_label": "You",
        "outcome": "ok",
        "a": approval["id"],
        "detail": "print_output (requested by Agent one)",
    }
    # The first decision wins: a second writes nothing.
    again = agent.post("/approvals/settle", _settle(approval["id"], "denied"))
    assert again == {"approval": None, "logged": []}
    assert _sql(tables, "SELECT count(*) AS n FROM ai_audit")[0]["n"] == 1
    assert _types(tables, session) == [
        "approval.required",
        "input.requested",
        "approval.resolved",
        "input.resolved",
    ]


def test_two_decisions_of_one_approval_write_once(agent: Agent, tables: PgPool) -> None:
    approval = agent.post("/approvals/create", _new(None))["approval"]
    results: list[Any] = []

    def decide(decision: str) -> None:
        results.append(agent.post("/approvals/settle", _settle(approval["id"], decision)))

    threads = [threading.Thread(target=decide, args=(d,)) for d in ("approved", "denied") * 3]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r["approval"] is not None for r in results) == 1
    assert _sql(tables, "SELECT count(*) AS n FROM ai_audit")[0]["n"] == 1


def test_an_expired_approval_is_not_approved(agent: Agent, tables: PgPool) -> None:
    approval = agent.post("/approvals/create", _new(None))["approval"]
    _sql(
        tables,
        "UPDATE ai_approvals SET expires_at = now() - interval '1 second' WHERE id = %s",
        (approval["id"],),
    )
    assert agent.post("/approvals/settle", _settle(approval["id"]))["approval"] is None
    assert agent.post("/approvals/due", {"limit": 10}) == {"ids": [approval["id"]]}
    assert agent.post("/approvals/due", {"limit": 10, "tried": [approval["id"], "../x"]}) == {
        "ids": []
    }
    expired = agent.post(
        "/approvals/settle", _settle(approval["id"], "expired", reason="no decision")
    )
    assert expired["approval"]["decision"] == "expired"
    [audit] = _sql(tables, "SELECT surface, principal_kind, outcome FROM ai_audit")
    assert audit == {"surface": "system", "principal_kind": "system", "outcome": "refused"}


def test_a_prepared_approval_is_used_once_by_its_asker(agent: Agent, tables: PgPool) -> None:
    body = {"approval": _new(None), "per_principal": 5, "total": 50, "evict_reason": "full"}
    approval = agent.post("/approvals/create-prepared", body)["approval"]
    assert [a["id"] for a in agent.post("/approvals/prepared", WHO)["approvals"]] == [
        approval["id"]
    ]
    use = {"id": approval["id"], "by": {"kind": "bearer", "id": "agent-1"}, "input_hash": HASH}
    # Not approved yet: nothing to use.
    assert agent.post("/approvals/consume", use) == {"approval": None}
    agent.post("/approvals/settle", _settle(approval["id"]))
    for refused in (
        {**use, "input_hash": "b" * 64},
        {**use, "by": {"kind": "bearer", "id": "agent-2"}},
        {**use, "id": "../x"},
    ):
        assert agent.post("/approvals/consume", refused) == {"approval": None}
    assert agent.post("/approvals/consume", use)["approval"]["consumed_at"] is not None
    assert agent.post("/approvals/consume", use) == {"approval": None}


def test_a_prepare_keeps_its_bounds(agent: Agent, tables: PgPool) -> None:
    def prepare(by: dict[str, str], traces: list[str] | None = None) -> Any:
        body = {
            "approval": _new(None, requested_by=by),
            "per_principal": 2,
            "total": 3,
            "evict_reason": "too many",
            "evict_traceparents": traces or [],
        }
        return agent.post("/approvals/create-prepared", body)

    first = prepare(AGENT)["approval"]
    prepare(AGENT)
    third = prepare(AGENT, [TRACE])
    # The asker's oldest is cancelled to keep it under its bound, with the span's context.
    assert [e["id"] for e in third["evicted"]] == [first["id"]]
    assert third["evicted"][0]["decision_traceparent"] == TRACE
    assert third["evicted"][0]["decision"] == "cancelled"
    other = {"kind": "bearer", "id": "agent-2", "label": "Agent two"}
    prepare(other)
    # At the total, nobody's row is evicted and nothing is written.
    assert prepare({**other, "id": "agent-3"}) == {"approval": None, "evicted": []}


def test_a_malformed_id_names_nothing(agent: Agent) -> None:
    for missing in (str(uuid.uuid4()), "../../areas", "not-a-uuid"):
        assert agent.get("/approvals/row", id=missing) == {"approval": None}
        assert agent.get("/approvals/session", session_id=missing) == {"session": None}
        assert agent.post("/approvals/settle", _settle(missing)) == {"approval": None, "logged": []}
        assert agent.post("/approvals/list", {"session_id": missing}) == {"approvals": []}
