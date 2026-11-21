"""A flow run's calls at the starting session's hop through the agent's CRUD routes (#5299 step
10, area ``escalations``, `api/crud_agent_escalations.py`): only the ``agent`` credential reaches
them; a pass up and a deny's claim move an entry only while it is the named parent session's and
its hop is live; a claim holds it from the sweep until released or its lease ends; a delivery
claims a call once, hands it back, or skips it to the person; and a malformed id names nothing."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api import crud_agent_access, crud_agent_escalations
from scadbuddy.render.agent_migrations import migrate_agent
from tests.conftest import PgPool
from tests.crud.test_crud_agent_gate import BASE, Agent, _app, _session, _sql, _token

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def tables(pg_pool: PgPool) -> PgPool:
    with pg_pool.connection() as conn:
        migrate_agent(conn)
    return pg_pool


@pytest.fixture
def app(tables: PgPool) -> FastAPI:
    app = _app(tables)
    app.include_router(crud_agent_escalations.router, prefix="/api/v1")
    app.include_router(crud_agent_access.router, prefix="/api/v1")
    return app


@pytest.fixture
def agent(app: FastAPI) -> Iterator[Agent]:
    with TestClient(app) as client:
        yield Agent(client, _token(app))


def _child(
    pool: PgPool, parent: str, *, hop_until: str = "now() + interval '5 minutes'", n: int = 1
) -> str:
    child = _session(pool)
    rid = f"durable:{child}:run-1:toolu_{n}"
    _sql(
        pool,
        "INSERT INTO ai_pending_input (request_id, session_id, workflow_id, workflow_run_id, kind,"
        " tool, responders, expires_at, flow_run_id, hop, hop_until, parent_session,"
        f" parent_principal) VALUES (%s, %s, 'w', 'run-1', 'approval', 'print_output',"
        f" ARRAY['browser'], now() + interval '1 hour', 'flow-1', 'parent', {hop_until}, %s,"
        " 'browser')",
        (rid, child, parent),
    )
    return rid


def _row(pool: PgPool, rid: str) -> dict[str, Any]:
    [row] = _sql(
        pool,
        "SELECT hop, hop_until = 'infinity' AS claimed, hop_claimed_at IS NOT NULL AS at,"
        " delivered_at IS NOT NULL AS delivered, hops FROM ai_pending_input WHERE request_id = %s",
        (rid,),
    )
    return row


def test_only_the_agent_credential_reaches_the_routes(app: FastAPI, tables: PgPool) -> None:
    parent = _session(tables)
    rid = _child(tables, parent)
    with TestClient(app) as client:
        for caller in ("print", "projects", "render"):
            body = {"request_id": rid, "session": parent}
            refused = client.post(
                f"{BASE}/escalations/pass-up", json=body, headers=_token(app, caller)
            )
            assert refused.status_code == 403, refused.text
        assert client.post(f"{BASE}/escalations/move-due").status_code == 401
    assert _row(tables, rid)["hop"] == "parent"


def test_the_areas_list_escalations(agent: Agent) -> None:
    assert "escalations" in agent.get("/areas")["areas"]


def test_a_body_out_of_bounds_is_refused(agent: Agent) -> None:
    session = str(uuid.uuid4())
    for path, body in (
        ("/escalations/pass-up", {"request_id": "x" * 2049, "session": session}),
        ("/escalations/pass-up", {"request_id": "r", "session": session, "extra": 1}),
        ("/escalations/claim", {"session": session, "child": ["r"] * 1001}),
        ("/escalations/waiting", {"grace_s": -1}),
        ("/escalations/deliveries/page", {"after": "", "limit": 0}),
        ("/escalations/deliveries/forget", {"ids": ["r"] * 1001}),
    ):
        assert agent.status(path, body) == 422, path


def test_a_pass_up_moves_only_the_named_parents_live_entry(agent: Agent, tables: PgPool) -> None:
    parent = _session(tables)
    rid = _child(tables, parent)
    entry = agent.get("/escalations/entry", request_id=rid)["entry"]
    assert entry == {
        "session_id": entry["session_id"],
        "hop": "parent",
        "due": False,
        "claimed": False,
        "parent_session": parent,
        "parent_principal": "browser",
    }
    for other in (str(uuid.uuid4()), "../x"):
        moved = agent.post("/escalations/pass-up", {"request_id": rid, "session": other})
        assert moved == {"moved": False}
    body = {"request_id": rid, "session": parent, "reason": "not mine"}
    assert agent.post("/escalations/pass-up", body) == {"moved": True}
    row = _row(tables, rid)
    assert row["hop"] == "person"
    [hop] = row["hops"]
    assert hop["action"] == "pass_up" and hop["actor"] == f"session:{parent}"
    assert hop["reason"] == "not mine" and hop["at"].endswith("Z")
    assert agent.post("/escalations/pass-up", body) == {"moved": False}
    assert agent.get("/escalations/entry", request_id="../x") == {"entry": None}


def test_a_deny_claim_holds_the_entry_until_released(agent: Agent, tables: PgPool) -> None:
    parent = _session(tables)
    rid = _child(tables, parent)
    claim = {"request_id": rid, "session": parent}
    held = agent.post("/escalations/claim-deny", claim)["hop_until"]
    assert held is not None
    assert _row(tables, rid)["claimed"] is True
    assert agent.post("/escalations/claim-deny", claim) == {"hop_until": None}
    assert agent.post("/escalations/pass-up", claim) == {"moved": False}
    # A skip leaves an entry a deny holds alone too, with no step added.
    before = _row(tables, rid)
    agent.post("/escalations/skip", {"session": parent, "child": [rid], "own": []})
    assert _row(tables, rid) == before
    assert before["hop"] == "parent" and before["claimed"] is True
    assert agent.get("/escalations/entry", request_id=rid)["entry"]["claimed"] is True
    # The sweep leaves a fresh claim alone, and the run's queue reads it as `infinity`.
    assert agent.post("/escalations/move-due", {}) == {"moved": 0}
    [listed] = agent.get("/escalations/run-entries", flow_run_id="flow-1")["entries"]
    assert listed["hop_until"] == "infinity"
    # A release names the parent session too: another session's moves nothing.
    other_release = {"request_id": rid, "session": str(uuid.uuid4()), "hop_until": held}
    assert agent.post("/escalations/release-deny", other_release) == {"done": False}
    assert _row(tables, rid)["claimed"] is True
    release = {"request_id": rid, "session": parent, "hop_until": held}
    assert agent.post("/escalations/release-deny", release) == {"done": True}
    assert _row(tables, rid)["claimed"] is False
    # Released, the deadline is a timestamp again, as text.
    [listed] = agent.get("/escalations/run-entries", flow_run_id="flow-1")["entries"]
    assert listed["hop_until"] != "infinity"
    assert datetime.fromisoformat(listed["hop_until"]) == datetime.fromisoformat(held)
    # A release never parks the entry past its expiry.
    assert agent.post("/escalations/claim-deny", claim)["hop_until"] is not None
    far = {"request_id": rid, "session": parent, "hop_until": "2999-01-01T00:00:00+00:00"}
    assert agent.post("/escalations/release-deny", far) == {"done": True}
    [capped] = _sql(
        tables,
        "SELECT hop_until = expires_at AS capped FROM ai_pending_input WHERE request_id = %s",
        (rid,),
    )
    assert capped["capped"] is True
    other = str(uuid.uuid4())
    assert agent.post("/escalations/claim-deny", {"request_id": rid, "session": other}) == {
        "hop_until": None
    }


def test_the_sweep_moves_a_due_entry_and_a_stale_claim(agent: Agent, tables: PgPool) -> None:
    parent = _session(tables)
    due = _child(tables, parent, hop_until="now() - interval '1 second'", n=1)
    stale = _child(tables, parent, hop_until="'infinity'", n=2)
    _sql(
        tables,
        "UPDATE ai_pending_input SET hop_claimed_at = now() - interval '301 seconds'"
        " WHERE request_id = %s",
        (stale,),
    )
    live = _child(tables, parent, n=3)
    assert agent.post("/escalations/move-due", {}) == {"moved": 2}
    for rid in (due, stale):
        row = _row(tables, rid)
        assert row["hop"] == "person" and row["at"] is False
        assert row["hops"][0]["action"] == "timeout" and row["hops"][0]["actor"] == "system"
    assert _row(tables, live)["hop"] == "parent"


def test_a_delivery_claims_once_hands_back_and_skips(agent: Agent, tables: PgPool) -> None:
    parent = _session(tables)
    rid = _child(tables, parent)
    own = f"flow:flow-9:wrun:{uuid.uuid4()}"
    [call] = agent.post("/escalations/waiting", {"grace_s": 60})["calls"]
    assert call["request_id"] == rid and call["parent_session"] == parent
    assert call["past_grace"] is False
    calls = {"session": parent, "child": [rid], "own": [own]}
    assert sorted(agent.post("/escalations/claim", calls)["claimed"]) == sorted([rid, own])
    assert agent.post("/escalations/claim", calls) == {"claimed": []}
    assert agent.post("/escalations/waiting", {"grace_s": 60}) == {"calls": []}
    assert agent.post("/escalations/deliveries/sent", {"ids": [own, "flow:x"]}) == {"ids": [own]}
    # Another session's names are not its to hand back.
    other = {"session": str(uuid.uuid4()), "child": [rid], "own": [own]}
    agent.post("/escalations/unclaim", other)
    assert _row(tables, rid)["delivered"] is True
    agent.post("/escalations/unclaim", calls)
    assert _row(tables, rid)["delivered"] is False
    assert agent.post("/escalations/deliveries/sent", {"ids": [own]}) == {"ids": []}
    agent.post("/escalations/skip", calls)
    row = _row(tables, rid)
    assert row["hop"] == "person" and row["hops"][0]["actor"] == "system"
    assert agent.post("/escalations/deliveries/sent", {"ids": [own]}) == {"ids": [own]}
    page = {"after": "", "limit": 10}
    assert agent.post("/escalations/deliveries/page", page) == {"ids": [own]}
    agent.post("/escalations/deliveries/forget", {"ids": [own]})
    assert agent.post("/escalations/deliveries/page", page) == {"ids": []}
    assert agent.post("/escalations/claim", {**calls, "session": "../x"}) == {"claimed": []}
