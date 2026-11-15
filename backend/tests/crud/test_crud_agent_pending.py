"""The gate's pending input through the agent's CRUD routes (#5299 step 10, area
``pending-input``, `api/crud_agent_pending.py`): only the ``agent`` credential reaches them;
the reads answer what ``gate/projection.ts`` read; a durable turn's trace mark lands only while
its placement holds the session (the turn fence, checked ``FOR SHARE``); an orphan's cancel
writes its row, events, status and audit row once; and an approval counts only for the call it
was given for.

The entries are opened through the ``gate`` routes, as the session workflow opens them."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.agent_data.pending_input import ORPHAN_REASON
from scadbuddy.api import crud_agent_pending
from scadbuddy.render.agent_migrations import migrate_agent
from tests.conftest import PgPool
from tests.crud.test_crud_agent_gate import (
    BASE,
    HASH,
    ME,
    SYSTEM,
    Agent,
    _app,
    _open,
    _rid,
    _session,
    _sql,
    _status,
    _token,
    _types,
)

pytestmark = pytest.mark.requires_postgres

TRACE = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


@pytest.fixture
def tables(pg_pool: PgPool) -> PgPool:
    with pg_pool.connection() as conn:
        migrate_agent(conn)
    return pg_pool


@pytest.fixture
def app(tables: PgPool) -> FastAPI:
    app = _app(tables)
    app.include_router(crud_agent_pending.router, prefix="/api/v1")
    return app


@pytest.fixture
def agent(app: FastAPI) -> Iterator[Agent]:
    with TestClient(app) as client:
        yield Agent(client, _token(app))


def _resolve(agent: Agent, request_id: str, outcome: str = "approved") -> None:
    body = {"request_id": request_id, "outcome": outcome, "responder": ME}
    assert agent.post("/gate/resolve", body)["resolved"] is True


def test_only_the_agent_credential_reaches_the_routes(app: FastAPI, tables: PgPool) -> None:
    with TestClient(app) as client:
        for caller in ("print", "projects", "render"):
            refused = client.post(
                f"{BASE}/pending-input/entries", json={}, headers=_token(app, caller)
            )
            assert refused.status_code == 403, refused.text
        assert client.post(f"{BASE}/pending-input/entries", json={}).status_code == 401
        params = {"request_id": "x", "input_hash": HASH}
        assert client.get(f"{BASE}/pending-input/approval", params=params).status_code == 401


def test_the_areas_list_pending_input(agent: Agent) -> None:
    assert "pending-input" in agent.get("/areas")["areas"]


def test_a_body_out_of_bounds_is_refused(agent: Agent, tables: PgPool) -> None:
    too_many = ["r"] * (crud_agent_pending.IDS_MAX + 1)
    assert agent.status("/pending-input/in-run", {"request_ids": too_many}) == 422
    assert agent.status("/pending-input/entries", {"session_id": "../not-a-session"}) == 422
    assert agent.status("/pending-input/entries", {"extra": 1}) == 422
    long_trace = {"request_id": "r", "traceparent": "x" * 257}
    assert agent.status("/pending-input/decision-trace", long_trace) == 422
    session = _session(tables)
    both = {"run": {"workflow_id": "w", "run_id": "r"}, "session_id": session}
    assert agent.status("/pending-input/orphans/cancel", both) == 422
    assert agent.status("/pending-input/orphans/cancel", {}) == 422


# -- reads -----------------------------------------------------------------------------------


def test_entries_answer_the_filter(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    other = _session(tables)
    agent.post("/gate/open", _open(session))
    agent.post("/gate/open", _open(other, kind="answer"))
    everything = agent.post("/pending-input/entries", {})["entries"]
    assert [e["session_id"] for e in everything] == [session, other]
    [one] = agent.post("/pending-input/entries", {"session_id": session})["entries"]
    assert one["request_id"] == _rid(session)
    assert one["kind"] == "approval" and one["input_hash"] == HASH
    assert one["expiring"] is False and one["flow_run_id"] is None
    stranger = {"owned_by": {"kind": "bearer", "id": "someone"}}
    assert agent.post("/pending-input/entries", stranger)["entries"] == []
    holder = agent.post("/pending-input/entries", {**stranger, "all_approvals": True})
    assert [e["request_id"] for e in holder["entries"]] == [_rid(session)]
    mine = agent.post("/pending-input/entries", {"owned_by": {"kind": "browser", "id": "browser"}})
    assert len(mine["entries"]) == 2
    # An answer outlives a handoff: not the browser user's to answer.
    _sql(tables, "UPDATE ai_sessions SET owner_kind = 'bearer' WHERE id = %s", (other,))
    own = agent.post("/pending-input/entries", {"own_answers": True})["entries"]
    assert [e["request_id"] for e in own] == [_rid(session)]


def test_in_run_decided_and_recorded(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    rid = _rid(session)
    agent.post("/gate/open", _open(session))
    ids = {"request_ids": [rid, "../../areas", "never"]}
    assert agent.post("/pending-input/in-run", ids) == {"request_ids": []}
    assert agent.post("/pending-input/in-run", {"request_ids": []}) == {"request_ids": []}
    assert agent.post("/pending-input/decided", ids) == {"request_ids": []}
    assert agent.get("/pending-input/recorded", request_id=rid) == {"recorded": None}
    _sql(tables, "UPDATE ai_pending_input SET flow_run_id = 'run-9' WHERE request_id = %s", (rid,))
    assert agent.post("/pending-input/in-run", ids) == {"request_ids": [rid]}
    _resolve(agent, rid)
    assert agent.post("/pending-input/decided", ids) == {"request_ids": [rid]}
    recorded = agent.get("/pending-input/recorded", request_id=rid)["recorded"]
    assert recorded == {
        "kind": "approval",
        "outcome": "approved",
        "response": None,
        "response_hash": None,
        "responder": ME,
        "reason": None,
        "decision_traceparent": None,
    }
    assert agent.get("/pending-input/recorded", request_id="../../areas") == {"recorded": None}


def test_a_tab_wait_is_an_agent_sessions_only(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    attention = {"reason": "tab_disconnected", "on_timeout": None}
    agent.post("/gate/open", _open(session, kind="answer", attention=attention))
    assert agent.get("/pending-input/tab-wait", session_id=session) == {"waits": False}
    _sql(tables, "UPDATE ai_sessions SET runtime = 'unified' WHERE id = %s", (session,))
    assert agent.get("/pending-input/tab-wait", session_id=session) == {"waits": True}
    bad = agent.client.get(
        f"{BASE}/pending-input/tab-wait",
        params={"session_id": "../x"},
        headers=agent.headers,
    )
    assert bad.status_code == 422


def test_the_traces_of_an_entry(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    rid = _rid(session)
    agent.post("/gate/open", _open(session))
    trace = agent.get("/pending-input/park-trace", request_id=rid)["trace"]
    assert trace["tool"] == "print_output" and trace["traceparent"] is None
    body = {"request_id": rid, "traceparent": TRACE}
    assert agent.post("/pending-input/decision-trace", body) == {"done": True}
    _resolve(agent, rid)
    recorded = agent.get("/pending-input/recorded", request_id=rid)["recorded"]
    assert recorded["decision_traceparent"] == TRACE
    assert agent.get("/pending-input/park-trace", request_id=rid) == {"trace": None}


def test_a_session_is_read_and_a_malformed_id_names_none(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    assert agent.get("/pending-input/session", session_id=session)["session"] == {
        "owner_kind": "browser",
        "owner_id": "browser",
        "owner_label": "You",
        "creator_kind": "browser",
        "creator_id": "browser",
    }
    for missing in (str(uuid.uuid4()), "../../areas", "not-a-uuid"):
        assert agent.get("/pending-input/session", session_id=missing) == {"session": None}


# -- the turn fence --------------------------------------------------------------------------


def _placed(tables: PgPool, session: str, attempt: str = "run-1/1") -> str:
    turn = str(uuid.uuid4())
    _sql(
        tables,
        "UPDATE ai_sessions SET turn_attempt = %s, durable_turn_id = %s WHERE id = %s",
        (attempt, turn, session),
    )
    return turn


def _mark(session: str, turn: str, attempt: str = "run-1/1", **extra: Any) -> dict[str, Any]:
    return {
        "request_id": _rid(session),
        "traceparent": TRACE,
        "session_id": session,
        "attempt": attempt,
        "turn_id": turn,
        **extra,
    }


def _traceparent(tables: PgPool, session: str) -> Any:
    rows = _sql(
        tables, "SELECT traceparent FROM ai_pending_input WHERE session_id = %s", (session,)
    )
    return rows[0]["traceparent"]


def test_a_trace_mark_lands_only_for_the_placement_that_holds_the_turn(
    agent: Agent, tables: PgPool
) -> None:
    session = _session(tables)
    other = _session(tables)
    agent.post("/gate/open", _open(session))
    turn = _placed(tables, session)
    other_turn = _placed(tables, other)
    for refused in (
        _mark(session, turn, attempt="run-1/2"),
        _mark(session, str(uuid.uuid4())),
        _mark(session, "not-a-uuid"),
        _mark(session, turn, session_id="../x"),
        # Another session's placement: the entry is not its own.
        _mark(other, other_turn, request_id=_rid(session)),
    ):
        assert agent.post("/pending-input/trace", refused) == {"marked": False}
    assert _traceparent(tables, session) is None
    assert agent.post("/pending-input/trace", _mark(session, turn)) == {"marked": True}
    assert _traceparent(tables, session) == TRACE


def test_a_trace_mark_waits_for_a_claim_and_then_finds_it_moved(
    agent: Agent, tables: PgPool, pg_conninfo: str
) -> None:
    """A new placement's claim lands wholly before the mark: the old placement writes nothing."""
    session = _session(tables)
    agent.post("/gate/open", _open(session))
    turn = _placed(tables, session)
    with psycopg.connect(pg_conninfo) as claim:
        claim.execute("UPDATE ai_sessions SET turn_attempt = 'run-1/2' WHERE id = %s", (session,))
        result: list[Any] = []
        thread = threading.Thread(
            target=lambda: result.append(agent.post("/pending-input/trace", _mark(session, turn)))
        )
        thread.start()
        thread.join(0.5)
        assert thread.is_alive()  # waiting on the claim's row lock
        claim.commit()
        thread.join()
    assert result == [{"marked": False}]
    assert _traceparent(tables, session) is None


# -- the orphan sweep ------------------------------------------------------------------------


def test_the_sweep_lists_stamps_and_cancels_a_runs_entries_once(
    agent: Agent, tables: PgPool
) -> None:
    session = _session(tables)
    rid = _rid(session)
    agent.post("/gate/open", _open(session))
    runs = agent.get("/pending-input/sweep/runs", min_age_s=0, recheck_s=600)["runs"]
    assert runs == [{"workflow_id": f"session-{session}", "workflow_run_id": "run-1"}]
    checked = {"workflow_id": f"session-{session}", "run_id": "run-1"}
    assert agent.post("/pending-input/sweep/checked", checked) == {"done": True}
    assert agent.get("/pending-input/sweep/runs", min_age_s=0, recheck_s=600)["runs"] == []
    assert _status(tables, session) == "waiting_approval"

    cancelled = agent.post("/pending-input/orphans/cancel", {"run": checked})
    assert cancelled["removed"] == 1
    [batch] = cancelled["logged"]
    assert [e["type"] for e in batch["events"]] == [
        "approval.resolved",
        "input.resolved",
        "session.status",
    ]
    assert batch["events"][0] == {
        "v": 1,
        "type": "approval.resolved",
        "sessionId": session,
        "id": rid,
        "approved": False,
        "decision": "cancelled",
        "reason": ORPHAN_REASON,
    }
    assert batch["events"][2]["status"] == "idle"
    assert _status(tables, session) == "idle"
    [row] = _sql(tables, "SELECT outcome, responder, reason FROM ai_input_responses")
    assert row == {
        "outcome": "cancelled",
        "responder": SYSTEM,
        "reason": ORPHAN_REASON,
    }
    [audit] = _sql(
        tables, "SELECT kind, action, surface, outcome, input_hash, detail FROM ai_audit"
    )
    assert audit == {
        "kind": "approval",
        "action": "cancelled",
        "surface": "system",
        "outcome": "refused",
        "input_hash": HASH,
        "detail": "print_output: the session's workflow run has ended (requested by You)",
    }
    # A second sweep, or the workflow's own resolve, writes nothing more.
    assert agent.post("/pending-input/orphans/cancel", {"run": checked}) == {
        "removed": 0,
        "logged": [],
    }
    assert _types(tables, session)[-3:] == ["approval.resolved", "input.resolved", "session.status"]


def test_a_sessions_entries_are_cancelled_with_their_reason(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    agent.post("/gate/open", _open(session, kind="answer"))
    body = {"session_id": session, "reason": "stopped"}
    cancelled = agent.post("/pending-input/orphans/cancel", body)
    assert cancelled["removed"] == 1
    assert cancelled["logged"][0]["events"][0] == {
        "v": 1,
        "type": "question.resolved",
        "sessionId": session,
        "id": _rid(session),
        "answered": False,
        "reason": "stopped",
    }
    # A question's cancel is not an approval's: no approval audit row.
    assert _sql(tables, "SELECT count(*) AS n FROM ai_audit")[0]["n"] == 0


# -- a recorded approval ---------------------------------------------------------------------


def test_an_approval_counts_only_for_its_own_call(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    rid = _rid(session)
    agent.post("/gate/open", _open(session))
    assert agent.get("/pending-input/approval", request_id=rid, input_hash=HASH) == {
        "responder": None
    }
    _resolve(agent, rid)
    assert agent.get("/pending-input/approval", request_id=rid, input_hash=HASH) == {
        "responder": ME
    }
    for request_id, digest in ((rid, "b" * 64), ("../../areas", HASH), ("never", HASH)):
        answer = agent.get("/pending-input/approval", request_id=request_id, input_hash=digest)
        assert answer == {"responder": None}


def test_a_denial_is_no_approval(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    rid = _rid(session)
    agent.post("/gate/open", _open(session))
    _resolve(agent, rid, outcome="denied")
    assert agent.get("/pending-input/approval", request_id=rid, input_hash=HASH) == {
        "responder": None
    }
